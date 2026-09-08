from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .combo_safety import slip_has_authoritative_combo_evidence


SLIP_SOURCES = {
    "primary": ("custom_slip", "80c+ Market Tier"),
    "leverage": ("leverage_slip", "75c+ Market Tier"),
    "all_day": ("all_day_slip", "All-Day 75-85c Tier"),
    "research_edge": ("research_edge_slip", "Research Scout Slip"),
}


def build_review_packet(payload: dict[str, Any], slip_key: str = "primary") -> dict[str, Any]:
    if slip_key not in SLIP_SOURCES:
        raise ValueError(f"unknown_slip_key:{slip_key}")
    payload_key, label = SLIP_SOURCES[slip_key]
    slip = payload.get(payload_key) or {}
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    verified_combo = slip_has_authoritative_combo_evidence(slip)
    source_legs = list(slip.get("legs") or [])
    legs = [_packet_leg(index, leg) for index, leg in enumerate(source_legs, start=1)] if verified_combo else []
    ready = slip.get("action") == "BUILD_SLIP" and bool(legs)
    summary = {
        "action": slip.get("action", "UNKNOWN"),
        "leg_count": len(legs),
        "blocked_unverified_leg_count": 0 if verified_combo else len(source_legs),
        "sports": list(slip.get("sports") or []),
        "combo_categories": list(slip.get("combo_categories") or []),
        "category_counts": dict(slip.get("category_counts") or {}),
        "estimated_combo_price_cents": slip.get("estimated_combo_price_cents"),
        "stake_dollars": slip.get("stake_dollars"),
        "estimated_payout_if_right": slip.get("estimated_payout_if_right"),
        "raw_probability": slip.get("raw_probability"),
        "adjusted_probability": slip.get("adjusted_probability"),
        "correlation_adjustment": slip.get("correlation_adjustment"),
        "correlation_adjustment_resolved": slip.get("correlation_adjustment_resolved"),
        "joint_basis": slip.get("joint_basis"),
        "overlap_safe": slip.get("overlap_safe"),
        "overlap_policy": slip.get("overlap_policy"),
        "combo_compatibility": slip.get("combo_compatibility") or {},
        "manual_entry_ready": slip.get("manual_entry_ready"),
        "listed_combo_market_ticker": slip.get("listed_combo_market_ticker"),
        "listed_combo_event_ticker": slip.get("listed_combo_event_ticker"),
        "listed_combo_side": slip.get("listed_combo_side"),
        "listed_combo_yes_bid_cents": slip.get("listed_combo_yes_bid_cents"),
        "listed_combo_yes_ask_cents": slip.get("listed_combo_yes_ask_cents"),
        "listed_combo_status": slip.get("listed_combo_status"),
        "listed_combo_fetched_at": slip.get("listed_combo_fetched_at"),
        "listed_combo_snapshot_hash": slip.get("listed_combo_snapshot_hash"),
        "combo_price_source": slip.get("combo_price_source"),
        "reason": slip.get("reason"),
    }
    compatibility = summary["combo_compatibility"] or {}
    blocked = not verified_combo
    if blocked:
        summary["combo_compatibility"] = {
            **compatibility,
            "status": "blocked",
            "exact_listed_combo": False,
            "rejection_reasons": sorted(
                set([*(compatibility.get("rejection_reasons") or []), "missing_verified_listed_combo"])
            ),
        }
    packet = {
        "packet_type": "kalshi_manual_review_packet",
        "slip_key": slip_key,
        "slip_label": label,
        "ready": ready and not blocked,
        "created_at": created_at,
        "source_date": payload.get("date"),
        "source_generated_at": payload.get("generated_at"),
        "source_note": payload.get("generated_at_note"),
        "safety": {
            "manual_review_only": True,
            "account_write_enabled": False,
            "auto_trade_enabled": False,
            "auto_bet_enabled": False,
            "order_submission_enabled": False,
            "requires_human_review_before_any_bet": True,
            "note": "Local review packet only. It does not create, stage, upload, or submit any Kalshi order.",
        },
        "summary": summary,
        "legs": legs,
        "review_checklist": [
            "Confirm the listed KXMVE combo ticker and every underlying market ticker in Kalshi before doing anything.",
            "Confirm category, side, event start time, live ask, close time, and market status.",
            "Confirm Kalshi allows every selected market to be combined before entering the full slip.",
            "Skip the slip if any leg changed materially, closed, or cannot be found.",
            "Do not treat this packet as proof of edge or profitability.",
        ],
    }
    packet["packet_hash"] = _packet_hash(packet)
    packet["copy_blocks"] = _copy_blocks(packet)
    return packet


