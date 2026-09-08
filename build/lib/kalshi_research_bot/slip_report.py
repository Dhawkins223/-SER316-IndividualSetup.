"""Turn a payload slip into a slip-analysis report the dashboard can render.

``slip_analysis`` is deliberately ignorant of this project's payload shape: it
takes priced legs and returns arithmetic. This module is the adapter, and the
two decisions it makes are the ones that determine whether the numbers mean
anything.

**Which price is the fair probability.** A Kalshi binary contract quotes a bid
and an ask, and the spread between them *is* the market maker's margin. The
midpoint is therefore the vig-free estimate, and it is what becomes
``fair_probability``. Using the ask there instead would hand every leg a fair
value equal to its own cost, and a slip of such legs reports a break-even it can
never beat.

**Which price is the cost.** You buy at the ask, so ``decimal_odds`` is
``100 / ask_cents``. The consequence is deliberate and worth reading off the
output: each leg's ``edge`` comes back as roughly *minus half the spread*,
because buying at the ask against a mid-fair value is a losing proposition by
exactly that much. A build that quietly used the midpoint on both sides would
show every leg at zero edge and every slip as fairly priced, which is the more
flattering answer and the wrong one.

Legs that cannot be priced, or whose quotes are stale, are dropped -- and every
drop is reported with its reason. Silently analysing four legs of a five-leg
slip would answer a question nobody asked.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping, Sequence

from .review_packet import SLIP_SOURCES
from .slip_analysis import (
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    SlipLeg,
    UnmodellableSlip,
    analyze_slip,
    conflicting_pairs,
    recommend_trim,
)

# Cheaper than the analysis default because this runs inside a request. A slip
# at these probabilities returns hundreds of hits here, which ``analyze_slip``
# grades and reports as ``precision`` on the response, so a thin estimate
# announces itself rather than passing as a firm one.
DASHBOARD_DRAWS = 6_000


def _probability(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 < number < 1.0 else None


def _cents(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 < number < 100.0 else None


def slip_legs_from_payload(
    rows: Sequence[Mapping[str, Any]],
    *,
    now: Any = None,
    max_quote_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    require_freshness: bool = True,
) -> tuple[list[SlipLeg], list[dict[str, Any]]]:
    """Priced legs, plus a reason for every row that did not become one."""

    from datetime import datetime, timezone

    def parse(value: Any) -> Any:
        if not value:
            return None
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    moment = parse(now) or datetime.now(timezone.utc)
    legs: list[SlipLeg] = []
    skipped: list[dict[str, Any]] = []

    for index, row in enumerate(rows):
        ticker = str(row.get("market_ticker") or f"leg_{index + 1}")

        def drop(reason: str) -> None:
            skipped.append({"leg_id": ticker, "reason": reason})

        fair = _probability(
            row.get("market_implied_probability")
            if row.get("market_implied_probability") is not None
            else row.get("probability")
        )
        if fair is None:
            drop("no_usable_market_implied_probability")
            continue

        # The ask is what a buyer pays. Falling back to the midpoint when no ask
        # is quoted would price the leg at better than it can be bought.
        ask = _cents(row.get("ask_cents"))
        if ask is None:
            drop("no_quoted_ask")
            continue

        if require_freshness:
            state = str(row.get("source_freshness_state") or row.get("source_state") or "").lower()
            if state in {"stale", "blocked", "failed", "empty", "unavailable", "cached", "missing"}:
                drop(f"source_state_{state or 'unknown'}")
                continue
            quoted = parse(row.get("api_fetched_at") or row.get("market_updated_at"))
            if quoted is None:
                drop("no_quote_timestamp")
                continue
            age = (moment - quoted).total_seconds()
            if age > max_quote_age_seconds:
                drop(f"quote_older_than_{max_quote_age_seconds}s")
                continue

        try:
            legs.append(
                SlipLeg(
                    leg_id=ticker,
                    selection=str(row.get("selection") or row.get("side") or ""),
                    decimal_odds=100.0 / ask,
                    fair_probability=fair,
                    event_id=str(row.get("event_ticker") or ""),
                    league=str(row.get("sport") or ""),
                    market=str(row.get("market_ticker") or ""),
                    team=str(row.get("team") or ""),
                    slate=str(row.get("slate") or row.get("game_date") or ""),
                    quoted_at=str(row.get("api_fetched_at") or ""),
                    source_state=str(row.get("source_freshness_state") or ""),
                )
            )
        except (TypeError, ValueError) as error:
            drop(f"unusable_leg:{error}")
    return legs, skipped


def _refusal(reason: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {
        "analysis_available": False,
        "reason": reason,
        "detail": detail,
        "evidence_class": "market_derived_arithmetic",
        "model_state": "baseline_only",
        "decision_status": "track_only",
        **extra,
    }


def build_slip_analysis(
    payload: Mapping[str, Any],
    slip_key: str = "primary",
    *,
    stake: float = 1.0,
    now: Any = None,
    draws: int = DASHBOARD_DRAWS,
) -> dict[str, Any]:
    """The full slip-analysis report for one of the payload's slips.

    Every path returns a dict a caller can render. A slip that cannot be
    analysed comes back with ``analysis_available: false`` and the reason,
    rather than as an error or as a report full of zeros -- "this slip has no
    fresh quotes" and "this slip is worth nothing" must not look the same on a
    screen.
    """

    if slip_key not in SLIP_SOURCES:
        raise ValueError(f"unknown_slip_key:{slip_key}")
    # ``float("nan")`` and ``float("inf")`` both parse, and ``nan <= 0`` is
    # False, so a positivity check alone lets them through to every figure in
    # the report -- and neither is a JSON literal, so the response would not
    # parse in a browser.
    if not isfinite(stake):
        raise ValueError("stake_must_be_finite")
    if stake <= 0:
        raise ValueError("stake_must_be_positive")

    payload_key, label = SLIP_SOURCES[slip_key]
    slip = payload.get(payload_key) or {}
    header = {
        "slip": slip_key,
        "label": label,
        "action": slip.get("action", "UNKNOWN"),
        "generated_at": payload.get("generated_at"),
        "stake": stake,
    }

    if slip.get("action") != "BUILD_SLIP":
        return {
            **header,
            **_refusal(
                "slip_not_built",
                str(slip.get("reason") or "The slip builder did not produce a slip for this tier."),
            ),
        }

    rows = list(slip.get("legs") or [])
    legs, skipped = slip_legs_from_payload(rows, now=now)
    if not legs:
        return {
            **header,
            **_refusal(
                "no_priceable_legs",
                "No leg in this slip carried both a fresh quote and a usable price.",
                submitted_leg_count=len(rows),
                skipped_legs=skipped,
            ),
        }
    if len(legs) < 2:
        return {
            **header,
            **_refusal(
                "single_leg_slip",
                "A joint probability needs at least two legs. One leg is its own price.",
                submitted_leg_count=len(rows),
                priced_leg_count=len(legs),
                skipped_legs=skipped,
            ),
        }

    try:
        report = analyze_slip(legs, stake=stake, draws=draws)
    except UnmodellableSlip as error:
        return {
            **header,
            **_refusal(
                "unmodellable_slip",
                str(error),
                submitted_leg_count=len(rows),
                priced_leg_count=len(legs),
                skipped_legs=skipped,
                conflicting_pairs=[list(pair) for pair in conflicting_pairs(legs)],
            ),
        }

    return {
        **header,
        "analysis_available": True,
        "submitted_leg_count": len(rows),
        "priced_leg_count": len(legs),
        # Carried on the response, not just logged: a slip analysed on four of
        # five legs is a different slip, and the screen has to be able to say so.
        "skipped_legs": skipped,
        "analysis": report,
        "trim": recommend_trim(legs, stake=stake, draws=max(2_000, draws // 3)),
    }
