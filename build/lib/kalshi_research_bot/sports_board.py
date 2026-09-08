"""Read the sports rows the sports-research worker uploads into PostgreSQL.

`sports_research` collects public scoreboards and odds and writes normalized,
content-hashed rows into `app.sports_prediction_logs`. Nothing read them back,
so the hosted dashboard could not show sports at all. This module is the read
side of that upload: it rebuilds the upcoming board, states an explicit
freshness verdict, and derives the two comparisons a researcher would otherwise
redo by hand -- the no-vig fair price of each market and the best price
currently posted across books.

Every number here comes from an observed bookmaker price. That makes the board a
market baseline, never a validated model edge, so the payload keeps the
`baseline_only` / `track_only` contract documented in
`docs/probability-and-decision-policy.md`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from .business_store import ensure_database_ready
from .database import DatabaseRow, DatabaseSettings, connection_pool
from .math.devig import DEFAULT_DEVIG_METHOD, method_disagreement, remove_margin


SPORTS_SOURCE_HEALTH_NAMES = ("espn_scoreboard", "the_odds_api", "sports_source")
SPORTS_WORKER_NAME = "sports-research"
BOARD_STALE_SECONDS = 60 * 60
PROBABILITY_SCALE = Decimal("0.000001")
DEFAULT_MAX_EVENTS = 60

MARKET_ORDER = {"moneyline": 0, "h2h": 0, "spread": 1, "spreads": 1, "total": 2, "totals": 2}


def _now(now: datetime | str | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(now).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _decimal_text(value: Any) -> str | None:
    """Serialize an exact value as a fixed-point string, never a binary float."""
    if value is None:
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(number, "f")


def _probability_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(PROBABILITY_SCALE), "f")


def american_implied_probability(odds: Decimal) -> Decimal | None:
    """Exact implied probability of an American price, including its vig."""
    if odds == 0:
        return None
    if odds > 0:
        return Decimal(100) / (odds + Decimal(100))
    magnitude = -odds
    return magnitude / (magnitude + Decimal(100))


def _better_price(candidate: Decimal, incumbent: Decimal) -> bool:
    """American odds pay more as the number rises (-105 beats -110, +150 beats +140)."""
    return candidate > incumbent


def _market_sort_key(market_type: str) -> int:
    return MARKET_ORDER.get(market_type.strip().lower(), 3)


def _rows_for_board(
    connection: Any,
    *,
    now: datetime,
    max_events: int,
) -> list[DatabaseRow]:
    """Latest observation per (event, market, selection, line, book) for upcoming games.

    Read from `app.sports_current_quotes`, which holds exactly one row per quote.
    This used to run `DISTINCT ON` across every unresolved row of every upcoming
    game and throw away all but the newest snapshot of each price -- work that
    grew with how long a game had been collected while the answer stayed the size
    of the slate. At 400,000 collected rows one board load took 1.6 seconds.

    The projection is maintained by trigger and re-derivable: see migration
    `0014` and `verify_current_quotes`, which recomputes the `DISTINCT ON` answer
    and reports any row the projection disagrees about.
    """
    return connection.execute(
        """
        SELECT event_id, sport, league, home_team, away_team, bookmaker,
               market_type, selection, line, odds, odds_format,
               game_start_time, odds_timestamp, api_fetched_at,
               prediction_timestamp, confidence_score, source_snapshot_hash,
               run_id, model_version, strategy
        FROM app.sports_current_quotes
        WHERE game_start_time > %s
          AND event_id IN (
              SELECT event_id
              FROM app.sports_current_quotes
              WHERE game_start_time > %s
              GROUP BY event_id
              ORDER BY MIN(game_start_time)
              LIMIT %s
          )
        ORDER BY event_id, market_type, selection, line, bookmaker
        """,
        (now, now, max_events),
    ).fetchall()


def verify_current_quotes(
    *,
    settings: DatabaseSettings | None = None,
) -> dict[str, Any]:
    """Re-derive the board's rows from the log and report any disagreement.

    A projection that silently diverges from its source is worse than no
    projection, so the cheap answer is kept checkable against the expensive one.
    Disagreements are reported by kind: a quote the log has and the projection
    does not, one the projection has and the log does not, and one where both
    exist but point at different rows.
    """
    configured = ensure_database_ready(settings)
    with connection_pool(configured).connection() as connection:
        row = connection.execute(
            """
            WITH truth AS (
                SELECT DISTINCT ON (event_id, market_type, selection, line, bookmaker)
                       event_id, market_type, selection, line, bookmaker, id
                FROM app.sports_prediction_logs
                WHERE validation_status = 'valid'
                  AND settlement_state = 'unresolved'
                ORDER BY event_id, market_type, selection, line, bookmaker,
                         prediction_timestamp DESC, id DESC
            )
            SELECT
                (SELECT COUNT(*) FROM truth) AS expected_quotes,
                (SELECT COUNT(*) FROM app.sports_current_quotes) AS projected_quotes,
                (SELECT COUNT(*) FROM truth
                  WHERE NOT EXISTS (
                      SELECT 1 FROM app.sports_current_quotes AS projected
                      WHERE projected.event_id = truth.event_id
                        AND projected.market_type = truth.market_type
                        AND projected.selection = truth.selection
                        AND projected.line IS NOT DISTINCT FROM truth.line
                        AND projected.bookmaker = truth.bookmaker
                  )) AS missing_from_projection,
                (SELECT COUNT(*) FROM app.sports_current_quotes AS projected
                  WHERE NOT EXISTS (
                      SELECT 1 FROM truth
                      WHERE truth.event_id = projected.event_id
                        AND truth.market_type = projected.market_type
                        AND truth.selection = projected.selection
                        AND truth.line IS NOT DISTINCT FROM projected.line
                        AND truth.bookmaker = projected.bookmaker
                  )) AS not_in_log,
                (SELECT COUNT(*) FROM truth
                  JOIN app.sports_current_quotes AS projected
                    ON projected.event_id = truth.event_id
                   AND projected.market_type = truth.market_type
                   AND projected.selection = truth.selection
                   AND projected.line IS NOT DISTINCT FROM truth.line
                   AND projected.bookmaker = truth.bookmaker
                  WHERE projected.prediction_log_id <> truth.id) AS pointing_at_another_row
            """
        ).fetchone()

    disagreements = (
        int(row["missing_from_projection"])
        + int(row["not_in_log"])
        + int(row["pointing_at_another_row"])
    )
    return {
        "expected_quotes": int(row["expected_quotes"]),
        "projected_quotes": int(row["projected_quotes"]),
        "missing_from_projection": int(row["missing_from_projection"]),
        "not_in_log": int(row["not_in_log"]),
        "pointing_at_another_row": int(row["pointing_at_another_row"]),
        "disagreements": disagreements,
        "consistent": disagreements == 0,
    }


def _latest_upload_time(connection: Any) -> datetime | None:
    """When the collector last uploaded any valid sports row.

    Freshness describes the collector, not the slate: a day with no upcoming games
    left must not read as a dead source, and a genuinely empty table must not read
    as a fresh one.
    """
    row = connection.execute(
        """
        SELECT MAX(api_fetched_at) AS latest
        FROM app.sports_prediction_logs
        WHERE validation_status = 'valid'
        """
    ).fetchone()
    return None if row is None else row["latest"]


def _source_health(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT source, last_attempted_at, last_successful_at, freshness_state,
               consecutive_failures, last_error
        FROM ops.source_health
        WHERE source = ANY(%s)
        ORDER BY source
        """,
        (list(SPORTS_SOURCE_HEALTH_NAMES),),
    ).fetchall()
    return [
        {
            "source": str(row["source"]),
            "freshness_state": str(row["freshness_state"]),
            "last_attempted_at": _iso(row["last_attempted_at"]),
            "last_successful_at": _iso(row["last_successful_at"]),
            "consecutive_failures": int(row["consecutive_failures"]),
            "last_error": row["last_error"],
        }
        for row in rows
    ]