def build_all_review_packets(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "packet_type": "kalshi_manual_review_packet_bundle",
        "source_date": payload.get("date"),
        "source_generated_at": payload.get("generated_at"),
        "safety": {
            "manual_review_only": True,
            "account_write_enabled": False,
            "auto_trade_enabled": False,
            "auto_bet_enabled": False,
            "order_submission_enabled": False,
        },
        "packets": {slip_key: build_review_packet(payload, slip_key) for slip_key in SLIP_SOURCES},
    }


def render_review_packet_text(packet: dict[str, Any]) -> str:
    summary = packet.get("summary") or {}
    safety = packet.get("safety") or {}
    probability_label = "Research combo estimate" if packet.get("slip_key") == "research_edge" else "Market-implied combo estimate"
    lines = [
        f"MANUAL REVIEW PACKET - {packet.get('slip_label', 'Slip')}",
        "NOT AN ORDER. No account upload. No auto-bet. Review manually before any action.",
        f"Source generated: {packet.get('source_generated_at') or 'unknown'}",
        f"Packet hash: {packet.get('packet_hash') or 'pending'}",
        "",
        "SUMMARY",
        f"- Ready: {packet.get('ready')}",
        f"- Legs: {summary.get('leg_count', 0)}",
        f"- Estimated combo price hint: {_format_cents(summary.get('estimated_combo_price_cents'))}",
        f"- Estimated payout if every leg hits: {_format_dollars(summary.get('estimated_payout_if_right'))}",
        f"- {probability_label}: {_format_percent(summary.get('adjusted_probability'))}",
        f"- Overlap safe: {summary.get('overlap_safe')}",
        f"- Combo compatibility: {(summary.get('combo_compatibility') or {}).get('status', 'unknown')}",
        f"- Exact listed combo: {(summary.get('combo_compatibility') or {}).get('exact_listed_combo', False)}",
        f"- Listed combo ticker: {summary.get('listed_combo_market_ticker') or 'n/a'}",
        f"- Listed combo side: {summary.get('listed_combo_side') or 'n/a'}",
        f"- Live combo ask: {_format_cents(summary.get('listed_combo_yes_ask_cents'))}",
        f"- Manual entry ready: {summary.get('manual_entry_ready')}",
        f"- Categories: {', '.join(summary.get('combo_categories') or summary.get('sports') or []) or 'n/a'}",
        f"- Manual review only: {safety.get('manual_review_only')}",
        "",
        "FAST ENTRY LINES",
    ]
    for leg in packet.get("legs") or []:
        lines.append(
            f"{leg.get('position')}. {leg.get('market_ticker')} | {leg.get('side')} | "
            f"{leg.get('selection')} | ask {_format_cents(leg.get('ask_cents'))} | "
            f"start {leg.get('event_start_time') or 'n/a'} | close {leg.get('market_close_time') or 'n/a'} | "
            f"status {leg.get('status') or 'n/a'} | "
            f"{leg.get('display_event')}"
        )
    lines.append("")
    lines.append("ENTRY DETAIL")
    for leg in packet.get("legs") or []:
        lines.append(
            f"{leg.get('position')}. category={leg.get('combo_category') or 'n/a'} | "
            f"event_ticker={leg.get('event_ticker') or 'n/a'} | overlap={leg.get('overlap_key') or 'n/a'} | "
            f"start={leg.get('event_start_time') or 'n/a'} | close={leg.get('market_close_time') or 'n/a'} | "
            f"fetched={leg.get('api_fetched_at') or 'n/a'} | warnings={', '.join(leg.get('manual_entry_warnings') or []) or 'none'}"
        )
    lines.extend(["", "TICKERS + SIDES", packet.get("copy_blocks", {}).get("ticker_stack", ""), "", "CHECKLIST"])
    for item in packet.get("review_checklist") or []:
        lines.append(f"[ ] {item}")
    return "\n".join(lines).strip() + "\n"


