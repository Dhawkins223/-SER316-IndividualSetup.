"""Where two venues price the same game differently.

Section O of `docs/sports-prediction-research-program.md` closed off the obvious
route to an edge: a walk-forward Elo rating loses decisively to the de-vigged
closing line, and blending it into the price adds nothing measurable. Those are
measured results, not opinions, and anything built on "our model knows better"
is built on a claim this platform's own data rejects.

What that finding does *not* rule out is disagreement between venues. When a
sportsbook and an exchange price the same game differently, one of them is
wrong, and establishing that requires no model at all — only that the two
prices refer to the same event and are compared on the same footing. That is
backlog E-08.

Three things make this honest rather than flattering:

1. **Matching is conjunctive and refuses ambiguity.** A Polymarket market is
   paired with a board event only when the start times agree within a tolerance
   *and* both teams correspond. If two events match one market, nothing is
   emitted for it. `cross_venue_gaps` in the Polymarket connector already
   refused to guess from slug text; this module earns the match instead of
   guessing it.

2. **Market equivalence is checked, not assumed.** A soccer "Will Brentford win
   on 2026-08-22?" resolves Yes/No over a three-way outcome, so its Yes price is
   not comparable to a de-vigged two-way moneyline. Those markets are excluded
   by name and counted, rather than silently compared against something they do
   not mean.

3. **A gap must clear what it costs to take it.** Both sides are fair
   probabilities with the margin removed, so capturing a gap means crossing the
   exchange's spread and paying the book's margin. A gap smaller than that is
   not an opportunity. It must also clear the de-vig method disagreement the
   board publishes: below that figure the gap is an artifact of how margin was
   removed, exactly as `math/devig.py` established.

The output is a research observation about two prices. It is not a signal, not a
recommendation, and creates no research candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

PROBABILITY_SCALE = Decimal("0.000001")
DEFAULT_START_TOLERANCE_MINUTES = 120

# Leagues whose moneyline is a genuine two-way market, so a binary exchange
# contract on the same game means the same thing. Soccer is deliberately absent:
# its three-way result makes a "will X win" contract a different question.
TWO_WAY_LEAGUE_SLUGS = {"mlb", "nfl", "nba", "nhl", "wnba"}

BOARD_MONEYLINE_TYPES = {"h2h", "moneyline"}

_SLUG_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")


@dataclass(frozen=True)
class VenueGap:
    """One selection priced by both venues, and what the difference is worth."""

    event_id: str
    league: str
    home_team: str
    away_team: str
    game_start_time: str | None
    selection: str
    board_probability: Decimal
    polymarket_probability: Decimal
    gap: Decimal
    execution_cost_estimate: Decimal
    devig_disagreement: Decimal
    threshold: Decimal
    exceeds_threshold: bool
    polymarket_market_id: str
    polymarket_slug: str
    match_basis: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "league": self.league,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "game_start_time": self.game_start_time,
            "selection": self.selection,
            "board_probability": _probability_text(self.board_probability),
            "polymarket_probability": _probability_text(self.polymarket_probability),
            "gap": _probability_text(self.gap),
            "execution_cost_estimate": _probability_text(self.execution_cost_estimate),
            "devig_disagreement": _probability_text(self.devig_disagreement),
            "threshold": _probability_text(self.threshold),
            "exceeds_threshold": self.exceeds_threshold,
            "polymarket_market_id": self.polymarket_market_id,
            "polymarket_slug": self.polymarket_slug,
            "match_basis": self.match_basis,
        }


def _probability_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(PROBABILITY_SCALE), "f")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def normalize_team(name: str | None) -> str:
    """Reduce a team name to comparable letters.

    Punctuation, spacing, and the corporate suffixes venues disagree about are
    dropped so "St. Louis Cardinals" and "St Louis Cardinals" are one team.
    """

    text = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()
    for suffix in (" fc", " sk", " afc", " sc", " cf"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return re.sub(r"\s+", "", text)


def team_nickname(name: str | None) -> str:
    """The last word of a team name, which is its nickname in US league sports."""

    tokens = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).split()
    return tokens[-1] if tokens else ""


def teams_correspond(left: str | None, right: str | None) -> bool:
    """Whether two venue spellings denote the same team.

    Full normalized equality first, then nickname equality, which covers a venue
    writing "NY Yankees" where another writes "New York Yankees".

    Substring containment is deliberately *not* accepted. It looks helpful and is
    the fastest way to a confidently wrong comparison: "ATL" is a substring of
    "atlantabraves", "atlantahawks" and "atlantafalcons" alike, so an
    abbreviation-keyed derivative market would match an unrelated game's
    moneyline. A nickname must also be at least four characters, which excludes
    those same three-letter codes from matching on their own.
    """

    left_key, right_key = normalize_team(left), normalize_team(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    left_nickname, right_nickname = team_nickname(left), team_nickname(right)
    return bool(left_nickname) and len(left_nickname) >= 4 and left_nickname == right_nickname


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def league_from_slug(slug: str | None) -> str | None:
    """League code from a Polymarket slug such as `mlb-atl-mil-2026-08-21`."""

    text = str(slug or "").strip().lower()
    if not text:
        return None
    head = text.split("-", 1)[0]
    return head or None


def outcome_names(market: Mapping[str, Any]) -> list[str]:
    """Outcome labels from a normalized Polymarket market.

    The connector emits each outcome as a mapping keyed `outcome`; a bare list of
    strings is accepted too so a raw Gamma row can be inspected directly.
    """

    names: list[str] = []
    for outcome in market.get("outcomes") or []:
        if isinstance(outcome, Mapping):
            names.append(str(outcome.get("outcome") or "").strip())
        else:
            names.append(str(outcome).strip())
    return names


def slug_has_derivative_suffix(slug: str | None) -> bool:
    """Whether a Polymarket slug names a derivative rather than the game itself.

    Gamma slugs run `{league}-{team}-{team}-{YYYY-MM-DD}` for a moneyline and
    append to that for anything else: `-spread-away-1pt5`, `-total-7pt5`, or a
    team code for a "will X win" contract. Text after the date therefore means
    the market is not the game's moneyline.
    """

    text = str(slug or "").strip().lower()
    if not text:
        return False
    match = _SLUG_DATE.search(text)
    if match is None:
        return False
    return bool(text[match.end() :].strip("-"))


def market_is_two_way_moneyline(market: Mapping[str, Any]) -> tuple[bool, str]:
    """Whether this Polymarket market means the same thing as a board moneyline.

    Returns the verdict and, when it is negative, the reason to count it under.
    """

    market_type = str(market.get("sports_market_type") or "").strip().lower()
    if market_type and market_type != "moneyline":
        # A spread or total priced on the same game answers a different
        # question. Comparing it to a moneyline would be a category error.
        return False, f"not_moneyline:{market_type}"
    if slug_has_derivative_suffix(market.get("slug")):
        # Belt and braces for a row carrying no classification: a pure moneyline
        # slug ends at the game date, and derivatives append to it.
        return False, "not_moneyline:slug_suffix"
    names = outcome_names(market)
    if len(names) != 2:
        return False, "not_two_outcomes"
    if {name.lower() for name in names} == {"yes", "no"}:
        # A three-way sport collapsed into "will this team win". The No side
        # carries the draw, so its price is not a moneyline probability.
        return False, "yes_no_market"
    league = league_from_slug(market.get("slug"))
    if league not in TWO_WAY_LEAGUE_SLUGS:
        return False, f"unsupported_league:{league or 'unknown'}"
    return True, ""


def _board_moneyline(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for market in event.get("markets") or []:
        if str(market.get("market_type", "")).strip().lower() in BOARD_MONEYLINE_TYPES:
            if market.get("no_vig_available"):
                return market
    return None


def match_market_to_event(
    market: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    start_tolerance_minutes: int = DEFAULT_START_TOLERANCE_MINUTES,
) -> tuple[Mapping[str, Any] | None, str]:
    """Pair one Polymarket market with one board event, or explain why not.

    The match requires start-time agreement *and* both teams corresponding. A
    market that matches more than one event is refused: a confident comparison
    of the wrong game is worse than no comparison.
    """

    outcomes = outcome_names(market)
    if len(outcomes) != 2:
        return None, "not_two_outcomes"
    market_start = _parse_time(market.get("game_start_time"))
    if market_start is None:
        return None, "missing_game_start_time"

    tolerance = timedelta(minutes=start_tolerance_minutes)
    candidates = []
    for event in events:
        event_start = _parse_time(event.get("game_start_time"))
        if event_start is None or abs(event_start - market_start) > tolerance:
            continue
        home, away = event.get("home_team"), event.get("away_team")
        forward = teams_correspond(outcomes[0], away) and teams_correspond(outcomes[1], home)
        reverse = teams_correspond(outcomes[0], home) and teams_correspond(outcomes[1], away)
        if forward or reverse:
            candidates.append(event)
    if not candidates:
        return None, "no_event_matched"
    if len(candidates) > 1:
        return None, "ambiguous_match"
    return candidates[0], "start_time_and_both_teams"


def _polymarket_probability(market: Mapping[str, Any], selection: str) -> Decimal | None:
    """The venue's normalized probability for the board's selection name."""

    for outcome in market.get("outcomes") or []:
        if not isinstance(outcome, Mapping):
            continue
        if teams_correspond(outcome.get("outcome"), selection):
            return _decimal_or_none(outcome.get("normalized_probability"))
    return None


def _execution_cost(market: Mapping[str, Any], board_market: Mapping[str, Any]) -> Decimal:
    """Approximate cost of actually capturing a gap between fair prices.

    Both sides are quoted with margin removed, so taking the difference means
    crossing the exchange's spread and paying the book's margin. Half of each is
    charged, which is what one side of a round trip costs. This is a screen, not
    a fee model: it is deliberately rough and deliberately not zero.
    """

    exchange_spread = _decimal_or_none(market.get("spread")) or Decimal(0)
    book_overround = _decimal_or_none(board_market.get("overround")) or Decimal(0)
    return (abs(exchange_spread) + abs(book_overround)) / Decimal(2)


def compare_venues(
    board: Mapping[str, Any],
    polymarket_markets: Iterable[Mapping[str, Any]],
    *,
    start_tolerance_minutes: int = DEFAULT_START_TOLERANCE_MINUTES,
) -> dict[str, Any]:
    """Compare every board event a Polymarket market can be matched to.

    Coverage is reported as carefully as the gaps are: a comparison that silently
    drops most of the slate would look precise while describing almost nothing.
    """

    events = list(board.get("events") or [])
    gaps: list[VenueGap] = []
    excluded: dict[str, int] = {}
    matched_markets = 0
    considered = 0

    def count(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for market in polymarket_markets:
        considered += 1
        comparable, reason = market_is_two_way_moneyline(market)
        if not comparable:
            count(reason)
            continue
        event, basis = match_market_to_event(
            market, events, start_tolerance_minutes=start_tolerance_minutes
        )
        if event is None:
            count(basis)
            continue
        board_market = _board_moneyline(event)
        if board_market is None:
            count("board_has_no_devigged_moneyline")
            continue
        matched_markets += 1

        disagreement = _decimal_or_none(board_market.get("no_vig_method_disagreement")) or Decimal(0)
        execution_cost = _execution_cost(market, board_market)
        threshold = execution_cost + disagreement
        for selection_row in board_market.get("selections") or []:
            selection = str(selection_row.get("selection") or "")
            board_probability = _decimal_or_none(selection_row.get("no_vig_probability"))
            venue_probability = _polymarket_probability(market, selection)
            if board_probability is None or venue_probability is None:
                continue
            gap = venue_probability - board_probability
            gaps.append(
                VenueGap(
                    event_id=str(event.get("event_id") or ""),
                    league=str(event.get("league") or ""),
                    home_team=str(event.get("home_team") or ""),
                    away_team=str(event.get("away_team") or ""),
                    game_start_time=event.get("game_start_time"),
                    selection=selection,
                    board_probability=board_probability,
                    polymarket_probability=venue_probability,
                    gap=gap,
                    execution_cost_estimate=execution_cost,
                    devig_disagreement=disagreement,
                    threshold=threshold,
                    exceeds_threshold=abs(gap) > threshold,
                    polymarket_market_id=str(market.get("market_id") or ""),
                    polymarket_slug=str(market.get("slug") or ""),
                    match_basis=basis,
                )
            )

    actionable = [gap for gap in gaps if gap.exceeds_threshold]
    widest = max((abs(gap.gap) for gap in gaps), default=Decimal(0))
    return {
        "board_state": board.get("board_state"),
        "board_event_count": len(events),
        "polymarket_markets_considered": considered,
        "matched_market_count": matched_markets,
        "compared_selection_count": len(gaps),
        "exceeding_threshold_count": len(actionable),
        "widest_gap": _probability_text(widest),
        "excluded_counts": dict(sorted(excluded.items())),
        "gaps": [gap.as_dict() for gap in sorted(gaps, key=lambda row: abs(row.gap), reverse=True)],
        "disclaimer": (
            "Cross-venue gaps are observations of two posted prices. A gap below "
            "its threshold is execution cost or de-vig assumption, not signal, and "
            "no gap here is a validated model edge or a recommendation."
        ),
    }


def render_venue_comparison(report: Mapping[str, Any], *, limit: int = 15) -> str:
    lines = [
        "Cross-venue price comparison (sportsbook board vs Polymarket)",
        f"  board state: {report.get('board_state')}  events: {report.get('board_event_count')}",
        f"  polymarket markets considered: {report.get('polymarket_markets_considered')}",
        f"  matched to a board event: {report.get('matched_market_count')}",
        f"  selections compared: {report.get('compared_selection_count')}",
        f"  gaps above threshold: {report.get('exceeding_threshold_count')}",
        f"  widest gap: {report.get('widest_gap')}",
    ]
    excluded = report.get("excluded_counts") or {}
    if excluded:
        lines.append("  not compared: " + ", ".join(f"{reason}={count}" for reason, count in excluded.items()))
    rows = list(report.get("gaps") or [])[:limit]
    if rows:
        lines.append("")
        lines.append("  selection                          board    venue      gap  threshold")
        for row in rows:
            marker = "*" if row.get("exceeds_threshold") else " "
            label = f"{row.get('away_team')} at {row.get('home_team')}: {row.get('selection')}"
            lines.append(
                f"  {marker}{label[:33]:<33} {row.get('board_probability'):>8} "
                f"{row.get('polymarket_probability'):>8} {row.get('gap'):>8} {row.get('threshold'):>10}"
            )
        lines.append("")
        lines.append("  * gap exceeds estimated execution cost plus de-vig disagreement")
    lines.append("")
    lines.append("  " + str(report.get("disclaimer")))
    return "\n".join(lines)
