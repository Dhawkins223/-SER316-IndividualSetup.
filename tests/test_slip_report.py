import base64
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from kalshi_research_bot.paper_server import (
    PaperHandler,
    combo_probability_display,
    dollars,
    leg_label,
    plural,
    render_research_record_track,
    render_slip_analysis,
    render_sports_event,
    significant_decimals,
)
from kalshi_research_bot.slip_report import build_slip_analysis, slip_legs_from_payload
from tests.postgres_support import PostgresTestCase

NOW = datetime(2026, 7, 6, 16, 30, tzinfo=timezone.utc)


def leg(
    ticker: str,
    *,
    event: str,
    probability: float = 0.82,
    ask: float = 84.0,
    bid: float = 80.0,
    age_seconds: int = 60,
    state: str = "fresh",
) -> dict:
    return {
        "sport": "MLB",
        "market_ticker": ticker,
        "event_ticker": event,
        "side": "yes",
        "status": "active",
        "probability": probability,
        "market_implied_probability": probability,
        "bid_cents": bid,
        "ask_cents": ask,
        "api_fetched_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        "source_freshness_state": state,
    }


def payload(*legs: dict, action: str = "BUILD_SLIP") -> dict:
    return {
        "generated_at": NOW.isoformat(),
        "custom_slip": {"action": action, "legs": list(legs)},
    }


class LegAdapterTests(unittest.TestCase):
    """The two price choices that decide whether the numbers mean anything."""

    def test_fair_value_is_the_midpoint_and_cost_is_the_ask(self) -> None:
        """Buying at the ask against a mid-fair value is a loss of half the spread.

        The leg must come back with a *negative* edge here. A build that used
        the midpoint on both sides would report zero edge on every leg, which is
        the flattering answer and the wrong one.
        """

        legs, skipped = slip_legs_from_payload([leg("A", event="e1", probability=0.82, bid=80, ask=84)], now=NOW)
        self.assertEqual(skipped, [])
        self.assertEqual(len(legs), 1)
        self.assertAlmostEqual(legs[0].fair_probability, 0.82)
        self.assertAlmostEqual(legs[0].decimal_odds, 100.0 / 84.0)
        self.assertAlmostEqual(legs[0].break_even, 0.84)
        self.assertAlmostEqual(legs[0].edge, 0.82 - 0.84)
        self.assertLess(legs[0].edge, 0.0)

    def test_a_leg_without_a_quoted_ask_is_dropped_not_defaulted(self) -> None:
        row = leg("A", event="e1")
        row["ask_cents"] = None
        legs, skipped = slip_legs_from_payload([row], now=NOW)
        self.assertEqual(legs, [])
        self.assertEqual(skipped, [{"leg_id": "A", "reason": "no_quoted_ask"}])

    def test_a_stale_quote_is_dropped_with_its_state(self) -> None:
        legs, skipped = slip_legs_from_payload([leg("A", event="e1", state="stale")], now=NOW)
        self.assertEqual(legs, [])
        self.assertEqual(skipped[0]["reason"], "source_state_stale")

    def test_an_old_quote_is_dropped_even_when_the_source_looks_healthy(self) -> None:
        legs, skipped = slip_legs_from_payload(
            [leg("A", event="e1", age_seconds=7200, state="fresh")], now=NOW
        )
        self.assertEqual(legs, [])
        self.assertIn("quote_older_than", skipped[0]["reason"])

    def test_an_untimed_quote_is_dropped(self) -> None:
        row = leg("A", event="e1")
        row["api_fetched_at"] = ""
        legs, skipped = slip_legs_from_payload([row], now=NOW)
        self.assertEqual(legs, [])
        self.assertEqual(skipped[0]["reason"], "no_quote_timestamp")


