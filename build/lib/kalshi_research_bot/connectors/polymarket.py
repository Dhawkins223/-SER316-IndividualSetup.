"""A second venue, so a price has something to be compared against.

Kalshi is the only exchange this platform reads. A single venue's price cannot
be checked against anything: when it moves, there is no way to tell whether the
world changed or that one order book did. Polymarket runs the same kind of
binary contract on many of the same events and publishes its market data through
a public read-only API with no key, no account, and no cost.

That makes it the cheapest possible answer to backlog E-08 — do exchange prices
disagree systematically? — and it is the first data in this repository that can
contradict Kalshi rather than merely accompany it.

**An exchange price is not a bookmaker price, and the difference matters here.**
A sportsbook's two prices imply probabilities summing above one, and the excess
is margin that a de-vig model such as Shin is designed to attribute back. An
order book's two sides sum near one already, and what they miss by is the spread
between resting orders. Applying a margin model to that would invent a
bookmaker's incentive where none exists, so this module normalizes
multiplicatively, publishes the pre-normalization sum, and says which it did.

The mapping is exercised against recorded fixtures and was revalidated against
live public Gamma responses on 2026-08-29, including the sports directory,
market assets, event identifiers, and quoted outcomes. `source-probe
polymarket` remains the contract check: it makes one request, runs the real
normalizer, and reports precisely which fields were present, missing, or
unparsable rather than failing later with a KeyError.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from ..private_research import deterministic_hash
from .http import HttpClient, live_probe_client, non_live_response_reason


VENUE = "polymarket"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
MARKETS_ENDPOINT = f"{GAMMA_BASE_URL}/markets"
SPORTS_ENDPOINT = f"{GAMMA_BASE_URL}/sports"
SPORTS_MARKET_TYPES_ENDPOINT = f"{GAMMA_BASE_URL}/sports/market-types"
TEAMS_ENDPOINT = f"{GAMMA_BASE_URL}/teams"
PARSER_VERSION = "polymarket_gamma_v2"

# An order book's two sides sum near one. A market whose quoted prices are far
# from that is not a tight two-sided market and is refused rather than
# normalized into looking like one.
MIN_PRICE_SUM = Decimal("0.80")
MAX_PRICE_SUM = Decimal("1.20")

PROBABILITY_SCALE = Decimal("0.000001")


@dataclass
class PolymarketNormalization:
    """Normalized markets plus an exact account of what was refused."""

    markets: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    api_fetched_at: str = ""
    source_url: str = MARKETS_ENDPOINT
    parser_version: str = PARSER_VERSION

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.rejections:
            reason = str(entry.get("reason"))
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def evidence(self) -> dict[str, Any]:
        return {
            "venue": VENUE,
            "source_url": self.source_url,
            "parser_version": self.parser_version,
            "api_fetched_at": self.api_fetched_at,
            "normalized_market_count": len(self.markets),
            "rejection_count": len(self.rejections),
            "rejection_reasons": self.rejection_counts(),
        }


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _string_list(value: Any) -> list[str] | None:
    """Gamma returns `outcomes` and `outcomePrices` as JSON *strings*, not arrays."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed]


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _probability_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(PROBABILITY_SCALE), "f")


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _market_rows(payload: Any) -> list[Mapping[str, Any]]:
    """Accept either a bare list or an object wrapping one."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("data", "markets", "results"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, Mapping)]
    return []


def normalize_polymarket_sports(payload: Any, *, api_fetched_at: str) -> PolymarketNormalization:
    """Normalize Gamma's sports directory without treating it as a market list."""
    result = PolymarketNormalization(
        api_fetched_at=api_fetched_at,
        source_url=SPORTS_ENDPOINT,
    )
    if not isinstance(payload, list):
        result.rejections.append({"sport_id": None, "reason": "sports_payload_not_a_list"})
        return result
    for row in payload:
        if not isinstance(row, Mapping):
            result.rejections.append({"sport_id": None, "reason": "sport_not_an_object"})
            continue
        sport_id = str(row.get("id") or "").strip()
        sport_code = str(row.get("sport") or "").strip()
        display_name = str(row.get("name") or "").strip()
        if not sport_id or not sport_code or not display_name:
            result.rejections.append(
                {"sport_id": sport_id or None, "reason": "missing_sport_identity"}
            )
            continue
        result.markets.append(
            {
                "source": VENUE,
                "source_sport_id": sport_id,
                "sport_code": sport_code,
                "display_name": display_name,
                "ordering": str(row.get("ordering") or "").strip() or None,
                "primary_tag_id": str(row.get("primaryTagId") or "").strip() or None,
                "series_id": str(row.get("series") or "").strip() or None,
                "resolution_url": str(row.get("resolution") or "").strip() or None,
                "image_url": str(row.get("image") or "").strip() or None,
                "metadata": {
                    "tags": str(row.get("tags") or "").strip() or None,
                    "created_at": _timestamp(row.get("createdAt")),
                },
                "api_fetched_at": api_fetched_at,
                "source_snapshot_hash": deterministic_hash(row),
                "parser_version": PARSER_VERSION,
            }
        )
    return result


