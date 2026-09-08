from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..combo_safety import slip_has_authoritative_combo_evidence
from ..business_store import create_store
from ..database import as_decimal, json_default
from .quality import confidence_guardrail, validation_status_for_log


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _source_snapshot_hash(leg: dict[str, Any], generated_at: str) -> str:
    explicit = leg.get("source_snapshot_hash") or leg.get("source_snapshot_id")
    if explicit:
        return str(explicit)
    payload = {
        "market_ticker": leg.get("market_ticker"),
        "event_ticker": leg.get("event_ticker"),
        "side": leg.get("side"),
        "bid_cents": leg.get("bid_cents"),
        "ask_cents": leg.get("ask_cents"),
        "midpoint_cents": leg.get("midpoint_cents"),
        "event_start_time": leg.get("event_start_time") or leg.get("occurrence_datetime") or leg.get("start_time"),
        "market_close_time": leg.get("market_close_time") or leg.get("close_time"),
        "market_updated_at": leg.get("market_updated_at") or leg.get("source_updated_at"),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=json_default).encode("utf-8")).hexdigest()
    return digest


def _input_data_for_leg(leg: dict[str, Any], slip: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = [
        "market_ticker",
        "event_ticker",
        "display_event",
        "title",
        "subtitle",
        "status",
        "side",
        "probability",
        "required_probability",
        "bid_cents",
        "ask_cents",
        "midpoint_cents",
        "spread_cents",
        "open_interest",
        "volume_24h",
        "risk_flags",
        "warning_flags",
        "evidence_count",
        "margin_of_error",
        "research_mode",
        "event_start_time",
        "market_close_time",
        "api_fetched_at",
        "source_updated_at",
        "market_updated_at",
        "source_snapshot_id",
        "source_snapshot_hash",
        "combo_market_ticker",
        "combo_market_status",
        "combo_market_fetched_at",
        "combo_market_snapshot_hash",
        "combo_market_leg_signature",
        "combo_exact_leg_count",
        "combo_evidence_status",
    ]
    return {
        "slip_min_leg_probability": slip.get("min_leg_probability"),
        "slip_adjusted_probability": slip.get("adjusted_probability"),
        "leg": {field: leg.get(field) for field in allowed_fields if field in leg},
    }


def _reason_features_for_leg(leg: dict[str, Any], slip: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": leg.get("title"),
        "subtitle": leg.get("subtitle"),
        "display_event": leg.get("display_event"),
        "probability": leg.get("probability"),
        "required_probability": leg.get("required_probability") or slip.get("min_leg_probability"),
        "spread_cents": leg.get("spread_cents"),
        "open_interest": leg.get("open_interest"),
        "volume_24h": leg.get("volume_24h"),
        "risk_flags": leg.get("risk_flags") or [],
        "warning_flags": leg.get("warning_flags") or [],
        "research_mode": leg.get("research_mode"),
        "evidence_count": leg.get("evidence_count"),
        "margin_of_error": leg.get("margin_of_error"),
        "market_status": leg.get("status"),
        "listed_combo_market_ticker": leg.get("combo_market_ticker"),
        "combo_evidence_status": leg.get("combo_evidence_status"),
    }


def extract_prediction_logs_from_payload(payload: dict[str, Any], *, prediction_timestamp: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
    generated_at = prediction_timestamp or payload.get("generated_at") or datetime.now().astimezone().isoformat(timespec="seconds")
    slips = [
        ("primary_80", "market_implied_slip_v1", payload.get("custom_slip") or {}),
        ("leverage_75", "market_implied_slip_v1", payload.get("leverage_slip") or {}),
        ("all_day_75_85", "market_implied_slip_v1", payload.get("all_day_slip") or {}),
        ("research_edge", (payload.get("research_edge_slip") or {}).get("model") or "research_edge_v1", payload.get("research_edge_slip") or {}),
    ]
    logs: list[dict[str, Any]] = []
    for slip_name, model_version, slip in slips:
        if slip.get("action") != "BUILD_SLIP" or not slip_has_authoritative_combo_evidence(slip):
            continue
        for leg in slip.get("legs") or []:
            probability = as_decimal(leg.get("probability"), default=Decimal("0")) or Decimal("0")
            spread = leg.get("spread_cents")
            guardrail = confidence_guardrail(
                probability=probability,
                evidence_count=int(leg.get("evidence_count") or 0),
                source_backed=leg.get("research_mode") == "source_backed",
                margin_of_error=as_decimal(leg.get("margin_of_error")),
                spread_cents=as_decimal(spread) if spread is not None else None,
            )
            snapshot_hash = _source_snapshot_hash(leg, generated_at)
            log = {
                "run_id": run_id,
                "timestamp": generated_at,
                "event": leg.get("display_event") or leg.get("event_ticker") or "",
                "event_id": leg.get("event_ticker") or "",
                "market": leg.get("market_ticker") or "",
                "market_id": leg.get("market_ticker") or "",
                "side": leg.get("side") or "",
                "market_status": leg.get("status") or "",
                "strategy": slip_name,
                "event_start_time": _first_value(leg.get("event_start_time"), leg.get("occurrence_datetime"), leg.get("start_time")),
                "market_close_time": _first_value(leg.get("market_close_time"), leg.get("close_time"), leg.get("expected_expiration_time"), leg.get("expiration_time")),
                "api_fetched_at": leg.get("api_fetched_at") or generated_at,
                "source_updated_at": _first_value(leg.get("source_updated_at"), leg.get("market_updated_at"), leg.get("updated_time")),
                "source_snapshot_id": snapshot_hash,
                "source_snapshot_hash": snapshot_hash,
                "entry_price_cents": leg.get("ask_cents"),
                "implied_probability": probability,
                "reason_features": _reason_features_for_leg(leg, slip),
                "input_data_used": _input_data_for_leg(leg, slip),
                "odds_used": {
                    "bid_cents": leg.get("bid_cents"),
                    "ask_cents": leg.get("ask_cents"),
                    "midpoint_cents": leg.get("midpoint_cents"),
                },
                "model_version": model_version,
                "confidence_score": as_decimal(guardrail["score"]),
                "confidence_label": guardrail["label"],
                "predicted_outcome": leg.get("side") or "",
                "settlement_state": "unresolved",
                "actual_outcome": None,
                "profit_loss_cents": None,
                "slip_name": slip_name,
            }
            log.update(validation_status_for_log(log))
            logs.append(log)
    return logs


def log_payload_predictions(payload: dict[str, Any]) -> int:
    logs = extract_prediction_logs_from_payload(payload)
    if not logs:
        return 0
    store = create_store()
    store.insert_prediction_logs(logs)
    return len(logs)