class SlipAnalysisReportTests(unittest.TestCase):
    def test_a_priced_slip_reports_its_arithmetic(self) -> None:
        report = build_slip_analysis(
            payload(leg("A", event="e1"), leg("B", event="e2")), "primary", stake=5.0, now=NOW
        )
        self.assertTrue(report["analysis_available"])
        analysis = report["analysis"]
        # Break-even is exact: two legs at 84c pay 1/(0.84*0.84).
        self.assertAlmostEqual(analysis["break_even_probability"], 0.84 * 0.84)
        self.assertAlmostEqual(analysis["independent_probability"], 0.82 * 0.82)
        self.assertAlmostEqual(analysis["payout_if_won"], 5.0 / (0.84 * 0.84))
        self.assertEqual(analysis["decision_status"], "track_only")
        self.assertEqual(analysis["model_state"], "baseline_only")

    def test_a_slip_with_no_fresh_legs_says_so_rather_than_returning_zeros(self) -> None:
        """"No fresh quotes" and "worth nothing" must not look the same."""

        report = build_slip_analysis(
            payload(leg("A", event="e1", state="stale"), leg("B", event="e2", state="stale")),
            "primary",
            now=NOW,
        )
        self.assertFalse(report["analysis_available"])
        self.assertEqual(report["reason"], "no_priceable_legs")
        self.assertNotIn("analysis", report)
        self.assertEqual(len(report["skipped_legs"]), 2)

    def test_dropped_legs_are_reported_on_a_successful_analysis(self) -> None:
        """Analysing two of three legs is a different slip, and must say so."""

        report = build_slip_analysis(
            payload(
                leg("A", event="e1"),
                leg("B", event="e2"),
                leg("C", event="e3", state="blocked"),
            ),
            "primary",
            now=NOW,
        )
        self.assertTrue(report["analysis_available"])
        self.assertEqual(report["submitted_leg_count"], 3)
        self.assertEqual(report["priced_leg_count"], 2)
        self.assertEqual(report["skipped_legs"], [{"leg_id": "C", "reason": "source_state_blocked"}])

    def test_a_single_leg_slip_is_refused(self) -> None:
        report = build_slip_analysis(payload(leg("A", event="e1")), "primary", now=NOW)
        self.assertFalse(report["analysis_available"])
        self.assertEqual(report["reason"], "single_leg_slip")

    def test_a_slip_the_builder_declined_is_passed_through_not_analysed(self) -> None:
        report = build_slip_analysis(payload(action="NO_SLIP"), "primary", now=NOW)
        self.assertFalse(report["analysis_available"])
        self.assertEqual(report["reason"], "slip_not_built")

    def test_same_market_legs_are_refused_with_the_conflicting_pair_named(self) -> None:
        """Two selections on one market of one game cannot both be modelled."""

        report = build_slip_analysis(
            payload(leg("A", event="e1"), leg("A", event="e1")), "primary", now=NOW
        )
        self.assertFalse(report["analysis_available"])
        self.assertEqual(report["reason"], "unmodellable_slip")
        self.assertEqual(report["conflicting_pairs"], [["A", "A"]])

    def test_same_event_legs_are_marked_unachievable(self) -> None:
        report = build_slip_analysis(
            payload(leg("A", event="one_game"), leg("B", event="one_game")), "primary", now=NOW
        )
        self.assertTrue(report["analysis_available"])
        self.assertFalse(report["analysis"]["expected_value_is_achievable"])
        self.assertIsNotNone(report["analysis"]["same_event_repricing_warning"])

    def test_unknown_slip_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_slip_analysis(payload(), "nonsense", now=NOW)

    def test_a_non_positive_stake_is_rejected(self) -> None:
        for bad in (0.0, -5.0):
            with self.assertRaises(ValueError):
                build_slip_analysis(payload(), "primary", stake=bad, now=NOW)

    def test_a_non_finite_stake_is_rejected(self) -> None:
        """``nan <= 0`` is False, so a positivity check alone lets NaN through.

        It then reaches every figure in the report, and neither NaN nor Infinity
        is a JSON literal -- the response would not parse in a browser.
        """

        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                build_slip_analysis(payload(), "primary", stake=bad, now=NOW)


