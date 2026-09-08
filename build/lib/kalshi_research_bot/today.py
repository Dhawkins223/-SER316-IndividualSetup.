from __future__ import annotations

import json
import math
import re
import urllib.error
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agents import DeepResearchBot, PublicIntelBot
from .combo_safety import (
    VERIFIED_COMBO_EVIDENCE,
    VERIFIED_COMBO_SOURCE,
    authoritative_combo_leg_rejection_reasons,
    authoritative_combo_slip_rejection_reasons,
    combo_leg_signature,
)
from .connectors.http import HttpClient
from .slip_analysis import (
    SlipLeg,
    UnmodellableSlip,
    correlation_matrix,
    simulate_correlation_adjustment,
)

if TYPE_CHECKING:
    from .evaluation.decision import ResearchDecisionPolicy
from .slip_safety import gate_slip_payload

# Sized from the standard error on the correlation *adjustment*, which is what
# the simulation is actually for -- the independent part is exact. At 4,000
# draws that error is around 0.0025 on an adjustment of 0.0067, so the sign
# moves between seeds; at 20,000 it is 0.0011 and does not. The uncorrelated
# case, which is the ordinary one for a cross-game combo, never reaches the
# simulator at all.
COMBO_JOINT_DRAWS = 20_000


ESPN_SCOREBOARDS = {
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={yyyymmdd}",
    "WNBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates={yyyymmdd}",
}

KALSHI_PUBLIC_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SPORTS_COMBO_WORDS = [
    "over",
    "under",
    "runs",
    "points",
    "goals",
    "touchdowns",
    "score",
    "wins by",
]
DEFAULT_MIN_LEG_PROBABILITY = 0.80
DEFAULT_LEVERAGE_MIN_LEG_PROBABILITY = 0.75
DEFAULT_ALL_DAY_MIN_LEG_PROBABILITY = 0.75
DEFAULT_ALL_DAY_MAX_LEG_PROBABILITY = 0.85
DEFAULT_RESEARCH_EDGE_MIN_PROBABILITY = 0.70
MONTH_NUMBERS = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}


def today_key(day: date | None = None) -> str:
    return (day or date.today()).strftime("%Y%m%d")


def date_key_from_ticker(*tickers: str) -> str | None:
    for ticker in tickers:
        match = re.search(r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})", (ticker or "").upper())
        if match:
            year, month_name, day = match.groups()
            return f"20{year}{MONTH_NUMBERS[month_name]}{day}"
    return None


def date_key_from_iso(value: str | None, timezone: str = "America/New_York") -> str | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone_utc())
    return timestamp.astimezone(local_timezone(timezone, timestamp)).strftime("%Y%m%d")


def timezone_utc() -> timezone:
    return timezone.utc


def local_timezone(timezone_name: str, timestamp: datetime) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name != "America/New_York":
            return timezone.utc
        month = timestamp.month
        day = timestamp.day
        is_dst = 4 <= month <= 10 or (month == 3 and day >= 8) or (month == 11 and day <= 7)
        return timezone(timedelta(hours=-4 if is_dst else -5))


def market_completion_date_keys(market: dict[str, Any]) -> set[str]:
    keys = {
        date_key_from_iso(str(market.get(field) or ""))
        for field in ["close_time", "expected_expiration_time", "expiration_time", "latest_expiration_time"]
    }
    keys.add(date_key_from_ticker(market.get("ticker", ""), market.get("event_ticker", "")))
    return {key for key in keys if key}


def overlap_key_from_ticker(ticker: str, sport: str = "") -> str | None:
    ticker = (ticker or "").upper()
    match = re.search(
        r"-(\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}\d{0,4}[A-Z]+)",
        ticker,
    )
    if match:
        return f"{sport}:{match.group(1)}".lower()
    return None


def normalize_matchup_text(value: str) -> str:
    text = (value or "").lower()
    for prefix in [
        "1st half of the ",
        "1st half of ",
        "first half of the ",
        "first half of ",
        "2nd half of the ",
        "2nd half of ",
        "second half of the ",
        "second half of ",
    ]:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    for separator in [
        " soccer tie ",
        " professional ",
        " total ",
        ": round",
        " in the ",
        " after ",
        "?",
    ]:
        if separator in text:
            text = text.split(separator, 1)[0]
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def overlap_key_for_leg(leg: dict[str, Any]) -> str:
    sport = leg.get("sport") or infer_sport(leg)
    ticker_key = overlap_key_from_ticker(leg.get("market_ticker", ""), sport)
    if ticker_key:
        return ticker_key
    for field in ["display_event", "rules", "title", "subtitle", "event_ticker"]:
        normalized = normalize_matchup_text(str(leg.get(field, "")))
        if " vs " in f" {normalized} " or " at " in f" {normalized} ":
            return f"{sport}:{normalized}".lower()
    return f"{sport}:{leg.get('event_ticker') or leg.get('market_ticker')}".lower()


def parse_espn_event(league: str, event: dict[str, Any]) -> dict[str, Any]:
    competition = (event.get("competitions") or [{}])[0]
    competitors = competition.get("competitors") or []
    home = next((item for item in competitors if item.get("homeAway") == "home"), {})
    away = next((item for item in competitors if item.get("homeAway") == "away"), {})
    home_team = home.get("team") or {}
    away_team = away.get("team") or {}
    return {
        "league": league,
        "event_id": event.get("id", ""),
        "name": event.get("name", ""),
        "short_name": event.get("shortName", ""),
        "start_time": event.get("date", ""),
        "status": ((event.get("status") or {}).get("type") or {}).get("description", ""),
        "home_team": home_team.get("displayName") or home_team.get("name", ""),
        "away_team": away_team.get("displayName") or away_team.get("name", ""),
        "home_abbrev": home_team.get("abbreviation", ""),
        "away_abbrev": away_team.get("abbreviation", ""),
        "venue": (competition.get("venue") or {}).get("fullName", ""),
    }