def _worker_state(connection: Any) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT worker_name, status, last_attempted_at, last_successful_at,
               consecutive_failures, last_error_code, data_fresh_at,
               source_fresh_at, heartbeat_at, model_state
        FROM ops.worker_status
        WHERE worker_name = %s
        """,
        (SPORTS_WORKER_NAME,),
    ).fetchone()
    if row is None:
        return None
    return {
        "worker_name": str(row["worker_name"]),
        "status": str(row["status"]),
        "last_attempted_at": _iso(row["last_attempted_at"]),
        "last_successful_at": _iso(row["last_successful_at"]),
        "consecutive_failures": int(row["consecutive_failures"]),
        "last_error_code": row["last_error_code"],
        "data_fresh_at": _iso(row["data_fresh_at"]),
        "source_fresh_at": _iso(row["source_fresh_at"]),
        "heartbeat_at": _iso(row["heartbeat_at"]),
        "model_state": row["model_state"],
    }


def _line_group(market_type: str, line: Any) -> Any:
    """Group the two sides of a market so they can be de-vigged together.

    Totals share one line (o8.5 / u8.5), but a spread's sides are mirrored
    (home -1.5 / away +1.5). Grouping spreads on the magnitude keeps that pair
    together; each selection still reports its own signed line.
    """
    if line is None:
        return None
    if "spread" in market_type.strip().lower():
        return abs(Decimal(str(line)))
    return Decimal(str(line))


def _selection_entry(row: DatabaseRow) -> dict[str, Any]:
    odds = Decimal(str(row["odds"]))
    implied = american_implied_probability(odds)
    return {
        "selection": str(row["selection"]),
        "line": _decimal_text(row["line"]),
        "bookmaker": str(row["bookmaker"]),
        "odds": _decimal_text(odds),
        "odds_format": str(row["odds_format"]),
        "implied_probability": _probability_text(implied),
        "odds_timestamp": _iso(row["odds_timestamp"]),
        "api_fetched_at": _iso(row["api_fetched_at"]),
        "source_snapshot_hash": str(row["source_snapshot_hash"]),
        "_odds": odds,
        "_implied": implied,
    }


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _consensus_probabilities(
    quotes: list[dict[str, Any]],
    selections: list[str],
) -> tuple[dict[str, Decimal], list[str], Decimal | None]:
    """De-vig each book's own market, then take the median across books.

    A book's margin lives in *its* pair of prices. De-vigging each book on its
    own and taking the median of the resulting fair probabilities keeps one
    book's margin out of the consensus, and the median keeps a single stale or
    outlying book from dragging it. Only books quoting every selection are
    counted: a partial quote has no margin to remove.

    The medians of a set of normalized vectors need not sum to one, so they are
    renormalized and the pre-normalization sum is returned for publication --
    a large deviation means the books disagree, and that is worth seeing rather
    than smoothing away.
    """
    by_book: dict[str, dict[str, Decimal]] = {}
    for quote in quotes:
        if quote["_implied"] is None:
            continue
        by_book.setdefault(quote["bookmaker"], {})
        # A book that posts the same selection twice keeps its better price,
        # matching how a bettor would actually take it.
        current = by_book[quote["bookmaker"]].get(quote["selection"])
        if current is None or quote["_implied"] < current:
            by_book[quote["bookmaker"]][quote["selection"]] = quote["_implied"]

    per_selection: dict[str, list[Decimal]] = {selection: [] for selection in selections}
    contributing: list[str] = []
    for bookmaker, implied_by_selection in sorted(by_book.items()):
        if any(selection not in implied_by_selection for selection in selections):
            continue
        ordered = [implied_by_selection[selection] for selection in selections]
        try:
            result = remove_margin(ordered, method=DEFAULT_DEVIG_METHOD)
        except (ValueError, ArithmeticError):
            continue
        if not result.converged:
            continue
        contributing.append(bookmaker)
        for selection, probability in zip(selections, result.probabilities, strict=True):
            per_selection[selection].append(probability)

    if not contributing:
        return {}, [], None

    medians = {selection: _median(values) for selection, values in per_selection.items()}
    total = sum(medians.values(), Decimal(0))
    if total <= 0:
        return {}, [], None
    return {selection: value / total for selection, value in medians.items()}, contributing, total


def _build_market(
    *,
    market_type: str,
    line: Any,
    quotes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Collapse every posted quote for one market into a comparable view.

    Each distinct selection keeps its best available price, so `best_odds` is a
    real line-shopping result rather than whichever book happened to sort first.

    Two fair-probability readings are published, because they answer different
    questions and disagree in a specific direction. `no_vig_probability` de-vigs
    the best price of each selection, which is what a bettor shopping every book
    can reach; those best prices carry less margin than any single book posts, so
    that reading flatters itself and can even imply an arbitrage. The consensus
    de-vigs each book on its own and takes the median, which is the market's
    estimate rather than the shopper's. The gap between the best available price
    and the consensus is the number a decision would rest on, and it is published
    as a price comparison -- not as a validated edge.
    """
    best_by_selection: dict[str, dict[str, Any]] = {}
    books: set[str] = set()
    for quote in quotes:
        books.add(quote["bookmaker"])
        selection = quote["selection"]
        incumbent = best_by_selection.get(selection)
        if incumbent is None or _better_price(quote["_odds"], incumbent["_odds"]):
            best_by_selection[selection] = quote

    selections = sorted(best_by_selection.values(), key=lambda entry: entry["selection"])
    implied_values = [entry["_implied"] for entry in selections]
    overround: Decimal | None = None
    no_vig: list[Decimal | None] = [None] * len(selections)
    devig_method: str | None = None
    devig_disagreement: Decimal | None = None
    if len(selections) >= 2 and all(value is not None for value in implied_values):
        total = sum(implied_values, Decimal(0))
        if total > 0:
            overround = total - Decimal(1)
            # Shin removes proportionally more margin from longshots than
            # proportional normalization does, which is the direction the
            # favourite-longshot literature supports. The disagreement across
            # methods is published alongside it: on skewed markets it can exceed
            # a candidate edge, and an "edge" smaller than it is a statement
            # about the de-vig assumption rather than about the game.
            try:
                result = remove_margin(implied_values, method=DEFAULT_DEVIG_METHOD)
                no_vig = list(result.probabilities)
                devig_method = result.method
                devig_disagreement = method_disagreement(implied_values)
            except (ValueError, ArithmeticError):
                no_vig = [value / total for value in implied_values]
                devig_method = "multiplicative"

    selection_names = [entry["selection"] for entry in selections]
    consensus, consensus_books, consensus_raw_total = _consensus_probabilities(quotes, selection_names)

    priced_selections = []
    for entry, fair in zip(selections, no_vig, strict=True):
        matching = [quote for quote in quotes if quote["selection"] == entry["selection"]]
        worst = min(matching, key=lambda quote: quote["_odds"])
        # What shopping is worth: the implied probability a bettor stops paying by
        # taking the best posted price instead of the worst one.
        shopping_gain = None
        if worst["_implied"] is not None and entry["_implied"] is not None:
            shopping_gain = worst["_implied"] - entry["_implied"]
        consensus_probability = consensus.get(entry["selection"])
        # Positive means the best posted price implies less probability than the
        # books' own consensus fair value -- the price is cheaper than the market
        # thinks the outcome is worth. It is a comparison between prices, and it
        # says nothing about whether the consensus itself is right.
        best_price_gap = None
        if consensus_probability is not None and entry["_implied"] is not None:
            best_price_gap = consensus_probability - entry["_implied"]
        priced_selections.append(
            {
                "selection": entry["selection"],
                "line": entry["line"],
                "best_odds": entry["odds"],
                "best_bookmaker": entry["bookmaker"],
                "worst_odds": worst["odds"],
                "worst_bookmaker": worst["bookmaker"],
                "line_shopping_gain_probability": _probability_text(shopping_gain),
                "odds_format": entry["odds_format"],
                "implied_probability": entry["implied_probability"],
                "no_vig_probability": _probability_text(fair),
                "consensus_probability": _probability_text(consensus_probability),
                "best_price_vs_consensus_probability": _probability_text(best_price_gap),
                "quoted_by": sorted({quote["bookmaker"] for quote in matching}),
                "quote_count": len(matching),
                "odds_timestamp": entry["odds_timestamp"],
                "api_fetched_at": entry["api_fetched_at"],
                "source_snapshot_hash": entry["source_snapshot_hash"],
            }
        )

    return {
        "market_type": market_type,
        "line": _decimal_text(line),
        "bookmakers": sorted(books),
        "bookmaker_count": len(books),
        "overround": _probability_text(overround),
        "no_vig_available": overround is not None,
        "no_vig_method": devig_method,
        "no_vig_method_disagreement": _probability_text(devig_disagreement),
        "consensus_available": bool(consensus),
        "consensus_method": f"per_book_{DEFAULT_DEVIG_METHOD}_median" if consensus else None,
        "consensus_bookmakers": consensus_books,
        "consensus_bookmaker_count": len(consensus_books),
        "consensus_median_sum_before_normalization": _probability_text(consensus_raw_total),
        "selections": priced_selections,
    }


