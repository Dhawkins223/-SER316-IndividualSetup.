"""Closing line value for the sports rows uploaded to PostgreSQL.

`app.sports_prediction_logs` has carried `closing_line` and `clv` columns since
the first migration, but nothing ever wrote them. This module fills them.

Closing line value is the honest, research-only way to ask whether a recorded
price was any good: it compares the price on a row against the last price the
market posted before the game started. It says nothing about profit and needs no
settled outcome, so it stays inside the research-only contract while still being
the measure that separates a real read from a lucky one.

Sign convention matches the rest of the board -- CLV is stated in probability
points and is positive when the taken price implied *less* probability than the
close, meaning the market moved toward that side after the row was recorded.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from .business_store import ensure_database_ready
from .database import DatabaseSettings, connection_pool
from .sports_board import american_implied_probability


CLV_SCALE = Decimal("0.000000000001")
PROBABILITY_DISPLAY_SCALE = Decimal("0.000001")


def _now(now: datetime | str | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(now).replace("Z", "+00:00")).astimezone(timezone.utc)


def _decimal_text(value: Any, *, scale: Decimal | None = None) -> str | None:
    if value is None:
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if scale is not None:
        number = number.quantize(scale)
    return format(number, "f")


def _mean_interval(average: Any, stddev: Any, sample: int) -> list[str] | None:
    """The 95% interval on a mean CLV, or ``None`` when there is not one.

    Two rows is the floor: a single row has no spread to measure, and Postgres
    returns null for ``STDDEV_SAMP`` there rather than pretending to zero. A
    zero-width interval on one observation would be the most confident claim on
    the panel and the least supported.

    Normal approximation on the sample mean. CLV per row is a bounded difference
    of two probabilities rather than anything heavy-tailed, so the central limit
    theorem is doing honest work by the time this panel has rows to show.
    """

    if average is None or stddev is None or sample < 2:
        return None
    mean = average if isinstance(average, Decimal) else Decimal(str(average))
    spread = stddev if isinstance(stddev, Decimal) else Decimal(str(stddev))
    margin = Decimal("1.96") * spread / Decimal(sample).sqrt()
    return [
        format((mean - margin).quantize(CLV_SCALE), "f"),
        format((mean + margin).quantize(CLV_SCALE), "f"),
    ]


def closing_line_value(taken_odds: Decimal, closing_odds: Decimal) -> Decimal | None:
    """Probability points gained against the close, positive when the market came to you."""
    taken = american_implied_probability(taken_odds)
    closing = american_implied_probability(closing_odds)
    if taken is None or closing is None:
        return None
    return (closing - taken).quantize(CLV_SCALE)


def _closing_rows(connection: Any, *, now: datetime, run_id: str | None) -> list[Mapping[str, Any]]:
    """The last price each book posted before kickoff for every started market.

    A price can only be beaten by the close of the book that posted it. Books do
    not close at the same number, so collapsing the close across books grades one
    book's row against whichever book happened to quote last: it credits a row
    with movement that happened where the bettor never held the price, and turns
    the per-bookmaker breakdown into a comparison of each book against a rival.
    The close therefore stays inside one
    (event, market, selection, line, bookmaker) series. Rows quoted after kickoff
    are excluded outright -- a live price is not a closing line.
    """
    return connection.execute(
        """
        SELECT DISTINCT ON (event_id, market_type, selection, line, bookmaker)
               event_id, market_type, selection, line, bookmaker, odds AS closing_odds,
               odds_timestamp AS closing_odds_timestamp
        FROM app.sports_prediction_logs
        WHERE validation_status = 'valid'
          AND game_start_time <= %s
          AND odds_timestamp < game_start_time
          AND (%s::text IS NULL OR run_id = %s)
        ORDER BY event_id, market_type, selection, line, bookmaker, odds_timestamp DESC, id DESC
        """,
        (now, run_id, run_id),
    ).fetchall()


def capture_sports_closing_lines(
    *,
    run_id: str | None = None,
    settings: DatabaseSettings | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Write `closing_line` and `clv` for every started market that has a close.

    Idempotent by construction: a repeat call recomputes the same values and
    reports zero updated rows, because the update only touches rows whose stored
    closing line or CLV differs from the recomputed one.
    """
    configured = ensure_database_ready(settings)
    moment = _now(now)
    markets_closed = 0
    rows_updated = 0
    rows_unchanged = 0

    with connection_pool(configured).connection() as connection:
        closes = _closing_rows(connection, now=moment, run_id=run_id)
        for close in closes:
            closing_odds = Decimal(str(close["closing_odds"]))
            markets_closed += 1
            priced = connection.execute(
                """
                SELECT id, odds, closing_line, clv
                FROM app.sports_prediction_logs
                WHERE validation_status = 'valid'
                  AND event_id = %s
                  AND market_type = %s
                  AND selection = %s
                  AND line IS NOT DISTINCT FROM %s
                  AND bookmaker = %s
                  AND odds_timestamp < game_start_time
                  AND (%s::text IS NULL OR run_id = %s)
                """,
                (
                    close["event_id"],
                    close["market_type"],
                    close["selection"],
                    close["line"],
                    close["bookmaker"],
                    run_id,
                    run_id,
                ),
            ).fetchall()
            for row in priced:
                clv = closing_line_value(Decimal(str(row["odds"])), closing_odds)
                stored_closing = None if row["closing_line"] is None else Decimal(str(row["closing_line"]))
                stored_clv = None if row["clv"] is None else Decimal(str(row["clv"]))
                if stored_closing == closing_odds and stored_clv == clv:
                    rows_unchanged += 1
                    continue
                connection.execute(
                    "UPDATE app.sports_prediction_logs SET closing_line = %s, clv = %s WHERE id = %s",
                    (closing_odds, clv, row["id"]),
                )
                rows_updated += 1

    return {
        "asset_class": "sports",
        "run_id": run_id,
        "evaluated_at": moment.isoformat(),
        "markets_closed": markets_closed,
        "rows_updated": rows_updated,
        "rows_unchanged": rows_unchanged,
    }