class SlipAnalysisRenderTests(unittest.TestCase):
    """The card must show its refusals as plainly as its numbers."""

    def report(self, **overrides) -> dict:
        return build_slip_analysis(
            payload(leg("A", event="e1"), leg("B", event="e2"), **overrides),
            "primary",
            stake=5.0,
            now=NOW,
        )

    def test_a_priced_slip_renders_its_arithmetic(self) -> None:
        markup = render_slip_analysis(self.report())
        self.assertIn("Estimate vs. price", markup)
        self.assertIn("Needs to hit", markup)
        self.assertIn("Estimated to hit", markup)
        self.assertIn("70.56%", markup)  # break-even: 0.84 * 0.84

    def test_an_unachievable_expected_value_is_withheld_not_greyed_out(self) -> None:
        """A number on screen gets read, whatever is written beside it."""

        same_game = build_slip_analysis(
            payload(leg("A", event="one_game"), leg("B", event="one_game")),
            "primary",
            stake=5.0,
            now=NOW,
        )
        markup = render_slip_analysis(same_game)
        self.assertIn("withheld", markup)
        self.assertNotIn("EV on $", markup)

    def test_dropped_legs_are_named_on_the_card(self) -> None:
        report = build_slip_analysis(
            payload(leg("A", event="e1"), leg("B", event="e2"), leg("C", event="e3", state="stale")),
            "primary",
            stake=5.0,
            now=NOW,
        )
        markup = render_slip_analysis(report)
        self.assertIn("Analysed 2 of 3 legs", markup)
        self.assertIn("source_state_stale", markup)

    def test_an_unavailable_analysis_renders_its_reason(self) -> None:
        markup = render_slip_analysis(
            {"analysis_available": False, "detail": "Slip arithmetic failed to render: X"}
        )
        self.assertIn("unavailable", markup)
        self.assertIn("Slip arithmetic failed to render", markup)

    def test_the_detail_text_is_escaped(self) -> None:
        markup = render_slip_analysis(
            {"analysis_available": False, "detail": "<script>alert(1)</script>"}
        )
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)


class HitRateStateTests(unittest.TestCase):
    """An absent hit rate must not wear the colour of a good one.

    Every value took the success accent before, so a bot with no settled rows
    showed "Unavailable" in the same green as a measured rate.
    """

    def test_a_measured_rate_gets_the_accent(self) -> None:
        markup = render_research_record_track({"bot_name": "B", "observed_hit_rate": 0.62})
        self.assertIn("is-measured", markup)
        self.assertIn("62.0%", markup)

    def test_a_measured_rate_is_quoted_to_one_decimal(self) -> None:
        """Two decimals on a rate whose band spans points is false precision.

        69 of 104 is 66.3462%, and the 95% interval on that sample runs from
        roughly 57% to 75%. Rendering "66.35%" put four significant figures on a
        number good to about one, which is the kind of claim this platform
        refuses everywhere else.
        """

        markup = render_research_record_track({"bot_name": "B", "observed_hit_rate": 0.663462})
        self.assertIn("66.3%", markup)
        self.assertNotIn("66.35%", markup)

    def test_the_interval_is_shown_beside_the_measured_rate(self) -> None:
        markup = render_research_record_track({
            "bot_name": "B",
            "observed_hit_rate": 0.663462,
            "observed_hit_rate_interval": [0.5683, 0.747],
            "win_loss_count": 104,
        })
        self.assertIn("95% CI 57-75% on 104 settled", markup)

    def test_a_measured_rate_without_an_interval_still_renders(self) -> None:
        """Older payloads carry no interval; the card must not break on them."""

        markup = render_research_record_track({"bot_name": "B", "observed_hit_rate": 0.62})
        self.assertIn("Settled sample", markup)
        self.assertNotIn("95% CI", markup)

    def test_a_pending_rate_is_not_measured(self) -> None:
        markup = render_research_record_track({"bot_name": "B", "observed_hit_rate_raw": 0.5})
        self.assertIn("is-pending", markup)
        self.assertNotIn("is-measured", markup)

    def test_an_absent_rate_is_not_measured(self) -> None:
        markup = render_research_record_track({"bot_name": "B"})
        self.assertIn("is-absent", markup)
        self.assertNotIn("is-measured", markup)
        self.assertIn("Unavailable", markup)


