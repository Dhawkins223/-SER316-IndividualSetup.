from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..database import as_decimal, json_default
from ..storage import PostgresStore
from ..today import infer_market_category
from .execution import ExecutionConfig, MarketSnapshot, PaperOrder, PriceLevel, settle_execution, simulate_order
from .exposure import ExposureCandidate, ExposureLimits, apply_exposure_limits


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"), parse_float=Decimal)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _number(value: Any) -> Decimal | None:
    number = as_decimal(value)
    return number if number is not None and number.is_finite() else None


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else None


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")


def _market_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("event_id") or row.get("event") or ""),
        str(row.get("market_id") or row.get("market") or ""),
        str(row.get("side") or "").lower(),
    )


def _market_dedupe(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_market_key(row)].append(row)
    return [
        sorted(
            group,
            key=lambda row: (
                str(row.get("prediction_timestamp") or ""),
                int(row.get("snapshot_sequence") or 1),
                str(row.get("strategy") or ""),
            ),
        )[0]
        for group in grouped.values()
    ]


def _category(row: Mapping[str, Any]) -> str:
    reason = _json(row.get("reason_features_json"))
    inputs = _json(row.get("input_data_json"))
    leg = _json(inputs.get("leg"))
    return infer_market_category(
        {
            "ticker": row.get("market_id") or row.get("market"),
            "event_ticker": row.get("event_id"),
            "title": reason.get("title") or leg.get("title") or row.get("event"),
            "category": reason.get("category") or leg.get("category") or leg.get("sport"),
        }
    )


def _model_probability(row: Mapping[str, Any]) -> Decimal | None:
    model_version = str(row.get("model_version") or "").lower()
    if "market_implied" in model_version:
        return None
    reason = _json(row.get("reason_features_json"))
    inputs = _json(row.get("input_data_json"))
    leg = _json(inputs.get("leg"))
    for value in (
        reason.get("model_probability"),
        reason.get("research_probability"),
        leg.get("model_probability"),
        leg.get("research_probability"),
    ):
        probability = _number(value)
        if probability is not None and 0 <= probability <= 1:
            return probability
    return None


def _liquidity(row: Mapping[str, Any]) -> Decimal | None:
    reason = _json(row.get("reason_features_json"))
    values = [_number(reason.get("open_interest")), _number(reason.get("volume_24h"))]
    available = [value for value in values if value is not None]
    return sum(available) if available else None


def _spread(row: Mapping[str, Any]) -> Decimal | None:
    reason = _json(row.get("reason_features_json"))
    odds = _json(row.get("odds_json"))
    spread = _number(reason.get("spread_cents"))
    if spread is not None:
        return spread
    ask = _number(odds.get("ask_cents"))
    bid = _number(odds.get("bid_cents"))
    return ask - bid if ask is not None and bid is not None else None


