"""Golden render tests for the server-rendered dashboard.

These exercise `render_dashboard` end to end with a payload that passes the
freshness and exact-combo gates, so a change that silently stops showing
verified rows -- or that starts showing withheld ones -- fails here instead of
in a browser.
"""

from __future__ import annotations

import gzip
import html
import json
import os
import pathlib
import re
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from kalshi_research_bot.browser_fixtures import (
    build_browser_fixture_payload,
    make_verified_fixture_payload,
)
from kalshi_research_bot.auth import AuthPrincipal
from kalshi_research_bot.dashboard_assets import SCRIPT, stylesheet_css
from kalshi_research_bot.paper_server import (
    render_dashboard,
    render_login_page,
    render_operator_page,
)

# The stylesheet and script are served as files now; read what ships.
CSS = stylesheet_css()
JS = SCRIPT.body.decode("utf-8")


def principal(role: str) -> AuthPrincipal:
    return AuthPrincipal(username="tester", role=role, auth_method="session")


def bootstrap(rendered: str) -> dict:
    """The values the page hands its script, read back out of the markup."""
    match = re.search(r'data-paper="([^"]*)"', rendered)
    assert match, "page carries no bootstrap payload"
    return json.loads(html.unescape(match.group(1)))


class FixtureExercisesSlipArithmeticTests(unittest.TestCase):
    """The fixture has to reach the analysis engine, not just the render.

    The engine drops any leg without a fresh quote timestamp. The fixture
    omitted one, so every leg was skipped and the slip-arithmetic block showed
    "no analysis available" in every test, screenshot, and local preview -- the
    most involved part of the product was never once exercised outside
    production.
    """

    def setUp(self) -> None:
        self.payload = make_verified_fixture_payload()

    def test_priced_slips_produce_a_usable_analysis(self) -> None:
        from kalshi_research_bot.slip_report import build_slip_analysis

        for slip_key in ("primary", "leverage"):
            with self.subTest(slip=slip_key):
                report = build_slip_analysis(self.payload, slip_key, stake=5.0)
                self.assertTrue(
                    report["analysis_available"],
                    f"{slip_key} analysis unavailable: {report.get('detail')}",
                )
                self.assertEqual(report.get("skipped_legs") or [], [])
                analysis = report["analysis"]
                for field in ("hit_probability", "break_even_probability", "edge_over_break_even"):
                    self.assertIsInstance(analysis[field], float)

    def test_unrelated_games_do_not_share_an_event_id(self) -> None:
        # The correlation model keys off event_ticker, so deriving it from the
        # market ticker collided two separate games onto one event and scored
        # them as same-event correlated.
        for slip_key in ("custom_slip", "leverage_slip"):
            with self.subTest(slip=slip_key):
                legs = self.payload[slip_key]["legs"]
                tickers = [leg["event_ticker"] for leg in legs]
                self.assertEqual(len(tickers), len(set(tickers)))

    def test_the_rendered_card_shows_the_arithmetic(self) -> None:
        rendered = render_dashboard(self.payload, principal=principal("admin"))
        self.assertIn("Estimate vs. price", rendered)
        self.assertIn("Needs to hit", rendered)
        self.assertIn("Estimated to hit", rendered)
        self.assertNotIn("No analysis available for this slip.", rendered)


class LegBreakdownTests(unittest.TestCase):
    """Per-leg price-versus-probability, surfaced from the analysis payload."""

    def setUp(self) -> None:
        self.payload = make_verified_fixture_payload()
        self.rendered = render_dashboard(self.payload, principal=principal("admin"))

    def test_identical_differences_get_identical_labels(self) -> None:
        # 0.86-0.84 and 0.84-0.82 are both two points, and in binary floating
        # point they land either side of the 0.02 threshold. Labelling them
        # differently would be the classifier reporting float noise.
        from kalshi_research_bot.paper_server import leg_edge_flag

        self.assertEqual(leg_edge_flag(0.86 - 0.84), leg_edge_flag(0.84 - 0.82))

    def test_flags_cover_every_sign(self) -> None:
        from kalshi_research_bot.paper_server import leg_edge_flag

        self.assertEqual(leg_edge_flag(-0.02)[0], "Priced over")
        self.assertEqual(leg_edge_flag(0.0)[0], "No edge")
        self.assertEqual(leg_edge_flag(0.005)[0], "Thin edge")
        self.assertEqual(leg_edge_flag(0.05)[0], "Good cushion")

    def test_breakdown_renders_a_row_for_every_analysed_leg(self) -> None:
        from kalshi_research_bot.slip_report import build_slip_analysis

        analysis = build_slip_analysis(self.payload, "primary", stake=5.0)["analysis"]
        self.assertIn(f"Leg breakdown ({len(analysis['legs'])})", self.rendered)
        for leg in analysis["legs"]:
            self.assertIn(html.escape(str(leg["selection"])), self.rendered)

    def test_breakdown_is_a_table_with_scoped_headers(self) -> None:
        # Numeric columns only stay comparable as a real table.
        self.assertIn('<th scope="col">Ask / break-even</th>', self.rendered)
        self.assertIn('<th scope="row">', self.rendered)
        self.assertIn("<caption>", self.rendered)

    def test_price_and_break_even_are_the_same_number(self) -> None:
        """The invariant that lets one column carry both.

        `slip_report` builds each leg with `decimal_odds = 100 / ask_cents`, and
        `SlipLeg.break_even` is `1 / decimal_odds`, so the ask in cents and the
        break-even in percent are the same figure. The table prints it once and
        says so. If that derivation ever changes, they stop being one number and
        the merged column starts lying -- so this fails here rather than in
        front of a reader.
        """
        from kalshi_research_bot.slip_report import build_slip_analysis

        analysis = build_slip_analysis(self.payload, "primary", stake=5.0)["analysis"]
        asks = {
            leg["market_ticker"]: float(leg["ask_cents"])
            for leg in self.payload["custom_slip"]["legs"]
        }
        self.assertTrue(analysis["legs"], "fixture produced no analysed legs")
        for leg in analysis["legs"]:
            self.assertAlmostEqual(
                float(leg["break_even"]) * 100.0,
                asks[leg["leg_id"]],
                places=9,
                msg=f"{leg['leg_id']}: break-even and ask have diverged",
            )

    def test_every_cell_carries_a_label_for_the_stacked_layout(self) -> None:
        # Below 560px the table reflows to one stacked block per leg and each
        # cell draws its own heading from `data-label`, because the column
        # headers are no longer above it. A cell without one renders as a bare
        # number with nothing to say what it measures.
        breakdown = re.search(
            r'<details class="leg-breakdown">.*?</details>', self.rendered, re.S
        )
        self.assertIsNotNone(breakdown, "no leg breakdown in the rendered page")
        cells = re.findall(r"<td\b[^>]*>", breakdown.group(0))
        self.assertTrue(cells)
        unlabelled = [cell for cell in cells if "data-label=" not in cell]
        self.assertEqual(unlabelled, [], "leg cells without a stacked-layout label")

    def test_scroll_container_ancestors_can_be_narrower_than_the_table(self) -> None:
        """Guards the fix for a page that scrolled sideways on a phone.

        A grid item defaults to `min-width: auto` and so refuses to lay out
        narrower than its content. With any step of the chain left at that
        default, the table's `overflow-x: auto` cannot clip: the ancestors widen
        instead, and the whole page scrolls sideways. Static assertion because a
        browser is the only other way to catch it.
        """
        for selector in (".panel", ".slip-card", ".slip-analysis", ".leg-breakdown"):
            with self.subTest(selector=selector):
                self.assertRegex(
                    CSS,
                    rf"(^|[,{{\s]){re.escape(selector)}\s*(,[^{{]*)?\{{[^}}]*min-width:\s*0",
                    f"{selector} must opt out of min-width:auto",
                )

    def test_empty_analysis_renders_nothing_rather_than_an_empty_table(self) -> None:
        from kalshi_research_bot.paper_server import render_leg_breakdown

        self.assertEqual(render_leg_breakdown({}), "")
        self.assertEqual(render_leg_breakdown({"legs": []}), "")


class DashboardRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = make_verified_fixture_payload()
        self.fresh = render_dashboard(self.payload)

    def test_fresh_payload_shows_verified_slip_rows(self) -> None:
        self.assertIn("Fresh data", self.fresh)
        self.assertIn("NYY @ BOS", self.fresh)
        self.assertIn("LAD @ SD", self.fresh)
        self.assertIn("PHI @ ATL", self.fresh)

    def test_fresh_payload_reports_ready_tier_counts(self) -> None:
        # Two tiers build in the fixture, and these pages render without a
        # principal -- so a reader, who sees three tiers rather than four: the
        # research scout is operator-only. The summary has to agree with the
        # panels actually on the page, not with a stale field or a literal.
        self.assertIn("2/3", self.fresh)

    def test_stale_payload_withholds_every_slip_row(self) -> None:
        stale = dict(self.payload)
        stale["generated_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        rendered = render_dashboard(stale)
        self.assertIn("Review blocked", rendered)
        self.assertNotIn("NYY @ BOS", rendered)
        self.assertNotIn("PHI @ ATL", rendered)
        self.assertIn("0/3", rendered)

    def test_refresh_failure_withholds_every_slip_row(self) -> None:
        failed = dict(self.payload)
        failed["refresh_error"] = "live_refresh_failed"
        rendered = render_dashboard(failed)
        self.assertIn("Review blocked", rendered)
        self.assertNotIn("NYY @ BOS", rendered)

    def test_empty_fixture_state_renders_without_slip_rows(self) -> None:
        empty = build_browser_fixture_payload(self.payload, "empty")
        rendered = render_dashboard(empty)
        self.assertNotIn("NYY @ BOS", rendered)
        self.assertIn("No Slip", rendered)

    def test_hostile_payload_text_is_escaped(self) -> None:
        hostile = dict(self.payload)
        hostile["combo_source_summary"] = {
            "active_kxmve_market_count": 1,
            "verified_current_day_contract_count": 1,
            "tiers": {},
        }
        hostile["dashboard_snapshot"] = {"source": "<script>alert(1)</script>"}
        rendered = render_dashboard(hostile)
        self.assertNotIn("<script>alert(1)</script>", rendered)

    def test_page_carries_no_inline_script(self) -> None:
        # The bootstrap payload rides on a data attribute so the CSP can refuse
        # inline script outright.
        self.assertNotIn("<script>", self.fresh)
        self.assertNotIn("<style>", self.fresh)

    def test_bootstrap_payload_cannot_break_out_of_its_attribute(self) -> None:
        payload = dict(self.payload)
        payload["generated_at"] = '2026-01-01T00:00:00+00:00" onload="alert(1)'
        rendered = render_dashboard(payload)
        self.assertNotIn('onload="alert(1)"', rendered)


class ViewerRoleTests(unittest.TestCase):
    """A reader who cannot refresh must not be shown controls that 403."""

    def setUp(self) -> None:
        self.payload = make_verified_fixture_payload()

    def test_admin_gets_the_refresh_control(self) -> None:
        rendered = render_dashboard(self.payload, principal=principal("admin"))
        self.assertIn('id="refresh-slip"', rendered)
        self.assertTrue(bootstrap(rendered)["can_refresh"])

    def test_reader_roles_get_no_refresh_control(self) -> None:
        for role in ("read_only", "researcher"):
            with self.subTest(role=role):
                rendered = render_dashboard(self.payload, principal=principal(role))
                self.assertNotIn('id="refresh-slip"', rendered)
                self.assertFalse(bootstrap(rendered)["can_refresh"])

    def test_missing_principal_is_treated_as_a_reader(self) -> None:
        rendered = render_dashboard(self.payload)
        self.assertFalse(bootstrap(rendered)["can_refresh"])

    def test_polling_uses_an_endpoint_every_role_may_read(self) -> None:
        # /quality.json is admin-only; polling it left other roles silently
        # stale, so the freshness poll must not go back to it.
        self.assertIn("/freshness.json", JS)
        self.assertNotIn("/quality.json", JS)


class CsrfTokenTests(unittest.TestCase):
    """The token has to survive a new tab, which per-tab storage does not."""

    def test_client_reads_the_csrf_cookie(self) -> None:
        self.assertIn("hawknetic_research_csrf=", JS)

    def test_csrf_cookie_is_readable_but_session_cookie_is_not(self) -> None:
        from kalshi_research_bot.paper_server import build_csrf_cookie, build_session_cookie

        session_cookie = build_session_cookie("session-value", secure=True)
        csrf_cookie = build_csrf_cookie("csrf-value", secure=True)
        self.assertIn("HttpOnly", session_cookie)
        self.assertNotIn("HttpOnly", csrf_cookie)
        for cookie in (session_cookie, csrf_cookie):
            self.assertIn("SameSite=Strict", cookie)
            self.assertIn("Secure", cookie)

    def test_cookies_drop_secure_off_a_hosted_runtime(self) -> None:
        from kalshi_research_bot.paper_server import build_csrf_cookie

        self.assertNotIn("Secure", build_csrf_cookie("csrf-value", secure=False))

    def test_logout_clears_both_cookies(self) -> None:
        from kalshi_research_bot.paper_server import clear_csrf_cookie, clear_session_cookie

        self.assertIn("Max-Age=0", clear_session_cookie(secure=False))
        self.assertIn("Max-Age=0", clear_csrf_cookie(secure=False))


def _srgb_channel(value: int) -> float:
    v = value / 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_srgb_channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """WCAG 2.x relative-contrast ratio, lighter over darker."""
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def over(top: tuple[int, int, int], alpha: float, bottom: tuple[int, int, int]) -> tuple[int, int, int]:
    """Composite a translucent overlay onto an opaque backdrop."""
    return tuple(round(alpha * t + (1 - alpha) * b) for t, b in zip(top, bottom))


def css_token(name: str) -> tuple[int, int, int]:
    match = re.search(rf"^\s*{re.escape(name)}:\s*#([0-9a-fA-F]{{6}});", CSS, re.M)
    assert match, f"token {name} not found in the stylesheet"
    value = match.group(1)
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


class TextContrastTests(unittest.TestCase):
    """The AA floor, asserted against the tokens that actually ship.

    Contrast was measured off the composited page rather than eyeballed: text
    made transparent, the page screenshotted, and the pixels behind each run
    sampled. That found `--text-muted` at 4.44:1 on the tinted ready-summary
    surface, just under the 4.5:1 floor. Re-running a browser in CI to catch a
    regression would be heavy, so the arithmetic is asserted here instead --
    the same formula, against the same colours the stylesheet declares.
    """

    ACCENT = (0, 230, 118)

    def surfaces(self) -> dict[str, tuple[int, int, int]]:
        surface_alt = css_token("--surface-alt")
        return {
            "--bg": css_token("--bg"),
            "--surface": css_token("--surface"),
            "--surface-alt": surface_alt,
            # `.ready-summary` lays a 10%-accent gradient over --surface-alt.
            # Text sits at the strong end of it, so that is the case to hold.
            ".ready-summary": over(self.ACCENT, 0.10, surface_alt),
        }

    def test_muted_text_clears_aa_on_every_surface_it_lands_on(self) -> None:
        muted = css_token("--text-muted")
        for name, background in self.surfaces().items():
            with self.subTest(surface=name):
                self.assertGreaterEqual(
                    contrast_ratio(muted, background),
                    4.5,
                    f"--text-muted is below WCAG AA on {name}",
                )

    def test_primary_and_secondary_text_clear_aa_too(self) -> None:
        for token in ("--text-primary", "--text-secondary"):
            colour = css_token(token)
            for name, background in self.surfaces().items():
                with self.subTest(token=token, surface=name):
                    self.assertGreaterEqual(contrast_ratio(colour, background), 4.5)

    def test_the_ratio_helper_agrees_with_known_values(self) -> None:
        # Anchor the formula so a bug in it cannot quietly pass the checks above.
        self.assertAlmostEqual(contrast_ratio((255, 255, 255), (0, 0, 0)), 21.0, places=6)
        self.assertAlmostEqual(contrast_ratio((0, 0, 0), (0, 0, 0)), 1.0, places=6)


class LabelledRegionTests(unittest.TestCase):
    """`aria-label` on a bare div is prohibited, and screen readers drop it.

    Both summaries carried one, so the names reached nobody: invisible to
    sighted readers by design, and discarded by assistive technology because a
    plain div exposes no role that can take a name.
    """

    def setUp(self) -> None:
        self.rendered = render_dashboard(make_verified_fixture_payload(), principal=principal("admin"))

    def test_labelled_groups_declare_a_role_that_can_carry_a_name(self) -> None:
        for match in re.finditer(r"<div\b[^>]*aria-label(?:ledby)?=[^>]*>", self.rendered):
            with self.subTest(tag=match.group(0)[:80]):
                self.assertIn("role=", match.group(0))

    def test_every_labelledby_points_at_an_element_that_exists(self) -> None:
        referenced = re.findall(r'aria-labelledby="([^"]+)"', self.rendered)
        self.assertTrue(referenced, "no aria-labelledby in the page")
        for group in referenced:
            for token in group.split():
                with self.subTest(id=token):
                    self.assertEqual(
                        len(re.findall(rf'id="{re.escape(token)}"', self.rendered)),
                        1,
                        f"aria-labelledby={token} must resolve to exactly one element",
                    )


class CustomerSurfaceTests(unittest.TestCase):
    """A reader gets the picks; the pipeline that produces them is the operator's.

    Collection health, source freshness, worker and database state, and the
    settled-record ledger all describe how the sausage is made. A reader cannot
    act on any of it -- a stalled worker is not theirs to restart, and the gate
    already withholds anything unsafe to show -- so it was noise sitting between
    the tiers they came for.
    """

    OPERATOR_PANEL_IDS = ("sports-board", "source-data", "quality", "record", "research-edge")
    READER_PANEL_IDS = ("map", "market-browser", "primary", "leverage", "all-day")

    def page(self, role: str) -> str:
        return render_dashboard(make_verified_fixture_payload(), principal=principal(role))

    def panel_ids(self, rendered: str) -> list[str]:
        return re.findall(r'<section class="panel" id="([\w-]+)"', rendered)

    def test_a_reader_sees_the_picks_and_not_the_pipeline(self) -> None:
        rendered = self.page("read_only")
        found = self.panel_ids(rendered)
        self.assertEqual(found, list(self.READER_PANEL_IDS))
        for panel in self.OPERATOR_PANEL_IDS:
            self.assertNotIn(panel, found)

    def test_an_operator_still_sees_everything(self) -> None:
        found = self.panel_ids(self.page("admin"))
        for panel in self.READER_PANEL_IDS + self.OPERATOR_PANEL_IDS:
            with self.subTest(panel=panel):
                self.assertIn(panel, found)

    def test_the_research_only_badge_is_never_hidden(self) -> None:
        """The one label that says this is not a betting product.

        It was `display: none` below 640px, so it vanished on phones -- the
        screens most readers use -- while the far less consequential role label
        ("View only") survived at every width. That is backwards, and it is the
        kind of regression a viewport-blind test suite cannot see, so the rule
        itself is what gets asserted here.
        """
        for role in ("read_only", "admin"):
            with self.subTest(role=role):
                self.assertIn("research-only-badge", self.page(role))

        # Anchored on the class rather than parsed block by block: a rule
        # inside a media query is not a top-level block, and matching those
        # first swallows the nested selector into the @media body -- which is
        # how the first version of this guard passed the very regression it
        # was written for. Any rule naming this badge is checked, nested or
        # not; if one ever needs to hide part of it, that is a decision for a
        # person to make deliberately.
        stylesheet = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        rules = re.findall(r"(\.research-only-badge[^{}]*)\{([^}]*)\}", stylesheet)
        self.assertTrue(rules, "no rule styles the research-only badge; the scan is looking at nothing")
        for selector, body in rules:
            with self.subTest(selector=selector.strip()):
                self.assertNotRegex(
                    body,
                    r"display\s*:\s*none",
                    "the research-only badge must stay visible at every width",
                )

    def test_operator_markup_is_absent_rather_than_hidden(self) -> None:
        # Withholding these in CSS would still ship worker and database state to
        # the browser, where anyone can read it out of the source.
        rendered = self.page("read_only")
        for probe in (
            "PostgreSQL",
            "sports-research worker",
            "validated rows to this database",
            "Track Record",
            "Live Status",
            # Gating the panel is not enough on its own: the tier summary named
            # this one too, so a reader saw a card for a section they had no
            # way to open.
            "Research Scout",
        ):
            with self.subTest(probe=probe):
                self.assertNotIn(probe, rendered)

    def test_the_ready_tier_count_matches_the_tiers_on_the_page(self) -> None:
        """The denominator has to describe what the viewer can actually see.

        It was the literal `4` in two places while the tier list it claimed to
        count was built elsewhere, so gating the research-scout tier left a
        reader with "2/4" for a page holding three tiers -- one of the counted
        four being a panel they could not reach. Deriving it is the fix;
        asserting the two agree is what keeps them together.
        """
        for role in ("read_only", "admin"):
            rendered = self.page(role)
            cards = re.findall(r'<div class="tier-head">\s*<span>([^<]+)</span>', rendered)
            ready_badges = len(re.findall(r'<span class="badge badge-success">Ready</span>', rendered))
            summary = re.findall(r'class="ready-count">(\d+)/(\d+)<', rendered)
            hero = re.findall(r"Review tiers ready</small><strong>(\d+)/(\d+)<", rendered)
            with self.subTest(role=role):
                self.assertTrue(cards, "no tier cards rendered")
                self.assertTrue(summary and hero, "expected both tier counts on the page")
                # Denominators describe the tiers actually on the page...
                for numerator, denominator in summary + hero:
                    self.assertEqual(int(denominator), len(cards))
                    # ...and the numerators describe the cards marked Ready.
                    self.assertEqual(int(numerator), ready_badges)
                # Both counts render a few hundred pixels apart on one page, so
                # they have to agree with each other, not merely each be
                # plausible on its own.
                self.assertEqual(summary[0], hero[0])

    def test_every_in_page_link_lands_on_something_that_exists(self) -> None:
        """Gating a panel silently breaks every link that pointed at it.

        The mobile bar has four fixed slots, two of which aimed at panels that
        are now operator-only -- for a reader those taps scrolled nowhere.
        """
        for role in ("read_only", "admin"):
            rendered = self.page(role)
            ids = set(re.findall(r'\bid="([\w-]+)"', rendered))
            targets = set(re.findall(r'href="#([\w-]+)"', rendered))
            with self.subTest(role=role):
                self.assertEqual(sorted(targets - ids), [], f"{role}: links to nothing")

    def test_the_reader_page_speaks_no_backend_vocabulary(self) -> None:
        visible = re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", self.page("read_only"), flags=re.S))
        for word in ("postgres", "database", "worker", "collector", "snapshot", "schema", "endpoint"):
            with self.subTest(word=word):
                self.assertNotRegex(visible.lower(), rf"\b{word}")


class ReaderResearchFramingTests(unittest.TestCase):
    """The customer's page is a research report, and must not read as a bet slip.

    Two of the product's content rules are load-bearing here. No wagering
    vocabulary in customer-facing copy -- and "payout" was on the slip card, the
    drawer and every tier tile. And a probability is an estimate with its basis
    visible, never a recommendation to act -- while the card told a reader what
    to verify "before placing anything yourself" and counted "legs to enter by
    hand". The operator's page keeps the working dollar figures; the reader's
    page is checked word by word.
    """

    # Whole words, so "better" does not trip "bet" and "bookmaker" is caught
    # while "notebook" would not be. Kept as one pattern so a new word is added
    # in one place.
    WAGERING = re.compile(
        r"\b(bet|bets|betting|wager\w*|stake\w*|parlay\w*|book|books|bookmaker\w*|sportsbook\w*"
        r"|odds boost\w*|payout\w*|pay out|placing|place a|enter by hand)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def visible_text(rendered: str) -> str:
        return re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", rendered, flags=re.S))

    def page(self, role: str, state: str = "live") -> str:
        payload = build_browser_fixture_payload(make_verified_fixture_payload(), state)
        return render_dashboard(payload, principal=principal(role))

    def test_the_reader_page_uses_no_wagering_vocabulary(self) -> None:
        for state in ("live", "empty", "stale", "error"):
            with self.subTest(state=state):
                hits = sorted({match.group(0).lower() for match in self.WAGERING.finditer(self.visible_text(self.page("read_only", state)))})
                self.assertEqual(hits, [], f"wagering words on the reader page: {hits}")

    def test_the_guard_actually_matches_the_words_it_names(self) -> None:
        # A regex that quietly matched nothing would pass the test above for
        # ever. These are the exact strings the reader page used to carry.
        for phrase in ("Est. $5 Payout", "before placing anything yourself", "5 legs to enter by hand", "best bookmaker"):
            with self.subTest(phrase=phrase):
                self.assertRegex(phrase, self.WAGERING)
        for phrase in ("Slightly better than its price", "break-even", "notebook"):
            with self.subTest(phrase=phrase):
                self.assertNotRegex(phrase, self.WAGERING)

    def test_reader_navigation_describes_review_in_all_states(self) -> None:
        for state in ("live", "empty", "stale", "error", "loading"):
            with self.subTest(state=state):
                rendered = self.page("read_only", state)
                self.assertNotRegex(self.visible_text(rendered), re.compile(r"\bbuilder\b", re.I))
                self.assertIn('aria-label="Review views"', rendered)
                self.assertIn('href="#prediction-drawer"', rendered)

    def test_summary_matches_the_viewers_responsibility(self) -> None:
        for role, count in (("read_only", 2), ("admin", 4)):
            rendered = self.page(role)
            hero = rendered.split('id="builder"', 1)[1].split('</section>', 1)[0]
            self.assertEqual(len(re.findall(r'class="stat-card\b', hero)), count)
            self.assertIn("Contracts checked", hero)

    def test_compact_views_use_the_card_estimate_without_recalculating(self) -> None:
        for role in ("read_only", "admin"):
            with self.subTest(role=role):
                rendered = self.page(role)
                drawer = rendered.split('id="prediction-drawer"', 1)[1].split('</aside>', 1)[0]
                card = rendered.split('id="primary"', 1)[1].split('</section>', 1)[0]
                pattern = r'<small>Estimated to hit</small><strong>([^<]+)</strong>'
                estimate = re.search(pattern, card).group(1)
                self.assertEqual(re.search(pattern, drawer).group(1), estimate)
                self.assertIn(f'<small>Estimated to hit {estimate}</small>', rendered)
                self.assertIn("Market implied:", drawer)

    def test_compact_views_withhold_an_estimate_for_only_some_legs(self) -> None:
        from kalshi_research_bot.paper_server import slip_estimate_display

        report = {
            "analysis_available": True,
            "skipped_legs": [{"leg_id": "missing", "reason": "no_quoted_ask"}],
            "analysis": {"hit_probability": .8},
        }
        self.assertEqual(slip_estimate_display(report), ("Unavailable", ""))

    def test_fragility_badges_explain_their_basis(self) -> None:
        from kalshi_research_bot.paper_server import render_slip_analysis
        from kalshi_research_bot.slip_report import build_slip_analysis

        report = build_slip_analysis(make_verified_fixture_payload(), "primary")
        for tier, label in (("low", "Few moving parts"), ("moderate", "Some fragility"), ("high", "Fragile"), ("very_high", "Very fragile")):
            report["analysis"]["risk_tier"] = tier
            rendered = render_slip_analysis(report)
            self.assertIn(f">{label}</span>", rendered)
            self.assertNotRegex(rendered, r'>[^<]* risk</span>')
            self.assertIn("leg count, correlation, and estimated hit probability", rendered)

    def test_compact_estimate_omits_malformed_intervals(self) -> None:
        from kalshi_research_bot.paper_server import slip_estimate_display

        for interval in (None, [], [0.2], [0.2, 0.3, 0.4], "bad", {0: 0.2, 1: 0.3},
                         [float("nan"), 0.9], [0.1, float("inf")], [10 ** 400, 0.9],
                         ["bad", 0.9], [-0.1, 0.9], [0.2, 1.1], [0.9, 0.1]):
            with self.subTest(interval=interval):
                report = {"analysis_available": True, "analysis": {
                    "hit_probability": 0.8, "hit_probability_interval": interval,
                }}
                self.assertEqual(slip_estimate_display(report), ("80.00%", ""))

    def test_dollar_figures_are_an_operator_view(self) -> None:
        reader = self.visible_text(self.page("read_only"))
        operator = self.visible_text(self.page("admin"))
        for figure in ("Est. $5 Payout", "Est. $5 payout", "EV on $5.00", "Est. $"):
            with self.subTest(figure=figure):
                self.assertNotIn(figure, reader)
                self.assertIn(figure, operator)

    def test_every_viewer_still_gets_the_finding(self) -> None:
        # Removing the money must not remove the research. The estimate, the
        # break-even and their difference are what the card is for.
        for role in ("read_only", "admin"):
            rendered = self.page(role)
            with self.subTest(role=role):
                for label in ("Estimate vs. price", "Needs to hit", "Estimated to hit", "Difference", "95% CI", "Leg breakdown"):
                    self.assertIn(label, rendered)

    def test_the_reader_tile_shows_the_listed_price_where_the_operator_sees_dollars(self) -> None:
        reader_tiles = re.findall(r'<div class="tier-meta">\s*<small>[^<]*</small>\s*<small>([^<]*)</small>', self.page("read_only"))
        self.assertTrue(reader_tiles)
        ready = [tile for tile in reader_tiles if tile != "Unavailable"]
        self.assertTrue(ready, "fixture has no ready tier")
        for tile in ready:
            self.assertRegex(tile, r"^Listed \d+\.\d{2}c$")

    def test_the_reader_drawer_lays_out_its_two_figures(self) -> None:
        # Two cells in a three-track grid hug the left edge, so the drawer
        # declares the count and the stylesheet has a rule for it.
        self.assertIn('class="drawer-metrics is-two-up"', self.page("read_only"))
        self.assertIn('class="drawer-metrics"', self.page("admin"))
        self.assertRegex(CSS, r"\.drawer-metrics\.is-two-up\s*\{[^}]*repeat\(2,")

    def test_the_estimate_leads_the_card_and_the_listing_follows(self) -> None:
        """Information order on the card, for both roles.

        The reader's question is what the model thinks against what the price
        requires. The listed contract's own figures are context for that answer,
        so on a card with an analysis they come after it -- and a card without
        one does not open on a dashed box saying so.
        """
        for role in ("read_only", "admin"):
            rendered = self.page(role)
            with self.subTest(role=role):
                cards = re.findall(r'<div class="slip-card">(.*?)<div class="slip-groups">', rendered, flags=re.S)
                self.assertTrue(cards)
                for card in cards:
                    analysis_at = card.find('class="slip-analysis"')
                    listing_at = card.find('class="listed-contract"')
                    self.assertGreater(analysis_at, -1)
                    self.assertGreater(listing_at, -1)
                    self.assertLess(analysis_at, listing_at, "the listed-contract strip came before the estimate")
                    self.assertIn("Listed contract", card)

    def test_a_card_without_an_analysis_opens_on_its_listing(self) -> None:
        from kalshi_research_bot.paper_server import render_slip_section

        payload = make_verified_fixture_payload()
        slip = payload["custom_slip"]
        with patch("kalshi_research_bot.paper_server.build_slip_analysis", return_value={"analysis_available": False, "detail": "nothing priced"}):
            card = render_slip_section(slip, "80c+ MARKET TIER", "primary", payload)
        self.assertLess(card.find('class="listed-contract"'), card.find('class="slip-analysis unavailable"'))

    def test_a_card_whose_analysis_fails_to_render_also_opens_on_its_listing(self) -> None:
        # The report exists, the block does not: the card must not lead with
        # the dashed fallback just because the report said it was available.
        from kalshi_research_bot.paper_server import render_slip_section

        from kalshi_research_bot.paper_server import render_slip_analysis

        # The card's fallback also goes through render_slip_analysis, so only
        # the first call -- the real report -- is made to fail.
        calls: list[dict] = []

        def flaky(report, **kwargs):
            calls.append(report)
            if len(calls) == 1:
                raise KeyError("boom")
            return render_slip_analysis(report, **kwargs)

        payload = make_verified_fixture_payload()
        with patch("kalshi_research_bot.paper_server.render_slip_analysis", side_effect=flaky):
            card = render_slip_section(payload["custom_slip"], "80c+ MARKET TIER", "primary", payload)
        self.assertEqual(len(calls), 2)
        self.assertIn("failed to render", card)
        self.assertLess(card.find('class="listed-contract"'), card.find('class="slip-analysis unavailable"'))


class OperatorFacingDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = make_verified_fixture_payload()
        self.rendered = render_dashboard(self.payload, principal=principal("admin"))

    def test_every_page_carries_an_inline_favicon(self) -> None:
        for page in (self.rendered, render_login_page(), render_operator_page()):
            self.assertIn('rel="icon"', page)
            self.assertIn('name="theme-color"', page)

    def test_build_timestamps_are_localized_by_the_client(self) -> None:
        # The server renders in its own zone; without this the header clock and
        # the event clocks disagreed on the same page.
        self.assertIn('data-format="timestamp"', self.rendered)
        self.assertIn('dataset.format === "timestamp"', JS)

    def test_build_timestamp_keeps_a_server_rendered_fallback(self) -> None:
        from kalshi_research_bot.paper_server import timestamp_element

        element = timestamp_element("2026-08-17T04:44:00+00:00")
        self.assertIn("<time", element)
        self.assertIn('datetime="2026-08-17T04:44:00+00:00"', element)
        # Text between the tags is what a reader without script still sees.
        self.assertRegex(element, r">[^<]+</time>")

    def test_missing_timestamp_does_not_emit_an_empty_time_element(self) -> None:
        from kalshi_research_bot.paper_server import timestamp_element

        self.assertNotIn("<time", timestamp_element(None))

    def test_refresh_status_label_stays_inside_the_topbar(self) -> None:
        # It was absolutely positioned with no positioned ancestor, so it
        # anchored to the sticky topbar and hung below the header border.
        self.assertNotIn("top: 49px", CSS)

    def test_stylesheet_does_not_fight_itself_with_important(self) -> None:
        # The retired sheet carried 77 !important declarations layered over a
        # design that no longer rendered.
        self.assertLessEqual(CSS.count("!important"), 4)

    def test_clipboard_failure_has_a_fallback(self) -> None:
        self.assertIn("execCommand", JS)
        self.assertIn("Copy failed", JS)

    def test_stylesheet_and_script_are_served_as_cacheable_files(self) -> None:
        self.assertRegex(self.rendered, r'<link rel="stylesheet" href="/assets/app\.[0-9a-f]+\.css">')
        self.assertRegex(self.rendered, r'<script src="/assets/app\.[0-9a-f]+\.js" defer>')

    def test_inter_is_self_hosted_rather_than_fetched_from_a_cdn(self) -> None:
        # A strict CSP blocks font CDNs, so a declared-but-unshipped Inter
        # silently fell back to system fonts.
        self.assertIn("@font-face", CSS)
        self.assertRegex(CSS, r"/assets/inter-latin\.[0-9a-f]+\.woff2")
        self.assertNotIn("fonts.googleapis.com", CSS)
        self.assertNotIn("fonts.gstatic.com", CSS)

    def test_internal_reason_codes_are_explained(self) -> None:
        from kalshi_research_bot.paper_server import explain_state_reason

        explained = explain_state_reason("sports_board_unavailable:OperationalError")
        self.assertIn("sports database", explained.lower())
        self.assertIn("OperationalError", explained)
        # An unmapped code still shows rather than silently disappearing.
        self.assertEqual(explain_state_reason("something_new:X"), "something_new:X")
        self.assertEqual(explain_state_reason(""), "")

    def test_readers_get_the_explanation_without_the_exception_class(self) -> None:
        # An operator can go and look at the service, so the class name earns
        # its place. A reader cannot, so it is jargon inside a warning box that
        # has already said what happened.
        from kalshi_research_bot.paper_server import (
            UNEXPLAINED_STATE_REASON,
            explain_state_reason,
        )

        plain = explain_state_reason("sports_board_unavailable:OperationalError", technical=False)
        self.assertIn("sports database", plain.lower())
        self.assertNotIn("OperationalError", plain)
        # An unmapped code degrades to prose rather than leaking the identifier.
        self.assertEqual(explain_state_reason("something_new:X", technical=False), UNEXPLAINED_STATE_REASON)
        self.assertEqual(explain_state_reason("", technical=False), "")

    def test_a_reader_never_sees_a_raw_reason_code_in_the_page(self) -> None:
        from kalshi_research_bot.paper_server import render_sports_clv_panel, render_sports_section

        report = {"graded_rows": 0, "unavailable_reason": "sports_clv_unavailable:OperationalError"}
        board = {
            "board_state": "unavailable",
            "is_current": False,
            "state_reason": "sports_board_unavailable:OperationalError",
            "events": [],
        }
        for name, admin, reader in (
            ("clv", render_sports_clv_panel(report), render_sports_clv_panel(report, technical=False)),
            ("board", render_sports_section(board), render_sports_section(board, technical=False)),
        ):
            with self.subTest(panel=name):
                # Compare the visible text only: the raw code lives in a title
                # attribute in both cases, so leaving it in would mask the
                # difference the gate is supposed to make.
                strip_titles = lambda markup: re.sub(r'\stitle="[^"]*"', "", markup)
                self.assertIn("OperationalError", strip_titles(admin))
                reader_body = strip_titles(reader)
                self.assertNotIn("OperationalError", reader_body)
                self.assertNotIn("sports_clv_unavailable", reader_body)
                self.assertNotIn("sports_board_unavailable", reader_body)
                # The raw code stays one hover away for whoever inspects it.
                self.assertIn("OperationalError", reader)

    def test_unreadable_sports_board_explains_itself(self) -> None:
        board = {
            "board_state": "unavailable",
            "is_current": False,
            "state_reason": "sports_board_unavailable:OperationalError",
            "events": [],
        }
        from kalshi_research_bot.paper_server import render_sports_section

        rendered = render_sports_section(board)
        self.assertIn("sports database", rendered.lower())
        # The raw code stays reachable for whoever is debugging it.
        self.assertIn("sports_board_unavailable:OperationalError", rendered)

    def test_each_page_links_the_script_that_drives_it(self) -> None:
        """Guard the page/asset split from the side that runs without a database.

        The equivalent sign-in assertion lives in a Postgres-gated test, so
        moving that script out of the markup broke only in CI.
        """
        from kalshi_research_bot.dashboard_assets import LOGIN_SCRIPT, OPS_SCRIPT

        login = render_login_page()
        self.assertRegex(login, r'<script src="/assets/login\.[0-9a-f]+\.js" defer>')
        self.assertIn("research_csrf_token", LOGIN_SCRIPT.body.decode("utf-8"))

        ops = render_operator_page()
        self.assertRegex(ops, r'<script src="/assets/ops\.[0-9a-f]+\.js" defer>')
        self.assertIn("operator-messages", OPS_SCRIPT.body.decode("utf-8"))

        for page in (self.rendered, login, ops):
            self.assertNotIn("<script>", page)

    def test_the_sign_in_form_cannot_submit_credentials_in_a_url(self) -> None:
        """A form with no method is a GET, and a GET puts the password in the URL.

        `login.js` intercepts submit and posts JSON, so this never fires on the
        happy path. It does not have to: the script is deferred, so the form is
        interactive while it is still downloading, and a submit before it runs
        used the HTML default. Measured in Chromium with the script absent, that
        produced

            GET /login?username=...&password=...

        with both filled in, putting the password in the address bar, browser
        history, the access log and the Referer header of every request after
        it. (The values are elided because a realistic-looking one here trips
        the repository's secret scanner.) `method="post"` keeps
        the credentials in the body whichever path submits the form.
        """
        login = render_login_page()
        form = re.search(r"<form[^>]*id=\"login-form\"[^>]*>", login)
        self.assertIsNotNone(form, "no sign-in form in the login page")
        markup = form.group(0)
        # Case-insensitive because HTML is: `method="POST"` is a perfectly good
        # form that a case-sensitive guard would reject. The closing quote is
        # what keeps `method="postfoo"` -- which browsers treat as GET -- from
        # passing, so it has to stay inside the pattern.
        self.assertRegex(
            markup,
            r'(?i)method="post"',
            f"the sign-in form would submit as GET, exposing the password: {markup}",
        )
        # The exact endpoint, not merely "an action": pointed anywhere else the
        # fallback stops reaching the sign-in handler, and a guard that only
        # asks for a non-empty value would not notice.
        self.assertRegex(
            markup,
            r'action="/auth/login"',
            f"the fallback submit must reach the sign-in endpoint: {markup}",
        )

    def test_a_script_less_submit_is_explained_rather_than_returned_as_json(self) -> None:
        """`<noscript>` cannot cover a script that fails to load.

        It is suppressed whenever scripting is enabled, and a 404 on
        `login.js` leaves scripting enabled. The form then submits natively,
        arrives url-encoded where the handler wants JSON, and used to render
        `{"error": "invalid_login_payload"}` on screen with nothing to say why.

        The handler answers a url-encoded submit with this page instead. The
        warning must stay off the normal render: shown by default it would
        flash on every load while the deferred script is still fetching, which
        trades a rare failure for a common one.
        """
        self.assertNotIn("did not load", render_login_page())
        warned = render_login_page(script_required=True)
        self.assertIn("did not load", warned)
        self.assertIn('role="alert"', warned)
        self.assertIn('id="login-form"', warned)

    def test_operator_queue_reports_a_failed_load(self) -> None:
        from kalshi_research_bot.dashboard_assets import OPS_SCRIPT

        ops = OPS_SCRIPT.body.decode("utf-8")
        self.assertIn("could not be", ops)
        self.assertIn("catch", ops)


class MetricStripLayoutTests(unittest.TestCase):
    """The figures in a metric strip sit on one line.

    Cells stretch to a common height, so the rows inside a cell would share out
    the slack: once "Estimated to hit" gained a confidence interval underneath,
    its figure sat 9px above the neighbouring "Needs to hit", and a row of four
    numbers read as ragged. Measured in a browser, then pinned here.
    """

    def test_metric_cells_pack_their_rows_from_the_top(self) -> None:
        block = re.search(r"\.metric-strip span \{(.*?)\}", CSS, re.S)
        self.assertIsNotNone(block, "the metric-strip cell rule is gone")
        self.assertIn("align-content: start", block.group(1))

    def test_cells_share_the_strips_rows_so_wrapped_labels_do_not_stagger(self) -> None:
        """align-content squares up cells whose labels are the same height.

        It cannot square up cells whose labels are not: at phone width "Listed
        combo price" wraps to two lines beside a single-line "Leg Floor", and
        its figure dropped 15px below its neighbour's. Only shared row tracks
        let one cell's label reserve height in another. Guarded, so a browser
        without subgrid keeps today's behaviour rather than a worse one.
        """

        rules = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        block = re.search(r"@supports \(grid-template-rows: subgrid\) \{(.*?)\n\}", rules, re.S)
        self.assertIsNotNone(block, "the subgrid guard is gone")
        self.assertIn("grid-template-rows: subgrid", block.group(1))
        self.assertIn("grid-row: span 3", block.group(1))
        self.assertNotIn(
            "grid-row: span 3",
            rules.replace(block.group(0), ""),
            "spanning three rows outside the guard staggers cells where subgrid is unsupported",
        )

    def test_an_interval_reads_as_a_number_not_a_caption(self) -> None:
        """``.metric-strip small`` uppercases and tracks out every ``small``,
        which is right for the cell label above and wrong for "95% CI 26.8-27.6%"."""

        block = re.search(r"\.metric-strip small\.metric-range \{(.*?)\}", CSS, re.S)
        self.assertIsNotNone(block, "the interval rule is gone")
        self.assertIn("text-transform: none", block.group(1))


class ComposedDashboardPanelTests(unittest.TestCase):
    """The panels the composed page reads from the database, rendered.

    `render_dashboard` does not take the sports board, the closing-line report
    or the research record from its payload -- it calls `safe_sports_board`,
    `safe_sports_clv_report` and `build_research_record` itself. Without
    PostgreSQL all three return their empty forms, so every golden render
    exercised the slip cards and nothing else, and four panels appeared in no
    test, screenshot or local preview.

    That gap has already cost something: a change to `render_sports_event` once
    made the whole board raise NameError, and nothing caught it, because no test
    had ever rendered a board with an event in it. The per-panel tests in
    `test_sports_board` and `test_sports_clv` cover the renderers; what was
    missing is the page that composes them.

    Patched rather than seeded, so this stays a pure unit test and runs wherever
    the suite runs.
    """

    def render(self, role: str = "admin") -> str:
        from kalshi_research_bot import browser_fixtures as fixtures

        with patch(
            "kalshi_research_bot.paper_server.safe_sports_board",
            return_value=fixtures.make_fixture_sports_board(),
        ), patch(
            "kalshi_research_bot.paper_server.safe_sports_clv_report",
            return_value=fixtures.make_fixture_sports_clv_report(),
        ), patch(
            "kalshi_research_bot.paper_server.build_research_record",
            return_value=fixtures.make_fixture_research_record(),
        ):
            return render_dashboard(
                fixtures.make_verified_fixture_payload(), principal=principal(role)
            )

    def test_the_sports_board_renders_its_events(self) -> None:
        rendered = self.render()
        for marker in ('class="sports-event"', 'class="sports-market"', "sports-selection"):
            with self.subTest(marker=marker):
                self.assertIn(marker, rendered)
        self.assertIn("Away Team @ Home Team", rendered)

    def test_both_market_shapes_reach_the_page(self) -> None:
        """A de-vigged market and a one-sided one take different branches, and
        only rendering both proves neither raises."""

        rendered = self.render()
        self.assertIn("Overround", rendered)
        self.assertIn("no de-vig", rendered)

    def test_a_shopping_gain_renders_as_a_pill(self) -> None:
        self.assertIn("sports-shop-pill", self.render())

    def test_the_record_shows_a_measured_rate_and_an_absent_one(self) -> None:
        """Both states on one page: the accent belongs to the measured card
        alone, which is only checkable when both are present."""

        rendered = self.render()
        self.assertIn("is-measured", rendered)
        self.assertIn("is-absent", rendered)
        self.assertIn("95% CI 57-75%", rendered)

    def test_the_closing_line_panel_renders_graded_rows(self) -> None:
        rendered = self.render()
        self.assertIn("Closing line value", rendered)
        self.assertIn("95% CI +0.80 to +1.60 pts", rendered)
        # An interval clear of zero is this panel's one "result" state.
        self.assertIn('class="decision good"', rendered)

    def test_a_reader_gets_the_product_without_the_operator_panels(self) -> None:
        """The role gate still holds once these panels have data to withhold.

        Empty panels are trivially absent for every role; this is the first time
        the gate is exercised against panels that would otherwise render.
        """

        reader = self.render("read_only")
        self.assertIn("Estimate vs. price", reader)
        for marker in ('id="refresh-slip"', "Track Record"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, reader)


class DashboardAssetTests(unittest.TestCase):
    """Guard against shipping CSS and JS that address markup nothing renders."""

    def setUp(self) -> None:
        payload = make_verified_fixture_payload()
        self.rendered_pages = [
            render_dashboard(payload, principal=principal("admin")),
            render_dashboard(payload, principal=principal("read_only")),
            render_login_page(),
            render_operator_page(),
        ]

    def _element_ids(self) -> set[str]:
        ids: set[str] = set()
        for page in self.rendered_pages:
            ids.update(re.findall(r'id="([A-Za-z][\w-]*)"', page))
        return ids

    def test_javascript_only_targets_ids_that_exist(self) -> None:
        referenced = set(re.findall(r'querySelector\(["\']#([A-Za-z][\w-]*)', JS))
        missing = sorted(referenced - self._element_ids())
        self.assertEqual(missing, [], f"JS targets ids no page renders: {missing}")

    def test_every_rendered_class_is_styled_or_used_by_script(self) -> None:
        """No class survives in markup unless something acts on it.

        Retired class names are how the previous stylesheet accumulated an
        override layer nobody could safely touch.
        """
        from kalshi_research_bot.dashboard_assets import LOGIN_SCRIPT, OPS_SCRIPT

        rendered_classes: set[str] = set()
        for page in self.rendered_pages:
            for attribute in re.findall(r'class="([^"]*)"', page):
                rendered_classes.update(token for token in attribute.split() if token)

        script_text = "\n".join(
            asset.body.decode("utf-8") for asset in (SCRIPT, LOGIN_SCRIPT, OPS_SCRIPT)
        )
        script_hooks = set(re.findall(r'classList\.(?:add|toggle|remove)\(["\']([^"\']+)', script_text))
        script_hooks |= set(re.findall(r'querySelector(?:All)?\(["\'][^"\']*?\.([\w-]+)', script_text))
        styled = set(re.findall(r"\.([A-Za-z][\w-]*)", CSS))

        orphans = sorted(rendered_classes - styled - script_hooks)
        self.assertEqual(orphans, [], f"classes rendered but never used: {orphans}")

    def test_stylesheet_has_no_leftover_layout_classes(self) -> None:
        # Classes from retired layouts keep accumulating override rules; this
        # names the ones already removed so they cannot quietly return.
        retired = {"hero", "hero-meta", "refresh-box", "cards", "ghost"}
        present = {name for name in retired if re.search(rf"\.{name}(?![\w-])", CSS)}
        self.assertEqual(present, set(), f"retired classes still styled: {sorted(present)}")


class ResponseCompressionTests(unittest.TestCase):
    """Text responses go out gzipped, and secret-bearing ones deliberately do not.

    The dashboard HTML is `no-store`, so it is re-sent in full on every load --
    it was the largest thing on the wire and the only one paid for repeatedly.
    These run against a real socket because the behaviour under test is the
    response, not the render.
    """

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile
        import threading
        from http.server import ThreadingHTTPServer
        from pathlib import Path

        from kalshi_research_bot.paper_server import PaperHandler

        cls._env = patch.dict(
            os.environ,
            {
                "DASHBOARD_PAYLOAD_SOURCE": "file",
                "DASHBOARD_AUTH_ENABLED": "false",
                "DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED": "false",
                "DASHBOARD_USER_AUTH_ENABLED": "false",
            },
            clear=False,
        )
        cls._env.start()

        cls._tmp = tempfile.mkdtemp()
        payload_path = Path(cls._tmp) / "payload.json"
        payload_path.write_text(json.dumps(make_verified_fixture_payload()), encoding="utf-8")

        class Handler(PaperHandler):
            data_path = payload_path
            refresh_seconds = 0
            refresh_config: dict = {}
            refresh_status: dict = {"state": "idle", "message": "Ready"}

            def log_message(self, *args: object) -> None:
                return

        cls._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls.base = f"http://127.0.0.1:{cls._server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()
        cls._thread.join(timeout=5)
        cls._env.stop()

    def fetch(self, path: str, accept_encoding: str | None = "gzip"):
        """Return the response headers and the raw (still-encoded) body.

        The headers object is returned as-is rather than as a dict: HTTP header
        names are case-insensitive, and `email.message.Message` honours that
        while a dict does not. Copying into one would make `assertNotIn` pass
        for a header that is present under different casing.
        """
        import urllib.request

        headers = {"Accept-Encoding": accept_encoding} if accept_encoding is not None else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.headers, response.read()

    def asset_url(self, suffix: str) -> str:
        """Find a hashed asset URL by following the references the page makes.

        The font is reached through the stylesheet rather than the page, so the
        search widens to the CSS instead of assuming a fingerprint.
        """
        _, page = self.fetch("/", accept_encoding=None)
        pattern = rf"/assets/[\w.-]+\.[0-9a-f]{{12}}{re.escape(suffix)}"
        match = re.search(pattern, page.decode("utf-8"))
        if match is None:
            _, css = self.fetch(self.asset_url(".css"), accept_encoding=None)
            match = re.search(pattern, css.decode("utf-8"))
        self.assertIsNotNone(match, f"no {suffix} asset referenced by the page or its stylesheet")
        return match.group(0)

    def test_html_is_compressed_and_round_trips_to_the_same_bytes(self) -> None:
        plain_headers, plain = self.fetch("/", accept_encoding=None)
        gzip_headers, packed = self.fetch("/")
        self.assertNotIn("Content-Encoding", plain_headers)
        self.assertEqual(gzip_headers.get("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(packed), plain)
        self.assertLess(len(packed), len(plain))

    def test_an_explicit_refusal_is_honoured(self) -> None:
        # `gzip;q=0` is a client saying it does not want gzip. Matching the bare
        # token would send it an encoding it just declined.
        headers, body = self.fetch("/", accept_encoding="gzip;q=0, identity")
        self.assertIsNone(headers.get("Content-Encoding"))
        # Readable as-is, so it really was sent unencoded.
        self.assertTrue(body[:64].lower().startswith(b"<!doctype html>"), body[:64])

    def test_a_named_encoding_outranks_the_wildcard(self) -> None:
        """`gzip;q=0, *` is a refusal of gzip, not a wildcard acceptance.

        Scanning for the first token with a positive quality got this backwards:
        it skipped the explicit `gzip;q=0` and then matched `*`, sending the
        client exactly the encoding it had just declined.
        """
        for header in ("gzip;q=0, *", "*, gzip;q=0", "gzip;q=0.0, *;q=1.0"):
            with self.subTest(accept_encoding=header):
                headers, body = self.fetch("/", accept_encoding=header)
                self.assertIsNone(headers.get("Content-Encoding"), header)
                self.assertTrue(body[:64].lower().startswith(b"<!doctype html>"))
        # The wildcard alone is still an acceptance.
        headers, _ = self.fetch("/", accept_encoding="*")
        self.assertEqual(headers.get("Content-Encoding"), "gzip")

    def test_every_negotiated_response_varies_on_accept_encoding(self) -> None:
        # One URL can now answer with two different bodies, so a shared cache
        # has to key on what the client asked for.
        for path in ("/", self.asset_url(".css"), self.asset_url(".js")):
            for encoding in ("gzip", None):
                with self.subTest(path=path, accept_encoding=encoding):
                    headers, _ = self.fetch(path, accept_encoding=encoding)
                    self.assertEqual(headers.get("Vary"), "Accept-Encoding")

    def test_static_text_assets_are_compressed(self) -> None:
        for suffix in (".css", ".js"):
            with self.subTest(asset=suffix):
                path = self.asset_url(suffix)
                _, plain = self.fetch(path, accept_encoding=None)
                headers, packed = self.fetch(path)
                self.assertEqual(headers.get("Content-Encoding"), "gzip")
                self.assertEqual(gzip.decompress(packed), plain)

    def test_the_font_is_left_alone(self) -> None:
        # woff2 carries its own compression; gzipping it again spends CPU to
        # make the response marginally larger.
        path = self.asset_url(".woff2")
        headers, packed = self.fetch(path)
        _, plain = self.fetch(path, accept_encoding=None)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(packed, plain)

    def test_json_responses_are_never_compressed(self) -> None:
        """The BREACH guard, asserted rather than left to a comment.

        `send_json` answers the login POST with a CSRF token in its body.
        Compressing a secret alongside text an attacker can influence is the
        precondition that attack needs, so JSON stays uncompressed.

        The endpoint under test has to be one whose body clears
        `MIN_COMPRESSIBLE_BYTES`, or the assertion passes for the wrong reason:
        a 62-byte `/healthz` would come back uncompressed under any policy,
        including one that compressed JSON. `/data.json` is several kilobytes,
        so it would be encoded if the rule were keyed on size alone. The login
        POST itself needs a database, which would confine this to environments
        that have one -- `send_json` is the shared code path, so exercising it
        through a large public response covers the same branch.
        """
        from kalshi_research_bot.paper_server import MIN_COMPRESSIBLE_BYTES

        headers, body = self.fetch("/data.json")
        self.assertGreater(
            len(body),
            MIN_COMPRESSIBLE_BYTES,
            "pick a larger JSON endpoint; this one cannot show the policy",
        )
        self.assertIsNone(headers.get("Content-Encoding"))
        json.loads(body)

    def test_compression_is_a_real_saving_not_a_rounding_error(self) -> None:
        # Guards against a future change that technically still compresses but
        # stops mattering -- the point of the work was the size on the wire.
        _, plain = self.fetch("/", accept_encoding=None)
        _, packed = self.fetch("/")
        self.assertLess(len(packed), len(plain) / 2, "gzip should at least halve the page")


class ShippedFontTests(unittest.TestCase):
    """The self-hosted Inter files, guarded without a font-parsing dependency.

    `scripts/optimize_dashboard_fonts.py` narrows the variable weight axis from
    100-900 to the 400-700 the stylesheet actually asks for. The first attempt
    at that script also re-partitioned character coverage from guessed Unicode
    blocks and produced a *valid* 904-byte font mapping zero codepoints -- it
    would have loaded without error and rendered every accented name in a
    fallback face.

    So the size window has two jobs, and the lower bound is the interesting one:
    the ceiling catches an unoptimized font being dropped back in, and the floor
    catches one that has been gutted. fontTools is a build-time tool rather than
    a runtime dependency, so these read bytes and CSS text instead of parsing
    tables, which keeps them running everywhere.
    """

    # (floor, ceiling) in bytes. Wide enough not to trip on an upstream Inter
    # release, tight enough that neither failure mode fits through.
    BUDGETS = {
        "inter-latin.woff2": (20_000, 40_000),
        "inter-latin-ext.woff2": (6_000, 16_000),
    }
    DECLARED_AXIS = (400, 700)

    def font_path(self, name: str):
        from kalshi_research_bot import dashboard_assets

        return pathlib.Path(dashboard_assets.__file__).parent / name

    def test_each_font_is_within_its_size_budget(self) -> None:
        for name, (floor, ceiling) in self.BUDGETS.items():
            with self.subTest(font=name):
                size = self.font_path(name).stat().st_size
                self.assertGreater(
                    size, floor, f"{name} is {size} B -- too small to still carry its glyphs"
                )
                self.assertLess(
                    size, ceiling, f"{name} is {size} B -- run scripts/optimize_dashboard_fonts.py"
                )

    def test_the_face_declares_the_axis_the_files_were_built_for(self) -> None:
        # Both @font-face blocks must advertise the range the binaries carry;
        # promising a weight the font no longer has gets it synthesized.
        declared = re.findall(
            r"@font-face\s*\{[^}]*?font-weight:\s*(\d+)\s+(\d+)", self.rules(), re.S | re.I
        )
        self.assertEqual(len(declared), len(self.BUDGETS), "expected one @font-face per shipped file")
        for low, high in declared:
            self.assertEqual((int(low), int(high)), self.DECLARED_AXIS)

    # `normal` and `bold` are the two weight keywords with a fixed numeric
    # value. The global keywords resolve either to the initial value (400) or
    # to an inherited one, and every inherited value is itself a declaration
    # this test checks, so they are in range whenever the rest of the sheet is.
    FIXED_KEYWORDS = {"normal": 400, "bold": 700}
    SAFE_KEYWORDS = frozenset({"inherit", "initial", "unset", "revert", "revert-layer"})

    @staticmethod
    def rules() -> str:
        """The stylesheet with its comments removed.

        A comment is not a rule. Scanning the raw text reads `font-weight: 900`
        out of prose that documents why 900 is unavailable, and fails a build
        that changed nothing about what renders.
        """
        return re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)

    def author_weight_declarations(self) -> list[str]:
        """Every `font-weight` value in the sheet except the @font-face descriptors.

        The descriptors state what the binaries carry and are checked by the
        test above; everything else is a request against that range. Excising
        the blocks rather than reading from the last one means a rule placed
        before or between them is still seen -- @font-face blocks contain no
        nested braces, so a non-greedy match to the first `}` ends each one.

        Property names are matched case-insensitively because CSS treats them
        that way: `FONT-WEIGHT: 900` is a real declaration a browser honours,
        and a guard it can walk past is not a guard.
        """
        authored = re.sub(r"@font-face\s*\{[^}]*\}", "", self.rules(), flags=re.S | re.I)
        return [
            value.strip()
            for value in re.findall(r"font-weight\s*:\s*([^;}]+)", authored, flags=re.I)
        ]

    def test_no_rule_asks_for_a_weight_the_font_cannot_render(self) -> None:
        """A weight outside the axis is synthesized by the browser, not drawn.

        This is what keeps the axis honest: narrowing it is only safe while
        nothing requests a weight outside it, and that is a property of the
        stylesheet rather than of the font.
        """
        low, high = self.DECLARED_AXIS
        declarations = self.author_weight_declarations()
        self.assertTrue(declarations, "found no font-weight rules; the scan is not looking at the sheet")
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                value = re.sub(r"\s*!important$", "", declaration).strip().lower()
                if value in self.SAFE_KEYWORDS:
                    continue
                weight = self.FIXED_KEYWORDS.get(value)
                if weight is None:
                    # `bolder`/`lighter` step off the parent's weight and can
                    # land outside the axis; var() cannot be resolved here.
                    # Either needs a person to decide, not a silent pass.
                    self.assertRegex(
                        value, r"^\d+(\.\d+)?$", f"font-weight: {declaration} -- cannot be checked against the axis"
                    )
                    weight = float(value)
                self.assertGreaterEqual(weight, low, f"font-weight: {declaration} is below the axis")
                self.assertLessEqual(weight, high, f"font-weight: {declaration} is above the axis")

    def test_the_font_shorthand_never_sets_a_weight(self) -> None:
        """Keeps the scan above honest: it reads `font-weight`, not `font`.

        The shorthand can carry a weight too, so if one ever appears there the
        axis check would be quietly incomplete. This fails instead, which is
        the prompt to teach the scan about it.
        """
        for value in re.findall(r"(?<![\w-])font\s*:\s*([^;}]+)", self.rules(), flags=re.I):
            with self.subTest(shorthand=value.strip()):
                # Everything before the `/` is the weight-and-size half; a
                # var() after it supplies line-height, which is not a weight.
                head = value.strip().lower().split("/")[0]
                tokens = set(re.split(r"\s+", head))
                weights = tokens & (set(self.FIXED_KEYWORDS) | {"bolder", "lighter"})
                numeric = {token for token in tokens if re.fullmatch(r"\d+(\.\d+)?", token)}
                self.assertFalse(
                    weights or numeric or "var(" in head,
                    f"font: {value.strip()} may set a weight; extend the font-weight scan to cover the shorthand",
                )


class SourceAgeTests(unittest.TestCase):
    """The operations panel's freshness chip, which had no test at all.

    That is how it came to answer "Fresh" for an age it did not have.
    `build_source_quality_report` sets `data_age_seconds` to None in exactly one
    situation -- `generated_at` missing or unparseable -- where it also records
    `missing_or_invalid_generated_at` and takes twenty points off the quality
    score. The panel turned the platform's least-informed state into its
    strongest word for freshness, and could print it beside its own
    "Review blocked" heading.
    """

    def chip(self, status: dict, gate: dict | None = None) -> tuple[str, str]:
        from kalshi_research_bot.paper_server import render_quality_panel

        match = re.search(
            r"<strong>([^<]*)</strong><span>([^<]*)</span>",
            render_quality_panel(status, gate),
        )
        assert match, "quality panel rendered no status heading"
        return match.group(1), match.group(2)

    def test_an_age_the_platform_does_not_have_is_not_called_fresh(self) -> None:
        from kalshi_research_bot.paper_server import source_age_text

        for absent in (None, ""):
            with self.subTest(absent=absent):
                self.assertEqual(source_age_text(absent), "Age unknown")

    def test_the_heading_and_the_chip_cannot_contradict_each_other(self) -> None:
        """A blocked payload has no `data_age_seconds`, which is how the two
        halves of one heading came to read "Review blocked" and "Fresh"."""

        heading, chip = self.chip({}, {"status": "blocked", "message": "Live data is too old."})
        self.assertEqual(heading, "Review blocked")
        self.assertEqual(chip, "Age unknown")

    def test_a_measured_zero_is_not_the_same_answer_as_no_measurement(self) -> None:
        from kalshi_research_bot.paper_server import source_age_text

        self.assertEqual(source_age_text(0), "0s old")
        self.assertNotEqual(source_age_text(0), source_age_text(None))

    def test_the_ladder_does_not_run_out_above_hours(self) -> None:
        """A payload stopped in 2000 read `233712h old`, which no reader
        converts. Hours were the top tier and had no ceiling."""

        from kalshi_research_bot.paper_server import source_age_text

        self.assertEqual(source_age_text(45), "45s old")
        self.assertEqual(source_age_text(240), "4m old")
        self.assertEqual(source_age_text(7200), "2h old")
        self.assertEqual(source_age_text(86399), "23h old")
        self.assertEqual(source_age_text(172800), "2d old")
        self.assertNotIn("h old", source_age_text(841363200))

    def test_an_unparseable_age_does_not_take_the_page_down(self) -> None:
        """`int(float(age))` raised ValueError on a non-numeric field, and this
        text is drawn on the operations page -- one bad value 500ed all of it."""

        from kalshi_research_bot.paper_server import source_age_text

        # Every junk type that actually reaches this function, chosen by which
        # exception each one raises rather than by what looks like junk. The
        # first version of this test used `"unknown"` and `object()` -- a
        # ValueError and a TypeError -- and passed while a list, a dict, an
        # infinity and an oversized int all still crashed the page:
        #
        #   []             TypeError    unhashable, raised by the `in {None, ""}`
        #   {}             TypeError    guard itself, before any parsing
        #   float("inf")   OverflowError  int(float("inf"))
        #   10 ** 400      OverflowError  float(10 ** 400)
        #
        # A test that covers the harmless junk and not the harmful junk reads
        # like coverage and is not.
        for junk in ("unknown", "n/a", float("nan"), object(), [], {}, (1, 2),
                     float("inf"), float("-inf"), 10 ** 400):
            with self.subTest(junk=repr(junk)):
                self.assertEqual(source_age_text(junk), "Age unknown")


class ProbabilityPointsTests(unittest.TestCase):
    """A difference between two probabilities is in points, not percent.

    The estimate-versus-price card read

        Needs to hit 56.47%   Estimated to hit 60.1%   Difference +3.6%

    and that last figure is 60.1 minus 56.47. Written with a percent sign it
    also reads as 3.6% *of* 56.47%, which is 2.0 points -- nearly a factor of
    two on the one number the card exists to communicate.

    The platform had already settled this and not applied it: the closing-line
    panel quotes "+1.20 pts" and the report's own name for the measure is
    `probability_points_versus_closing_line`. Four other places said "%".
    """

    def test_a_difference_of_probabilities_carries_its_unit(self) -> None:
        from kalshi_research_bot.paper_server import probability_points

        self.assertEqual(probability_points(0.036), "+3.6 pts")
        self.assertEqual(probability_points(-0.011), "-1.1 pts")
        self.assertEqual(probability_points(0.012, 2), "+1.20 pts")
        self.assertEqual(probability_points(0.036, signed=False), "3.6 pts")

    def test_an_absent_difference_is_not_a_measured_zero(self) -> None:
        """Same rule as every other statistic on this page."""

        from kalshi_research_bot.paper_server import probability_points

        for absent in (None, "", "unknown", [], {}):
            with self.subTest(absent=repr(absent)):
                self.assertEqual(probability_points(absent), "n/a")

    def test_the_closing_line_panel_keeps_the_wording_it_established(self) -> None:
        """`_clv_points_text` now delegates, so the convention has one
        definition rather than two that can drift apart."""

        from kalshi_research_bot.paper_server import _clv_points_text

        self.assertEqual(_clv_points_text("0.0120"), "+1.20 pts")
        self.assertEqual(_clv_points_text(None), "n/a")


class RenderedUnitsTests(unittest.TestCase):
    """The units as a reader actually receives them, from a full render."""

    def setUp(self) -> None:
        from kalshi_research_bot.browser_fixtures import (
            make_fixture_research_record,
            make_fixture_sports_board,
            make_fixture_sports_clv_report,
        )
        from kalshi_research_bot.paper_server import render_dashboard

        with patch(
            "kalshi_research_bot.paper_server.safe_sports_board",
            return_value=make_fixture_sports_board(),
        ), patch(
            "kalshi_research_bot.paper_server.safe_sports_clv_report",
            return_value=make_fixture_sports_clv_report(),
        ), patch(
            "kalshi_research_bot.paper_server.build_research_record",
            return_value=make_fixture_research_record(),
        ):
            self.rendered = render_dashboard(
                make_verified_fixture_payload(), principal=principal("admin")
            )
        self.text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.rendered))

    def test_the_estimate_versus_price_card_reads_in_points(self) -> None:
        found = re.findall(r"Difference ([+-][\d.]+(?: pts|%))", self.text)
        self.assertTrue(found, "no Difference figure rendered; fixture no longer reaches the card")
        for value in found:
            with self.subTest(value=value):
                self.assertTrue(value.endswith(" pts"), f"Difference rendered as {value}")

    def test_the_price_comparison_pills_read_in_points(self) -> None:
        """`Shop +1.5%` and `vs consensus +0.8%` are gaps between two implied
        probabilities, the same quantity under different names."""

        for label in ("Shop", "vs consensus"):
            with self.subTest(label=label):
                found = re.findall(rf"{label} ([+-][\d.]+(?: pts|%))", self.text)
                # Asserted, not assumed: both pills render only on a positive
                # gap, so a fixture change could leave this loop iterating over
                # nothing and passing. Today they read +0.6 and +1.1.
                self.assertTrue(found, f"no {label} pill rendered; fixture no longer reaches it")
                for value in found:
                    self.assertTrue(value.endswith(" pts"), f"{label} rendered as {value}")

    def test_a_probability_itself_is_still_a_percentage(self) -> None:
        """The change is to differences only. A probability is a percentage and
        must not be relabelled, or the fix trades one wrong unit for another."""

        self.assertIn("Needs to hit", self.text)
        self.assertRegex(self.text, r"Needs to hit [\d.]+%")
        self.assertRegex(self.text, r"Estimated to hit [\d.]+%")


class DisplayFormatterTests(unittest.TestCase):
    """No display formatter may render a number that is not one.

    Every formatter in this module had written its own numeric coercion and
    every one had the same two holes. `float()` raises OverflowError -- not
    TypeError, not ValueError -- on an int too large to convert, so `10 ** 400`
    escaped each `except` clause and 500ed the page. NaN and infinity convert
    without complaint, so they rendered onto the card:

        percent               nan%        inf%       OverflowError
        money                 nan         inf        OverflowError
        dollars               $nan        $inf       OverflowError
        probability_points    +nan pts    +inf pts   OverflowError
        format_american_odds  nan         +inf       OverflowError

    A crash is at least visible; `+nan pts` sits on the estimate-versus-price
    card looking like a measurement. cubic raised one of these on
    `probability_points`; the same shape was in all six.
    """

    def formatters(self) -> list[tuple[str, object, str]]:
        """Every formatter, with the word each uses for a value it cannot read."""

        from kalshi_research_bot import paper_server as ps

        return [
            ("percent", ps.percent, "n/a"),
            ("money", ps.money, "n/a"),
            ("dollars", ps.dollars, "n/a"),
            ("probability_points", ps.probability_points, "n/a"),
            ("_clv_points_text", ps._clv_points_text, "n/a"),
            ("format_american_odds", ps.format_american_odds, "n/a"),
            ("format_market_line", lambda v: ps.format_market_line(v, "total"), ""),
        ]

    def test_no_formatter_renders_a_non_finite_number(self) -> None:
        for name, fn, absent in self.formatters():
            for label, value in (
                ("nan", float("nan")),
                ("inf", float("inf")),
                ("-inf", float("-inf")),
                ("oversized int", 10 ** 400),
                # As strings, because a numeric field crossing JSON arrives as
                # one. `finite_number` refused these from the start; the echo
                # fallback handed them straight back until it split on whether
                # the value parses rather than on its Python type.
                ("nan as text", "nan"),
                ("NaN as text", "NaN"),
                ("Infinity as text", "Infinity"),
                ("overflowing text", "1e400"),
                # Decimal because the sports board and the closing-line report
                # hand this module every number as one, and two versions of the
                # guard branched on Python type and let it straight through.
                ("Decimal NaN", Decimal("NaN")),
                ("Decimal Infinity", Decimal("Infinity")),
                ("Decimal -Infinity", Decimal("-Infinity")),
                ("a list", []),
                ("a dict", {}),
            ):
                with self.subTest(formatter=name, value=label):
                    self.assertEqual(fn(value), absent)

    def test_a_string_it_cannot_read_is_still_echoed(self) -> None:
        """Deliberate, and preserved: an odds or line value in a format this
        code does not recognise is more use to a reader shown than swallowed.
        A non-finite float is not that case -- it is a number meaning no
        number -- which is why the two are separated rather than merged.
        """

        from kalshi_research_bot.paper_server import format_american_odds, money

        self.assertEqual(money("EVEN"), "EVEN")
        self.assertEqual(format_american_odds("EVEN"), "EVEN")

    def test_ordinary_values_are_untouched(self) -> None:
        from kalshi_research_bot import paper_server as ps

        self.assertEqual(ps.percent(0.036), "3.60%")
        self.assertEqual(ps.money(-1.5), "-1.50")
        self.assertEqual(ps.dollars(-1.5), "-$1.50")
        self.assertEqual(ps.probability_points(0.036), "+3.6 pts")
        self.assertEqual(ps.format_american_odds(118), "+118")
        self.assertEqual(ps.format_market_line("218.5", "total"), "218.5")

    # `finite_number` and `unreadable_number` are the two functions allowed to
    # coerce: the first decides, the second classifies what the first refused.
    COERCION_HELPERS = ("finite_number", "unreadable_number")
    # Anything that turns arbitrary input into a number. Named rather than just
    # `float`, because the hole is reintroduced as easily by `int("1e400")` or
    # `Decimal(value)` as by the call this started with.
    COERCION_CALLS = ("float", "int", "Decimal", "complex")

    # Formatters shaped like the others but handed a timestamp rather than a
    # number. Listed so the inventory check below can tell "not a number
    # formatter" from "a number formatter nobody exercised".
    NOT_NUMERIC = ("display_timestamp", "timestamp_element", "display_event_time")

    def module_formatters(self) -> list[str]:
        """Every display formatter the module actually defines.

        Read from `paper_server` rather than listed here, because a
        hand-maintained inventory is how a renderer ends up outside the guard
        that was written for it -- the same failure as the hand-written fixtures
        in #100. The shape is unambiguous: a module-level function taking
        `value` first and returning `str`.
        """

        import ast
        import inspect

        from kalshi_research_bot import paper_server as ps

        tree = ast.parse(inspect.getsource(ps))
        return [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.args.args
            and node.args.args[0].arg == "value"
            and self.returns_a_string(node)
        ]

    # Every way of annotating "this returns display text", and nothing else.
    # Matching a bare `ast.Name` was too narrow -- `-> "str"` is a Constant and
    # `-> str | None` a BinOp, so each dropped its formatter out of the
    # inventory silently. Searching the unparsed text for the word was then too
    # wide: `tuple[str, str]` contains it and returns no such thing. Comparing
    # the whole normalised annotation against this set is neither.
    STRING_ANNOTATIONS = frozenset(
        {"str", "str | None", "None | str", "Optional[str]", "typing.Optional[str]"}
    )

    @classmethod
    def returns_a_string(cls, node) -> bool:
        import ast

        if node.returns is None:
            return False
        # Quotes stripped because `from __future__ import annotations` makes the
        # quoted form ordinary, and it means exactly the same thing.
        return ast.unparse(node.returns).strip().strip("\"'") in cls.STRING_ANNOTATIONS

    def test_the_exercised_inventory_is_the_modules_inventory(self) -> None:
        """A formatter added later fails this until someone classifies it."""

        classified = (
            {name for name, _, _ in self.formatters()}
            | set(self.COERCION_HELPERS)
            | set(self.NOT_NUMERIC)
        )
        unclassified = sorted(set(self.module_formatters()) - classified)
        self.assertEqual(
            unclassified,
            [],
            "these display formatters are exercised by nothing above; add them to "
            f"formatters() or to NOT_NUMERIC with a reason: {unclassified}",
        )

    def formatter_bodies(self):
        import ast
        import inspect

        from kalshi_research_bot import paper_server as ps

        wanted = {name for name, _, _ in self.formatters()}
        tree = ast.parse(inspect.getsource(ps))
        return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in wanted]

    def test_every_formatter_defers_to_the_shared_coercion(self) -> None:
        """The structural half, stated positively.

        Six copies of a guard is how six of them came to be wrong the same way.
        A blacklist of `float` was the first version of this test and it was too
        weak: `int()`, `Decimal()` or an aliased `float` reintroduce the hole
        and pass. So each formatter must *reach* the shared coercion, either by
        calling it or by delegating to another formatter that does.
        """

        import ast

        allowed = set(self.COERCION_HELPERS) | {name for name, _, _ in self.formatters()}
        missing = []
        for node in self.formatter_bodies():
            called = {
                inner.func.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            }
            if not called & allowed:
                missing.append(node.name)
        self.assertEqual(
            missing,
            [],
            f"these formatters reach neither the shared coercion nor a formatter that does: {missing}",
        )

    def test_no_formatter_coerces_its_own_input(self) -> None:
        """And the negative half, broadened past the one call it started with.

        Kept alongside the positive check because a formatter can both call
        `finite_number` and then coerce something itself, which passes the test
        above while putting the hole back.
        """

        import ast

        offenders = []
        for node in self.formatter_bodies():
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                name = (
                    inner.func.id
                    if isinstance(inner.func, ast.Name)
                    else inner.func.attr
                    if isinstance(inner.func, ast.Attribute)
                    else None
                )
                if name in self.COERCION_CALLS:
                    offenders.append(f"{node.name}:{inner.lineno} calls {name}()")
        self.assertEqual(
            offenders,
            [],
            f"these formatters coerce their own numbers instead of using finite_number: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
