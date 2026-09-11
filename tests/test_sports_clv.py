from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kalshi_research_bot.sports_board import american_implied_probability
from kalshi_research_bot.sports_clv import (
    _mean_interval,
    build_sports_clv_report,
    capture_sports_closing_lines,
    closing_line_value,
    render_sports_clv_report,
)
from kalshi_research_bot.sports_research import log_sports_predictions, normalize_the_odds_api_payload

from tests.postgres_support import PostgresTestCase


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SportsClvTests(PostgresTestCase):
    def _log_quote(
        self,
        *,
        run_id: str = "clv",
        home_price: int,
        away_price: int,
        minutes_before_start: float,
        start_offset_hours: float = -1,
        bookmaker: str = "book_a",
        event_id: str = "g1",
    ) -> dict:
        """Record one collection cycle for a game that starts `start_offset_hours` from now."""
        now = _now()
        start = now + timedelta(hours=start_offset_hours)
        fetched = (start - timedelta(minutes=minutes_before_start)).isoformat()
        source = [
            {
                "id": event_id,
                "commence_time": start.isoformat(),
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": bookmaker,
                        "last_update": fetched,
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": home_price},
                                    {"name": "Away", "price": away_price},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
        records = normalize_the_odds_api_payload(
            source, sport="baseball_mlb", league="mlb", api_fetched_at=fetched
        )
        return log_sports_predictions(
            run_id=run_id,
            payload={
                "asset_class": "sports",
                "model_version": "sports_odds_research_v1",
                "strategy": "pregame_odds_snapshot_v1",
                "generated_at": fetched,
                "records": records,
            },
        )

    def test_closing_line_value_is_probability_points_against_the_close(self) -> None:
        # Took -110, market closed -130: the market moved toward this side.
        gained = closing_line_value(Decimal("-110"), Decimal("-130"))
        self.assertGreater(gained, 0)
        expected = american_implied_probability(Decimal("-130")) - american_implied_probability(Decimal("-110"))
        self.assertEqual(gained, expected.quantize(Decimal("0.000000000001")))

        # Took -130, market closed -110: the market moved away.
        self.assertLess(closing_line_value(Decimal("-130"), Decimal("-110")), 0)
        # Closed where it was taken.
        self.assertEqual(closing_line_value(Decimal("-110"), Decimal("-110")), Decimal("0").quantize(Decimal("0.000000000001")))

    def test_started_games_get_a_closing_line_and_clv(self) -> None:
        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120)
        self._log_quote(home_price=-130, away_price=110, minutes_before_start=10)

        capture = capture_sports_closing_lines(run_id="clv")
        self.assertEqual(capture["markets_closed"], 2)
        self.assertEqual(capture["rows_updated"], 4)

        rows = self.query_all(
            "SELECT selection, odds, closing_line, clv FROM app.sports_prediction_logs ORDER BY selection, odds"
        )
        self.assertTrue(all(row["closing_line"] is not None and row["clv"] is not None for row in rows))
        by_key = {(str(row["selection"]), Decimal(str(row["odds"]))): row for row in rows}

        # The Home side opened -110 and closed -130, so the earlier price beat the close.
        opened_home = by_key[("Home", Decimal("-110"))]
        self.assertEqual(Decimal(str(opened_home["closing_line"])), Decimal("-130"))
        self.assertGreater(Decimal(str(opened_home["clv"])), 0)

        # The Away side opened -110 and closed +110, so the earlier price lost to the close.
        opened_away = by_key[("Away", Decimal("-110"))]
        self.assertLess(Decimal(str(opened_away["clv"])), 0)

        # A closing quote is its own close and grades at exactly zero.
        closing_home = by_key[("Home", Decimal("-130"))]
        self.assertEqual(Decimal(str(closing_home["clv"])), 0)

    def test_each_book_is_graded_against_its_own_close(self) -> None:
        """A price is only beaten by the close its own book posted.

        Books close at different numbers. Grading every book's rows against
        whichever book happened to post last credits a row with movement that
        happened somewhere the bettor never had the price, and makes the
        per-bookmaker breakdown compare each book against a rival.
        """

        # book_a barely moves: -110 -> -112. book_b moves hard: -110 -> -160,
        # and posts last, so it owns the slate's final pre-start quote.
        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120, bookmaker="book_a")
        self._log_quote(home_price=-112, away_price=-108, minutes_before_start=20, bookmaker="book_a")
        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120, bookmaker="book_b")
        self._log_quote(home_price=-160, away_price=140, minutes_before_start=10, bookmaker="book_b")

        capture_sports_closing_lines(run_id="clv")
        rows = self.query_all(
            """
            SELECT bookmaker, selection, odds, closing_line, clv
            FROM app.sports_prediction_logs
            WHERE selection = 'Home'
            ORDER BY bookmaker, odds DESC
            """
        )
        closes = {
            (str(row["bookmaker"]), Decimal(str(row["odds"]))): Decimal(str(row["closing_line"]))
            for row in rows
        }
        self.assertEqual(closes[("book_a", Decimal("-110"))], Decimal("-112"))
        self.assertEqual(closes[("book_a", Decimal("-112"))], Decimal("-112"))
        self.assertEqual(closes[("book_b", Decimal("-110"))], Decimal("-160"))
        self.assertEqual(closes[("book_b", Decimal("-160"))], Decimal("-160"))

        by_book = {
            str(row["bookmaker"]): Decimal(str(row["clv"]))
            for row in rows
            if Decimal(str(row["odds"])) == Decimal("-110")
        }
        # Both books opened -110; only book_b's market actually moved that far.
        self.assertLess(by_book["book_a"], by_book["book_b"])
        self.assertEqual(
            by_book["book_a"],
            closing_line_value(Decimal("-110"), Decimal("-112")),
        )

    def test_capture_is_idempotent(self) -> None:
        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120)
        self._log_quote(home_price=-130, away_price=110, minutes_before_start=10)

        first = capture_sports_closing_lines(run_id="clv")
        second = capture_sports_closing_lines(run_id="clv")

        self.assertGreater(first["rows_updated"], 0)
        self.assertEqual(second["rows_updated"], 0)
        self.assertEqual(second["rows_unchanged"], first["rows_updated"])

    def test_upcoming_games_are_not_graded(self) -> None:
        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=30, start_offset_hours=4)

        capture = capture_sports_closing_lines(run_id="clv")
        self.assertEqual(capture["markets_closed"], 0)
        self.assertEqual(capture["rows_updated"], 0)

        report = build_sports_clv_report(run_id="clv")
        self.assertEqual(report["graded_rows"], 0)
        self.assertEqual(report["pending_rows"], 2)
        self.assertIsNone(report["beat_rate"])

    def test_a_later_close_supersedes_an_earlier_one(self) -> None:
        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120)
        capture_sports_closing_lines(run_id="clv")

        # A quote collected closer to kickoff becomes the new close.
        self._log_quote(home_price=-150, away_price=130, minutes_before_start=5)
        recapture = capture_sports_closing_lines(run_id="clv")
        self.assertGreater(recapture["rows_updated"], 0)

        opened = self.query_one(
            "SELECT closing_line, clv FROM app.sports_prediction_logs WHERE selection = 'Home' AND odds = %s",
            (Decimal("-110"),),
        )
        self.assertEqual(Decimal(str(opened["closing_line"])), Decimal("-150"))

    def test_report_separates_beat_lost_and_matched(self) -> None:
        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120)
        self._log_quote(home_price=-130, away_price=110, minutes_before_start=10)
        capture_sports_closing_lines(run_id="clv")

        report = build_sports_clv_report(run_id="clv")
        self.assertEqual(report["graded_rows"], 4)
        self.assertEqual(report["beat_close"], 1)
        self.assertEqual(report["lost_to_close"], 1)
        self.assertEqual(report["matched_close"], 2)
        self.assertEqual(report["beat_rate_denominator"], 2)
        self.assertEqual(Decimal(report["beat_rate"]), Decimal("0.5"))
        self.assertEqual(report["model_state"], "baseline_only")
        self.assertEqual(report["decision_status"], "track_only")

        self.assertEqual([entry["market_type"] for entry in report["by_market"]], ["moneyline"])
        self.assertEqual([entry["bookmaker"] for entry in report["by_bookmaker"]], ["book_a"])

    def test_the_average_carries_its_spread_and_its_interval(self) -> None:
        """An average CLV with no interval is a point with no scale.

        Four graded rows here, two of them exactly at the close, so the spread
        is real and the interval has to be wide enough to show it.
        """

        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120)
        self._log_quote(home_price=-130, away_price=110, minutes_before_start=10)
        capture_sports_closing_lines(run_id="clv")

        report = build_sports_clv_report(run_id="clv")
        self.assertEqual(report["average_clv_sample"], 4)
        low, high = (Decimal(bound) for bound in report["average_clv_interval"])
        self.assertLess(low, Decimal(report["average_clv"]))
        self.assertGreater(high, Decimal(report["average_clv"]))
        # Two of four rows matched the close exactly and two moved in opposite
        # directions, so nothing here distinguishes the average from zero.
        self.assertLess(low, 0)

    def test_report_scopes_to_a_run_when_asked(self) -> None:
        self._log_quote(run_id="run_a", home_price=-110, away_price=-110, minutes_before_start=120)
        self._log_quote(run_id="run_a", home_price=-130, away_price=110, minutes_before_start=10)
        self._log_quote(run_id="run_b", home_price=-105, away_price=-115, minutes_before_start=90, event_id="g2")
        self._log_quote(run_id="run_b", home_price=-140, away_price=120, minutes_before_start=8, event_id="g2")
        capture_sports_closing_lines()

        self.assertEqual(build_sports_clv_report(run_id="run_a")["graded_rows"], 4)
        self.assertEqual(build_sports_clv_report(run_id="run_b")["graded_rows"], 4)
        self.assertEqual(build_sports_clv_report()["graded_rows"], 8)

    def test_exact_values_are_serialized_as_fixed_point_strings(self) -> None:
        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120)
        self._log_quote(home_price=-130, away_price=110, minutes_before_start=10)
        capture_sports_closing_lines(run_id="clv")

        report = build_sports_clv_report(run_id="clv")
        self.assertIsInstance(report["average_clv"], str)
        self.assertNotIn("e", report["average_clv"].lower())
        Decimal(report["average_clv"])
        Decimal(report["total_clv"])

    def test_rendered_report_states_it_is_not_profit(self) -> None:
        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120)
        self._log_quote(home_price=-130, away_price=110, minutes_before_start=10)
        capture_sports_closing_lines(run_id="clv")

        rendered = render_sports_clv_report(build_sports_clv_report(run_id="clv"))
        self.assertIn("Sports Closing Line Value", rendered)
        self.assertIn("not profit", rendered)

    def test_dashboard_panel_reports_both_states(self) -> None:
        from kalshi_research_bot.paper_server import render_sports_clv_panel

        empty = render_sports_clv_panel(build_sports_clv_report(run_id="clv"))
        self.assertIn("No closing lines recorded", empty)

        self._log_quote(home_price=-110, away_price=-110, minutes_before_start=120)
        self._log_quote(home_price=-130, away_price=110, minutes_before_start=10)
        capture_sports_closing_lines(run_id="clv")

        graded = render_sports_clv_panel(build_sports_clv_report(run_id="clv"))
        self.assertIn("Closing line value", graded)
        self.assertIn("Beat close", graded)
        self.assertIn("Not profit", graded)


