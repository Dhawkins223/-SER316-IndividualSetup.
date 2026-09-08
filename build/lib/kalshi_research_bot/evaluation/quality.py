from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any


FORBIDDEN_LOOKAHEAD_KEYS = {
    "actual",
    "actual_outcome",
    "away_score",
    "boxscore",
    "closing_odds",
    "final",
    "final_score",
    "home_score",
    "linescore",
    "post_event_stats",
    "resolution",
    "result",
    "settled",
    "settled_at",
    "winner",
}

NON_TRADABLE_MARKET_STATUSES = {
    "closed",
    "settled",
    "resolved",
    "canceled",
    "cancelled",
    "void",
    "inactive",
}


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        timestamp = value
    else:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def find_lookahead_fields(payload: Any, path: str = "") -> list[str]:
    leaks: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            has_value = value not in {None, "", False} if not isinstance(value, (dict, list, set)) else bool(value)
            if key_text.lower() in FORBIDDEN_LOOKAHEAD_KEYS and has_value:
                leaks.append(child_path)
                continue
            leaks.extend(find_lookahead_fields(value, child_path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            leaks.extend(find_lookahead_fields(item, f"{path}[{index}]"))
    return leaks


def data_quality_failures(
    snapshot: dict[str, Any],
    *,
    max_age_seconds: int | None = None,
    required_probability: bool = True,
) -> list[str]:
    failures: list[str] = []
    event = snapshot.get("event") or {}
    market = snapshot.get("market") or {}
    prediction = snapshot.get("prediction") or snapshot.get("model") or {}
    snapshot_at = parse_timestamp(snapshot.get("snapshot_at") or snapshot.get("as_of"))
    event_start = parse_timestamp(event.get("start_time") or event.get("event_start_time") or snapshot.get("event_start") or snapshot.get("event_start_time"))
    market_close = parse_timestamp(
        market.get("close_time")
        or market.get("market_close_time")
        or snapshot.get("market_close_time")
        or snapshot.get("close_time")
    )
    if snapshot_at is None:
        failures.append("missing_snapshot_timestamp")
    if event_start is None:
        failures.append("missing_event_start_time")
    if market_close is None:
        failures.append("missing_market_close_time")
    if snapshot_at and event_start and snapshot_at >= event_start:
        failures.append("snapshot_not_before_event_start")
    if event_start and market_close and market_close < event_start:
        failures.append("market_closes_before_event_start")
    if snapshot_at and max_age_seconds is not None:
        age_seconds = (datetime.now(timezone.utc) - snapshot_at.astimezone(timezone.utc)).total_seconds()
        if age_seconds > max_age_seconds:
            failures.append("stale_snapshot")
    if not (event.get("event_id") or snapshot.get("event_id")):
        failures.append("missing_event_id")
    if not (market.get("ticker") or market.get("market_ticker")):
        failures.append("missing_market_ticker")
    if not (market.get("side") or prediction.get("side") or snapshot.get("side")):
        failures.append("missing_prediction_side")
    if required_probability and prediction.get("probability") is None and market.get("model_probability") is None:
        yes_ask = market.get("yes_ask_cents")
        no_ask = market.get("no_ask_cents")
        if yes_ask is None or no_ask is None:
            failures.append("missing_probability_or_prices")
    for leak in find_lookahead_fields(snapshot.get("input") or snapshot.get("features") or {}):
        failures.append(f"lookahead_field:{leak}")
    for container_name in ["event", "market", "prediction", "model"]:
        container = snapshot.get(container_name) or {}
        for leak in find_lookahead_fields(container):
            failures.append(f"lookahead_field:{container_name}.{leak}")
    return sorted(set(failures))


def prediction_validation_errors(log: dict[str, Any]) -> list[str]:
    timestamp = parse_timestamp(log.get("timestamp") or log.get("prediction_timestamp"))
    event_start = parse_timestamp(log.get("event_start_time"))
    market_close = parse_timestamp(log.get("market_close_time"))
    market_status = str(log.get("market_status") or log.get("status") or "").strip().lower()
    errors: list[str] = []
    if not log.get("run_id"):
        errors.append("missing_run_id")
    if timestamp is None:
        errors.append("missing_prediction_timestamp")
    if event_start is None:
        errors.append("missing_event_start_time")
    if market_close is None:
        errors.append("missing_market_close_time")
    if timestamp and event_start and timestamp >= event_start:
        errors.append("prediction_after_event_start")
    if timestamp and market_close and timestamp >= market_close:
        errors.append("prediction_after_market_close")
    if event_start and market_close and market_close < event_start:
        errors.append("market_closes_before_event_start")
    if market_status in NON_TRADABLE_MARKET_STATUSES:
        errors.append(f"market_not_tradable:{market_status}")
    return sorted(set(errors))


def validation_status_for_log(log: dict[str, Any]) -> dict[str, Any]:
    errors = prediction_validation_errors(log)
    return {
        "validation_status": "invalid" if errors else "valid",
        "validation_errors": errors,
        "evaluable": not errors,
    }


def confidence_guardrail(
    *,
    probability: Decimal | int | float | str,
    evidence_count: int = 0,
    source_backed: bool = False,
    margin_of_error: Decimal | int | float | str | None = None,
    spread_cents: Decimal | int | float | str | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    def decimal_value(value: Decimal | int | float | str, *, field_name: str) -> Decimal:
        if isinstance(value, bool):
            raise ValueError(f"{field_name}_must_be_numeric")
        try:
            result = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field_name}_must_be_numeric") from exc
        if not result.is_finite():
            raise ValueError(f"{field_name}_must_be_finite")
        return result

    probability_value = decimal_value(probability, field_name="probability")
    margin_value = None if margin_of_error is None else decimal_value(margin_of_error, field_name="margin_of_error")
    spread_value = None if spread_cents is None else decimal_value(spread_cents, field_name="spread_cents")
    reasons: list[str] = []
    if stale:
        reasons.append("stale_data")
    if spread_value is None:
        reasons.append("missing_spread")
    elif spread_value > Decimal("12"):
        reasons.append("wide_spread")
    if not source_backed and evidence_count < 2:
        reasons.append("market_implied_only")
    if margin_value is not None and margin_value > Decimal("0.08"):
        reasons.append("margin_of_error_too_wide")
    score = max(Decimal("0"), min(Decimal("1"), probability_value))
    if reasons:
        score = min(score, Decimal("0.69"))
    elif evidence_count >= 2 and source_backed:
        score = min(Decimal("0.99"), score + Decimal("0.03"))
    label = "high_confidence" if score >= Decimal("0.80") and not reasons else "price_implied"
    return {
        "score": round(score, 6),
        "label": label,
        "high_confidence_allowed": label == "high_confidence",
        "reasons": reasons,
    }