def build_sports_clv_report(
    *,
    run_id: str | None = None,
    settings: DatabaseSettings | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Summarize recorded CLV without claiming profitability.

    Beat rate is reported over rows that actually have a closing line, and rows
    that closed at exactly the taken price are counted separately rather than
    folded into either side.
    """
    configured = ensure_database_ready(settings)
    bounded_limit = max(1, min(int(limit), 200))
    with connection_pool(configured).connection() as connection:
        totals = connection.execute(
            """
            SELECT COUNT(*) AS graded_rows,
                   COUNT(*) FILTER (WHERE clv > 0) AS beat_close,
                   COUNT(*) FILTER (WHERE clv < 0) AS lost_to_close,
                   COUNT(*) FILTER (WHERE clv = 0) AS matched_close,
                   AVG(clv) AS average_clv,
                   -- The spread of individual CLVs, without which the average is
                   -- a point with no scale: "+1.2 points" over twelve rows that
                   -- ranged from -8 to +11 is not a read, and the panel cannot
                   -- tell the difference from the average alone. Null on a
                   -- single row, which is correct -- one row has no spread.
                   STDDEV_SAMP(clv) AS clv_stddev,
                   SUM(clv) AS total_clv
            FROM app.sports_prediction_logs
            WHERE validation_status = 'valid'
              AND clv IS NOT NULL
              AND (%s::text IS NULL OR run_id = %s)
            """,
            (run_id, run_id),
        ).fetchone()
        pending = connection.execute(
            """
            SELECT COUNT(*) AS pending_rows
            FROM app.sports_prediction_logs
            WHERE validation_status = 'valid'
              AND clv IS NULL
              AND (%s::text IS NULL OR run_id = %s)
            """,
            (run_id, run_id),
        ).fetchone()
        by_market = connection.execute(
            """
            SELECT market_type,
                   COUNT(*) AS graded_rows,
                   COUNT(*) FILTER (WHERE clv > 0) AS beat_close,
                   AVG(clv) AS average_clv
            FROM app.sports_prediction_logs
            WHERE validation_status = 'valid'
              AND clv IS NOT NULL
              AND (%s::text IS NULL OR run_id = %s)
            GROUP BY market_type
            ORDER BY market_type
            """,
            (run_id, run_id),
        ).fetchall()
        by_book = connection.execute(
            """
            SELECT bookmaker,
                   COUNT(*) AS graded_rows,
                   COUNT(*) FILTER (WHERE clv > 0) AS beat_close,
                   AVG(clv) AS average_clv
            FROM app.sports_prediction_logs
            WHERE validation_status = 'valid'
              AND clv IS NOT NULL
              AND (%s::text IS NULL OR run_id = %s)
            GROUP BY bookmaker
            ORDER BY AVG(clv) DESC, bookmaker
            LIMIT %s
            """,
            (run_id, run_id, bounded_limit),
        ).fetchall()

    graded = int(totals["graded_rows"] or 0)
    beat = int(totals["beat_close"] or 0)
    lost = int(totals["lost_to_close"] or 0)
    matched = int(totals["matched_close"] or 0)
    decided = beat + lost
    return {
        "asset_class": "sports",
        "run_id": run_id,
        "graded_rows": graded,
        "pending_rows": int(pending["pending_rows"] or 0),
        "beat_close": beat,
        "lost_to_close": lost,
        "matched_close": matched,
        "beat_rate": None if decided == 0 else _decimal_text(
            Decimal(beat) / Decimal(decided), scale=PROBABILITY_DISPLAY_SCALE
        ),
        "beat_rate_denominator": decided,
        "average_clv": _decimal_text(totals["average_clv"], scale=CLV_SCALE),
        # The average alone cannot say whether the edge it describes is real.
        # CLV per row is noisy in both directions, so at the sample sizes this
        # panel sees, an average of +1.2 points and an average of +1.2 points
        # that straddles zero look identical -- and the panel paints itself green
        # on the sign. This is the interval that separates them.
        "average_clv_interval": _mean_interval(totals["average_clv"], totals["clv_stddev"], graded),
        "average_clv_sample": graded,
        "total_clv": _decimal_text(totals["total_clv"], scale=CLV_SCALE),
        "by_market": [
            {
                "market_type": str(row["market_type"]),
                "graded_rows": int(row["graded_rows"]),
                "beat_close": int(row["beat_close"]),
                "average_clv": _decimal_text(row["average_clv"], scale=CLV_SCALE),
            }
            for row in by_market
        ],
        "by_bookmaker": [
            {
                "bookmaker": str(row["bookmaker"]),
                "graded_rows": int(row["graded_rows"]),
                "beat_close": int(row["beat_close"]),
                "average_clv": _decimal_text(row["average_clv"], scale=CLV_SCALE),
            }
            for row in by_book
        ],
        "model_state": "baseline_only",
        "decision_status": "track_only",
        "measure": "probability_points_versus_closing_line",
        "disclaimer": (
            "Closing line value compares a recorded price against the last price posted "
            "before the game started. It is not profit, not a settled result, and not "
            "evidence that a model is validated or profitable."
        ),
    }


def render_sports_clv_report(report: Mapping[str, Any]) -> str:
    lines = [
        "Sports Closing Line Value",
        f"Run: {report.get('run_id') or 'all runs'}",
        f"Graded rows: {report.get('graded_rows')}",
        f"Pending rows (no close yet): {report.get('pending_rows')}",
        f"Beat close / lost / matched: {report.get('beat_close')} / {report.get('lost_to_close')} / {report.get('matched_close')}",
        f"Beat rate: {report.get('beat_rate') or 'unavailable / no graded rows'}",
        f"Average CLV (probability points): {report.get('average_clv') or 'n/a'}",
        "",
        "By market:",
    ]
    for entry in report.get("by_market") or []:
        lines.append(
            f"  {entry['market_type']}: {entry['graded_rows']} rows, "
            f"{entry['beat_close']} beat close, average {entry['average_clv']}"
        )
    lines.extend(["", "By bookmaker:"])
    for entry in report.get("by_bookmaker") or []:
        lines.append(
            f"  {entry['bookmaker']}: {entry['graded_rows']} rows, "
            f"{entry['beat_close']} beat close, average {entry['average_clv']}"
        )
    lines.extend(["", str(report.get("disclaimer") or "")])
    return "\n".join(lines)