class MeanIntervalTests(unittest.TestCase):
    """The interval on an average CLV, which decides the panel's colour."""

    def test_two_rows_are_enough_for_an_interval(self) -> None:
        low, high = _mean_interval(Decimal("0.01"), Decimal("0.02"), 2)
        self.assertLess(Decimal(low), Decimal("0.01"))
        self.assertGreater(Decimal(high), Decimal("0.01"))

    def test_one_row_has_no_interval(self) -> None:
        """Postgres returns null for STDDEV_SAMP on a single row rather than
        pretending to zero, and a zero-width band would be the most confident
        claim on the panel resting on one observation."""

        self.assertIsNone(_mean_interval(Decimal("0.04"), None, 1))
        self.assertIsNone(_mean_interval(Decimal("0.04"), Decimal("0"), 1))

    def test_no_rows_have_no_interval(self) -> None:
        self.assertIsNone(_mean_interval(None, None, 0))

    def test_the_band_narrows_with_the_sample(self) -> None:
        """Same mean, same spread, more rows -- the claim gets tighter."""

        def width(sample: int) -> Decimal:
            low, high = _mean_interval(Decimal("0.01"), Decimal("0.03"), sample)
            return Decimal(high) - Decimal(low)

        self.assertGreater(width(10), width(100))
        self.assertGreater(width(100), width(1000))