def analysis(
    *,
    hit: float = 0.2722,
    interval: list | None = None,
    break_even: float = 0.223,
    precision: str = "good",
    payout: float = 4.484,
    stake: float = 1.0,
) -> dict:
    """A slip-analysis report shaped like ``analyze_slip``'s, for the card."""

    return {
        "analysis_available": True,
        "skipped_legs": [],
        "priced_leg_count": 5,
        "submitted_leg_count": 5,
        "analysis": {
            "hit_probability": hit,
            "hit_probability_interval": interval,
            "break_even_probability": break_even,
            "edge_over_break_even": hit - break_even,
            "expected_value_is_achievable": True,
            "expected_value": hit * payout - stake,
            "payout_if_won": payout,
            "stake": stake,
            "precision": precision,
            "verdict": "strong_value",
            "risk_tier": "low",
            "same_event_repricing_warning": None,
        },
    }


class SignificantDecimalsTests(unittest.TestCase):
    """A digit is worth printing only when it exceeds the uncertainty on it."""

    def test_a_band_wider_than_a_point_earns_no_decimals(self) -> None:
        self.assertEqual(significant_decimals([0.20, 0.24]), 0)

    def test_a_band_of_tenths_earns_one_decimal(self) -> None:
        self.assertEqual(significant_decimals([0.2684, 0.2761]), 1)

    def test_a_tight_band_earns_two(self) -> None:
        self.assertEqual(significant_decimals([0.27210, 0.27230]), 2)

    def test_a_missing_or_malformed_interval_falls_back_to_two(self) -> None:
        for value in (None, [], [0.2], "wide", {}):
            self.assertEqual(significant_decimals(value), 2)


class SlipArithmeticPrecisionTests(unittest.TestCase):
    """The card conceded in a note that it could not support two decimals,
    and then printed two decimals.

    ``analyze_slip`` has always returned ``hit_probability_interval``; the card
    rendered ``27.22%`` from a band of +/-0.39 points and dropped the band.
    """

    def test_the_estimate_is_quoted_to_the_digits_its_band_supports(self) -> None:
        markup = render_slip_analysis(analysis(hit=0.272219, interval=[0.26836, 0.27608]))
        self.assertIn("27.2%", markup)
        self.assertNotIn("27.22%", markup)

    def test_the_estimates_band_is_on_the_card(self) -> None:
        markup = render_slip_analysis(analysis(hit=0.272219, interval=[0.26836, 0.27608]))
        self.assertIn("95% CI 26.8-27.6%", markup)

    def test_the_difference_is_quoted_like_the_estimate_it_comes_from(self) -> None:
        """Break-even is exact, so the difference is uncertain by exactly as
        much as the estimate. Quoting it finer would invent precision."""

        markup = render_slip_analysis(analysis(hit=0.272219, interval=[0.26836, 0.27608]))
        self.assertIn("+4.9 pts", markup)
        # Asserted without the unit so this keeps guarding the precision if the
        # wording changes again. It is percentage points rather than percent
        # because it is 27.2 minus 22.3, not a fraction of either.
        self.assertNotIn("+4.92", markup)

    def test_the_exact_break_even_keeps_its_decimals(self) -> None:
        """It is arithmetic on quoted prices, not a simulation."""

        markup = render_slip_analysis(analysis(hit=0.272219, interval=[0.26836, 0.27608], break_even=0.223013))
        self.assertIn("22.30%", markup)

    def test_a_report_without_an_interval_still_renders(self) -> None:
        markup = render_slip_analysis(analysis(hit=0.272219, interval=None))
        self.assertIn("27.22%", markup)
        self.assertNotIn("95% CI", markup)

    def test_the_note_no_longer_promises_what_the_card_does_not_do(self) -> None:
        markup = render_slip_analysis(
            analysis(hit=0.0027, interval=[0.0016, 0.0039], precision="insufficient_draws")
        )
        self.assertIn("insufficient draws", markup)
        self.assertNotIn("not firm enough to quote to two decimals", markup)


class ExpectedValueBandTests(unittest.TestCase):
    """EV is the estimate times the payout, so it inherits the whole error.

    On a long slip the multiplier is in the thousands, which turns a tenth of a
    point of simulation error into dollars.
    """

    def test_the_band_carries_through_the_payout(self) -> None:
        markup = render_slip_analysis(
            analysis(hit=0.0027, interval=[0.0016, 0.0039], payout=1607.0, precision="insufficient_draws")
        )
        self.assertIn("95% CI $1.57 to $5.27", markup)

    def test_a_band_under_a_cent_is_not_shown(self) -> None:
        """Nothing a reader can act on, and an exact analysis has no band."""

        markup = render_slip_analysis(analysis(hit=0.81, interval=[0.8099, 0.8101], payout=1.0))
        self.assertNotIn("95% CI $", markup)

    def test_a_negative_expected_value_reads_as_money(self) -> None:
        markup = render_slip_analysis(analysis(hit=0.81, interval=None, payout=1.17, stake=1.0))
        self.assertIn("-$0.05", markup)
        self.assertNotIn("$-0.05", markup)