def safe_review_packet_filename(packet: dict[str, Any], extension: str) -> str:
    source_date = "".join(ch for ch in str(packet.get("source_date") or "today") if ch.isalnum() or ch in {"-", "_"})
    slip_key = "".join(ch for ch in str(packet.get("slip_key") or "slip") if ch.isalnum() or ch in {"-", "_"})
    clean_extension = extension.lstrip(".") or "txt"
    return f"{source_date}_{slip_key}_manual_review_packet.{clean_extension}"


def _packet_leg(position: int, leg: dict[str, Any]) -> dict[str, Any]:
    side = str(leg.get("side") or "").upper()
    selection = str(leg.get("subtitle") or leg.get("title") or leg.get("market_ticker") or "")
    return {
        "position": position,
        "sport": leg.get("sport"),
        "combo_category": leg.get("combo_category") or leg.get("category") or leg.get("sport"),
        "market_ticker": leg.get("market_ticker"),
        "event_ticker": leg.get("event_ticker"),
        "display_event": leg.get("display_event") or leg.get("event_ticker"),
        "side": side,
        "selection": selection,
        "probability": leg.get("probability"),
        "required_probability": leg.get("required_probability"),
        "bid_cents": leg.get("bid_cents"),
        "ask_cents": leg.get("ask_cents"),
        "status": leg.get("status"),
        "market_close_time": leg.get("market_close_time") or leg.get("close_time"),
        "event_start_time": leg.get("event_start_time"),
        "api_fetched_at": leg.get("api_fetched_at"),
        "market_updated_at": leg.get("market_updated_at") or leg.get("source_updated_at"),
        "overlap_key": leg.get("overlap_key"),
        "combo_eligible": leg.get("combo_eligible") is True,
        "combo_rejection_reasons": list(leg.get("combo_rejection_reasons") or []),
        "combo_market_ticker": leg.get("combo_market_ticker"),
        "combo_market_status": leg.get("combo_market_status"),
        "combo_market_fetched_at": leg.get("combo_market_fetched_at"),
        "combo_market_snapshot_hash": leg.get("combo_market_snapshot_hash"),
        "combo_market_leg_signature": leg.get("combo_market_leg_signature"),
        "combo_exact_leg_count": leg.get("combo_exact_leg_count"),
        "combo_evidence_status": leg.get("combo_evidence_status"),
        "combo_source": leg.get("combo_source"),
        "manual_entry_ready": leg.get("manual_entry_ready"),
        "manual_entry_warnings": list(leg.get("manual_entry_warnings") or []),
        "source": "local_public_kalshi_market_data",
    }


def _copy_blocks(packet: dict[str, Any]) -> dict[str, str]:
    fast_lines = []
    ticker_lines = []
    for leg in packet.get("legs") or []:
        ticker = leg.get("market_ticker") or ""
        side = leg.get("side") or ""
        fast_lines.append(
            f"{leg.get('position')}. {ticker} | {side} | {_format_cents(leg.get('ask_cents'))} | "
            f"{leg.get('selection')} | status {leg.get('status') or 'n/a'} | "
            f"start {leg.get('event_start_time') or 'n/a'} | close {leg.get('market_close_time') or 'n/a'} | "
            f"{leg.get('display_event')}"
        )
        ticker_lines.append(
            f"{ticker}\t{side}\t{leg.get('selection')}\t{_format_cents(leg.get('ask_cents'))}\t"
            f"{leg.get('event_start_time') or 'n/a'}"
        )
    return {
        "fast_entry": "\n".join(fast_lines),
        "ticker_stack": "\n".join(ticker_lines),
        "review_packet": render_review_packet_text({**packet, "copy_blocks": {"ticker_stack": "\n".join(ticker_lines)}}),
    }


def _packet_hash(packet: dict[str, Any]) -> str:
    stable = {
        "packet_type": packet.get("packet_type"),
        "slip_key": packet.get("slip_key"),
        "source_date": packet.get("source_date"),
        "source_generated_at": packet.get("source_generated_at"),
        "summary": packet.get("summary"),
        "legs": packet.get("legs"),
        "safety": packet.get("safety"),
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _format_cents(value: Any) -> str:
    try:
        return f"{float(value):.2f}c"
    except (TypeError, ValueError):
        return "n/a"


def _format_dollars(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _format_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "n/a"