def clv_panel_report(**overrides: object) -> dict:
    """The one report both panel-render test classes start from.

    Single-sourced because they are describing the same panel: the colour tests
    vary the interval and the sample, the absence tests vary the average, and a
    second copy of the base row counts would let one class drift into testing a
    panel the other never renders.
    """

    report = {
        "graded_rows": 12, "beat_close": 7, "lost_to_close": 5, "matched_close": 0,
        "beat_rate": "0.583333", "beat_rate_denominator": 12,
        "average_clv": "0.0120", "average_clv_interval": ["0.0080", "0.0160"],
        "average_clv_sample": 200, "pending_rows": 0,
    }
    report.update(overrides)
    return report


class ClvPanelColourTests(unittest.TestCase):
    """Green is this dashboard's word for a result.

    The panel used to take it on the sign of the average alone, so a handful of
    rows averaging positive on no edge at all -- which happens about half the
    time -- painted the same green as a real read.
    """

    def panel(self, interval, sample: int) -> str:
        from kalshi_research_bot.paper_server import render_sports_clv_panel

        return render_sports_clv_panel(
            clv_panel_report(average_clv_interval=interval, average_clv_sample=sample)
        )

    def test_an_interval_clear_of_zero_is_a_result(self) -> None:
        self.assertIn('class="decision good"', self.panel(["0.0080", "0.0160"], 200))

    def test_the_same_average_straddling_zero_is_not(self) -> None:
        """Identical +1.20 pts average -- only the sample tells them apart."""

        self.assertIn('class="decision warning"', self.panel(["-0.0310", "0.0550"], 12))

    def test_no_interval_does_not_inherit_the_benefit_of_the_doubt(self) -> None:
        self.assertIn('class="decision warning"', self.panel(None, 1))

    def test_the_beat_rate_carries_its_decided_sample(self) -> None:
        """Decided rows -- beat plus lost -- not the graded count beside it: a
        row that matched the close decided nothing."""

        self.assertIn("on 12 decided", self.panel(["0.0080", "0.0160"], 200))