def _build_events(rows: list[DatabaseRow], *, now: datetime) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        event_id = str(row["event_id"])
        event = grouped.get(event_id)
        if event is None:
            start_time = row["game_start_time"]
            event = grouped[event_id] = {
                "event_id": event_id,
                "sport": str(row["sport"]),
                "league": str(row["league"]),
                "home_team": str(row["home_team"]),
                "away_team": str(row["away_team"]),
                "game_start_time": _iso(start_time),
                "_start": start_time,
                "_markets": {},
            }
        market_type = str(row["market_type"])
        key = (market_type, _line_group(market_type, row["line"]))
        event["_markets"].setdefault(key, []).append(_selection_entry(row))

    events: list[dict[str, Any]] = []
    for event in grouped.values():
        start_time = event.pop("_start")
        markets_by_key: dict[tuple[str, Any], list[dict[str, Any]]] = event.pop("_markets")
        markets = [
            _build_market(market_type=market_type, line=line, quotes=quotes)
            for (market_type, line), quotes in markets_by_key.items()
        ]
        markets.sort(key=lambda market: (_market_sort_key(market["market_type"]), market["market_type"], market["line"] or ""))
        seconds_to_start = int((start_time - now).total_seconds()) if start_time else None
        events.append(
            {
                **event,
                "seconds_to_start": seconds_to_start,
                "market_count": len(markets),
                "markets": markets,
            }
        )
    events.sort(key=lambda event: (event["game_start_time"] or "", event["event_id"]))
    return events