def fetch_espn_schedule_with_status(
    http: HttpClient,
    yyyymmdd: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    games: list[dict[str, Any]] = []
    endpoints: list[dict[str, Any]] = []
    for league, url_template in ESPN_SCOREBOARDS.items():
        source_url = url_template.format(yyyymmdd=yyyymmdd)
        try:
            response = http.get_text(source_url)
            payload = response.json()
        except (TimeoutError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            failure_reason = _public_source_failure_reason(exc)
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            endpoints.append(
                {
                    "league": league,
                    "source_url": source_url,
                    "status": "blocked" if isinstance(exc, urllib.error.HTTPError) else "failed",
                    "failure_reason": failure_reason,
                    "record_count": 0,
                }
            )
            continue
        parsed_games = [parse_espn_event(league, event) for event in payload.get("events", [])]
        for game in parsed_games:
            game.update(
                {
                    "source_name": "espn_scoreboard",
                    "source_url": source_url,
                    "api_fetched_at": response.fetched_at,
                    "source_snapshot_hash": response.content_hash,
                }
            )
        games.extend(parsed_games)
        endpoints.append(
            {
                "league": league,
                "source_url": source_url,
                "status": "stale" if response.stale else "fresh",
                "failure_reason": response.stale_reason if response.stale else "",
                "api_fetched_at": response.fetched_at,
                "source_snapshot_hash": response.content_hash,
                "record_count": len(parsed_games),
            }
        )
    successful = [item for item in endpoints if item["status"] == "fresh"]
    degraded = [item for item in endpoints if item["status"] != "fresh"]
    if successful and not degraded:
        state = "configured_healthy"
    elif successful:
        state = "configured_degraded"
    else:
        state = "unavailable_optional"
    return games, {
        "source_name": "espn_scoreboard",
        "required": False,
        "state": state,
        "record_count": len(games),
        "failure_reasons": [
            f"{item['league']}:{item['failure_reason']}"
            for item in degraded
            if item.get("failure_reason")
        ],
        "endpoints": endpoints,
    }


def _public_source_failure_reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, TimeoutError):
            return "timeout"
        return "source_unavailable"
    return "source_failed"


def cents_from_dollars(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return round(float(value) * 100.0, 2)
    except ValueError:
        return None


def probability_from_cents(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, value / 100.0))


def midpoint_cents(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None and ask > 0:
        return round((bid + ask) / 2.0, 2)
    if ask is not None and ask > 0:
        return ask
    if bid is not None and bid > 0:
        return bid
    return None


def split_market_title(title: str) -> list[str]:
    return [part.strip() for part in title.split(",") if part.strip()]


def display_event_from_rules(rules: str) -> str:
    if " wins the " in rules:
        tail = rules.split(" wins the ", 1)[1]
        if " professional" in tail:
            return " ".join(tail.split(" professional", 1)[0].split())
    markers = [
        " in the ",
        " by ",
    ]
    for marker in markers:
        if marker in rules:
            tail = rules.split(marker, 1)[1]
            for suffix in [
                " professional",
                " originally",
                " after ",
                " at ",
                ", then",
            ]:
                if suffix in tail:
                    tail = tail.split(suffix, 1)[0]
            if " vs " in tail or " at " in tail:
                return " ".join(tail.split())
    return ""


def side_quote_from_market(market: dict[str, Any], side: str) -> dict[str, Any]:
    side_key = side.lower()
    if side_key not in {"yes", "no"}:
        side_key = "yes"
    bid = cents_from_dollars(market.get(f"{side_key}_bid_dollars"))
    ask = cents_from_dollars(market.get(f"{side_key}_ask_dollars"))
    midpoint = midpoint_cents(bid, ask)
    return {
        "side": side_key,
        "bid_cents": bid,
        "ask_cents": ask,
        "midpoint_cents": midpoint,
        "market_implied_probability": probability_from_cents(midpoint),
    }


def fetch_market_by_ticker(http: HttpClient, ticker: str) -> dict[str, Any] | None:
    url = f"{KALSHI_PUBLIC_BASE_URL}/markets/{ticker}"
    try:
        response = http.get_text(url)
        market = response.json().get("market")
        if market:
            market["_api_fetched_at"] = response.fetched_at
            market["_source_url"] = response.url
            market["_source_snapshot_hash"] = response.content_hash
            market["_source_freshness_state"] = response.freshness_state
        return market
    except Exception:
        return None


def combo_joint_probability(priced_legs: list[dict[str, Any]]) -> dict[str, Any]:
    """Joint probability for a combo, from the copula.

    This replaces a flat 3%-per-repeated-event *penalty*, which had the sign
    backwards. For a combo where every leg must hit, positive correlation raises
    the joint probability: two standard normals both below their medians
    co-occur with probability ``1/4 + arcsin(rho)/(2*pi)``, which rises from
    0.2500 at rho=0 to 0.3155 at rho=0.40. The penalty moved the same pair down
    to 0.2425.

    Each entry needs ``probability``, ``event_ticker`` and ``market_ticker``,
    aligned -- a leg without a price cannot be included, because dropping its
    probability while keeping its event ticker (which the penalty tolerated,
    since it never looked at the two together) would correlate a leg that is not
    in the product.

    Kalshi's cross-game combos are built across distinct events, so in the
    ordinary case every modelled correlation is zero and the joint is exactly
    the product. That case is returned as the exact product rather than
    simulated, because a Monte Carlo there only adds noise to a known answer.

    Every branch returns the same keys, so a caller never has to know which one
    it took to read the result. ``correlation_adjustment_resolved`` is True when
    the adjustment is known well enough to act on -- which covers both an exact
    zero and a simulated value whose sign the draws established -- and False
    when the simulation could not separate it from zero.
    """

    def report(
        *,
        raw_probability: float | None,
        adjusted_probability: float | None,
        adjustment: float = 0.0,
        standard_error: float = 0.0,
        resolved: bool = True,
        basis: str,
        unmodellable_reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "raw_probability": None if raw_probability is None else round(raw_probability, 6),
            "adjusted_probability": (
                None if adjusted_probability is None else round(adjusted_probability, 6)
            ),
            "correlation_adjustment": round(adjustment, 6),
            "correlation_adjustment_standard_error": round(standard_error, 6),
            "correlation_adjustment_resolved": resolved,
            "joint_basis": basis,
            "unmodellable_reason": unmodellable_reason,
        }

    probabilities = [float(leg["probability"]) for leg in priced_legs]
    raw = product_probability(probabilities)
    if raw is None:
        return report(
            raw_probability=None,
            adjusted_probability=None,
            resolved=False,
            basis="no_priced_legs",
        )

    slip_legs: list[SlipLeg] = []
    for leg in priced_legs:
        probability = float(leg["probability"])
        if not 0.0 < probability < 1.0:
            # A leg quoted at 0 or 100 carries no uncertainty to correlate, and
            # the copula's normal quantile is infinite there. The product still
            # stands; only the adjustment is withheld.
            return report(
                raw_probability=raw,
                adjusted_probability=raw,
                basis="exact_product_leg_at_bound",
            )
        slip_legs.append(
            SlipLeg(
                leg_id=str(leg.get("market_ticker") or ""),
                selection=str(leg.get("side") or "yes"),
                # A binary contract trading at probability p pays 1, so its
                # decimal odds are exactly 1/p.
                decimal_odds=1.0 / probability,
                fair_probability=probability,
                event_id=str(leg.get("event_ticker") or ""),
                market=str(leg.get("market_ticker") or ""),
            )
        )

    matrix = correlation_matrix(slip_legs)
    size = len(matrix)
    if not any(matrix[i][j] != 0.0 for i in range(size) for j in range(i + 1, size)):
        return report(
            raw_probability=raw,
            adjusted_probability=raw,
            basis="exact_product_no_modelled_correlation",
        )

    try:
        simulation = simulate_correlation_adjustment(slip_legs, draws=COMBO_JOINT_DRAWS)
    except UnmodellableSlip as error:
        # Legs from one market of one event are mutually exclusive or
        # deterministically linked. Neither is a correlation, so no joint is
        # reported rather than a plausible-looking wrong one.
        return report(
            raw_probability=raw,
            adjusted_probability=None,
            resolved=False,
            basis="unmodellable",
            unmodellable_reason=str(error),
        )
    return report(
        raw_probability=raw,
        adjusted_probability=simulation["hit_probability"],
        adjustment=simulation["correlation_adjustment"],
        standard_error=simulation["adjustment_standard_error"],
        resolved=simulation["adjustment_resolved"],
        basis=(
            "copula_resolved" if simulation["adjustment_resolved"] else "copula_unresolved"
        ),
    )


def product_probability(values: list[float]) -> float | None:
    if not values:
        return None
    probability = 1.0
    for value in values:
        probability *= value
    return max(0.0, min(1.0, probability))


def infer_sport(leg: dict[str, Any]) -> str:
    ticker = (leg.get("market_ticker") or "").upper()
    text = f"{leg.get('title', '')} {leg.get('subtitle', '')} {leg.get('rules', '')} {ticker}".lower()
    if "KXMLB" in ticker or "runs scored" in text:
        return "Pro Baseball"
    if "KXWNBA" in ticker or "points scored" in text or "women's" in text:
        return "Pro Basketball (W)"
    if "KXWC" in ticker or "reg time" in text or "goals scored" in text or "advances" in text:
        return "World Soccer Cup"
    if "tennis" in text or "wimbledon" in text or "KXATP" in ticker or "KXWTA" in ticker or "MATCH" in ticker or "CHALLENGER" in ticker:
        return "Tennis"
    return "Sports"


def infer_market_category(market: dict[str, Any]) -> str:
    ticker = (market.get("ticker") or market.get("event_ticker") or "").upper()
    text = " ".join(
        str(market.get(field, ""))
        for field in ["ticker", "event_ticker", "title", "category", "category_slug", "rules_primary"]
    ).lower()
    raw_category = str(market.get("category") or market.get("category_slug") or "").strip()
    if any(word in text for word in ["bitcoin", "btc", "ethereum", "eth", "crypto", "xrp", "solana", "doge", "litecoin", "bnb", "near"]) or ticker.startswith(("KXBTC", "KXETH", "KXCRYPTO", "KXXRP", "KXSOL", "KXDOGE", "KXBNB", "KXNEAR")):
        return "Crypto"
    if any(word in text for word in ["esports", "gaming", "map 1", "map 2", "map 3"]):
        return "Esports"
    if ticker.startswith(("KXMLB", "KXNBA", "KXWNBA", "KXNFL", "KXWC", "KXATP", "KXWTA", "KXITF", "KXPGA", "KXGOLF")):
        return "Sports"
    if any(word in text for word in ["temperature", "rain", "weather", "hurricane", "snow"]):
        return "Weather"
    if any(word in text for word in ["election", "president", "senate", "congress", "mayor", "trump", "donald"]):
        return "Politics"
    if any(word in text for word in ["s&p", "nasdaq", "dow", "stock", "treasury", "fed", "rate cut"]):
        return "Markets"
    if any(word in text for word in ["fox", "tv", "stream", "podcast", "movie", "song", "album", "corden"]):
        return "Media"
    if raw_category:
        normalized = raw_category.replace("_", " ").replace("-", " ").title()
        if normalized.lower() not in {"Other", "All"}:
            return normalized
    return "Kalshi"


def combo_category_for_leg(leg: dict[str, Any]) -> str:
    category = str(leg.get("combo_category") or leg.get("category") or "").strip()
    if category:
        return category
    inferred_category = infer_market_category(
        {
            "ticker": leg.get("market_ticker"),
            "event_ticker": leg.get("event_ticker"),
            "title": leg.get("title") or leg.get("display_event"),
            "rules_primary": leg.get("rules"),
        }
    )
    if inferred_category != "Kalshi":
        return inferred_category
    sport = str(leg.get("sport") or infer_sport(leg) or "").strip()
    if sport in {
        "Pro Baseball",
        "Pro Basketball (W)",
        "World Soccer Cup",
        "Tennis",
        "Sports",
        "MLB",
        "WNBA",
        "Soccer",
    }:
        return "Sports"
    return inferred_category


def is_supported_slip_leg(leg: dict[str, Any]) -> bool:
    subtitle = (leg.get("subtitle") or "").lower()
    title = (leg.get("title") or "").lower()
    ticker = (leg.get("market_ticker") or "").upper()
    text = f"{subtitle} {title}"
    if "KXMLBHR" in ticker:
        return False
    if ":" in subtitle and not subtitle.startswith(("reg time:", "goal diff reg time:")) and "over" not in subtitle:
        return False
    return any(
        phrase in text
        for phrase in [
            "over",
            "under",
            "points scored",
            "runs scored",
            "goals scored",
            "advances",
            "wins by",
        ]
    ) or "MATCH" in ticker


def selection_text_for_leg(leg: dict[str, Any]) -> str:
    return str(leg.get("subtitle") or leg.get("title") or leg.get("display_event") or leg.get("market_ticker") or "")


def combo_rejection_reasons_for_leg(leg: dict[str, Any], *, require_supported_market: bool = False) -> list[str]:
    reasons = authoritative_combo_leg_rejection_reasons(leg)
    side = str(leg.get("side") or "").lower()
    status = str(leg.get("status") or "").lower()
    if not leg.get("market_ticker"):
        reasons.append("missing_market_ticker")
    if side not in {"yes", "no"}:
        reasons.append("invalid_side")
    if leg.get("ask_cents") is None:
        reasons.append("missing_live_ask")
    if status not in {"active", "open"}:
        reasons.append("market_not_open_or_active")
    if not selection_text_for_leg(leg):
        reasons.append("missing_selection_text")
    if require_supported_market and not is_supported_slip_leg(leg):
        reasons.append("unsupported_market_type")
    for flag in leg.get("risk_flags") or []:
        reasons.append(f"risk_flag:{flag}")
    return sorted(set(reasons))


def manual_entry_warnings_for_leg(leg: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not (leg.get("market_close_time") or leg.get("close_time")):
        warnings.append("missing_market_close_time")
    if not leg.get("api_fetched_at"):
        warnings.append("missing_api_fetched_at")
    if leg.get("bid_cents") is None:
        warnings.append("missing_live_bid")
    if not (leg.get("event_start_time") or leg.get("expected_expiration_time") or leg.get("expiration_time")):
        warnings.append("missing_event_start_or_expiration_time")
    return sorted(set(warnings))


def annotate_combo_leg(leg: dict[str, Any], *, require_supported_market: bool = False) -> dict[str, Any]:
    annotated = dict(leg)
    category = combo_category_for_leg(annotated)
    overlap_key = annotated.get("overlap_key") or overlap_key_for_leg(annotated)
    rejection_reasons = combo_rejection_reasons_for_leg(
        annotated,
        require_supported_market=require_supported_market,
    )
    warnings = manual_entry_warnings_for_leg(annotated)
    selection = selection_text_for_leg(annotated)
    annotated["combo_category"] = category
    annotated["category"] = category
    annotated["overlap_key"] = overlap_key
    annotated["combo_eligible"] = not rejection_reasons
    annotated["combo_rejection_reasons"] = rejection_reasons
    annotated["manual_entry_warnings"] = warnings
    annotated["manual_entry_ready"] = not rejection_reasons and not warnings
    annotated["manual_entry"] = {
        "market_ticker": annotated.get("market_ticker"),
        "event_ticker": annotated.get("event_ticker"),
        "side": str(annotated.get("side") or "").upper(),
        "selection": selection,
        "display_event": annotated.get("display_event") or annotated.get("event_ticker"),
        "category": category,
        "status": annotated.get("status"),
        "bid_cents": annotated.get("bid_cents"),
        "ask_cents": annotated.get("ask_cents"),
        "market_close_time": annotated.get("market_close_time") or annotated.get("close_time"),
        "event_start_time": annotated.get("event_start_time"),
        "api_fetched_at": annotated.get("api_fetched_at"),
        "market_updated_at": annotated.get("market_updated_at") or annotated.get("source_updated_at"),
        "overlap_key": overlap_key,
    }
    annotated["combo_compatibility"] = {
        "eligible": not rejection_reasons,
        "manual_entry_ready": annotated["manual_entry_ready"],
        "category": category,
        "overlap_key": overlap_key,
        "rejection_reasons": rejection_reasons,
        "warnings": warnings,
    }
    return annotated


def combo_compatibility_summary(legs: list[dict[str, Any]]) -> dict[str, Any]:
    category_counts: dict[str, int] = {}
    overlap_counts: dict[str, int] = {}
    market_side_counts: dict[tuple[str, str], int] = {}
    rejection_reasons: list[str] = []
    warning_reasons: list[str] = []
    rejection_reasons.extend(authoritative_combo_slip_rejection_reasons(legs))
    for leg in legs:
        category = str(leg.get("combo_category") or combo_category_for_leg(leg))
        category_counts[category] = category_counts.get(category, 0) + 1
        overlap_key = str(leg.get("overlap_key") or overlap_key_for_leg(leg))
        overlap_counts[overlap_key] = overlap_counts.get(overlap_key, 0) + 1
        market_side = (str(leg.get("market_ticker") or ""), str(leg.get("side") or "").lower())
        market_side_counts[market_side] = market_side_counts.get(market_side, 0) + 1
        rejection_reasons.extend(leg.get("combo_rejection_reasons") or [])
        warning_reasons.extend(leg.get("manual_entry_warnings") or [])
    duplicate_overlap_count = sum(1 for count in overlap_counts.values() if count > 1)
    duplicate_market_side_count = sum(1 for count in market_side_counts.values() if count > 1)
    if duplicate_overlap_count:
        rejection_reasons.append("duplicate_event_family")
    if duplicate_market_side_count:
        rejection_reasons.append("duplicate_market_side")
    compatible = not rejection_reasons
    combo_market_tickers = sorted({str(leg.get("combo_market_ticker") or "") for leg in legs if leg.get("combo_market_ticker")})
    return {
        "status": "compatible" if compatible else "blocked",
        "manual_entry_ready": compatible and not warning_reasons,
        "can_mix_categories": compatible and len(category_counts) > 1,
        "exact_listed_combo": compatible and len(combo_market_tickers) == 1,
        "authoritative_evidence": VERIFIED_COMBO_EVIDENCE if compatible else None,
        "listed_combo_market_ticker": combo_market_tickers[0] if len(combo_market_tickers) == 1 else None,
        "category_policy": (
            "Categories may mix only when every displayed leg is the exact selected-leg set of one current, active, "
            "quoted Kalshi KXMVE market. Unknown or synthetic combinations are blocked."
        ),
        "categories": sorted(category_counts),
        "category_counts": dict(sorted(category_counts.items())),
        "duplicate_overlap_count": duplicate_overlap_count,
        "duplicate_market_side_count": duplicate_market_side_count,
        "blocked_leg_count": sum(1 for leg in legs if leg.get("combo_eligible") is not True),
        "warning_count": len(warning_reasons),
        "rejection_reasons": sorted(set(rejection_reasons)),
        "warnings": sorted(set(warning_reasons)),
    }


def total_line_from_text(value: str) -> float | None:
    match = re.search(r"over\s+(\d+(?:\.\d+)?)", (value or "").lower())
    if not match:
        return None
    return float(match.group(1))


def is_colorado_mlb_game(leg: dict[str, Any]) -> bool:
    text = " ".join(
        str(leg.get(field, ""))
        for field in ["market_ticker", "event_ticker", "display_event", "title", "subtitle", "rules"]
    ).lower()
    return "colorado" in text or "miacol" in text or "col" in (leg.get("market_ticker") or "").lower()


def is_mlb_total_runs_leg(leg: dict[str, Any]) -> bool:
    sport = leg.get("sport") or infer_sport(leg)
    subtitle = (leg.get("subtitle") or leg.get("title") or "").lower()
    return sport == "Pro Baseball" and "runs scored" in subtitle and total_line_from_text(subtitle) is not None


def is_high_scoring_total_environment(leg: dict[str, Any]) -> bool:
    if not is_mlb_total_runs_leg(leg):
        return False
    line = total_line_from_text(leg.get("subtitle") or leg.get("title") or "")
    return bool(line is not None and line >= 10.5) or is_colorado_mlb_game(leg)


def leg_risk_flags(leg: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    sport = leg.get("sport") or infer_sport(leg)
    subtitle = leg.get("subtitle") or leg.get("title") or ""
    line = total_line_from_text(subtitle)
    side = (leg.get("side") or "").lower()
    if sport == "Pro Baseball" and line is not None and "runs scored" in subtitle.lower():
        if side == "no" and is_high_scoring_total_environment(leg):
            flags.append("high_scoring_mlb_total_under_blocked")
        if side == "no" and line >= 14.5:
            flags.append("extreme_mlb_total_under_tail_risk")
        if side == "no" and line >= 12.5 and is_colorado_mlb_game(leg):
            flags.append("coors_or_colorado_total_under_tail_risk")
    return flags


def leg_warning_flags(leg: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if is_mlb_total_runs_leg(leg) and is_colorado_mlb_game(leg):
        flags.append("colorado_run_environment_review")
    if is_high_scoring_total_environment(leg):
        flags.append("high_scoring_total_environment")
    return flags


def required_leg_probability(leg: dict[str, Any], base_probability: float) -> float:
    if not is_mlb_total_runs_leg(leg):
        return base_probability
    side = (leg.get("side") or "").lower()
    if side != "yes":
        return base_probability
    line = total_line_from_text(leg.get("subtitle") or leg.get("title") or "") or 0.0
    if is_colorado_mlb_game(leg) or line >= 12.5:
        return max(base_probability, 0.90)
    if is_high_scoring_total_environment(leg):
        return max(base_probability, 0.85)
    return base_probability


def numeric_text(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value not in {None, ""} else default)
    except (TypeError, ValueError):
        return default


def leg_spread_cents(leg: dict[str, Any]) -> float:
    bid = leg.get("bid_cents")
    ask = leg.get("ask_cents")
    if bid is None or ask is None:
        return 100.0
    return max(0.0, float(ask) - float(bid))


def exact_bet_score(leg: dict[str, Any]) -> float:
    probability_score = float(leg.get("probability") or leg.get("market_implied_probability") or 0) * 100.0
    spread_penalty = leg_spread_cents(leg) * 0.35
    liquidity = numeric_text(leg.get("open_interest")) + numeric_text(leg.get("volume_24h"))
    liquidity_bonus = min(8.0, liquidity / 250.0)
    subtitle = (leg.get("subtitle") or "").lower()
    market_penalty = 1.5 if "wins by" in subtitle else 0.0
    return round(max(0.0, min(100.0, probability_score - spread_penalty + liquidity_bonus - market_penalty)), 2)


def slip_adjusted_probability(legs: list[dict[str, Any]]) -> dict[str, Any]:
    """Raw and correlation-adjusted probability for a slip's legs.

    Returns the full ``combo_joint_probability`` report rather than a tuple,
    because the basis and any unmodellable reason have to travel with the
    numbers -- a caller that sees only the adjusted figure cannot tell an exact
    product from a simulated one, or a withheld joint from a low one.
    """

    return combo_joint_probability(
        [
            {
                "probability": leg["probability"],
                "event_ticker": leg.get("event_ticker", ""),
                "market_ticker": leg.get("market_ticker", ""),
                "side": leg.get("side", "yes"),
            }
            for leg in legs
        ]
    )


def slip_summary(legs: list[dict[str, Any]], min_leg_probability: float, stake_dollars: float) -> dict[str, Any]:
    from .evaluation.quality import confidence_guardrail

    legs = [annotate_combo_leg(leg) for leg in legs]
    leg_guardrails = [
        confidence_guardrail(
            probability=float(leg.get("probability") or 0),
            evidence_count=int(leg.get("evidence_count") or 0),
            source_backed=leg.get("research_mode") == "source_backed",
            margin_of_error=leg.get("margin_of_error"),
            spread_cents=leg.get("spread_cents"),
        )
        for leg in legs
    ]
    high_confidence_allowed = bool(leg_guardrails) and all(item["high_confidence_allowed"] for item in leg_guardrails)
    compatibility = combo_compatibility_summary(legs)
    if compatibility["status"] != "compatible":
        return {
            "action": "NO_SLIP",
            "reason": "The selected legs are not the exact verified leg set of one active, quoted Kalshi combo market.",
            "min_leg_probability": min_leg_probability,
            "eligible_leg_count": 0,
            "excluded_combo_leg_count": len(legs),
            "combo_compatibility": compatibility,
            "manual_entry_ready": False,
            "leg_count": 0,
            "legs": [],
        }
    joint = slip_adjusted_probability(legs)
    if joint["adjusted_probability"] is None:
        # No joint probability could be computed. Reported as its own refusal
        # rather than folded into a zero, which would read as "this slip cannot
        # win" instead of "this slip cannot be priced".
        return {
            "action": "NO_SLIP",
            "reason": joint["unmodellable_reason"]
            or "No joint probability could be computed for the selected legs.",
            "min_leg_probability": min_leg_probability,
            "eligible_leg_count": 0,
            "excluded_combo_leg_count": len(legs),
            "combo_compatibility": compatibility,
            "joint_basis": joint["joint_basis"],
            "manual_entry_ready": False,
            "leg_count": 0,
            "legs": [],
        }
    raw = joint["raw_probability"]
    adjusted = joint["adjusted_probability"]
    estimated_cost_cents = round(adjusted * 100.0, 2)
    estimated_payout = round(stake_dollars / adjusted, 2) if adjusted > 0 else 0.0
    return {
        "action": "BUILD_SLIP",
        "min_leg_probability": min_leg_probability,
        "raw_probability": round(raw, 6),
        "adjusted_probability": round(adjusted, 6),
        "correlation_adjustment": joint["correlation_adjustment"],
        "correlation_adjustment_standard_error": joint["correlation_adjustment_standard_error"],
        "correlation_adjustment_resolved": joint["correlation_adjustment_resolved"],
        "joint_basis": joint["joint_basis"],
        "estimated_combo_price_cents": estimated_cost_cents,
        "stake_dollars": stake_dollars,
        "estimated_payout_if_right": estimated_payout,
        "estimated_profit_if_right": round(estimated_payout - stake_dollars, 2),
        "leg_count": len(legs),
        "sports": sorted({leg["sport"] for leg in legs}),
        "combo_categories": compatibility["categories"],
        "category_counts": compatibility["category_counts"],
        "unique_matchup_count": len({leg.get("overlap_key") for leg in legs}),
        "overlap_safe": len({leg.get("overlap_key") for leg in legs}) == len(legs),
        "overlap_policy": "one normalized matchup per combo slip",
        "combo_compatibility": compatibility,
        "manual_entry_ready": compatibility["manual_entry_ready"],
        "confidence_label": "high_confidence" if high_confidence_allowed else "price_implied",
        "high_confidence_allowed": high_confidence_allowed,
        "confidence_guardrail_reasons": sorted({reason for item in leg_guardrails for reason in item.get("reasons", [])}),
        "legs": legs,
        "note": "Exact active Kalshi combo market. No legs were synthesized across unrelated combo markets.",
    }


def verified_combo_market_legs(
    market: dict[str, Any],
    *,
    min_leg_probability: float,
    max_leg_probability: float,
    yyyymmdd: str | None,
    require_supported_market: bool,
    intel_by_overlap_key: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    ticker = str(market.get("ticker") or "")
    status = str(market.get("status") or "").lower()
    raw_legs = list(market.get("leg_details") or [])
    if not ticker.startswith("KXMVE") or status not in {"active", "open"} or not market.get("real_data_ready"):
        return []
    if not market_is_tradable(market) or not raw_legs:
        return []
    expected_signature = combo_leg_signature(raw_legs)
    selected: list[dict[str, Any]] = []
    overlap_keys: set[str] = set()
    intel_by_overlap_key = intel_by_overlap_key or {}
    for leg in raw_legs:
        probability = leg.get("market_implied_probability")
        if probability is None or not 0 < float(probability) < 1:
            return []
        category = combo_category_for_leg(leg)
        sport = infer_sport(leg) if category == "Sports" else category
        event_date = (
            leg.get("event_date")
            or date_key_from_ticker(leg.get("market_ticker", ""), leg.get("event_ticker", ""))
            or date_key_from_iso(leg.get("event_start_time"))
        )
        candidate = {
            **leg,
            "sport": sport,
            "probability": float(probability),
            "spread_cents": leg_spread_cents(leg),
            "open_interest_value": numeric_text(leg.get("open_interest")),
            "volume_24h_value": numeric_text(leg.get("volume_24h")),
            "event_date": event_date,
        }
        candidate["overlap_key"] = overlap_key_for_leg(candidate)
        candidate["risk_flags"] = leg_risk_flags(candidate)
        candidate["warning_flags"] = leg_warning_flags(candidate)
        candidate["required_probability"] = required_leg_probability(candidate, min_leg_probability)
        candidate["public_intel_score"] = round(float(intel_by_overlap_key.get(candidate["overlap_key"], 0.0)), 2)
        candidate["exact_bet_score"] = round(
            min(100.0, exact_bet_score(candidate) + candidate["public_intel_score"]),
            2,
        )
        candidate = annotate_combo_leg(candidate, require_supported_market=require_supported_market)
        if candidate.get("combo_market_ticker") != ticker:
            return []
        if candidate.get("combo_market_leg_signature") != expected_signature:
            return []
        if not candidate["combo_eligible"] or candidate["risk_flags"]:
            return []
        if not candidate["required_probability"] <= candidate["probability"] <= max_leg_probability:
            return []
        if yyyymmdd is not None and event_date != yyyymmdd:
            return []
        if candidate["spread_cents"] > 25:
            return []
        if candidate["overlap_key"] in overlap_keys:
            return []
        overlap_keys.add(candidate["overlap_key"])
        selected.append(candidate)
    if authoritative_combo_slip_rejection_reasons(selected):
        return []
    return selected


def listed_combo_slip(
    market: dict[str, Any],
    legs: list[dict[str, Any]],
    *,
    min_leg_probability: float,
    max_leg_probability: float,
    stake_dollars: float,
) -> dict[str, Any]:
    slip = slip_summary(legs, min_leg_probability, stake_dollars)
    if slip.get("action") != "BUILD_SLIP":
        return slip
    combo_ask = float(market["yes_ask_cents"])
    combo_probability = combo_ask / 100.0
    estimated_payout = round(stake_dollars / combo_probability, 2)
    slip.update(
        {
            "max_leg_probability": max_leg_probability,
            "listed_combo_market_ticker": market.get("ticker"),
            "listed_combo_event_ticker": market.get("event_ticker"),
            "listed_combo_side": "YES",
            "listed_combo_yes_bid_cents": market.get("yes_bid_cents"),
            "listed_combo_yes_ask_cents": combo_ask,
            "listed_combo_status": market.get("status"),
            "listed_combo_fetched_at": market.get("api_fetched_at"),
            "listed_combo_snapshot_hash": market.get("source_snapshot_hash"),
            "combo_price_source": "live_kalshi_mve_yes_ask",
            "probability_source": "market_implied",
            "model_state": "baseline_only",
            "decision_status": "track_only",
            "execution_allowed": False,
            "execution_cost_status": "unavailable",
            "net_roi_status": "unavailable_without_fee_and_slippage_model",
            "estimated_combo_price_cents": combo_ask,
            "estimated_payout_if_right": estimated_payout,
            "estimated_gross_profit_if_right_before_costs": round(estimated_payout - stake_dollars, 2),
            "estimated_profit_if_right": round(estimated_payout - stake_dollars, 2),
        }
    )
    return slip


def build_custom_slip(
    markets: list[dict[str, Any]],
    target_probability: float = DEFAULT_MIN_LEG_PROBABILITY,
    min_leg_probability: float | None = None,
    max_leg_probability: float = 0.985,
    min_legs: int = 3,
    max_legs: int = 20,
    stake_dollars: float = 5.0,
    yyyymmdd: str | None = None,
    intel_by_overlap_key: dict[str, float] | None = None,
) -> dict[str, Any]:
    min_leg_probability = target_probability if min_leg_probability is None else min_leg_probability
    candidates: list[dict[str, Any]] = []
    for market in markets:
        legs = verified_combo_market_legs(
            market,
            min_leg_probability=min_leg_probability,
            max_leg_probability=max_leg_probability,
            yyyymmdd=yyyymmdd,
            require_supported_market=True,
            intel_by_overlap_key=intel_by_overlap_key,
        )
        if not min_legs <= len(legs) <= max_legs:
            continue
        slip = listed_combo_slip(
            market,
            legs,
            min_leg_probability=min_leg_probability,
            max_leg_probability=max_leg_probability,
            stake_dollars=stake_dollars,
        )
        if slip.get("action") == "BUILD_SLIP":
            candidates.append(slip)
    if not candidates:
        return {
            "action": "NO_SLIP",
            "reason": "No exact active Kalshi combo market matched the leg, date, price, and safety filters.",
            "min_leg_probability": min_leg_probability,
            "eligible_leg_count": 0,
            "eligible_combo_count": 0,
            "legs": [],
        }
    best = min(
        candidates,
        key=lambda slip: (
            abs(min(float(leg["probability"]) for leg in slip["legs"]) - min_leg_probability),
            sum(float(leg["spread_cents"]) for leg in slip["legs"]) / len(slip["legs"]),
            -sum(float(leg["exact_bet_score"]) for leg in slip["legs"]) / len(slip["legs"]),
        ),
    )
    best["eligible_leg_count"] = sum(candidate["leg_count"] for candidate in candidates)
    best["eligible_combo_count"] = len(candidates)
    best["skipped_overlap_count"] = 0
    return best


def fetch_kalshi_same_day_markets(
    http: HttpClient,
    yyyymmdd: str,
    limit: int = 500,
    max_pages: int = 16,
) -> list[dict[str, Any]]:
    raw_markets: dict[str, dict[str, Any]] = {}
    cursor = ""
    for _ in range(max_pages):
        request = {"limit": limit, "status": "open", "mve_filter": "exclude"}
        if cursor:
            request["cursor"] = cursor
        url = f"{KALSHI_PUBLIC_BASE_URL}/markets?{urlencode(request)}"
        response = http.get_text(url)
        payload = response.json()
        for market in payload.get("markets", []):
            ticker = market.get("ticker", "")
            if ticker and yyyymmdd in market_completion_date_keys(market):
                market["_api_fetched_at"] = response.fetched_at
                market["_source_url"] = response.url
                market["_source_snapshot_hash"] = response.content_hash
                market["_source_freshness_state"] = response.freshness_state
                raw_markets[ticker] = market
        cursor = payload.get("cursor") or ""
        if not cursor:
            break
    return list(raw_markets.values())


def all_day_candidate_legs(
    markets: list[dict[str, Any]],
    yyyymmdd: str,
    min_leg_probability: float = DEFAULT_ALL_DAY_MIN_LEG_PROBABILITY,
    max_leg_probability: float = DEFAULT_ALL_DAY_MAX_LEG_PROBABILITY,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for market in markets:
        legs = verified_combo_market_legs(
            market,
            min_leg_probability=min_leg_probability,
            max_leg_probability=max_leg_probability,
            yyyymmdd=yyyymmdd,
            require_supported_market=False,
        )
        if legs:
            candidates.extend(legs)
    return candidates


def build_all_day_slip(
    markets: list[dict[str, Any]],
    yyyymmdd: str,
    min_leg_probability: float = DEFAULT_ALL_DAY_MIN_LEG_PROBABILITY,
    max_leg_probability: float = DEFAULT_ALL_DAY_MAX_LEG_PROBABILITY,
    min_legs: int = 8,
    max_legs: int = 24,
    stake_dollars: float = 5.0,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for market in markets:
        legs = verified_combo_market_legs(
            market,
            min_leg_probability=min_leg_probability,
            max_leg_probability=max_leg_probability,
            yyyymmdd=yyyymmdd,
            require_supported_market=False,
        )
        if not min_legs <= len(legs) <= max_legs:
            continue
        slip = listed_combo_slip(
            market,
            legs,
            min_leg_probability=min_leg_probability,
            max_leg_probability=max_leg_probability,
            stake_dollars=stake_dollars,
        )
        if slip.get("action") == "BUILD_SLIP":
            candidates.append(slip)
    if not candidates:
        return {
            "action": "NO_SLIP",
            "reason": "No exact listed same-day Kalshi combo matched the 75-85% leg range and safety rules.",
            "min_leg_probability": min_leg_probability,
            "max_leg_probability": max_leg_probability,
            "eligible_leg_count": 0,
            "eligible_combo_count": 0,
            "legs": [],
        }
    best = min(
        candidates,
        key=lambda slip: (
            sum(float(leg["spread_cents"]) for leg in slip["legs"]) / len(slip["legs"]),
            abs(min(float(leg["probability"]) for leg in slip["legs"]) - min_leg_probability),
            -sum(float(leg["exact_bet_score"]) for leg in slip["legs"]) / len(slip["legs"]),
        ),
    )
    best["max_leg_probability"] = max_leg_probability
    best["eligible_leg_count"] = sum(candidate["leg_count"] for candidate in candidates)
    best["eligible_combo_count"] = len(candidates)
    best["skipped_overlap_count"] = 0
    best["note"] = "Exact listed same-day Kalshi combo. Cross-category legs appear only when Kalshi lists them together in this KXMVE market."
    return best

def clamp_probability(value: float, low: float = 0.01, high: float = 0.99) -> float:
    return max(low, min(high, value))


def signal_is_public(signal: dict[str, Any]) -> bool:
    return bool(signal.get("is_public", True)) and bool(signal.get("url") or signal.get("source_url")) and not signal.get("contains_private_info", False)


def signal_source_type_bonus(signal: dict[str, Any]) -> float:
    source_type = str(signal.get("source_type") or signal.get("type") or "").lower().replace("-", "_")
    if source_type in {"primary_source", "official_data", "official_report", "peer_reviewed"}:
        return 1.0
    if source_type in {"expert", "industry_expert", "trusted_bettor", "analyst"}:
        return 0.82
    if source_type in {"news", "public_bettor", "social", "prediction_market"}:
        return 0.62
    return 0.45


def wilson_lower_bound(wins: float, total: float, z_score: float = 1.28) -> float:
    if total <= 0:
        return 0.50
    proportion = wins / total
    denominator = 1.0 + z_score * z_score / total
    center = proportion + z_score * z_score / (2.0 * total)
    margin = z_score * math.sqrt((proportion * (1.0 - proportion) + z_score * z_score / (4.0 * total)) / total)
    return clamp_probability((center - margin) / denominator)


def signal_probability(signal: dict[str, Any]) -> float:
    for field in ["model_probability", "probability", "estimated_probability"]:
        if signal.get(field) is not None:
            value = numeric_text(signal.get(field))
            return clamp_probability(value / 100.0 if value > 1 else value)
    confidence = numeric_text(signal.get("confidence"), 0.5)
    if confidence > 1:
        confidence /= 100.0
    wins = numeric_text(signal.get("historical_wins") or signal.get("wins"))
    total = numeric_text(signal.get("historical_total") or signal.get("sample_size"))
    if total > 0:
        return clamp_probability((clamp_probability(confidence) * 0.45) + (wilson_lower_bound(wins, total) * 0.55))
    return clamp_probability(confidence)


def signal_quality_score(signal: dict[str, Any]) -> float:
    if not signal_is_public(signal):
        return 0.0
    wins = numeric_text(signal.get("historical_wins") or signal.get("wins"))
    total = numeric_text(signal.get("historical_total") or signal.get("sample_size"))
    track_record = wilson_lower_bound(wins, total)
    confidence = signal_probability(signal)
    sample_depth = min(1.0, math.sqrt(total) / 18.0) if total > 0 else 0.12
    declared_quality = numeric_text(signal.get("source_quality"), 0.6)
    if declared_quality > 1:
        declared_quality /= 100.0
    source_type = signal_source_type_bonus(signal)
    score = (
        track_record * 0.28
        + confidence * 0.26
        + clamp_probability(declared_quality, 0.0, 1.0) * 0.18
        + sample_depth * 0.14
        + source_type * 0.14
    )
    return round(score * 100.0, 2)


def token_match_score(hint: str, text: str) -> float:
    tokens = [token for token in re.sub(r"[^a-z0-9]+", " ", (hint or "").lower()).split() if len(token) >= 3]
    if not tokens:
        return 0.0
    haystack = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return sum(1 for token in tokens if token in haystack) / len(tokens)


def research_leg_text(leg: dict[str, Any]) -> str:
    return " ".join(
        str(leg.get(field, ""))
        for field in ["market_ticker", "event_ticker", "display_event", "title", "subtitle", "rules", "side"]
    )


def research_signal_matches(leg: dict[str, Any], signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leg_text = research_leg_text(leg)
    matches: list[dict[str, Any]] = []
    for signal in signals:
        if not signal_is_public(signal):
            continue
        market_score = token_match_score(str(signal.get("market_hint") or signal.get("event_hint") or ""), leg_text)
        selection_score = token_match_score(str(signal.get("selection_hint") or ""), leg_text)
        if market_score < 0.42 and selection_score < 0.42:
            continue
        if signal.get("selection_hint") and selection_score < 0.34:
            continue
        quality = signal_quality_score(signal)
        if quality <= 0:
            continue
        matches.append(
            {
                "source": signal.get("source", "unknown"),
                "platform": signal.get("platform", "unknown"),
                "url": signal.get("url") or signal.get("source_url", ""),
                "source_type": signal.get("source_type") or signal.get("type") or "public",
                "quality_score": quality,
                "signal_probability": round(signal_probability(signal), 4),
                "match_score": round(max(market_score, selection_score), 4),
            }
        )
    return sorted(matches, key=lambda item: (item["quality_score"], item["match_score"]), reverse=True)


def research_probability_for_leg(leg: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    weights = [max(0.01, (match["quality_score"] / 100.0) * match["match_score"]) for match in matches]
    total_weight = sum(weights)
    external_probability = sum(match["signal_probability"] * weight for match, weight in zip(matches, weights)) / total_weight
    kalshi_probability = float(leg.get("probability") or 0.5)
    source_count = len(matches)
    disagreement = 0.0
    if source_count >= 2:
        mean = external_probability
        disagreement = math.sqrt(sum((match["signal_probability"] - mean) ** 2 for match in matches) / source_count)
    spread_penalty = min(0.10, leg_spread_cents(leg) / 100.0 * 0.30)
    source_penalty = 0.08 if source_count == 1 else max(0.0, 0.04 - source_count * 0.005)
    margin_of_error = min(0.25, max(0.035, disagreement + spread_penalty + source_penalty))
    blended = (external_probability * 0.72) + (kalshi_probability * 0.18) + ((sum(match["quality_score"] for match in matches) / source_count) / 100.0 * 0.10)
    research_probability = clamp_probability(blended - margin_of_error)
    return {
        "external_probability": round(external_probability, 6),
        "kalshi_probability": round(kalshi_probability, 6),
        "research_probability": round(research_probability, 6),
        "margin_of_error": round(margin_of_error, 6),
        "evidence_score": round(sum(match["quality_score"] for match in matches) / source_count, 2),
    }


def market_only_research_probability(leg: dict[str, Any]) -> dict[str, Any]:
    kalshi_probability = float(leg.get("probability") or 0.5)
    spread = leg_spread_cents(leg)
    liquidity = numeric_text(leg.get("open_interest")) + numeric_text(leg.get("volume_24h"))
    liquidity_bonus = min(0.035, math.log1p(max(0.0, liquidity)) / 160.0)
    spread_penalty = min(0.085, spread / 100.0 * 0.30)
    category = str(leg.get("sport") or "Kalshi")
    category_penalty = {
        "Weather": 0.025,
        "Markets": 0.045,
        "Crypto": 0.055,
        "Sports": 0.060,
        "Esports": 0.070,
        "Politics": 0.075,
        "Media": 0.080,
        "Kalshi": 0.070,
    }.get(category, 0.070)
    margin_of_error = min(0.18, max(0.075, category_penalty + spread_penalty + (0.025 if liquidity < 25 else 0.0)))
    research_probability = clamp_probability(kalshi_probability + liquidity_bonus - margin_of_error)
    return {
        "external_probability": None,
        "kalshi_probability": round(kalshi_probability, 6),
        "research_probability": round(research_probability, 6),
        "margin_of_error": round(margin_of_error, 6),
        "evidence_score": round(max(35.0, min(62.0, 50.0 + liquidity_bonus * 200.0 - spread_penalty * 100.0)), 2),
    }


def build_research_edge_slip(
    markets: list[dict[str, Any]],
    yyyymmdd: str,
    public_signals: list[dict[str, Any]],
    min_research_probability: float = DEFAULT_RESEARCH_EDGE_MIN_PROBABILITY,
    min_legs: int = 4,
    max_legs: int = 12,
    stake_dollars: float = 5.0,
) -> dict[str, Any]:
    allowed_signals = [signal for signal in public_signals if signal_is_public(signal)]
    scout_mode = not allowed_signals
    candidates: list[dict[str, Any]] = []
    for market in markets:
        base_legs = verified_combo_market_legs(
            market,
            min_leg_probability=0.62,
            max_leg_probability=0.92,
            yyyymmdd=yyyymmdd,
            require_supported_market=False,
        )
        if not min_legs <= len(base_legs) <= max_legs:
            continue
        enriched: list[dict[str, Any]] = []
        for leg in base_legs:
            if scout_mode:
                matches: list[dict[str, Any]] = []
                probability_summary = market_only_research_probability(leg)
            else:
                matches = research_signal_matches(leg, allowed_signals)
                if not matches:
                    enriched = []
                    break
                has_primary_source = any(
                    str(match.get("source_type", "")).lower().replace("-", "_")
                    in {"primary_source", "official_data", "official_report", "peer_reviewed"}
                    for match in matches
                )
                if len(matches) < 2 and not has_primary_source:
                    enriched = []
                    break
                probability_summary = research_probability_for_leg(leg, matches)
            if probability_summary["research_probability"] < min_research_probability:
                enriched = []
                break
            if probability_summary["margin_of_error"] > (0.18 if scout_mode else 0.13):
                enriched = []
                break
            enriched.append(
                {
                    **leg,
                    "probability": probability_summary["research_probability"],
                    "research_probability": probability_summary["research_probability"],
                    "external_probability": probability_summary["external_probability"],
                    "kalshi_probability": probability_summary["kalshi_probability"],
                    "margin_of_error": probability_summary["margin_of_error"],
                    "evidence_score": probability_summary["evidence_score"],
                    "evidence_count": len(matches),
                    "evidence": matches[:4],
                    "research_mode": "market_only_scout" if scout_mode else "source_backed",
                    "required_probability": min_research_probability,
                    "exact_bet_score": round(
                        min(100.0, float(leg.get("exact_bet_score") or 0) * 0.35 + probability_summary["evidence_score"] * 0.65),
                        2,
                    ),
                }
            )
        if len(enriched) != len(base_legs):
            continue
        slip = listed_combo_slip(
            market,
            enriched,
            min_leg_probability=min_research_probability,
            max_leg_probability=0.99,
            stake_dollars=stake_dollars,
        )
        if slip.get("action") == "BUILD_SLIP":
            candidates.append(slip)
    if not candidates:
        return {
            "action": "NO_SLIP",
            "reason": "No exact listed Kalshi combo had every leg clear the Research Edge rules.",
            "min_research_probability": min_research_probability,
            "eligible_leg_count": 0,
            "eligible_combo_count": 0,
            "evidence_signal_count": len(allowed_signals),
            "legs": [],
        }
    best = min(
        candidates,
        key=lambda slip: (
            -sum(float(leg["research_probability"]) for leg in slip["legs"]) / len(slip["legs"]),
            sum(float(leg["margin_of_error"]) for leg in slip["legs"]) / len(slip["legs"]),
            sum(float(leg["spread_cents"]) for leg in slip["legs"]) / len(slip["legs"]),
        ),
    )
    best["model"] = "research_edge_v1"
    best["model_state"] = "experimental"
    best["decision_status"] = "track_only"
    best["execution_allowed"] = False
    best["research_mode"] = "market_only_scout" if scout_mode else "source_backed"
    best["min_research_probability"] = min_research_probability
    best["eligible_leg_count"] = sum(candidate["leg_count"] for candidate in candidates)
    best["eligible_combo_count"] = len(candidates)
    best["evidence_signal_count"] = len(allowed_signals)
    best["skipped_overlap_count"] = 0
    best["note"] = (
        "Experimental exact listed Kalshi combo in scout mode; track only because this heuristic is not a validated outcome model."
        if scout_mode
        else "Experimental exact listed Kalshi combo where every leg cleared source checks; track only pending out-of-sample validation."
    )
    return best


def _combo_tier_source_summary(
    markets: list[dict[str, Any]],
    *,
    yyyymmdd: str,
    min_leg_probability: float,
    max_leg_probability: float,
    min_legs: int,
    max_legs: int,
    require_supported_market: bool,
) -> dict[str, int]:
    exact_contract_count = 0
    eligible_exact_combo_count = 0
    for market in markets:
        legs = verified_combo_market_legs(
            market,
            min_leg_probability=min_leg_probability,
            max_leg_probability=max_leg_probability,
            yyyymmdd=yyyymmdd,
            require_supported_market=require_supported_market,
        )
        if not legs:
            continue
        exact_contract_count += 1
        if min_legs <= len(legs) <= max_legs:
            eligible_exact_combo_count += 1
    return {
        "exact_contract_count": exact_contract_count,
        "eligible_exact_combo_count": eligible_exact_combo_count,
    }


def build_combo_source_summary(
    markets: list[dict[str, Any]],
    yyyymmdd: str,
    *,
    primary_min_leg_probability: float,
    primary_max_leg_probability: float,
    primary_min_legs: int,
    primary_max_legs: int,
    leverage_min_leg_probability: float,
) -> dict[str, Any]:
    active_markets = [
        market
        for market in markets
        if str(market.get("ticker") or "").upper().startswith("KXMVE")
        and str(market.get("status") or "").lower() in {"active", "open"}
    ]
    verified_current_day_contract_count = sum(
        1
        for market in active_markets
        if verified_combo_market_legs(
            market,
            min_leg_probability=0.01,
            max_leg_probability=0.99,
            yyyymmdd=yyyymmdd,
            require_supported_market=False,
        )
    )
    return {
        "active_kxmve_market_count": len(active_markets),
        "tradable_kxmve_market_count": sum(1 for market in active_markets if market_is_tradable(market)),
        "verified_current_day_contract_count": verified_current_day_contract_count,
        "tiers": {
            "primary": _combo_tier_source_summary(
                active_markets,
                yyyymmdd=yyyymmdd,
                min_leg_probability=primary_min_leg_probability,
                max_leg_probability=primary_max_leg_probability,
                min_legs=primary_min_legs,
                max_legs=primary_max_legs,
                require_supported_market=True,
            ),
            "leverage": _combo_tier_source_summary(
                active_markets,
                yyyymmdd=yyyymmdd,
                min_leg_probability=leverage_min_leg_probability,
                max_leg_probability=primary_max_leg_probability,
                min_legs=primary_min_legs,
                max_legs=primary_max_legs,
                require_supported_market=True,
            ),
            "all_day": _combo_tier_source_summary(
                active_markets,
                yyyymmdd=yyyymmdd,
                min_leg_probability=DEFAULT_ALL_DAY_MIN_LEG_PROBABILITY,
                max_leg_probability=DEFAULT_ALL_DAY_MAX_LEG_PROBABILITY,
                min_legs=primary_min_legs,
                max_legs=max(primary_max_legs, 24),
                require_supported_market=False,
            ),
            "research_edge": _combo_tier_source_summary(
                active_markets,
                yyyymmdd=yyyymmdd,
                min_leg_probability=0.62,
                max_leg_probability=0.92,
                min_legs=4,
                max_legs=12,
                require_supported_market=False,
            ),
        },
    }


def enrich_combo_market(http: HttpClient, market: dict[str, Any], market_cache: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    enriched_legs: list[dict[str, Any]] = []
    # Price, event and market kept together per leg. They used to be two lists
    # appended on different conditions, so an unpriced leg dropped out of the
    # probabilities while its event ticker stayed in the correlation input.
    priced_legs: list[dict[str, Any]] = []
    selected_legs = list(market.get("legs") or [])
    selected_leg_signature = combo_leg_signature(selected_legs)
    combo_evidence = {
        "combo_market_ticker": market.get("ticker", ""),
        "combo_event_ticker": market.get("event_ticker", ""),
        "combo_collection_ticker": market.get("mve_collection_ticker", ""),
        "combo_market_status": market.get("status", ""),
        "combo_market_yes_ask_cents": market.get("yes_ask_cents"),
        "combo_market_yes_bid_cents": market.get("yes_bid_cents"),
        "combo_market_fetched_at": market.get("api_fetched_at", ""),
        "combo_market_updated_at": market.get("market_updated_at", ""),
        "combo_market_source_url": market.get("source_url", ""),
        "combo_market_snapshot_hash": market.get("source_snapshot_hash", ""),
        "combo_market_leg_signature": selected_leg_signature,
        "combo_exact_leg_count": len(selected_legs),
        "combo_evidence_status": VERIFIED_COMBO_EVIDENCE,
        "combo_source": VERIFIED_COMBO_SOURCE,
    }
    for leg in selected_legs:
        ticker = leg.get("market_ticker", "")
        side = leg.get("side", "yes")
        if ticker not in market_cache:
            market_cache[ticker] = fetch_market_by_ticker(http, ticker)
        leg_market = market_cache[ticker]
        if leg_market:
            quote = side_quote_from_market(leg_market, side)
            probability = quote["market_implied_probability"]
            if probability is not None:
                priced_legs.append(
                    {
                        "probability": probability,
                        "event_ticker": leg_market.get("event_ticker", leg.get("event_ticker", "")),
                        "market_ticker": ticker,
                        "side": quote["side"],
                    }
                )
            enriched_legs.append(
                {
                    "market_ticker": ticker,
                    "event_ticker": leg_market.get("event_ticker", leg.get("event_ticker", "")),
                    "side": quote["side"],
                    "title": leg_market.get("title", ""),
                    "subtitle": leg_market.get(f"{quote['side']}_sub_title", ""),
                    "rules": leg_market.get("rules_primary", ""),
                    "display_event": display_event_from_rules(leg_market.get("rules_primary", "")),
                    "status": leg_market.get("status", ""),
                    "volume_24h": leg_market.get("volume_24h_fp", ""),
                    "open_interest": leg_market.get("open_interest_fp", ""),
                    "event_start_time": leg_market.get("occurrence_datetime", ""),
                    "close_time": leg_market.get("close_time", ""),
                    "market_close_time": leg_market.get("close_time", ""),
                    "expected_expiration_time": leg_market.get("expected_expiration_time", ""),
                    "expiration_time": leg_market.get("expiration_time", ""),
                    "api_fetched_at": leg_market.get("_api_fetched_at", ""),
                    "source_snapshot_hash": leg_market.get("_source_snapshot_hash", ""),
                    "source_freshness_state": leg_market.get("_source_freshness_state", "unknown"),
                    "market_updated_at": leg_market.get("updated_time", ""),
                    "source_updated_at": leg_market.get("updated_time", ""),
                    **combo_evidence,
                    **quote,
                }
            )
        else:
            enriched_legs.append(
                {
                    "market_ticker": ticker,
                    "event_ticker": leg.get("event_ticker", ""),
                    "side": side,
                    "title": "",
                    "subtitle": "",
                    "rules": "",
                    "status": "missing",
                    "market_implied_probability": None,
                    "source_freshness_state": "missing",
                    **combo_evidence,
                }
            )
    joint = combo_joint_probability(priced_legs)
    raw_probability = joint["raw_probability"]
    adjusted_probability = joint["adjusted_probability"]
    combo_yes_ask = market.get("yes_ask_cents")
    combo_ev_cents = (
        None
        if adjusted_probability is None or combo_yes_ask is None or combo_yes_ask <= 0
        else round(adjusted_probability * 100.0 - combo_yes_ask, 2)
    )
    missing_leg_count = sum(1 for leg in enriched_legs if leg.get("market_implied_probability") is None)
    source_states = [str(market.get("source_freshness_state") or "unknown").lower()]
    source_states.extend(str(leg.get("source_freshness_state") or "unknown").lower() for leg in enriched_legs)
    if "stale" in source_states:
        source_freshness_state = "stale"
    elif source_states and all(state == "fresh" for state in source_states):
        source_freshness_state = "fresh"
    else:
        source_freshness_state = "unknown"
    market.update(
        {
            "leg_details": enriched_legs,
            "raw_market_implied_probability": raw_probability,
            "adjusted_market_implied_probability": adjusted_probability,
            "correlation_adjustment": joint["correlation_adjustment"],
            "correlation_adjustment_standard_error": joint["correlation_adjustment_standard_error"],
            "correlation_adjustment_resolved": joint["correlation_adjustment_resolved"],
            "joint_basis": joint["joint_basis"],
            "joint_unmodellable_reason": joint["unmodellable_reason"],
            "missing_leg_count": missing_leg_count,
            # A combo whose joint could not be modelled is not ready either: the
            # legs are priced, but there is no joint probability to act on.
            "real_data_ready": (
                missing_leg_count == 0
                and raw_probability is not None
                and adjusted_probability is not None
            ),
            "source_freshness_state": source_freshness_state,
            "market_product_type": "cross_game_combo",
            "combo_ev_cents": combo_ev_cents,
            "real_data_warning": (
                "All leg probabilities are market-implied from live Kalshi bid/ask, not predictive model guarantees."
                if missing_leg_count == 0
                else "Some underlying legs could not be priced from public Kalshi data."
            ),
            "combo_evidence_status": VERIFIED_COMBO_EVIDENCE,
            "combo_market_leg_signature": selected_leg_signature,
            "combo_exact_leg_count": len(selected_legs),
        }
    )
    return market


def parse_kalshi_market(market: dict[str, Any]) -> dict[str, Any]:
    legs = market.get("mve_selected_legs") or []
    return {
        "ticker": market.get("ticker", ""),
        "event_ticker": market.get("event_ticker", ""),
        "title": market.get("title", ""),
        "legs_text": split_market_title(market.get("title", "")),
        "legs": legs,
        "yes_ask_cents": cents_from_dollars(market.get("yes_ask_dollars")),
        "yes_bid_cents": cents_from_dollars(market.get("yes_bid_dollars")),
        "no_ask_cents": cents_from_dollars(market.get("no_ask_dollars")),
        "no_bid_cents": cents_from_dollars(market.get("no_bid_dollars")),
        "liquidity_dollars": market.get("liquidity_dollars", ""),
        "volume_24h": market.get("volume_24h_fp", ""),
        "close_time": market.get("close_time", ""),
        "expected_expiration_time": market.get("expected_expiration_time", ""),
        "api_fetched_at": market.get("_api_fetched_at", ""),
        "market_updated_at": market.get("updated_time", ""),
        "source_updated_at": market.get("updated_time", ""),
        "source_url": market.get("_source_url", ""),
        "source_snapshot_hash": market.get("_source_snapshot_hash", ""),
        "source_freshness_state": market.get("_source_freshness_state", "unknown"),
        "status": market.get("status", ""),
        "mve_collection_ticker": market.get("mve_collection_ticker", ""),
        "custom_strike": market.get("custom_strike") or {},
        "source": "kalshi_public",
    }


def fetch_kalshi_combo_markets(http: HttpClient, limit: int = 100) -> list[dict[str, Any]]:
    raw_markets: dict[str, dict[str, Any]] = {}
    queries = [
        "MLB WNBA sports total runs points goals",
        "over under runs points goals",
        "",
    ]
    for query in queries:
        cursor = ""
        pages = 1 if query else 5
        for _ in range(pages):
            request = {"limit": limit, "status": "open", "mve_filter": "only"}
            if query:
                request["query"] = query
            if cursor:
                request["cursor"] = cursor
            url = f"{KALSHI_PUBLIC_BASE_URL}/markets?{urlencode(request)}"
            response = http.get_text(url)
            payload = response.json()
            for market in payload.get("markets", []):
                ticker = market.get("ticker", "")
                if ticker:
                    market["_api_fetched_at"] = response.fetched_at
                    market["_source_url"] = response.url
                    market["_source_snapshot_hash"] = response.content_hash
                    market["_source_freshness_state"] = response.freshness_state
                    raw_markets[ticker] = market
            cursor = payload.get("cursor") or ""
            if not cursor:
                break
    markets = [parse_kalshi_market(market) for market in raw_markets.values()]
    filtered_markets = [
        market
        for market in markets
        if market["ticker"].startswith("KXMVE")
        and any(word in market["title"].lower() for word in SPORTS_COMBO_WORDS)
    ]
    market_cache: dict[str, dict[str, Any] | None] = {}
    enriched_markets = [enrich_combo_market(http, market, market_cache) for market in filtered_markets]
    return sorted(
        enriched_markets,
        key=lambda market: (
            market.get("real_data_ready", False),
            market.get("adjusted_market_implied_probability") or 0,
        ),
        reverse=True,
    )


def market_is_tradable(market: dict[str, Any]) -> bool:
    ask = market.get("yes_ask_cents")
    return ask is not None and 0 < ask < 100


def _yes_spread_cents(market: dict[str, Any]) -> Decimal | None:
    explicit = market.get("spread_cents")
    if explicit is not None:
        return Decimal(str(explicit))
    ask = market.get("yes_ask_cents")
    bid = market.get("yes_bid_cents")
    if ask is None or bid is None:
        return None
    return max(Decimal("0"), Decimal(str(ask)) - Decimal(str(bid)))


def _public_decision(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        key: format(value, "f") if isinstance(value, Decimal) else value
        for key, value in decision.items()
    }


def evaluate_combo_market_decision(
    market: dict[str, Any],
    *,
    policy: ResearchDecisionPolicy | None = None,
) -> dict[str, Any]:
    from .evaluation.decision import evaluate_binary_contract_decision

    product_type = str(
        market.get("market_product_type")
        or ("cross_game_combo" if str(market.get("ticker") or "").upper().startswith("KXMVE") else "binary_contract")
    )
    return evaluate_binary_contract_decision(
        model_probability=market.get("model_probability"),
        confidence_interval_low=market.get("model_probability_ci_low"),
        confidence_interval_high=market.get("model_probability_ci_high"),
        market_price_cents=market.get("yes_ask_cents"),
        model_state=str(market.get("model_state") or "baseline_only"),
        test_sample_size=int(market.get("model_test_sample_size") or 0),
        source_freshness_state=str(market.get("source_freshness_state") or "unknown"),
        market_status=str(market.get("status") or "unknown"),
        spread_cents=_yes_spread_cents(market),
        fee_cents=market.get("estimated_fee_cents"),
        slippage_cents=market.get("estimated_slippage_cents"),
        market_product_type=product_type,
        validated_product_type=market.get("validated_product_type"),
        market_horizon_bucket=market.get("market_horizon_bucket"),
        validated_horizon_bucket=market.get("validated_horizon_bucket"),
        rejected=bool(market.get("rejected") or market.get("rejection_reason")),
        unresolved=bool(market.get("unresolved_record")),
        blocked=bool(
            market.get("blocked")
            or market.get("source_blocked")
            or ("real_data_ready" in market and not market.get("real_data_ready"))
        ),
        duplicate=bool(market.get("duplicate") or market.get("duplicate_record")),
        model_version=str(market.get("model_version") or "unversioned"),
        feature_version=str(market.get("feature_version") or "unversioned"),
        dataset_version=str(market.get("dataset_version") or "unversioned"),
        policy=policy,
    )


def build_market_decisions(
    markets: list[dict[str, Any]],
    *,
    policy: ResearchDecisionPolicy | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for market in markets:
        decision = evaluate_combo_market_decision(market, policy=policy)
        records.append(
            {
                "ticker": market.get("ticker", ""),
                "title": market.get("title", ""),
                "yes_ask_cents": market.get("yes_ask_cents"),
                "market_implied_probability": market.get("adjusted_market_implied_probability"),
                "leg_count": len(market.get("leg_details") or []),
                "legs": market.get("leg_details") or [],
                "decision": _public_decision(decision),
            }
        )
    action_rank = {"BET_CANDIDATE": 2, "WAIT_FOR_DATA": 1, "NO_BET": 0}
    return sorted(
        records,
        key=lambda item: (
            action_rank.get(str(item["decision"].get("action")), -1),
            Decimal(str(item["decision"].get("lower_bound_net_edge_cents") or "-999")),
            Decimal(str(item["decision"].get("point_net_edge_cents") or "-999")),
        ),
        reverse=True,
    )


def build_bet_candidates(
    markets: list[dict[str, Any]],
    *,
    policy: ResearchDecisionPolicy | None = None,
) -> list[dict[str, Any]]:
    return [
        _candidate_from_decision_record(record)
        for record in build_market_decisions(markets, policy=policy)
        if record["decision"].get("action") == "BET_CANDIDATE"
    ]


def _candidate_from_decision_record(record: dict[str, Any]) -> dict[str, Any]:
    decision = record["decision"]
    return {
        **record,
        "model_probability": decision.get("model_probability"),
        "confidence_interval_low": decision.get("confidence_interval_low"),
        "confidence_interval_high": decision.get("confidence_interval_high"),
        "point_net_edge_cents": decision.get("point_net_edge_cents"),
        "lower_bound_net_edge_cents": decision.get("lower_bound_net_edge_cents"),
        "uncertainty_adjusted_kelly_fraction": decision.get("uncertainty_adjusted_kelly_fraction"),
        "reason": (
            "Validated research model passed its out-of-sample, calibration, uncertainty, "
            "freshness, product-segment, spread, fee, and slippage gates."
        ),
    }


def build_pick_summary(markets: list[dict[str, Any]]) -> dict[str, Any]:
    from .evaluation.decision import ResearchDecisionPolicy

    policy = ResearchDecisionPolicy()
    thresholds = {
        "policy_version": policy.version,
        "minimum_test_rows": policy.minimum_test_rows,
        "minimum_point_edge_cents": format(policy.minimum_point_edge_cents, "f"),
        "minimum_lower_bound_edge_cents": format(policy.minimum_lower_bound_edge_cents, "f"),
        "maximum_spread_cents": format(policy.maximum_spread_cents, "f"),
        "kelly_multiplier": format(policy.kelly_multiplier, "f"),
        "maximum_bankroll_fraction": format(policy.maximum_bankroll_fraction, "f"),
    }
    decisions = build_market_decisions(markets, policy=policy)
    candidates = [
        _candidate_from_decision_record(record)
        for record in decisions
        if record["decision"].get("action") == "BET_CANDIDATE"
    ]
    tradable_count = sum(1 for market in markets if market_is_tradable(market))
    decision_counts = {
        action: sum(1 for item in decisions if item["decision"].get("action") == action)
        for action in ["BET_CANDIDATE", "WAIT_FOR_DATA", "NO_BET"]
    }
    if not candidates:
        action = "WAIT_FOR_DATA" if decision_counts["WAIT_FOR_DATA"] else "NO_BET"
        reason = (
            "No research candidate is available because required fresh model, interval, market, or execution-cost data is incomplete."
            if action == "WAIT_FOR_DATA"
            else "No market passed the validated-model, uncertainty-adjusted edge, freshness, product, and execution-cost gates."
        )
        return {
            "action": action,
            "reason": reason,
            "research_only": True,
            "execution_allowed": False,
            "thresholds": thresholds,
            "tradable_combo_count": tradable_count,
            "decision_counts": decision_counts,
            "candidates": [],
            "watchlist": decisions[:10],
        }
    return {
        "action": "BET_CANDIDATE",
        "reason": "At least one research candidate passed every validation and uncertainty-adjusted decision gate.",
        "research_only": True,
        "execution_allowed": False,
        "thresholds": thresholds,
        "tradable_combo_count": tradable_count,
        "decision_counts": decision_counts,
        "candidates": candidates[:5],
        "watchlist": [item for item in decisions if item["decision"].get("action") != "BET_CANDIDATE"][:10],
    }


def build_failure_guardrail_summary() -> dict[str, Any]:
    return {
        "status": "ACTIVE",
        "latest_lesson": "Miami vs Colorado failed because a high-probability-looking NO on Over 16.5 still carried extreme MLB tail risk in a Colorado run environment.",
        "active_blocks": [
            {
                "flag": "high_scoring_mlb_total_under_blocked",
                "rule": "Never take MLB total-runs unders in high-scoring environments.",
            },
            {
                "flag": "extreme_mlb_total_under_tail_risk",
                "rule": "Exclude MLB NO legs on Over 14.5+ total-runs markets.",
            },
            {
                "flag": "coors_or_colorado_total_under_tail_risk",
                "rule": "Exclude Colorado/Coors-style MLB NO legs on Over 12.5+ total-runs markets.",
            },
            {
                "flag": "colorado_run_environment_review",
                "rule": "Colorado/Coors-style over legs require 90%+; unders stay blocked.",
            },
            {
                "flag": "high_scoring_over_threshold",
                "rule": "High-scoring MLB over legs require 85%+; Colorado or 12.5+ lines require 90%+.",
            },
            {
                "flag": "overlap_guard",
                "rule": "Keep one normalized matchup per slip.",
            },
        ],
        "not_fixed_by": [
            "Adding more legs",
            "Trusting market-implied probability alone",
            "Assuming a 19-leg combo should behave like one individual 80%+ leg",
        ],
    }


def build_today_payload(
    yyyymmdd: str | None = None,
    slip_target_probability: float = DEFAULT_MIN_LEG_PROBABILITY,
    slip_min_leg_probability: float | None = None,
    slip_max_leg_probability: float = 0.985,
    slip_min_legs: int = 8,
    slip_max_legs: int = 20,
    slip_stake_dollars: float = 5.0,
    leverage_min_leg_probability: float = DEFAULT_LEVERAGE_MIN_LEG_PROBABILITY,
    public_intel_path: str | Path | None = None,
) -> dict[str, Any]:
    run_date = yyyymmdd or today_key()
    schedule_http = HttpClient()
    market_http = HttpClient()
    games, schedule_source_status = fetch_espn_schedule_with_status(schedule_http, run_date)
    markets = fetch_kalshi_combo_markets(market_http)
    all_day_markets = fetch_kalshi_same_day_markets(market_http, run_date)
    pick_summary = build_pick_summary(markets)
    intel_bot = PublicIntelBot()
    public_signals = intel_bot.load_signals(public_intel_path)
    initial_public_intel = intel_bot.build_summary(markets, public_signals, overlap_key_fn=overlap_key_for_leg)
    intel_by_overlap_key = initial_public_intel.get("intel_by_overlap_key", {})
    custom_slip = build_custom_slip(
        markets,
        target_probability=slip_target_probability,
        min_leg_probability=slip_min_leg_probability,
        max_leg_probability=slip_max_leg_probability,
        min_legs=slip_min_legs,
        max_legs=slip_max_legs,
        stake_dollars=slip_stake_dollars,
        yyyymmdd=run_date,
        intel_by_overlap_key=intel_by_overlap_key,
    )
    leverage_slip = build_custom_slip(
        markets,
        target_probability=leverage_min_leg_probability,
        min_leg_probability=leverage_min_leg_probability,
        max_leg_probability=slip_max_leg_probability,
        min_legs=slip_min_legs,
        max_legs=slip_max_legs,
        stake_dollars=slip_stake_dollars,
        yyyymmdd=run_date,
        intel_by_overlap_key=intel_by_overlap_key,
    )
    if leverage_slip.get("action") == "BUILD_SLIP":
        leverage_slip["note"] = (
            "Leverage tier built from live Kalshi bid/ask probabilities with a lower 75% individual-leg filter. "
            "This can create bigger payouts but materially lowers the full combo probability."
        )
    all_day_slip = build_all_day_slip(
        markets,
        run_date,
        min_leg_probability=DEFAULT_ALL_DAY_MIN_LEG_PROBABILITY,
        max_leg_probability=DEFAULT_ALL_DAY_MAX_LEG_PROBABILITY,
        min_legs=slip_min_legs,
        max_legs=max(slip_max_legs, 24),
        stake_dollars=slip_stake_dollars,
    )
    research_edge_slip = build_research_edge_slip(
        markets,
        run_date,
        public_signals,
        min_research_probability=DEFAULT_RESEARCH_EDGE_MIN_PROBABILITY,
        min_legs=4,
        max_legs=12,
        stake_dollars=slip_stake_dollars,
    )
    public_intel_summary = intel_bot.build_summary(
        markets,
        public_signals,
        primary_slip=custom_slip,
        leverage_slip=leverage_slip,
        overlap_key_fn=overlap_key_for_leg,
    )
    research_summary = DeepResearchBot().build_summary(markets, custom_slip, leverage_slip)
    source_cache_status = market_http.cache_status()
    source_freshness_note = (
        "Some required Kalshi API responses came from stale cache after a fresh fetch failed; treat slips as research-only watch data."
        if source_cache_status.get("stale_fallback_count")
        else "Required Kalshi API responses were fetched live or served from the short-lived cache."
    )
    combo_source_summary = build_combo_source_summary(
        markets,
        run_date,
        primary_min_leg_probability=slip_min_leg_probability or slip_target_probability,
        primary_max_leg_probability=slip_max_leg_probability,
        primary_min_legs=slip_min_legs,
        primary_max_legs=slip_max_legs,
        leverage_min_leg_probability=leverage_min_leg_probability,
    )
    payload = {
        "date": run_date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generated_at_note": (
            "Generated from public Kalshi market data; supplemental ESPN schedules are included only when available."
        ),
        "source_status": {
            "kalshi_market_data": {
                "source_name": "kalshi_public_api",
                "required": True,
                "state": "configured_healthy",
                "record_count": len(markets),
                "retrieval_method": "official_api",
            },
            "espn_schedule": schedule_source_status,
        },
        "source_cache_status": source_cache_status,
        "source_freshness_note": source_freshness_note,
        "safety_note": "Paper view only. Use this for manual research; no automated real-money trading.",
        "games": games,
        "markets": markets,
        "combo_source_summary": combo_source_summary,
        "all_day_market_count": len(all_day_markets),
        "pick_summary": pick_summary,
        "custom_slip": custom_slip,
        "leverage_slip": leverage_slip,
        "all_day_slip": all_day_slip,
        "research_edge_slip": research_edge_slip,
        "research_summary": research_summary,
        "public_intel_summary": public_intel_summary,
        "failure_guardrail_summary": build_failure_guardrail_summary(),
        "manual_probability_note": "Real-data mode: combo probabilities are market-implied from public Kalshi leg bid/ask, not fake projections.",
    }
    return gate_slip_payload(payload)


def write_today_payload(
    path: str | Path,
    yyyymmdd: str | None = None,
    slip_target_probability: float = DEFAULT_MIN_LEG_PROBABILITY,
    slip_min_leg_probability: float | None = None,
    slip_max_leg_probability: float = 0.985,
    slip_min_legs: int = 8,
    slip_max_legs: int = 20,
    slip_stake_dollars: float = 5.0,
    leverage_min_leg_probability: float = DEFAULT_LEVERAGE_MIN_LEG_PROBABILITY,
    public_intel_path: str | Path | None = None,
) -> dict[str, Any]:
    payload = build_today_payload(
        yyyymmdd,
        slip_target_probability=slip_target_probability,
        slip_min_leg_probability=slip_min_leg_probability,
        slip_max_leg_probability=slip_max_leg_probability,
        slip_min_legs=slip_min_legs,
        slip_max_legs=slip_max_legs,
        slip_stake_dollars=slip_stake_dollars,
        leverage_min_leg_probability=leverage_min_leg_probability,
        public_intel_path=public_intel_path,
    )
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary_path.replace(output_path)
    return payload
