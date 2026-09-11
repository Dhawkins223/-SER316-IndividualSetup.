import unittest

from kalshi_research_bot.combo_safety import (
    VERIFIED_COMBO_EVIDENCE,
    VERIFIED_COMBO_SOURCE,
    authoritative_combo_slip_rejection_reasons,
    COMBO_QUOTE_MESSAGES,
    combo_leg_signature,
    combo_public_quote_state,
    combo_quote_message,
    market_is_tradable,
    slip_has_authoritative_combo_evidence,
)


def _verified_slip(legs, *, combo_ticker="KXMVE-TEST"):
    signature = combo_leg_signature(legs)
    verified_legs = [
        {
            **leg,
            "combo_eligible": True,
            "combo_market_ticker": combo_ticker,
            "combo_market_status": "active",
            "combo_market_yes_ask_cents": 50,
            "combo_market_fetched_at": "2026-07-13T12:00:00Z",
            "combo_market_snapshot_hash": "sha256:combo-snapshot",
            "combo_market_leg_signature": signature,
            "combo_exact_leg_count": len(legs),
            "combo_evidence_status": VERIFIED_COMBO_EVIDENCE,
            "combo_source": VERIFIED_COMBO_SOURCE,
        }
        for leg in legs
    ]
    return {
        "action": "BUILD_SLIP",
        "combo_compatibility": {"status": "compatible", "exact_listed_combo": True},
        "listed_combo_market_ticker": combo_ticker,
        "legs": verified_legs,
    }


class ComboSafetyTests(unittest.TestCase):
    def test_signature_is_deterministic_across_leg_order(self):
        first = [
            {"market_ticker": "MKT-B", "side": "NO"},
            {"market_ticker": "MKT-A", "side": "YES"},
        ]
        second = list(reversed(first))
        self.assertEqual(combo_leg_signature(first), combo_leg_signature(second))

    def test_exact_listed_combo_is_accepted(self):
        slip = _verified_slip(
            [
                {"market_ticker": "KXMLB-A", "side": "yes"},
                {"market_ticker": "KXBTC-B", "side": "no"},
            ]
        )
        self.assertTrue(slip_has_authoritative_combo_evidence(slip))
        self.assertEqual(authoritative_combo_slip_rejection_reasons(slip["legs"]), [])

    def test_missing_combo_evidence_is_rejected(self):
        slip = {
            "action": "BUILD_SLIP",
            "combo_compatibility": {"status": "compatible", "exact_listed_combo": True},
            "legs": [{"market_ticker": "MKT-A", "side": "yes", "combo_eligible": True}],
        }
        self.assertFalse(slip_has_authoritative_combo_evidence(slip))
        self.assertIn(
            "missing_authoritative_combo_evidence",
            authoritative_combo_slip_rejection_reasons(slip["legs"]),
        )

    def test_legs_from_two_combo_markets_are_rejected(self):
        slip = _verified_slip(
            [
                {"market_ticker": "MKT-A", "side": "yes"},
                {"market_ticker": "MKT-B", "side": "no"},
            ]
        )
        slip["legs"][1]["combo_market_ticker"] = "KXMVE-OTHER"
        reasons = authoritative_combo_slip_rejection_reasons(slip["legs"])
        self.assertIn("legs_not_from_one_listed_combo_market", reasons)
        self.assertFalse(slip_has_authoritative_combo_evidence(slip))

    def test_top_level_combo_ticker_must_match_leg_evidence(self):
        slip = _verified_slip([{"market_ticker": "MKT-A", "side": "yes"}])
        slip["listed_combo_market_ticker"] = "KXMVE-WRONG"

        self.assertFalse(slip_has_authoritative_combo_evidence(slip))

    def test_subset_of_listed_combo_is_rejected(self):
        slip = _verified_slip(
            [
                {"market_ticker": "MKT-A", "side": "yes"},
                {"market_ticker": "MKT-B", "side": "no"},
            ]
        )
        slip["legs"] = slip["legs"][:1]
        reasons = authoritative_combo_slip_rejection_reasons(slip["legs"])
        self.assertIn("combo_leg_count_mismatch", reasons)
        self.assertIn("combo_leg_signature_mismatch", reasons)
        self.assertFalse(slip_has_authoritative_combo_evidence(slip))