def _board_state(
    *,
    events: list[dict[str, Any]],
    latest_source_fetched_at: datetime | None,
    source_health: list[dict[str, Any]],
    now: datetime,
    stale_after_seconds: int,
) -> tuple[str, str]:
    """Name the board's condition explicitly; a stale cache is never 'fresh'."""
    blocked = [entry for entry in source_health if entry["freshness_state"] in {"blocked", "failed"}]
    if blocked:
        reason = blocked[0]["last_error"] or blocked[0]["freshness_state"]
        return "blocked", f"sports_source_{blocked[0]['freshness_state']}:{reason}"
    if latest_source_fetched_at is None:
        return "unavailable", "no_sports_rows_uploaded"
    age_seconds = (now - latest_source_fetched_at).total_seconds()
    if age_seconds > stale_after_seconds:
        return "stale", f"sports_source_older_than_{stale_after_seconds}s"
    if not events:
        return "empty", "no_upcoming_events_with_posted_odds"
    return "fresh", "sports_source_within_freshness_window"


def load_sports_board(
    *,
    settings: DatabaseSettings | None = None,
    now: datetime | str | None = None,
    max_events: int = DEFAULT_MAX_EVENTS,
    stale_after_seconds: int = BOARD_STALE_SECONDS,
) -> dict[str, Any]:
    """Build the current sports board from rows already uploaded to PostgreSQL."""
    configured = ensure_database_ready(settings)
    moment = _now(now)
    bounded_events = max(1, min(int(max_events), 500))
    with connection_pool(configured).connection() as connection:
        rows = _rows_for_board(connection, now=moment, max_events=bounded_events)
        latest_fetched = _latest_upload_time(connection)
        source_health = _source_health(connection)
        worker = _worker_state(connection)

    events = _build_events(rows, now=moment)
    state, reason = _board_state(
        events=events,
        latest_source_fetched_at=latest_fetched,
        source_health=source_health,
        now=moment,
        stale_after_seconds=stale_after_seconds,
    )
    is_current = state == "fresh"
    return {
        "asset_class": "sports",
        "generated_at": moment.isoformat(),
        "board_state": state,
        "state_reason": reason,
        "is_current": is_current,
        "stale_after_seconds": stale_after_seconds,
        "latest_source_fetched_at": _iso(latest_fetched),
        "source_age_seconds": int((moment - latest_fetched).total_seconds()) if latest_fetched else None,
        "events": events if is_current else [],
        "withheld_event_count": 0 if is_current else len(events),
        "event_count": len(events) if is_current else 0,
        "quote_count": len(rows) if is_current else 0,
        "source_health": source_health,
        "worker": worker,
        "model_state": "baseline_only",
        "decision_status": "track_only",
        "probability_source": "bookmaker_implied",
        "disclaimer": (
            "No-vig probabilities and best available prices are observations of posted "
            "bookmaker markets. They are a baseline, not a validated model edge, and "
            "never a betting recommendation."
        ),
    }