class ClvPanelAbsenceTests(unittest.TestCase):
    """A statistic that was not computed must not render as one that was.

    `AVG(clv)` is SQL NULL whenever no row is graded, so `average_clv` of None
    is the report's ordinary empty state rather than a corruption. The headline
    computed its own text with a `0.0` fallback and announced `+0.00 pts` for
    it -- a measured average of exactly zero -- while the per-market and
    per-bookmaker rows a few lines below correctly read `n/a` from
    `_clv_points_text`. Same panel, same statistic, same units, two answers.
    """

    def panel(self, report_overrides: dict) -> str:
        from kalshi_research_bot.paper_server import render_sports_clv_panel

        return render_sports_clv_panel(clv_panel_report(**report_overrides))

    def test_a_missing_average_is_not_a_measured_zero(self) -> None:
        for absent in (None, ""):
            with self.subTest(absent=absent):
                rendered = self.panel({"average_clv": absent})
                self.assertNotIn("+0.00 pts", rendered)
                self.assertIn("n/a", rendered)

    def test_a_missing_average_takes_its_interval_with_it(self) -> None:
        """A confidence interval is a claim about where a point estimate sits.

        Both come from the same report but are separate keys, so a degraded one
        can carry either alone -- and it rendered `+0.00 pts` beside
        `95% CI +0.80 to +1.60 pts`, a point estimate outside its own interval.
        No sample produces that.
        """

        rendered = self.panel({"average_clv": None})
        self.assertNotIn("95% CI +0.80", rendered)

    def test_a_present_average_still_shows_its_interval(self) -> None:
        """The guard above must not suppress the panel's ordinary state."""

        rendered = self.panel({})
        self.assertIn("+1.20 pts", rendered)
        self.assertIn("95% CI +0.80 to +1.60 pts on 200 rows", rendered)

    def test_the_headline_reads_as_english_when_there_is_no_average(self) -> None:
        self.assertIn("No average yet", self.panel({"average_clv": None}))
        self.assertNotIn("n/a average", self.panel({"average_clv": None}))

    def test_no_average_does_not_paint_green_on_its_interval_alone(self) -> None:
        """Green is this dashboard's word for a result.

        The colour is decided by the interval clearing zero, which it does
        independently of whether the average survived. A report degraded enough
        to lose `average_clv` was still painting the success accent -- on an
        interval around a number it did not have.
        """

        self.assertIn('class="decision warning"', self.panel({"average_clv": None}))
        self.assertIn('class="decision good"', self.panel({}))

    def test_a_measured_zero_is_still_shown_as_a_measured_zero(self) -> None:
        """Absence and zero are different answers, and only one is a result."""

        rendered = self.panel({"average_clv": "0.0000"})
        self.assertIn("+0.00 pts", rendered)