class ComboPublicQuoteStateTests(unittest.TestCase):
    """One classifier, used by both the collector and the dashboard.

    It lived in two places -- `today.combo_public_quote_state` stamping the
    payload and a copy in `paper_server` rendering it -- which is two answers
    waiting to disagree about the same market. These pin the one that remains.
    """

    RFQ_BOOK = {
        "ticker": "KXMVE-RFQ-1",
        "status": "active",
        "yes_ask_cents": 0,
        "yes_bid_cents": 0,
        "no_ask_cents": 100,
        "no_bid_cents": 100,
    }

    def test_the_all_or_nothing_book_is_rfq_required_not_a_zero_price(self):
        self.assertEqual(combo_public_quote_state(self.RFQ_BOOK), "rfq_required")

    def test_a_real_yes_ask_is_tradable(self):
        self.assertEqual(combo_public_quote_state({**self.RFQ_BOOK, "yes_ask_cents": 81}), "tradable")

    def test_the_rfq_shape_needs_a_live_kxmve_contract(self):
        # The same book on a settled contract, or on one that is not a combo,
        # is not Kalshi withholding a quote.
        self.assertEqual(combo_public_quote_state({**self.RFQ_BOOK, "status": "settled"}), "unavailable")
        self.assertEqual(combo_public_quote_state({**self.RFQ_BOOK, "ticker": "KXNFL-1"}), "unavailable")

    def test_a_stamped_state_wins_over_rederiving_it(self):
        # The collector saw the market when it collected it. A snapshot read
        # back later must not be re-judged from fields that may have been
        # normalised since.
        self.assertEqual(
            combo_public_quote_state({**self.RFQ_BOOK, "public_quote_state": "tradable"}),
            "tradable",
        )

    def test_an_unrecognised_stamp_is_ignored_rather_than_trusted(self):
        self.assertEqual(
            combo_public_quote_state({**self.RFQ_BOOK, "public_quote_state": "probably-fine"}),
            "rfq_required",
        )

    def test_a_missing_book_is_unavailable_rather_than_rfq_required(self):
        self.assertEqual(combo_public_quote_state({"ticker": "KXMVE-1", "status": "active"}), "unavailable")

    # A value that is not a price, by each of the ways a payload can carry one.
    # `10 ** 400` is the one that is easy to miss: JSON puts no bound on an
    # integer literal, so a few hundred digits parse to a Python int that
    # `float()` refuses with OverflowError -- neither a TypeError nor a
    # ValueError, and so not caught by a guard written for those two.
    MALFORMED = ("not-a-number", None, object(), 10**400, float("nan"))

    def test_an_unparseable_quote_is_unavailable(self):
        # Every field, not just the ones the classifier parses inside its own
        # try/except. A first version of this test only dirtied `no_ask_cents`
        # and so never reached `market_is_tradable`, which read `yes_ask_cents`
        # ahead of that guard and raised on the way past it.
        for field in ("yes_ask_cents", "yes_bid_cents", "no_ask_cents", "no_bid_cents"):
            for value in self.MALFORMED:
                if value is None or isinstance(value, float):
                    # `or 0` makes these indistinguishable from an absent or
                    # zero quote, which is the RFQ sentinel's own shape.
                    continue
                with self.subTest(field=field, value=type(value).__name__):
                    self.assertEqual(
                        combo_public_quote_state({**self.RFQ_BOOK, field: value}),
                        "unavailable",
                    )

    def test_a_malformed_ask_is_not_tradable_rather_than_an_exception(self):
        for ask in self.MALFORMED:
            with self.subTest(ask=type(ask).__name__):
                self.assertFalse(market_is_tradable({"yes_ask_cents": ask}))
        self.assertFalse(market_is_tradable({}))

    def test_a_numeric_string_ask_still_reads_as_a_price(self):
        self.assertTrue(market_is_tradable({"yes_ask_cents": "81"}))


class ComboQuoteMessageTests(unittest.TestCase):
    def test_the_message_follows_the_state(self):
        for state, market in (
            ("tradable", {**ComboPublicQuoteStateTests.RFQ_BOOK, "yes_ask_cents": 81}),
            ("rfq_required", ComboPublicQuoteStateTests.RFQ_BOOK),
            ("unavailable", {"ticker": "KXMVE-1", "status": "settled"}),
        ):
            with self.subTest(state=state):
                self.assertEqual(combo_quote_message(market), COMBO_QUOTE_MESSAGES[state])

    def test_the_rfq_message_reports_the_observation_before_naming_the_rfq(self):
        message = COMBO_QUOTE_MESSAGES["rfq_required"]
        self.assertIn("No executable combo price is quoted publicly", message)
        # The old wording opened by asserting the exchange's reason, which is
        # inferred from the orderbook shape rather than reported by Kalshi.
        self.assertFalse(message.startswith("Kalshi requires"))

    def test_a_stamped_message_from_an_older_collector_does_not_survive(self):
        superseded = (
            "Kalshi requires an authenticated RFQ for this exact combo; "
            "the public orderbook has no executable price."
        )
        stale = {**ComboPublicQuoteStateTests.RFQ_BOOK, "public_quote_message": superseded}
        self.assertEqual(combo_quote_message(stale), COMBO_QUOTE_MESSAGES["rfq_required"])


if __name__ == "__main__":
    unittest.main()