def normalize_polymarket_teams(payload: Any, *, api_fetched_at: str) -> PolymarketNormalization:
    """Normalize the public Gamma team directory into source entities."""
    result = PolymarketNormalization(
        api_fetched_at=api_fetched_at,
        source_url=TEAMS_ENDPOINT,
    )
    if not isinstance(payload, list):
        result.rejections.append({"team_id": None, "reason": "teams_payload_not_a_list"})
        return result
    for row in payload:
        if not isinstance(row, Mapping):
            result.rejections.append({"team_id": None, "reason": "team_not_an_object"})
            continue
        team_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not team_id or not name:
            result.rejections.append(
                {"team_id": team_id or None, "reason": "missing_team_identity"}
            )
            continue
        result.markets.append(
            {
                "source": VENUE,
                "source_entity_id": f"team:{team_id}",
                "entity_type": "team",
                "display_name": name,
                "competition": str(row.get("league") or "").strip() or None,
                "source_id": team_id,
                "source_ids": {
                    "abbreviation": str(row.get("abbreviation") or "").strip() or None,
                    "alias": str(row.get("alias") or "").strip() or None,
                },
                "details": {
                    "record": str(row.get("record") or "").strip() or None,
                    "created_at": _timestamp(row.get("createdAt")),
                    "logo_url": str(row.get("logo") or "").strip() or None,
                },
                "source_updated_at": _timestamp(row.get("updatedAt")),
            }
        )
    return result


def normalize_polymarket_markets(
    payload: Any,
    *,
    api_fetched_at: str,
    source_url: str = MARKETS_ENDPOINT,
    include_closed: bool = False,
) -> PolymarketNormalization:
    """Turn a Gamma markets response into comparable two-sided prices.

    A market is kept only when its outcomes and prices line up one-to-one and its
    prices sum close enough to one to be an order book rather than a stub. Every
    refusal is recorded with the market's identifier and a reason, so a change in
    the upstream shape shows up as a named rejection count rather than as an
    empty result.
    """
    result = PolymarketNormalization(api_fetched_at=api_fetched_at, source_url=source_url)
    rows = _market_rows(payload)
    if not rows:
        result.rejections.append({"market_id": None, "reason": "no_markets_in_payload"})
        return result

    for row in rows:
        market_id = str(row.get("id") or row.get("conditionId") or "").strip()
        slug = str(row.get("slug") or "").strip()
        identifier = market_id or slug
        if not identifier:
            result.rejections.append({"market_id": None, "reason": "missing_identifier"})
            continue

        closed = bool(row.get("closed"))
        active = row.get("active")
        if closed and not include_closed:
            result.rejections.append({"market_id": identifier, "reason": "closed_market"})
            continue
        if active is False and not include_closed:
            result.rejections.append({"market_id": identifier, "reason": "inactive_market"})
            continue

        outcomes = _string_list(row.get("outcomes"))
        price_texts = _string_list(row.get("outcomePrices"))
        if outcomes is None or price_texts is None:
            result.rejections.append({"market_id": identifier, "reason": "missing_outcomes_or_prices"})
            continue
        if len(outcomes) != len(price_texts):
            result.rejections.append({"market_id": identifier, "reason": "outcome_price_length_mismatch"})
            continue
        if len(outcomes) < 2:
            result.rejections.append({"market_id": identifier, "reason": "not_two_sided"})
            continue

        prices = [_decimal_or_none(text) for text in price_texts]
        if any(price is None for price in prices):
            result.rejections.append({"market_id": identifier, "reason": "unparsable_price"})
            continue
        if any(price < 0 for price in prices):  # type: ignore[operator]
            result.rejections.append({"market_id": identifier, "reason": "negative_price"})
            continue

        price_sum = sum(prices, Decimal(0))  # type: ignore[arg-type]
        if price_sum <= 0:
            result.rejections.append({"market_id": identifier, "reason": "zero_price_sum"})
            continue
        if not (MIN_PRICE_SUM <= price_sum <= MAX_PRICE_SUM):
            result.rejections.append(
                {
                    "market_id": identifier,
                    "reason": "price_sum_outside_two_sided_band",
                    "price_sum": format(price_sum, "f"),
                }
            )
            continue

        token_ids = _string_list(row.get("clobTokenIds")) or []
        normalized = [price / price_sum for price in prices]  # type: ignore[operator]
        entries = []
        for index, (outcome, price, probability) in enumerate(zip(outcomes, prices, normalized, strict=True)):
            entries.append(
                {
                    "outcome": outcome,
                    "price": _decimal_text(price),
                    "normalized_probability": _probability_text(probability),
                    "clob_token_id": token_ids[index] if index < len(token_ids) else None,
                }
            )

        events = row.get("events") if isinstance(row.get("events"), list) else []
        first_event = events[0] if events and isinstance(events[0], Mapping) else {}
        result.markets.append(
            {
                "venue": VENUE,
                "market_id": market_id or None,
                "slug": slug or None,
                "question": str(row.get("question") or "").strip() or None,
                "description": str(row.get("description") or "").strip() or None,
                "condition_id": str(row.get("conditionId") or "").strip() or None,
                "source_event_id": str(first_event.get("id") or first_event.get("ticker") or "").strip() or None,
                "game_id": str(row.get("gameId") or "").strip() or None,
                # Gamma's own classification: moneyline, spreads, totals. Kept
                # because a consumer comparing venues must not put a spread
                # market beside a moneyline, and the slug is a weaker guide.
                "sports_market_type": str(row.get("sportsMarketType") or "").strip() or None,
                "line": _decimal_text(_decimal_or_none(row.get("line"))),
                "outcomes": entries,
                "price_sum": _decimal_text(price_sum),
                # On an exchange the miss from one is the spread between resting
                # orders, not a bookmaker's margin, so no margin model is applied.
                "normalization": "multiplicative",
                "normalization_note": (
                    "Exchange prices are already probabilities; the deviation from one is "
                    "the book's spread, not bookmaker margin, so no de-vig model is applied."
                ),
                "best_bid": _decimal_text(_decimal_or_none(row.get("bestBid"))),
                "best_ask": _decimal_text(_decimal_or_none(row.get("bestAsk"))),
                "spread": _decimal_text(_decimal_or_none(row.get("spread"))),
                "last_trade_price": _decimal_text(_decimal_or_none(row.get("lastTradePrice"))),
                "volume": _decimal_text(_decimal_or_none(row.get("volumeNum") or row.get("volume"))),
                "liquidity": _decimal_text(_decimal_or_none(row.get("liquidityNum") or row.get("liquidity"))),
                "game_start_time": _timestamp(row.get("gameStartTime")),
                "start_date": _timestamp(row.get("startDate")),
                "end_date": _timestamp(row.get("endDate")),
                "source_updated_at": _timestamp(row.get("updatedAt")),
                "image_url": str(row.get("image") or "").strip() or None,
                "icon_url": str(row.get("icon") or "").strip() or None,
                "closed": closed,
                "active": bool(active) if active is not None else None,
                "api_fetched_at": api_fetched_at,
                "source_payload_ref": f"{VENUE}:{identifier}",
                "source_snapshot_hash": deterministic_hash(row),
                "parser_version": PARSER_VERSION,
            }
        )

    return result