class ComboProbabilityDisplayTests(unittest.TestCase):
    """The joint probability is computed two ways and quoted like one.

    ``today.py`` says which on every slip, and reports the standard error when a
    simulation is behind the number. The comment on that field in
    ``slip_analysis`` says it exists so a consumer rendering the headline
    probability can see that it is not yet worth quoting. This page was that
    consumer and read none of it.
    """

    def display(self, **slip) -> tuple[str, str]:
        return combo_probability_display(slip)

    def test_an_exact_product_earns_two_decimals_and_no_band(self) -> None:
        """No simulation, no simulation error."""

        for basis in ("exact_product_no_modelled_correlation", "exact_product_leg_at_bound"):
            text, band = self.display(
                adjusted_probability=0.593012,
                joint_basis=basis,
                correlation_adjustment_standard_error=0.0,
            )
            self.assertEqual(text, "59.30%")
            self.assertEqual(band, "")

    def test_a_tight_simulation_keeps_its_decimals(self) -> None:
        text, band = self.display(
            adjusted_probability=0.593012,
            joint_basis="copula_resolved",
            correlation_adjustment_standard_error=0.0004,
        )
        self.assertEqual(text, "59.30%")
        self.assertIn("95% CI 59.22-59.38%", band)

    def test_a_loose_simulation_loses_them(self) -> None:
        """A standard error of 0.8 points leaves nothing after the units digit."""

        text, band = self.display(
            adjusted_probability=0.593012,
            joint_basis="copula_resolved",
            correlation_adjustment_standard_error=0.008,
        )
        self.assertEqual(text, "59%")
        self.assertIn("95% CI 58-61%", band)

    def test_an_unresolved_correlation_says_so(self) -> None:
        """The draws could not establish even the sign of the adjustment. The
        point estimate is still the best available, which is why it shows -- but
        "the correction is small" and "we could not measure the correction" are
        different statements."""

        text, band = self.display(
            adjusted_probability=0.593012,
            joint_basis="copula_unresolved",
            correlation_adjustment_standard_error=0.0031,
            correlation_adjustment_resolved=False,
        )
        self.assertIn("correlation unresolved", band)
        self.assertTrue(text.startswith("59."))

    def test_a_payload_without_the_fields_still_renders(self) -> None:
        text, band = self.display(adjusted_probability=0.593012)
        self.assertEqual(text, "59.30%")
        self.assertEqual(band, "")

    def test_an_unusable_slip_degrades_rather_than_raising(self) -> None:
        self.assertEqual(combo_probability_display(None), ("n/a", ""))
        self.assertEqual(combo_probability_display({"adjusted_probability": "wat"}), ("n/a", ""))


class DollarsTests(unittest.TestCase):
    def test_the_sign_sits_outside_the_currency_symbol(self) -> None:
        self.assertEqual(dollars(-0.05), "-$0.05")
        self.assertEqual(dollars(0.22), "$0.22")
        self.assertEqual(dollars(0), "$0.00")

    def test_an_unusable_value_degrades_rather_than_raising(self) -> None:
        self.assertEqual(dollars(None), "n/a")


class LegLabelTests(unittest.TestCase):
    """A one-leg slip read "1 LEGS" on the rendered card."""

    def test_one_is_singular_and_everything_else_is_plural(self) -> None:
        self.assertEqual(leg_label(1), "leg")
        self.assertEqual(leg_label("1"), "leg")
        self.assertEqual(leg_label(0), "legs")
        self.assertEqual(leg_label(3), "legs")

    def test_the_tier_card_placeholder_is_plural(self) -> None:
        """Tier cards show "-" when no slip was built; that is not a count."""

        self.assertEqual(leg_label("-"), "legs")


