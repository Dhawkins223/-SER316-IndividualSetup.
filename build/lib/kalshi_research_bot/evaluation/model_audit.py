from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from ..business_store import create_store
from ..crypto_research import _deduped_crypto_rows
from ..database import DatabaseSession, as_decimal, json_default
from ..sports_research import _deduped_sports_rows, american_odds_implied_probability
from .kalshi_decomposition import _category, _market_dedupe
from .model_validation import EvaluationRecord, evaluate_category_model, persist_category_evaluation


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"), parse_float=Decimal)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _policy_category(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"sports", "esports"}:
        return "sports"
    if normalized == "crypto":
        return "crypto"
    if normalized == "weather":
        return "weather"
    if normalized == "politics":
        return "politics"
    if normalized in {"markets", "economics"}:
        return "economics"
    if normalized in {"company", "companies"}:
        return "company"
    return "event"


def _kalshi_records(connection: DatabaseSession, run_id: str) -> dict[str, list[EvaluationRecord]]:
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT * FROM app.prediction_logs
            WHERE run_id = %s AND validation_status = 'valid'
              AND settlement_state IN ('win', 'loss')
            ORDER BY prediction_timestamp, id
            """,
            (run_id,),
        ).fetchall()
    ]
    grouped: dict[str, list[EvaluationRecord]] = defaultdict(list)
    for row in _market_dedupe(rows):
        category = _policy_category(_category(row))
        reason = _json(row.get("reason_features_json"))
        market_probability = as_decimal(row.get("implied_probability"))
        if market_probability is None and row.get("entry_price_cents") is not None:
            entry_price = as_decimal(row["entry_price_cents"])
            market_probability = entry_price / Decimal("100") if entry_price is not None else None
        if market_probability is None:
            continue
        observed_at = row.get("source_updated_at") or row.get("api_fetched_at") or row.get("prediction_timestamp")
        grouped[category].append(
            EvaluationRecord(
                record_id=f"kalshi:{row['id']}",
                category=category,
                prediction_timestamp=row["prediction_timestamp"],
                settlement_timestamp=row.get("settlement_updated_at"),
                outcome=1 if row["settlement_state"] == "win" else 0,
                market_probability=market_probability,
                model_probability=None,
                model_version=row.get("model_version") or "market_implied_baseline",
                feature_version="kalshi_quote_features_v1",
                features={
                    "category": _category(row),
                    "spread_cents": reason.get("spread_cents"),
                    "open_interest": reason.get("open_interest"),
                    "volume_24h": reason.get("volume_24h"),
                    "strategy": row.get("strategy"),
                },
                feature_observed_at={"kalshi_quote_features": str(observed_at)},
            )
        )
    return grouped


def _crypto_records(connection: DatabaseSession, run_id: str) -> list[EvaluationRecord]:
    rows = _deduped_crypto_rows(connection, run_id, settled_only=True)
    records = []
    for row in rows:
        if row["actual_outcome"] not in {"win", "loss"}:
            continue
        features = _json(row["features_json"])
        records.append(
            EvaluationRecord(
                record_id=f"crypto:{row['id']}",
                category="crypto",
                prediction_timestamp=row["prediction_timestamp"],
                settlement_timestamp=row["settlement_updated_at"] or row["settlement_time"],
                outcome=1 if row["actual_outcome"] == "win" else 0,
                market_probability=as_decimal(row["implied_probability"], default=Decimal("0.5")),
                model_probability=as_decimal(row["confidence_score"]),
                model_version=row["model_version"],
                feature_version="crypto_ohlcv_features_v1",
                features={
                    "exchange": row["exchange"],
                    "symbol": row["symbol"],
                    "horizon": row["horizon"],
                    "side": row["side"],
                    **features,
                },
                feature_observed_at={"crypto_ohlcv_features": row["candle_close_time"]},
            )
        )
    return records


def _sports_records(connection: DatabaseSession, run_id: str) -> list[EvaluationRecord]:
    rows = _deduped_sports_rows(connection, run_id, settled_only=True)
    records = []
    for row in rows:
        if row["actual_outcome"] not in {"win", "loss"}:
            continue
        features = _json(row["features_json"])
        implied = features.get("implied_probability")
        if implied is None:
            implied = american_odds_implied_probability(row["odds"])
        records.append(
            EvaluationRecord(
                record_id=f"sports:{row['id']}",
                category="sports",
                prediction_timestamp=row["prediction_timestamp"],
                settlement_timestamp=row["settlement_updated_at"],
                outcome=1 if row["actual_outcome"] == "win" else 0,
                market_probability=as_decimal(implied),
                model_probability=None,
                model_version=row["model_version"],
                feature_version="sports_pregame_odds_features_v1",
                features={
                    "sport": row["sport"],
                    "league": row["league"],
                    "bookmaker": row["bookmaker"],
                    "market_type": row["market_type"],
                    "selection": row["selection"],
                    "line": row["line"],
                    "odds": row["odds"],
                    "match_confidence": features.get("match_confidence"),
                },
                feature_observed_at={"sports_pregame_odds_features": row["odds_timestamp"]},
            )
        )
    return records


def build_platform_model_audit(
    *,
    kalshi_run_id: str,
    crypto_run_id: str,
    sports_run_id: str,
    persist: bool = True,
) -> dict[str, Any]:
    store = create_store()
    with store.connect() as connection:
        kalshi_groups = _kalshi_records(connection, kalshi_run_id)
        crypto_records = _crypto_records(connection, crypto_run_id)
        sports_records = _sports_records(connection, sports_run_id)
    evaluations: dict[str, Any] = {}
    for category, records in sorted(kalshi_groups.items()):
        result = evaluate_category_model(records, category=category)
        key = f"kalshi:{category}"
        evaluations[key] = {
            "workflow": "kalshi",
            "market_category": category,
            "result": result,
            "persistence": persist_category_evaluation(records, result) if persist else None,
        }
    if crypto_records:
        result = evaluate_category_model(crypto_records, category="crypto")
        evaluations["crypto:crypto"] = {
            "workflow": "crypto",
            "market_category": "crypto",
            "result": result,
            "persistence": persist_category_evaluation(crypto_records, result) if persist else None,
        }
    if sports_records:
        result = evaluate_category_model(sports_records, category="sports")
        evaluations["sports:sports"] = {
            "workflow": "sports",
            "market_category": "sports",
            "result": result,
            "persistence": persist_category_evaluation(sports_records, result) if persist else None,
        }
    state_counts: dict[str, int] = defaultdict(int)
    for evaluation in evaluations.values():
        state_counts[evaluation["result"]["model_state"]] += 1
    usable = [
        name
        for name, evaluation in evaluations.items()
        if evaluation["result"]["model_state"] == "validated_research"
    ]
    return {
        "report_type": "platform_model_validation",
        "research_only": True,
        "baseline": "market_implied_probability",
        "split_policy": "chronological_60_20_20_with_untouched_test_set",
        "walk_forward_supported": True,
        "evaluations": evaluations,
        "state_counts": dict(sorted(state_counts.items())),
        "usable_research_models": usable,
        "live_prediction_logic_changed": False,
        "model_training_started": False,
        "profitability_claim_allowed": False,
    }


def render_platform_model_audit(report: Mapping[str, Any]) -> str:
    lines = [
        "Platform Model Validation Audit",
        "Status: research-only; no live-rule change and no profitability claim",
        f"Baseline: {report['baseline']}",
        f"Split policy: {report['split_policy']}",
        "",
    ]
    for name, evaluation in report["evaluations"].items():
        result = evaluation["result"]
        selected = result.get("selected_challenger") or "market_implied"
        metrics = (result.get("test_metrics") or {}).get(selected) or (result.get("test_metrics") or {}).get("market_implied") or {}
        periods = result.get("periods") or {}
        lines.extend(
            [
                f"{name}:",
                f"- state: {result['model_state']}",
                f"- reason: {result.get('reason')}",
                f"- selected: {selected}",
                f"- sample size: {result.get('sample_size')}",
                f"- train: {periods.get('train')}",
                f"- validation: {periods.get('validation')}",
                f"- test: {periods.get('test')}",
                f"- Brier: {metrics.get('brier_score')}",
                f"- log loss: {metrics.get('log_loss')}",
                f"- calibration error: {metrics.get('calibration_error')}",
                f"- accuracy: {metrics.get('accuracy')}",
                f"- accuracy CI95: {metrics.get('accuracy_ci95')}",
                "",
            ]
        )
    lines.append(f"Usable research models: {report['usable_research_models'] or 'none'}")
    return "\n".join(lines)


def write_platform_model_audit(report: Mapping[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_platform_model_audit(report), encoding="utf-8")
    output.with_suffix(".json").write_text(json.dumps(report, indent=2, sort_keys=True, default=json_default), encoding="utf-8")


def default_platform_model_audit_path() -> Path:
    return Path("data") / "model_validation_audit.txt"