def summarize_sports_board(board: Mapping[str, Any]) -> dict[str, Any]:
    """Compact counters for status panels that should not embed the full board."""
    events = board.get("events") or []
    market_count = sum(int(event.get("market_count") or 0) for event in events)
    no_vig_markets = sum(
        1
        for event in events
        for market in event.get("markets") or []
        if market.get("no_vig_available")
    )
    multi_book_markets = sum(
        1
        for event in events
        for market in event.get("markets") or []
        if int(market.get("bookmaker_count") or 0) > 1
    )
    disagreements = [
        Decimal(str(market.get("no_vig_method_disagreement")))
        for event in events
        for market in event.get("markets") or []
        if market.get("no_vig_method_disagreement") is not None
    ]
    consensus_markets = sum(
        1
        for event in events
        for market in event.get("markets") or []
        if market.get("consensus_available")
    )
    best_price_gaps = [
        Decimal(str(selection["best_price_vs_consensus_probability"]))
        for event in events
        for market in event.get("markets") or []
        for selection in market.get("selections") or []
        if selection.get("best_price_vs_consensus_probability") is not None
    ]
    next_event = events[0] if events else None
    return {
        "board_state": board.get("board_state"),
        "state_reason": board.get("state_reason"),
        "is_current": bool(board.get("is_current")),
        "event_count": len(events),
        "market_count": market_count,
        "no_vig_market_count": no_vig_markets,
        "no_vig_method": DEFAULT_DEVIG_METHOD,
        "max_no_vig_method_disagreement": None if not disagreements else format(max(disagreements), "f"),
        "consensus_market_count": consensus_markets,
        "best_price_vs_consensus_max": None if not best_price_gaps else format(max(best_price_gaps), "f"),
        "line_shopping_market_count": multi_book_markets,
        "latest_source_fetched_at": board.get("latest_source_fetched_at"),
        "source_age_seconds": board.get("source_age_seconds"),
        "next_event": None
        if next_event is None
        else {
            "event_id": next_event.get("event_id"),
            "league": next_event.get("league"),
            "home_team": next_event.get("home_team"),
            "away_team": next_event.get("away_team"),
            "game_start_time": next_event.get("game_start_time"),
        },
    }