def _bucket_price(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    if value < Decimal("50"):
        return "00-49"
    if value < Decimal("70"):
        return "50-69"
    if value < Decimal("80"):
        return "70-79"
    if value < Decimal("90"):
        return "80-89"
    return "90-100"


def _bucket_confidence(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    if value < Decimal("0.65"):
        return "00-64"
    if value < Decimal("0.75"):
        return "65-74"
    if value < Decimal("0.85"):
        return "75-84"
    return "85-100"


def _bucket_hours(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    if value <= Decimal("1"):
        return "<=1h"
    if value <= Decimal("6"):
        return "1-6h"
    if value <= Decimal("24"):
        return "6-24h"
    if value <= Decimal("72"):
        return "1-3d"
    return ">3d"


def _bucket_liquidity(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    if value < Decimal("100"):
        return "<100"
    if value < Decimal("1000"):
        return "100-999"
    if value < Decimal("10000"):
        return "1k-9.9k"
    return "10k+"


def _winning_side(row: Mapping[str, Any]) -> str:
    predicted_side = str(row.get("side") or row.get("predicted_outcome") or "").lower()
    if str(row.get("settlement_state") or "").lower() == "win":
        return predicted_side
    return "no" if predicted_side == "yes" else "yes"


def _execution(row: Mapping[str, Any], *, config: ExecutionConfig) -> dict[str, Any]:
    side = str(row.get("side") or row.get("predicted_outcome") or "").lower()
    entry = _number(row.get("entry_price_cents"))
    if side not in {"yes", "no"} or entry is None:
        return {"fill_state": "rejected", "rejection_reason": "missing_side_or_entry_price"}
    odds = _json(row.get("odds_json"))
    bid = _number(odds.get("bid_cents"))
    source_time = row.get("source_updated_at") or row.get("api_fetched_at") or row.get("prediction_timestamp")
    kwargs: dict[str, Any] = {
        "market_id": str(row.get("market_id") or row.get("market")),
        "snapshot_timestamp": str(source_time),
        "market_status": "open",
        "close_timestamp": row.get("market_close_time"),
        "source_snapshot_hash": row.get("source_snapshot_hash") or row.get("source_snapshot_id"),
    }
    if side == "yes":
        kwargs.update(yes_bid_cents=bid, yes_ask_cents=entry, yes_ask_depth=(PriceLevel(entry, 1),))
    else:
        kwargs.update(no_bid_cents=bid, no_ask_cents=entry, no_ask_depth=(PriceLevel(entry, 1),))
    snapshot = MarketSnapshot(**kwargs)
    order = PaperOrder(
        order_id=f"historical-{row.get('id') or row.get('prediction_id')}",
        market_id=str(row.get("market_id") or row.get("market")),
        signal_timestamp=str(row.get("prediction_timestamp")),
        order_timestamp=str(row.get("prediction_timestamp")),
        contract_side=side,
        order_type="market",
        quantity=1,
        intended_price_cents=entry,
    )
    result = simulate_order(order, snapshot, config=config)
    settled = settle_execution(result, winning_side=_winning_side(row))
    return settled.to_dict()


def _annotate(rows: Sequence[dict[str, Any]], *, config: ExecutionConfig) -> list[dict[str, Any]]:
    annotated = []
    for source in rows:
        row = dict(source)
        row["prediction_id"] = str(row.get("prediction_id") or row.get("id"))
        row["category"] = _category(row)
        row["model_probability"] = _model_probability(row)
        row["liquidity_value"] = _liquidity(row)
        row["spread_cents"] = _spread(row)
        prediction_time = _timestamp(row.get("prediction_timestamp"))
        expiration_time = _timestamp(row.get("market_close_time"))
        row["time_to_expiration_hours"] = (
            Decimal(str((expiration_time - prediction_time).total_seconds())) / Decimal("3600")
            if prediction_time and expiration_time
            else None
        )
        row["execution"] = _execution(row, config=config)
        annotated.append(row)
    return annotated


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    filled = [
        row
        for row in rows
        if _json(row.get("execution")).get("fill_state") in {"filled", "partial_fill"}
    ]
    winners = [row for row in filled if str(row.get("settlement_state")).lower() == "win"]
    losers = [row for row in filled if str(row.get("settlement_state")).lower() == "loss"]
    entry_prices = [_number(_json(row["execution"])["simulated_fill_price_cents"]) for row in filled]
    entry_prices = [value for value in entry_prices if value is not None]
    implied = [
        value
        for row in filled
        if (value := _number(row.get("implied_probability"))) is not None
    ]
    model_edges = [
        row["model_probability"] - _number(row.get("implied_probability"))
        for row in filled
        if row.get("model_probability") is not None and _number(row.get("implied_probability")) is not None
    ]
    gross_values = [_number(_json(row["execution"])["gross_return_cents"]) for row in filled]
    fee_values = [_number(_json(row["execution"])["fee_estimate_cents"]) for row in filled]
    net_values = [_number(_json(row["execution"])["net_return_cents"]) for row in filled]
    slippage_values = [_number(_json(row["execution"])["slippage_cents"]) for row in filled]
    winning_gross = [_number(_json(row["execution"])["gross_return_cents"]) for row in winners]
    losing_gross = [abs(value) for row in losers if (value := _number(_json(row["execution"])["gross_return_cents"])) is not None]
    gross_values = [value for value in gross_values if value is not None]
    fee_values = [value for value in fee_values if value is not None]
    net_values = [value for value in net_values if value is not None]
    slippage_values = [value for value in slippage_values if value is not None]
    winning_gross = [value for value in winning_gross if value is not None]
    risked = sum(entry_prices, Decimal("0"))
    return {
        "settled_rows": len(rows),
        "filled_positions": len(filled),
        "no_fill_or_rejected": len(rows) - len(filled),
        "winners": len(winners),
        "losers": len(losers),
        "directional_accuracy": Decimal(len(winners)) / Decimal(len(filled)) if filled else None,
        "average_winning_gross_profit_cents": _mean(winning_gross),
        "average_losing_amount_cents": _mean(losing_gross),
        "average_entry_price_cents": _mean(entry_prices),
        "median_entry_price_cents": _median(entry_prices),
        "average_implied_probability": _mean(implied),
        "average_model_edge": _mean(model_edges),
        "model_edge_sample_size": len(model_edges),
        "gross_simulated_return_cents": sum(gross_values, Decimal("0")),
        "fees_cents": sum(fee_values, Decimal("0")),
        "slippage_cents": sum(slippage_values, Decimal("0")),
        "net_simulated_return_cents": sum(net_values, Decimal("0")),
        "capital_at_risk_cents": risked,
        "gross_return_on_risk": sum(gross_values, Decimal("0")) / risked if risked else None,
        "net_return_on_risk": sum(net_values, Decimal("0")) / risked if risked else None,
    }


def _grouped(rows: Sequence[dict[str, Any]], key) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(key(row))].append(row)
    return {name: _summary(group) for name, group in sorted(groups.items())}


def _decisions_for(rows: Sequence[dict[str, Any]], *, limits: ExposureLimits) -> dict[str, Any]:
    candidates = [
        ExposureCandidate(
            prediction_id=str(row["prediction_id"]),
            prediction_timestamp=str(row.get("prediction_timestamp")),
            event_id=str(row.get("event_id") or row.get("event")),
            market_id=str(row.get("market_id") or row.get("market")),
            category=str(row.get("category") or "Kalshi"),
            contract_side=str(row.get("side") or row.get("predicted_outcome")),
            capital_at_risk_cents=_number(row.get("entry_price_cents")) or Decimal("0.01"),
            underlying_ids=(str(row.get("event_id") or row.get("event")),),
            confidence=_number(row.get("confidence_score")),
        )
        for row in rows
    ]
    return apply_exposure_limits(candidates, limits=limits)


def build_kalshi_return_decomposition_from_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    execution_config: ExecutionConfig | None = None,
) -> dict[str, Any]:
    config = execution_config or ExecutionConfig(signal_to_order_move_cents=Decimal("1"), market_slippage_cents=Decimal("2"))
    settled = [
        dict(row)
        for row in rows
        if str(row.get("validation_status") or "valid") == "valid"
        and str(row.get("settlement_state") or "").lower() in {"win", "loss"}
    ]
    market_deduped = _market_dedupe(settled)
    annotated_raw = _annotate(settled, config=config)
    annotated_deduped = _annotate(market_deduped, config=config)
    event_only_limits = ExposureLimits(
        maximum_simulated_capital_cents=Decimal("1000000000"),
        maximum_position_cents=Decimal("1000000"),
        maximum_event_exposure_cents=Decimal("1000000"),
        maximum_category_exposure_cents=Decimal("1000000000"),
        maximum_underlying_exposure_cents=Decimal("1000000"),
        maximum_correlated_exposure_cents=Decimal("1000000"),
        maximum_markets_per_event=1,
    )
    event_decisions = _decisions_for(annotated_deduped, limits=event_only_limits)
    event_accepted_ids = {
        decision["prediction_id"]
        for decision in event_decisions["decisions"]
        if decision["accepted"]
    }
    event_adjusted = [row for row in annotated_deduped if row["prediction_id"] in event_accepted_ids]
    portfolio_decisions = _decisions_for(annotated_deduped, limits=ExposureLimits())
    portfolio_accepted_ids = {
        decision["prediction_id"]
        for decision in portfolio_decisions["decisions"]
        if decision["accepted"]
    }
    portfolio_adjusted = [row for row in annotated_deduped if row["prediction_id"] in portfolio_accepted_ids]
    event_counts = Counter(str(row.get("event_id") or row.get("event")) for row in annotated_deduped)
    average_entry = _summary(annotated_deduped)["average_entry_price_cents"]
    accuracy = _summary(annotated_deduped)["directional_accuracy"]
    return {
        "report_type": "kalshi_return_decomposition",
        "run_id": run_id,
        "research_only": True,
        "profitability_claim_allowed": False,
        "execution_assumptions": {
            "contracts_per_exposure": 1,
            "depth_status": "historical depth unavailable; one-contract top-of-book simulation only",
            "signal_to_order_move_cents": config.signal_to_order_move_cents,
            "market_slippage_ceiling_cents": config.market_slippage_cents,
            "fee_schedule_version": config.fee_schedule_version,
            "fee_scope_warning": "General fee formula only; special-product fee schedules require separate validation.",
        },
        "counts": {
            "raw_settled_rows": len(settled),
            "market_deduped_settled_exposures": len(annotated_deduped),
            "event_adjusted_settled_exposures": len(event_adjusted),
            "portfolio_limited_settled_exposures": len(portfolio_adjusted),
            "duplicate_snapshot_or_strategy_rows": len(settled) - len(annotated_deduped),
            "additional_correlated_event_markets": len(annotated_deduped) - len(event_adjusted),
            "events_with_multiple_markets": sum(count > 1 for count in event_counts.values()),
            "maximum_markets_on_one_event": max(event_counts.values(), default=0),
        },
        "raw_row_performance": _summary(annotated_raw),
        "market_deduped_performance": _summary(annotated_deduped),
        "event_adjusted_performance": _summary(event_adjusted),
        "portfolio_limited_performance": _summary(portfolio_adjusted),
        "price_buckets": _grouped(annotated_deduped, lambda row: _bucket_price(_number(row.get("entry_price_cents")))),
        "category_buckets": _grouped(annotated_deduped, lambda row: row.get("category") or "unknown"),
        "confidence_buckets": _grouped(annotated_deduped, lambda row: _bucket_confidence(_number(row.get("confidence_score")))),
        "time_to_expiration_buckets": _grouped(annotated_deduped, lambda row: _bucket_hours(_number(row.get("time_to_expiration_hours")))),
        "liquidity_buckets": _grouped(annotated_deduped, lambda row: _bucket_liquidity(_number(row.get("liquidity_value")))),
        "event_exposure_decisions": event_decisions,
        "portfolio_exposure_decisions": portfolio_decisions,
        "explanation": {
            "average_price_break_even_accuracy_before_costs": average_entry / Decimal("100") if average_entry is not None else None,
            "observed_accuracy": accuracy,
            "high_accuracy_negative_return_reason": (
                "Binary-contract accuracy is not economic value. Expensive winning contracts earn only 100 minus entry price, "
                "while each loss forfeits the full entry price; fees and adverse price movement raise break-even further."
            ),
        },
        "conclusion": "research_only_not_proof_of_tradable_profitability",
    }


def build_kalshi_return_decomposition(
    store: PostgresStore,
    *,
    run_id: str,
    execution_config: ExecutionConfig | None = None,
) -> dict[str, Any]:
    with store.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, run_id, prediction_timestamp, event, event_id, market, market_id,
                       side, strategy, input_data_json, odds_json, model_version,
                       confidence_score, confidence_label, predicted_outcome,
                       event_start_time, market_close_time, api_fetched_at,
                       source_updated_at, source_snapshot_id, source_snapshot_hash,
                       snapshot_sequence, entry_price_cents, implied_probability,
                       reason_features_json, validation_status, settlement_state,
                       actual_outcome, profit_loss_cents
                FROM app.prediction_logs
                WHERE run_id = %s
                ORDER BY prediction_timestamp, id
                """,
                (run_id,),
            ).fetchall()
        ]
    return build_kalshi_return_decomposition_from_rows(
        rows,
        run_id=run_id,
        execution_config=execution_config,
    )


def render_kalshi_return_decomposition(report: Mapping[str, Any]) -> str:
    counts = report["counts"]
    summary = report["market_deduped_performance"]
    event_summary = report["event_adjusted_performance"]
    explanation = report["explanation"]
    lines = [
        "Kalshi Return and Exposure Decomposition",
        f"Run ID: {report['run_id']}",
        "Status: research-only; not proof of tradable profitability",
        "",
        "Counts:",
        f"- raw settled rows: {counts['raw_settled_rows']}",
        f"- market de-duped settled exposures: {counts['market_deduped_settled_exposures']}",
        f"- event-adjusted settled exposures: {counts['event_adjusted_settled_exposures']}",
        f"- portfolio-limited settled exposures: {counts['portfolio_limited_settled_exposures']}",
        f"- repeated snapshot/strategy rows removed: {counts['duplicate_snapshot_or_strategy_rows']}",
        f"- additional correlated event markets excluded: {counts['additional_correlated_event_markets']}",
        "",
        "Market de-duped accounting:",
        f"- winners / losers: {summary['winners']} / {summary['losers']}",
        f"- accuracy: {summary['directional_accuracy']}",
        f"- average entry: {summary['average_entry_price_cents']}c",
        f"- average win / average loss: {summary['average_winning_gross_profit_cents']}c / {summary['average_losing_amount_cents']}c",
        f"- gross return: {summary['gross_simulated_return_cents']}c",
        f"- fees: {summary['fees_cents']}c",
        f"- adverse movement/slippage: {summary['slippage_cents']}c",
        f"- net simulated return: {summary['net_simulated_return_cents']}c",
        f"- net return on risk: {summary['net_return_on_risk']}",
        "",
        "Event-adjusted accounting:",
        f"- winners / losers: {event_summary['winners']} / {event_summary['losers']}",
        f"- accuracy: {event_summary['directional_accuracy']}",
        f"- net simulated return: {event_summary['net_simulated_return_cents']}c",
        f"- net return on risk: {event_summary['net_return_on_risk']}",
        "",
        "Why accuracy can be high while return is negative:",
        f"- observed accuracy: {explanation['observed_accuracy']}",
        f"- average-price break-even accuracy before costs: {explanation['average_price_break_even_accuracy_before_costs']}",
        f"- {explanation['high_accuracy_negative_return_reason']}",
        "",
        "Execution caveat:",
        f"- {report['execution_assumptions']['depth_status']}",
        f"- {report['execution_assumptions']['fee_scope_warning']}",
    ]
    for section in ("price_buckets", "category_buckets", "confidence_buckets", "time_to_expiration_buckets", "liquidity_buckets"):
        lines.extend(["", section.replace("_", " ").title() + ":"])
        for name, values in report[section].items():
            lines.append(
                f"- {name}: n={values['filled_positions']} wins={values['winners']} losses={values['losers']} "
                f"accuracy={values['directional_accuracy']} net={values['net_simulated_return_cents']}c "
                f"net_return_on_risk={values['net_return_on_risk']}"
            )
    return "\n".join(lines)


def write_kalshi_return_decomposition(report: Mapping[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_kalshi_return_decomposition(report), encoding="utf-8")
    output.with_suffix(".json").write_text(json.dumps(report, indent=2, sort_keys=True, default=json_default), encoding="utf-8")


def default_kalshi_return_decomposition_path(run_id: str) -> Path:
    return Path("data") / "paper_runs" / f"{run_id}_return_decomposition.txt"
