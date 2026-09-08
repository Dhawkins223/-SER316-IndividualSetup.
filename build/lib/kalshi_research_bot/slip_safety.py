from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .combo_safety import slip_has_authoritative_combo_evidence


DEFAULT_MAX_SLIP_AGE_SECONDS = 30 * 60
SLIP_PAYLOAD_KEYS = (
    "custom_slip",
    "leverage_slip",
    "all_day_slip",
    "research_edge_slip",
)


def slip_payload_gate(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_SLIP_AGE_SECONDS,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if payload.get("refresh_error"):
        return _blocked_gate(
            "blocked_refresh_failed",
            "The latest live refresh failed. Slips are hidden until a fresh refresh succeeds.",
        )

    source_cache_status = payload.get("source_cache_status") or {}
    stale_fallback_count = int(source_cache_status.get("stale_fallback_count") or 0)
    if stale_fallback_count:
        return _blocked_gate(
            "blocked_stale_source",
            "Fresh source data is unavailable. Slips are hidden until a live refresh succeeds.",
            stale_fallback_count=stale_fallback_count,
        )

    generated_at = _parse_timestamp(payload.get("generated_at"))
    if generated_at is None:
        return _blocked_gate(
            "blocked_missing_generated_at",
            "The live data timestamp is unavailable. Slips are hidden until the next valid refresh.",
        )

    age_seconds = int((now - generated_at.astimezone(timezone.utc)).total_seconds())
    if age_seconds < -300:
        return _blocked_gate(
            "blocked_invalid_generated_at",
            "The live data timestamp is invalid. Slips are hidden until the next valid refresh.",
            data_age_seconds=age_seconds,
        )
    if age_seconds > max(0, int(max_age_seconds)):
        return _blocked_gate(
            "blocked_stale_payload",
            "Live data is too old. Slips are hidden until a fresh refresh succeeds.",
            data_age_seconds=age_seconds,
        )
    return {
        "status": "ready",
        "code": "fresh_data_ready",
        "message": "Live data is fresh enough for manual review.",
        "data_age_seconds": max(0, age_seconds),
        "stale_fallback_count": 0,
    }


def gate_slip_payload(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_SLIP_AGE_SECONDS,
) -> dict[str, Any]:
    gated = dict(payload)
    gate = slip_payload_gate(payload, now=now, max_age_seconds=max_age_seconds)
    gated["public_data_gate"] = gate
    if gate["status"] == "ready":
        blocked_combo_count = 0
        for key in SLIP_PAYLOAD_KEYS:
            previous = payload.get(key) or {}
            if previous.get("action") != "BUILD_SLIP" or slip_has_authoritative_combo_evidence(previous):
                continue
            blocked_combo_count += 1
            gated[key] = {
                "action": "NO_SLIP",
                "reason": "Combo hidden because its exact active Kalshi KXMVE listing could not be verified.",
                "source_gate_status": "blocked_unverified_combo",
                "eligible_leg_count": 0,
                "blocked_previous_leg_count": int(previous.get("leg_count") or len(previous.get("legs") or [])),
                "leg_count": 0,
                "legs": [],
            }
        gated["combo_safety_gate"] = {
            "status": "ready" if not blocked_combo_count else "blocked_partial",
            "code": "verified_listed_combos_only",
            "blocked_slip_count": blocked_combo_count,
        }
        return gated

    for key in SLIP_PAYLOAD_KEYS:
        previous = payload.get(key) or {}
        gated[key] = {
            "action": "NO_SLIP",
            "reason": gate["message"],
            "source_gate_status": gate["code"],
            "eligible_leg_count": 0,
            "blocked_previous_leg_count": int(previous.get("leg_count") or 0),
            "leg_count": 0,
            "legs": [],
        }
    gated["pick_summary"] = {
        "action": "NO_BET",
        "reason": gate["message"],
        "source_gate_status": gate["code"],
        "candidates": [],
        "watchlist": [],
    }
    return gated


def consumer_payload(payload: dict[str, Any], *, include_research_scout: bool = True) -> dict[str, Any]:
    """The slip feed, carrying what the reader of it is allowed to see.

    The research-scout tier is operator-only on the dashboard, so leaving it in
    this feed meant a reader could fetch by hand the one tier their page
    withholds. Hiding a panel is not withholding data when the data has its own
    URL.
    """
    keys = [
        "date",
        "generated_at",
        "generated_at_note",
        "safety_note",
        "public_data_gate",
        "custom_slip",
        "leverage_slip",
        "all_day_slip",
    ]
    if include_research_scout:
        keys.append("research_edge_slip")
    return {key: payload.get(key) for key in keys}


def _blocked_gate(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "status": "blocked",
        "code": code,
        "message": message,
        "data_age_seconds": details.pop("data_age_seconds", None),
        "stale_fallback_count": details.pop("stale_fallback_count", 0),
        **details,
    }


def _parse_timestamp(value: Any) -> datetime | None:
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
