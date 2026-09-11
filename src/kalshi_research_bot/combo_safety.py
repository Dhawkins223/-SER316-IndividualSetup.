from __future__ import annotations

import hashlib
import json
from typing import Any


VERIFIED_COMBO_EVIDENCE = "listed_kalshi_mve_market"
VERIFIED_COMBO_SOURCE = "kalshi_public_mve_market"


def market_is_tradable(market: dict[str, Any]) -> bool:
    ask = market.get("yes_ask_cents")
    return ask is not None and 0 < ask < 100


def combo_public_quote_state(market: dict[str, Any]) -> str:
    """Classify an exact Kalshi combo without inventing a public executable price.

    This lives here rather than in `today.py` because both the collector that
    stamps `public_quote_state` and the dashboard that renders it need the same
    answer, and `paper_server` cannot import `today` at module scope without
    reintroducing the evaluation cycle 4790889 fixed. One copy, in the leaf
    module they both already import, is what stops the two from drifting.

    A stamped `public_quote_state` wins: the collector saw the market at
    collection time, and re-deriving from a snapshot that may have been
    normalised since would second-guess it. Everything below is the fallback
    for a payload written before the field existed.

    The all-or-nothing book -- nothing bid on YES, NO offered at the full
    dollar -- is the shape Kalshi leaves on a combination it will not quote
    publicly. It is inferred from the orderbook, not reported by the API, so
    what the dashboard says about it stays on the observation ("no executable
    price is quoted") rather than asserting the exchange's intent.
    """
    stamped = str(market.get("public_quote_state") or "").lower()
    if stamped in {"tradable", "rfq_required", "unavailable"}:
        return stamped
    if market_is_tradable(market):
        return "tradable"
    ticker = str(market.get("ticker") or "").upper()
    status = str(market.get("status") or "").lower()
    try:
        yes_ask = float(market.get("yes_ask_cents") or 0)
        yes_bid = float(market.get("yes_bid_cents") or 0)
        no_ask = float(market.get("no_ask_cents") or 0)
        no_bid = float(market.get("no_bid_cents") or 0)
    except (TypeError, ValueError):
        return "unavailable"
    if (
        ticker.startswith("KXMVE")
        and status in {"active", "open"}
        and yes_ask == 0
        and yes_bid == 0
        and no_ask == 100
        and no_bid == 100
    ):
        return "rfq_required"
    return "unavailable"


def combo_leg_signature(legs: list[dict[str, Any]]) -> str:
    selected = sorted(
        (
            str(leg.get("market_ticker") or ""),
            str(leg.get("side") or "").lower(),
        )
        for leg in legs
    )
    encoded = json.dumps(selected, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def authoritative_combo_leg_rejection_reasons(
    leg: dict[str, Any],
    *,
    require_tradable_quote: bool = True,
) -> list[str]:
    reasons: list[str] = []
    combo_ticker = str(leg.get("combo_market_ticker") or "").upper()
    combo_status = str(leg.get("combo_market_status") or "").lower()
    combo_quote = leg.get("combo_market_yes_ask_cents")
    if leg.get("combo_evidence_status") != VERIFIED_COMBO_EVIDENCE:
        reasons.append("missing_authoritative_combo_evidence")
    if leg.get("combo_source") != VERIFIED_COMBO_SOURCE:
        reasons.append("invalid_combo_evidence_source")
    if not combo_ticker.startswith("KXMVE"):
        reasons.append("missing_authoritative_combo_market")
    if combo_status not in {"active", "open"}:
        reasons.append("combo_market_not_open_or_active")
    if not leg.get("combo_market_fetched_at"):
        reasons.append("missing_combo_market_fetched_at")
    if not leg.get("combo_market_snapshot_hash"):
        reasons.append("missing_combo_market_snapshot_hash")
    if not leg.get("combo_market_leg_signature"):
        reasons.append("missing_combo_market_leg_signature")
    try:
        exact_leg_count = int(leg.get("combo_exact_leg_count") or 0)
    except (TypeError, ValueError):
        exact_leg_count = 0
    if exact_leg_count <= 0:
        reasons.append("missing_combo_exact_leg_count")
    if require_tradable_quote:
        try:
            live_quote = float(combo_quote)
        except (TypeError, ValueError):
            live_quote = 0.0
        if not 0.0 < live_quote < 100.0:
            reasons.append("combo_quote_not_tradable")
    return sorted(set(reasons))


def authoritative_combo_slip_rejection_reasons(
    legs: list[dict[str, Any]],
    *,
    require_tradable_quote: bool = True,
) -> list[str]:
    if not legs:
        return ["missing_combo_legs"]
    reasons: list[str] = []
    combo_tickers: set[str] = set()
    evidence_signatures: set[str] = set()
    expected_leg_counts: set[int] = set()
    for leg in legs:
        reasons.extend(
            authoritative_combo_leg_rejection_reasons(
                leg,
                require_tradable_quote=require_tradable_quote,
            )
        )
        if leg.get("combo_eligible") is not True:
            reasons.append("unverified_combo_leg")
        combo_ticker = str(leg.get("combo_market_ticker") or "")
        if combo_ticker:
            combo_tickers.add(combo_ticker)
        signature = str(leg.get("combo_market_leg_signature") or "")
        if signature:
            evidence_signatures.add(signature)
        try:
            expected_leg_counts.add(int(leg.get("combo_exact_leg_count") or 0))
        except (TypeError, ValueError):
            expected_leg_counts.add(0)
    if len(combo_tickers) != 1:
        reasons.append("legs_not_from_one_listed_combo_market")
    if len(evidence_signatures) != 1:
        reasons.append("inconsistent_combo_leg_signature")
    if expected_leg_counts != {len(legs)}:
        reasons.append("combo_leg_count_mismatch")
    actual_signature = combo_leg_signature(legs)
    if evidence_signatures and actual_signature not in evidence_signatures:
        reasons.append("combo_leg_signature_mismatch")
    return sorted(set(reasons))


def slip_has_authoritative_combo_evidence(slip: dict[str, Any]) -> bool:
    if slip.get("action") != "BUILD_SLIP":
        return False
    legs = list(slip.get("legs") or [])
    compatibility = slip.get("combo_compatibility") or {}
    combo_tickers = {str(leg.get("combo_market_ticker") or "") for leg in legs}
    listed_combo_ticker = str(slip.get("listed_combo_market_ticker") or "")
    return (
        compatibility.get("status") == "compatible"
        and compatibility.get("exact_listed_combo") is True
        and combo_tickers == {listed_combo_ticker}
        and listed_combo_ticker.upper().startswith("KXMVE")
        and not authoritative_combo_slip_rejection_reasons(legs)
    )