def fetch_polymarket_markets(
    *,
    client: HttpClient | None = None,
    limit: int = 100,
    offset: int = 0,
    closed: bool = False,
    tag_id: str | None = None,
    order_by: str | None = None,
    url: str = MARKETS_ENDPOINT,
    timeout_seconds: int = 20,
) -> tuple[Any, str, int, str | None]:
    """One read-only request.

    Returns the payload, its fetch time, the status, and -- when the body did not
    come from a request that just succeeded -- the reason it did not.
    """
    http = client or HttpClient()
    parameters = [f"limit={int(limit)}", f"offset={int(offset)}", f"closed={'true' if closed else 'false'}"]
    if order_by:
        parameters.append(f"order={order_by}")
        parameters.append("ascending=false")
    if tag_id:
        parameters.append(f"tag_id={tag_id}")
    request_url = f"{url}?{'&'.join(parameters)}"
    response = http.get_text(request_url, timeout=timeout_seconds)
    status = int(getattr(response, "status", 200))
    fetched_at = str(getattr(response, "fetched_at", ""))
    not_live = non_live_response_reason(response)
    if status != 200:
        return None, fetched_at, status, not_live
    return response.json(), fetched_at, status, not_live


def probe_polymarket(
    *,
    client: HttpClient | None = None,
    limit: int = 25,
    url: str = MARKETS_ENDPOINT,
) -> dict[str, Any]:
    """Fetch once and report exactly what the live response did and did not carry.

    This exists because the module was written against documentation rather than
    against a response. It names the fields the normalizer depends on and whether
    each was present, so a shape change is a report rather than a stack trace.
    """
    universal_fields = (
        "id",
        "slug",
        "question",
        "conditionId",
        "outcomes",
        "outcomePrices",
        "clobTokenIds",
        "bestBid",
        "bestAsk",
        "spread",
        "lastTradePrice",
        "volumeNum",
        "liquidityNum",
        "startDate",
        "endDate",
        "closed",
        "active",
    )
    # Gamma only sets these on sports markets. Judging them against a sample of
    # politics and crypto questions reports a mapping break that does not exist,
    # and a readiness check that cries wolf gets ignored when it is right.
    sports_only_fields = ("gameStartTime", "sportsMarketType")
    try:
        payload, fetched_at, status, not_live = fetch_polymarket_markets(
            client=client or live_probe_client(), limit=limit, url=url, order_by="volume24hr"
        )
    except Exception as exc:  # noqa: BLE001 - the probe reports failures, it does not raise them
        return {
            "venue": VENUE,
            "reachable": False,
            "error": type(exc).__name__,
            "error_detail": str(exc)[:200],
            "source_url": url,
        }
    if payload is None:
        return {"venue": VENUE, "reachable": False, "http_status": status, "source_url": url}
    if not_live:
        # A cached or stale body is a record of the last time this worked, not
        # evidence that it works now, and a readiness check must not accept it.
        return {
            "venue": VENUE,
            "reachable": False,
            "http_status": status,
            "source_url": url,
            "error": "response_not_live",
            "error_detail": not_live,
        }

    rows = _market_rows(payload)
    sports_rows = [row for row in rows if row.get("sportsMarketType") not in (None, "")]
    present: dict[str, int] = {name: 0 for name in universal_fields + sports_only_fields}
    for row in rows:
        for name in universal_fields:
            if row.get(name) not in (None, ""):
                present[name] += 1
    for row in sports_rows:
        for name in sports_only_fields:
            if row.get(name) not in (None, ""):
                present[name] += 1

    missing = [name for name in universal_fields if present[name] == 0]
    if sports_rows:
        missing += [name for name in sports_only_fields if present[name] == 0]

    normalization = normalize_polymarket_markets(payload, api_fetched_at=fetched_at, source_url=url)
    sample = normalization.markets[0] if normalization.markets else None
    return {
        "venue": VENUE,
        "reachable": True,
        "http_status": status,
        "source_url": url,
        "api_fetched_at": fetched_at,
        "markets_in_response": len(rows),
        "sports_markets_in_response": len(sports_rows),
        "field_presence": present,
        "missing_everywhere": sorted(missing),
        "sports_fields_unjudged": not sports_rows,
        "normalized_market_count": len(normalization.markets),
        "rejection_reasons": normalization.rejection_counts(),
        "sample_market": sample,
    }


