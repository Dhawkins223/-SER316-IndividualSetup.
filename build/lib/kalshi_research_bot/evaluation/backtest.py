from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .quality import confidence_guardrail, data_quality_failures, parse_timestamp


DEFAULT_MODEL_VERSION = "historical_replay_v1"
MIN_SETTLED_FOR_PERFORMANCE = 100
MIN_SETTLED_FOR_BUCKET = 30
UNRESOLVED_STATES = {"", "open", "active", "unresolved", "pending"}
WIN_STATES = {"yes", "win", "won", "true"}
LOSS_STATES = {"no", "loss", "lost", "false"}
PUSH_STATES = {"push", "tie"}
VOID_STATES = {"void", "canceled", "cancelled", "cancel"}
FAIR_MARKET_STATES = {"fair_market", "fair-market", "fair market"}
EARLY_EXIT_STATES = {"early_exit", "early-exit", "early exit"}
ZERO = Decimal("0")
ONE = Decimal("1")
ONE_HUNDRED = Decimal("100")
DecimalInput = Decimal | int | float | str


def _decimal(value: DecimalInput, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name}_must_be_numeric")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name}_must_be_numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name}_must_be_finite")
    return number


def _decimal_or_zero(value: Any, *, field_name: str) -> Decimal:
    return ZERO if value is None or value == "" else _decimal(value, field_name=field_name)


def _rounded(value: Decimal, places: int) -> Decimal:
    return round(value, places)


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("mean_requires_values")
    return sum(values, ZERO) / Decimal(len(values))


def _probability_to_decimal_odds(probability: Decimal) -> Decimal:
    if probability <= ZERO:
        raise ValueError("probability_must_be_positive")
    return _rounded(ONE / probability, 6)


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _prediction_key(snapshot: dict[str, Any]) -> tuple[str, str, str]:
    event = snapshot.get("event") or {}
    market = snapshot.get("market") or {}
    prediction = snapshot.get("prediction") or snapshot.get("model") or {}
    return (
        str(event.get("event_id") or snapshot.get("event_id") or ""),
        str(market.get("ticker") or market.get("market_ticker") or ""),
        str(prediction.get("side") or market.get("side") or snapshot.get("side") or "yes").lower(),
    )


def _prediction_id(snapshot: dict[str, Any]) -> str:
    digest = hashlib.sha256("|".join(_prediction_key(snapshot)).encode("utf-8")).hexdigest()
    return digest[:16]


def _strategy(snapshot: dict[str, Any]) -> str:
    prediction = snapshot.get("prediction") or snapshot.get("model") or {}
    return str(snapshot.get("strategy") or snapshot.get("slip_name") or prediction.get("strategy") or "default")


def _entry_price(market: dict[str, Any], side: str) -> Decimal | None:
    side_key = side.lower()
    for field in [f"{side_key}_ask_cents", "entry_price_cents", "ask_cents"]:
        if market.get(field) is not None:
            return _decimal(market[field], field_name=field)
    return None


def _probability(snapshot: dict[str, Any], side: str) -> Decimal | None:
    prediction = snapshot.get("prediction") or snapshot.get("model") or {}
    market = snapshot.get("market") or {}
    for field in ["probability", "model_probability"]:
        if prediction.get(field) is not None:
            return _decimal(prediction[field], field_name=field)
        if market.get(field) is not None:
            return _decimal(market[field], field_name=field)
    if market.get("yes_ask_cents") is not None and market.get("no_ask_cents") is not None:
        yes_quote = _decimal(market["yes_ask_cents"], field_name="yes_ask_cents") / ONE_HUNDRED
        no_quote = _decimal(market["no_ask_cents"], field_name="no_ask_cents") / ONE_HUNDRED
        total_quote = yes_quote + no_quote
        if total_quote <= ZERO:
            return None
        yes_probability = yes_quote / total_quote
        return yes_probability if side.lower() == "yes" else ONE - yes_probability
    return None


def _safe_input(snapshot: dict[str, Any]) -> dict[str, Any]:
    market = snapshot.get("market") or {}
    return {
        "snapshot_at": snapshot.get("snapshot_at") or snapshot.get("as_of"),
        "event": snapshot.get("event") or {},
        "market": market,
        "features": snapshot.get("features") or snapshot.get("input") or {},
        "source_snapshot_id": snapshot.get("source_snapshot_id"),
        "api_fetched_at": snapshot.get("api_fetched_at"),
        "source_updated_at": market.get("updated_time") or market.get("source_updated_at") or snapshot.get("source_updated_at"),
    }