def _request(url, *, username=None, password=None):
    headers = {}
    if username is not None:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode())


class SlipAnalysisEndpointTests(PostgresTestCase):
    """The route itself, so the wiring is exercised rather than assumed."""

    def test_the_endpoint_is_researcher_gated_and_validates_its_stake(self) -> None:
        class Handler(PaperHandler):
            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        env = {
            "DASHBOARD_AUTH_ENABLED": "true",
            "DASHBOARD_AUTH_USERNAME": "owner",
            "DASHBOARD_AUTH_PASSWORD": "secret",
            "DASHBOARD_BASIC_FALLBACK_ENABLED": "true",
            "DASHBOARD_BASIC_AUTH_ROLE": "read_only",
        }
        try:
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
                    _request(base + "/slip-analysis.json")
                self.assertEqual(unauthenticated.exception.code, 401)
                unauthenticated.exception.close()

                with self.assertRaises(urllib.error.HTTPError) as forbidden:
                    _request(base + "/slip-analysis.json", username="owner", password="secret")
                self.assertEqual(forbidden.exception.code, 403)
                forbidden.exception.close()

                os.environ["DASHBOARD_BASIC_AUTH_ROLE"] = "researcher"
                status, report = _request(
                    base + "/slip-analysis.json", username="owner", password="secret"
                )
                self.assertEqual(status, 200)
                self.assertEqual(report["slip"], "primary")
                self.assertIn("analysis_available", report)

                with self.assertRaises(urllib.error.HTTPError) as bad_slip:
                    _request(
                        base + "/slip-analysis.json?slip=nonsense",
                        username="owner",
                        password="secret",
                    )
                self.assertEqual(bad_slip.exception.code, 400)
                bad_slip.exception.close()

                with self.assertRaises(urllib.error.HTTPError) as bad_stake:
                    _request(
                        base + "/slip-analysis.json?stake=abc",
                        username="owner",
                        password="secret",
                    )
                self.assertEqual(bad_stake.exception.code, 400)
                bad_stake.exception.close()

                # A stake of zero is a caller error, not a server error.
                with self.assertRaises(urllib.error.HTTPError) as zero_stake:
                    _request(
                        base + "/slip-analysis.json?stake=0",
                        username="owner",
                        password="secret",
                    )
                self.assertEqual(zero_stake.exception.code, 400)
                zero_stake.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()


class PluralTests(unittest.TestCase):
    """Counts read as English. "1 markets" and "1 LEGS" both shipped."""

    def test_one_is_singular(self) -> None:
        self.assertEqual(plural(1, "market"), "market")
        self.assertEqual(plural("1", "game"), "game")

    def test_everything_else_is_plural(self) -> None:
        self.assertEqual(plural(0, "market"), "markets")
        self.assertEqual(plural(2, "game"), "games")

    def test_a_placeholder_is_not_the_number_one(self) -> None:
        """Several cards show "-" when there is nothing to count."""

        self.assertEqual(plural("-", "leg"), "legs")

    def test_an_irregular_plural_can_be_given(self) -> None:
        self.assertEqual(plural(2, "entry", "entries"), "entries")
        self.assertEqual(plural(1, "entry", "entries"), "entry")

    def test_leg_label_still_delegates(self) -> None:
        self.assertEqual(leg_label(1), "leg")
        self.assertEqual(leg_label(3), "legs")


class SportsEventHeadingTests(unittest.TestCase):
    """The sports board rendered "1 markets" under every single-market game."""

    def event(self, market_count: int) -> dict:
        return {
            "away_team": "PHX", "home_team": "LAL", "league": "nba",
            "game_start_time": "2026-07-06T23:00:00+00:00",
            "market_count": market_count, "markets": [],
        }

    def test_one_market_is_singular(self) -> None:
        markup = render_sports_event(self.event(1))
        self.assertIn("1 market<", markup)
        self.assertNotIn("1 markets", markup)

    def test_several_markets_are_plural(self) -> None:
        self.assertIn("3 markets", render_sports_event(self.event(3)))

    def test_no_markets_is_plural(self) -> None:
        self.assertIn("0 markets", render_sports_event(self.event(0)))