def render_polymarket_probe(report: Mapping[str, Any]) -> str:
    if not report.get("reachable"):
        detail = report.get("error_detail") or report.get("http_status")
        return (
            f"Polymarket probe: unreachable ({report.get('error') or 'http'} {detail}).\n"
            f"  url: {report.get('source_url')}"
        )
    lines = [
        "Polymarket probe",
        f"  url: {report.get('source_url')}",
        f"  markets in response: {report.get('markets_in_response')}"
        f" ({report.get('sports_markets_in_response', 0)} sports)",
        f"  normalized: {report.get('normalized_market_count')}",
    ]
    if report.get("sports_fields_unjudged"):
        lines.append("  no sports markets in this sample; sports-only fields were not judged")
    rejections = report.get("rejection_reasons") or {}
    if rejections:
        lines.append("  refused: " + ", ".join(f"{reason}={count}" for reason, count in sorted(rejections.items())))
    missing = report.get("missing_everywhere") or []
    if missing:
        lines.append("  fields absent from every market: " + ", ".join(missing))
        lines.append("  the normalizer expects these names; absence means the mapping needs updating")
    else:
        lines.append("  every expected field appeared on at least one market")
    return "\n".join(lines)


def cross_venue_gaps(
    polymarket_markets: Sequence[Mapping[str, Any]],
    reference_probabilities: Mapping[str, Decimal],
    *,
    outcome_name: str,
) -> list[dict[str, Any]]:
    """Signed probability gaps between this venue and a reference, where both priced it.

    Matching is by explicit key, supplied by the caller. Nothing here guesses
    which Polymarket market corresponds to which game: entity resolution across
    venues is backlog E-49 and doing it on a slug's text would produce confident
    comparisons of unrelated events.
    """
    gaps: list[dict[str, Any]] = []
    for market in polymarket_markets:
        reference = reference_probabilities.get(str(market.get("source_payload_ref") or ""))
        if reference is None:
            continue
        match = next(
            (entry for entry in market.get("outcomes") or [] if str(entry.get("outcome")) == outcome_name),
            None,
        )
        if match is None or match.get("normalized_probability") is None:
            continue
        venue_probability = Decimal(str(match["normalized_probability"]))
        gaps.append(
            {
                "market_id": market.get("market_id"),
                "slug": market.get("slug"),
                "outcome": outcome_name,
                "venue_probability": _probability_text(venue_probability),
                "reference_probability": _probability_text(reference),
                "gap_probability": _probability_text(venue_probability - reference),
            }
        )
    return gaps