def _build_prediction(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    event = snapshot.get("event") or {}
    market = snapshot.get("market") or {}
    prediction = snapshot.get("prediction") or snapshot.get("model") or {}
    side = str(prediction.get("side") or market.get("side") or snapshot.get("side") or "yes").lower()
    probability = _probability(snapshot, side)
    entry_price = _entry_price(market, side)
    if probability is None or entry_price is None:
        return None
    spread = None
    if market.get("yes_ask_cents") is not None and market.get("yes_bid_cents") is not None:
        spread = _decimal(market["yes_ask_cents"], field_name="yes_ask_cents") - _decimal(
            market["yes_bid_cents"], field_name="yes_bid_cents"
        )
    guardrail = confidence_guardrail(
        probability=probability,
        evidence_count=int(prediction.get("evidence_count") or 0),
        source_backed=bool(prediction.get("source_backed")),
        margin_of_error=None
        if prediction.get("margin_of_error") is None
        else _decimal(prediction["margin_of_error"], field_name="margin_of_error"),
        spread_cents=spread,
    )
    return {
        "prediction_id": _prediction_id(snapshot),
        "timestamp": snapshot.get("snapshot_at") or snapshot.get("as_of"),
        "api_fetched_at": snapshot.get("api_fetched_at") or snapshot.get("snapshot_at") or snapshot.get("as_of"),
        "source_updated_at": market.get("updated_time") or market.get("source_updated_at") or snapshot.get("source_updated_at"),
        "source_snapshot_id": snapshot.get("source_snapshot_id") or _prediction_id(snapshot),
        "event": event.get("name") or event.get("event_id") or "",
        "event_id": event.get("event_id") or "",
        "event_start_time": event.get("start_time") or event.get("event_start_time") or "",
        "market_close_time": market.get("close_time") or market.get("market_close_time") or "",
        "market": market.get("ticker") or market.get("market_ticker") or "",
        "side": side,
        "strategy": _strategy(snapshot),
        "input_data_used": _safe_input(snapshot),
        "odds_used": {
            "entry_price_cents": entry_price,
            "decimal_odds": _probability_to_decimal_odds(entry_price / ONE_HUNDRED) if entry_price > ZERO else None,
            "yes_ask_cents": market.get("yes_ask_cents"),
            "no_ask_cents": market.get("no_ask_cents"),
        },
        "model_version": prediction.get("model_version") or DEFAULT_MODEL_VERSION,
        "confidence_score": guardrail["score"],
        "confidence_label": guardrail["label"],
        "predicted_probability": _rounded(probability, 6),
        "predicted_outcome": side,
        "entry_price_cents": entry_price,
        "settlement_state": "unresolved",
        "actual_outcome": None,
        "profit_loss_cents": None,
    }


def _outcome_map(outcomes: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    mapped: dict[tuple[str, str], dict[str, Any]] = {}
    for outcome in outcomes:
        mapped[(str(outcome.get("event_id") or ""), str(outcome.get("market") or outcome.get("market_ticker") or ""))] = outcome
    return mapped


def _normal_state(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ")


def _coerce_actual_outcome(value: Any, side: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        if value in {0, 1}:
            return bool(value)
        return None
    normalized = _normal_state(value)
    if normalized in {"true", "win", "won"}:
        return True
    if normalized in {"false", "loss", "lost"}:
        return False
    if normalized in {"yes", "no"}:
        return normalized == side
    return None


def _outcome_settlement_state(prediction: dict[str, Any], outcome: dict[str, Any]) -> tuple[str, bool | None, Decimal | None]:
    explicit_state = _normal_state(outcome.get("settlement_state") or outcome.get("state") or outcome.get("status"))
    result = _normal_state(outcome.get("winning_side") or outcome.get("result") or outcome.get("resolution") or outcome.get("expiration_value"))
    if explicit_state in UNRESOLVED_STATES and not result:
        return "unresolved", None, None
    if explicit_state in VOID_STATES or result in VOID_STATES:
        cancel_value = explicit_state if explicit_state in VOID_STATES else result
        return "cancelled" if cancel_value in {"canceled", "cancelled", "cancel"} else "void", None, ZERO
    if explicit_state in PUSH_STATES or result in PUSH_STATES:
        return "push", None, ZERO
    if explicit_state in FAIR_MARKET_STATES or result in FAIR_MARKET_STATES:
        price = _first_not_none(
            outcome.get("fair_market_price_cents"),
            outcome.get("fair_market_value_cents"),
            outcome.get("settlement_price_cents"),
        )
        if price is None:
            return "fair_market", None, None
        return "fair_market", None, _rounded(
            _decimal(price, field_name="fair_market_price_cents")
            - _decimal(prediction["entry_price_cents"], field_name="entry_price_cents"),
            2,
        )
    if explicit_state in EARLY_EXIT_STATES or result in EARLY_EXIT_STATES:
        price = _first_not_none(
            outcome.get("exit_price_cents"),
            outcome.get("early_exit_price_cents"),
            outcome.get("settlement_price_cents"),
        )
        if price is None:
            return "early_exit", None, None
        return "early_exit", None, _rounded(
            _decimal(price, field_name="early_exit_price_cents")
            - _decimal(prediction["entry_price_cents"], field_name="entry_price_cents"),
            2,
        )
    actual = _coerce_actual_outcome(outcome.get("actual_outcome"), prediction["side"])
    if actual is not None:
        is_win = actual
        return ("win" if is_win else "loss"), is_win, None
    if result in {"yes", "no"}:
        is_win = prediction["side"] == result
        return ("win" if is_win else "loss"), is_win, None
    if result in WIN_STATES:
        return "win", True, None
    if result in LOSS_STATES:
        return "loss", False, None
    return "unresolved", None, None


def _settle(prediction: dict[str, Any], outcomes: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    outcome = outcomes.get((prediction["event_id"], prediction["market"]))
    if not outcome:
        return prediction
    state, actual, explicit_profit = _outcome_settlement_state(prediction, outcome)
    prediction["settlement_state"] = state
    if state == "unresolved":
        return prediction
    entry = _decimal(prediction["entry_price_cents"], field_name="entry_price_cents")
    prediction["actual_outcome"] = None if actual is None else bool(actual)
    if explicit_profit is not None:
        prediction["profit_loss_cents"] = explicit_profit
    elif actual is not None:
        prediction["profit_loss_cents"] = _rounded(ONE_HUNDRED - entry if actual else -entry, 2)
    return prediction


def run_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    snapshots = payload.get("snapshots") or []
    outcomes = _outcome_map(payload.get("outcomes") or [])
    latest_pre_event: dict[tuple[str, str, str], dict[str, Any]] = {}
    data_quality_failures_rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        failures = data_quality_failures(snapshot, required_probability=False)
        if failures:
            data_quality_failures_rows.append(
                {
                    "snapshot_at": snapshot.get("snapshot_at") or snapshot.get("as_of"),
                    "key": _prediction_key(snapshot),
                    "failures": failures,
                }
            )
            continue
        key = _prediction_key(snapshot)
        snapshot_time = parse_timestamp(snapshot.get("snapshot_at") or snapshot.get("as_of"))
        current_time = parse_timestamp(latest_pre_event.get(key, {}).get("snapshot_at") or latest_pre_event.get(key, {}).get("as_of"))
        if snapshot_time and (current_time is None or snapshot_time > current_time):
            latest_pre_event[key] = snapshot
    predictions: list[dict[str, Any]] = []
    for snapshot in latest_pre_event.values():
        prediction = _build_prediction(snapshot)
        if prediction is None:
            data_quality_failures_rows.append(
                {
                    "snapshot_at": snapshot.get("snapshot_at") or snapshot.get("as_of"),
                    "key": _prediction_key(snapshot),
                    "failures": ["missing_probability_or_entry_price"],
                }
            )
            continue
        predictions.append(_settle(prediction, outcomes))
    return build_backtest_report(predictions, data_quality_failures_rows)


def _bucket(confidence: Decimal) -> str:
    if confidence >= Decimal("0.85"):
        return "85-100"
    if confidence >= Decimal("0.75"):
        return "75-85"
    if confidence >= Decimal("0.65"):
        return "65-75"
    return "0-65"


def _dedupe_for_overall(predictions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for prediction in predictions:
        grouped[(prediction.get("event_id", ""), prediction.get("market", ""), prediction.get("side", ""))].append(prediction)
    deduped: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for key, rows in grouped.items():
        sorted_rows = sorted(rows, key=lambda row: (str(row.get("timestamp") or ""), str(row.get("strategy") or "")))
        deduped.append(sorted_rows[0])
        if len(sorted_rows) > 1:
            duplicates.append(
                {
                    "key": key,
                    "count": len(sorted_rows),
                    "strategies": sorted({str(row.get("strategy") or "default") for row in sorted_rows}),
                }
            )
    return deduped, duplicates


def _is_resolved(prediction: dict[str, Any]) -> bool:
    return _normal_state(prediction.get("settlement_state")) not in UNRESOLVED_STATES


def _is_win_loss(prediction: dict[str, Any]) -> bool:
    return _normal_state(prediction.get("settlement_state")) in {"win", "loss"}


def _has_pl(prediction: dict[str, Any]) -> bool:
    return _is_resolved(prediction) and prediction.get("profit_loss_cents") is not None


def _sample_status(count: int, required: int) -> str:
    return "sufficient_sample" if count >= required else f"insufficient_sample ({count}/{required})"


def _normalized_prediction_rows(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for prediction in predictions:
        row = dict(prediction)
        if not _is_resolved(row):
            row["settlement_state"] = "unresolved"
            row["actual_outcome"] = None
            row["profit_loss_cents"] = None
        normalized.append(row)
    return normalized


def build_backtest_report(predictions: list[dict[str, Any]], data_quality_failures_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    predictions = _normalized_prediction_rows(predictions)
    deduped_predictions, duplicate_exposures = _dedupe_for_overall(predictions)
    resolved = [prediction for prediction in deduped_predictions if _is_resolved(prediction)]
    win_loss = [prediction for prediction in resolved if _is_win_loss(prediction)]
    roi_rows = [prediction for prediction in resolved if _has_pl(prediction)]
    wins = sum(1 for prediction in win_loss if prediction.get("settlement_state") == "win")
    risked = sum(
        (_decimal_or_zero(prediction.get("entry_price_cents"), field_name="entry_price_cents") for prediction in roi_rows),
        ZERO,
    )
    profit = sum(
        (_decimal_or_zero(prediction.get("profit_loss_cents"), field_name="profit_loss_cents") for prediction in roi_rows),
        ZERO,
    )
    bucket_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for prediction in win_loss:
        bucket_rows[_bucket(_decimal_or_zero(prediction.get("confidence_score"), field_name="confidence_score"))].append(prediction)
    confidence_buckets = {}
    for bucket, rows in sorted(bucket_rows.items()):
        bucket_wins = sum(1 for row in rows if row.get("actual_outcome") is True)
        bucket_risked = sum(
            (_decimal_or_zero(row.get("entry_price_cents"), field_name="entry_price_cents") for row in rows if _has_pl(row)),
            ZERO,
        )
        confidence_buckets[bucket] = {
            "picks": len(rows),
            "sample_status": _sample_status(len(rows), MIN_SETTLED_FOR_BUCKET),
            "win_rate": _rounded(Decimal(bucket_wins) / Decimal(len(rows)), 6)
            if len(rows) >= MIN_SETTLED_FOR_BUCKET
            else None,
            "roi_fee_excluded": (
                _rounded(
                    sum(
                        (
                            _decimal_or_zero(row.get("profit_loss_cents"), field_name="profit_loss_cents")
                            for row in rows
                            if _has_pl(row)
                        ),
                        ZERO,
                    )
                    / bucket_risked,
                    6,
                )
                if bucket_risked and len(rows) >= MIN_SETTLED_FOR_BUCKET
                else None
            ),
        }
    brier = None
    calibration_error = None
    if win_loss:
        brier = _mean(
            [
                (
                    _decimal(row["predicted_probability"], field_name="predicted_probability")
                    - (ONE if row.get("actual_outcome") else ZERO)
                )
                ** 2
                for row in win_loss
            ]
        )
        calibration_error = abs(
            _mean([_decimal(row["predicted_probability"], field_name="predicted_probability") for row in win_loss])
            - Decimal(wins) / Decimal(len(win_loss))
        )
    misses = sorted(
        [
            {
                "event": row.get("event"),
                "market": row.get("market"),
                "probability": row.get("predicted_probability"),
                "loss_cents": row.get("profit_loss_cents"),
            }
            for row in win_loss
            if row.get("settlement_state") == "loss"
        ],
        key=lambda item: (item["probability"] or 0),
        reverse=True,
    )[:10]
    strategy_exposure: dict[str, dict[str, Any]] = {}
    for prediction in predictions:
        strategy = str(prediction.get("strategy") or "default")
        row = strategy_exposure.setdefault(strategy, {"predictions": 0, "resolved": 0, "unresolved": 0})
        row["predictions"] += 1
        if _is_resolved(prediction):
            row["resolved"] += 1
        else:
            row["unresolved"] += 1
    performance_sample_status = _sample_status(len(win_loss), MIN_SETTLED_FOR_PERFORMANCE)
    return {
        "total_predictions": len(predictions),
        "overall_predictions_after_dedupe": len(deduped_predictions),
        "settled_predictions": len(resolved),
        "win_loss_predictions": len(win_loss),
        "unresolved_predictions": len([prediction for prediction in deduped_predictions if not _is_resolved(prediction)]),
        "performance_sample_status": performance_sample_status,
        "total_picks_tested": len(win_loss),
        "unsettled_picks": len(predictions) - len([prediction for prediction in predictions if _is_resolved(prediction)]),
        "win_rate": _rounded(Decimal(wins) / Decimal(len(win_loss)), 6)
        if win_loss and len(win_loss) >= MIN_SETTLED_FOR_PERFORMANCE
        else None,
        "roi_fee_excluded": _rounded(profit / risked, 6)
        if risked and len(win_loss) >= MIN_SETTLED_FOR_PERFORMANCE
        else None,
        "average_odds": _mean(
            [
                _probability_to_decimal_odds(
                    _decimal_or_zero(row.get("entry_price_cents"), field_name="entry_price_cents") / ONE_HUNDRED
                )
                for row in win_loss
            ]
        )
        if win_loss
        else None,
        "average_entry_price_cents": _mean(
            [_decimal_or_zero(row.get("entry_price_cents"), field_name="entry_price_cents") for row in win_loss]
        )
        if win_loss
        else None,
        "brier_score": _rounded(brier, 6) if brier is not None and len(win_loss) >= MIN_SETTLED_FOR_PERFORMANCE else None,
        "calibration_error": _rounded(calibration_error, 6)
        if calibration_error is not None and len(win_loss) >= MIN_SETTLED_FOR_PERFORMANCE
        else None,
        "confidence_bucket_performance": confidence_buckets,
        "duplicate_market_exposures": duplicate_exposures,
        "strategy_exposure": strategy_exposure,
        "biggest_misses": misses,
        "data_quality_failures": data_quality_failures_rows or [],
        "prediction_logs": predictions,
    }


def render_backtest_report(report: dict[str, Any]) -> str:
    lines = [
        "Backtest Evaluation Report",
        f"Total predictions: {report.get('total_predictions', 0)}",
        f"Overall predictions after de-dupe: {report.get('overall_predictions_after_dedupe', 0)}",
        f"Settled predictions: {report.get('settled_predictions', 0)}",
        f"Unresolved predictions: {report.get('unresolved_predictions', 0)}",
        f"Sample status: {report.get('performance_sample_status')}",
        f"Win rate: {report.get('win_rate')} ({report.get('performance_sample_status')})",
        f"ROI fee-excluded: {report.get('roi_fee_excluded')} ({report.get('performance_sample_status')})",
        f"Average odds: {report.get('average_odds')}",
        f"Average entry price: {report.get('average_entry_price_cents')}c",
        f"Brier score: {report.get('brier_score')}",
        f"Calibration error: {report.get('calibration_error')}",
        "",
        "Confidence buckets:",
    ]
    for bucket, row in (report.get("confidence_bucket_performance") or {}).items():
        lines.append(f"- {bucket}: picks={row['picks']} sample={row['sample_status']} win_rate={row['win_rate']} roi_fee_excluded={row['roi_fee_excluded']}")
    lines.append("")
    lines.append("Duplicate market exposure:")
    for duplicate in report.get("duplicate_market_exposures") or []:
        lines.append(f"- {duplicate['key']}: count={duplicate['count']} strategies={', '.join(duplicate['strategies'])}")
    lines.append("")
    lines.append("Biggest misses:")
    for miss in report.get("biggest_misses") or []:
        lines.append(f"- {miss['event']} | {miss['market']} | p={miss['probability']} | P/L={miss['loss_cents']}c")
    lines.append("")
    lines.append(f"Data-quality failures: {len(report.get('data_quality_failures') or [])}")
    for failure in report.get("data_quality_failures") or []:
        lines.append(f"- {failure.get('key')}: {', '.join(failure.get('failures') or [])}")
    return "\n".join(lines)


def load_backtest_payload(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_backtest_report(report: dict[str, Any], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_backtest_report(report), encoding="utf-8")
