import json
import os
import tempfile
import time
import unittest
import io
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kalshi_research_bot.auth import AuthPrincipal
from kalshi_research_bot.dashboard_assets import SCRIPT
from kalshi_research_bot.cli import main
from kalshi_research_bot.combo_safety import VERIFIED_COMBO_EVIDENCE, VERIFIED_COMBO_SOURCE, combo_leg_signature
from kalshi_research_bot.connectors.http import HttpClient, ResponseTooLargeError, prune_http_cache
from kalshi_research_bot.paper_server import (
    append_jsonl,
    build_quality_status,
    load_current_payload,
    refresh_payload,
    render_dashboard,
)
from kalshi_research_bot.source_quality import (
    _build_core_quality,
    _build_deployment_readiness,
    _build_workflow_quality,
    _optional_capability_status,
    active_refresh_errors,
    build_data_quality_report,
    build_zero_heartbeat_diagnosis,
    evaluate_prediction_table,
    evaluate_source_records,
    render_data_quality_report,
)
from kalshi_research_bot.connectors.status import build_connectors_status
from kalshi_research_bot.database import connection_pool
from tests.postgres_support import reset_database, test_settings


class QualityTests(unittest.TestCase):
    def test_quality_semantics_separate_core_workflows_and_optional_connectors(self):
        guardrails = {
            "research_only": True,
            "auto_trade_enabled": False,
            "kalshi_order_upload_enabled": False,
            "real_money_execution_enabled": False,
            "automatic_upload_enabled": False,
            "model_promotion_enabled": False,
            "stale_cache_as_fresh": False,
        }
        core = _build_core_quality(
            database_available=True,
            metric_checks={"metric_guard": "pass"},
            guardrails=guardrails,
        )
        kalshi = _build_workflow_quality("kalshi", [{"status": "OK", "score": 100, "name": "dashboard"}])
        crypto = _build_workflow_quality("crypto", [{"status": "OK", "score": 100, "name": "source"}])
        sports = _build_workflow_quality("sports", [{"status": "BLOCKED", "score": 20, "name": "source"}])
        capabilities = _optional_capability_status(build_connectors_status(env={}))
        readiness = _build_deployment_readiness({}, guardrails=guardrails)

        self.assertEqual(core["score"], 100)
        self.assertTrue(kalshi["ready"])
        self.assertTrue(crypto["ready"])
        self.assertFalse(sports["ready"])
        self.assertEqual(capabilities["firecrawl"]["state"], "unavailable_optional")
        self.assertFalse(readiness["ready"])
        self.assertIn("postgres_parity_validated", readiness["blockers"])

    def test_required_firecrawl_is_reported_without_changing_core_math(self):
        capability = _optional_capability_status(build_connectors_status(env={"FIRECRAWL_MODE": "required"}))
        self.assertEqual(capability["firecrawl"]["state"], "failed_required")
        self.assertTrue(capability["firecrawl"]["required"])

    def _refresh_fixture_payload(self) -> dict:
        leg = {
            "display_event": "Team A vs Team B",
            "event_ticker": "EVT-1",
            "market_ticker": "MKT-1",
            "side": "yes",
            "status": "open",
            "probability": 0.82,
            "required_probability": 0.8,
            "ask_cents": 82,
            "bid_cents": 81,
            "midpoint_cents": 81.5,
            "event_start_time": "2099-07-03T20:00:00+00:00",
            "market_close_time": "2099-07-03T20:30:00+00:00",
            "api_fetched_at": "2099-07-03T15:59:00+00:00",
            "source_updated_at": "2099-07-03T15:58:00+00:00",
            "evidence_count": 4,
            "research_mode": "source_backed",
        }
        leg.update(
            {
                "combo_eligible": True,
                "combo_market_ticker": "KXMVE-HOSTED-TEST",
                "combo_market_status": "active",
                "combo_market_yes_ask_cents": 50,
                "combo_market_fetched_at": "2099-07-03T15:59:00+00:00",
                "combo_market_snapshot_hash": "sha256:hosted-test-combo",
                "combo_market_leg_signature": combo_leg_signature([leg]),
                "combo_exact_leg_count": 1,
                "combo_evidence_status": VERIFIED_COMBO_EVIDENCE,
                "combo_source": VERIFIED_COMBO_SOURCE,
            }
        )
        return {
            "generated_at": "2099-07-03T16:00:00+00:00",
            "date": "2099-07-03",
            "games": [{"id": "game-1"}],
            "markets": [{"ticker": "MKT-1"}],
            "all_day_market_count": 0,
            "custom_slip": {
                "action": "BUILD_SLIP",
                "leg_count": 1,
                "combo_compatibility": {"status": "compatible", "exact_listed_combo": True},
                "listed_combo_market_ticker": "KXMVE-HOSTED-TEST",
                "legs": [leg],
            },
            "leverage_slip": {"action": "NO_SLIP", "leg_count": 0, "legs": []},
            "all_day_slip": {"action": "NO_SLIP", "leg_count": 0, "legs": []},
            "research_edge_slip": {"action": "NO_SLIP", "leg_count": 0, "legs": []},
        }

    def test_build_quality_status_reports_ok_with_fresh_slip(self):
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "custom_slip": {"leg_count": 2},
        }
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            error_path = Path(tmp) / "errors.jsonl"
            append_jsonl(audit_path, {"event": "refresh", "ok": True})
            status = build_quality_status(payload, audit_path, error_path)
        self.assertEqual(status["status"], "OK")
        self.assertEqual(status["slip_counts"]["primary"], 2)
        self.assertIn("cache", status["controls"])
        self.assertEqual(status["source_quality_gate"]["status"], "OK")
        self.assertFalse(status["metric_contamination_checks"]["auto_bet_enabled"])

    def test_build_quality_status_warns_on_refresh_error(self):
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "refresh_error": "boom",
        }
        with tempfile.TemporaryDirectory() as tmp:
            status = build_quality_status(payload, Path(tmp) / "audit.jsonl", Path(tmp) / "errors.jsonl")
        self.assertEqual(status["status"], "WATCH")
        self.assertIn("latest refresh has an error", status["warnings"])
        self.assertIn("latest_refresh_error", status["source_quality_gate"]["reasons"])

    def test_old_refresh_errors_do_not_keep_quality_in_watch_after_success(self):
        audit_rows = [
            {
                "ok": False,
                "started_at": "2026-07-05T12:00:00Z",
                "finished_at": "2026-07-05T12:00:10Z",
            },
            {
                "ok": True,
                "started_at": "2026-07-07T12:00:00Z",
                "finished_at": "2026-07-07T12:00:10Z",
            },
        ]
        errors = [
            {
                "error": "old sandbox networking error",
                "started_at": "2026-07-05T12:00:00Z",
                "finished_at": "2026-07-05T12:00:10Z",
            }
        ]
        active = active_refresh_errors(
            audit_rows=audit_rows,
            latest_errors=errors,
            now=datetime(2026, 7, 7, 12, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(active, [])

    def test_expired_failed_audits_do_not_downgrade_a_fresh_postgres_payload(self):
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "custom_slip": {"action": "BUILD_SLIP", "leg_count": 1, "legs": []},
        }
        expired_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            error_path = Path(tmp) / "errors.jsonl"
            append_jsonl(audit_path, {"event": "refresh", "ok": False, "finished_at": expired_at})
            append_jsonl(error_path, {"error": "expired source error", "finished_at": expired_at})
            status = build_quality_status(payload, audit_path, error_path)

        self.assertEqual(status["status"], "OK")
        self.assertEqual(status["source_quality_gate"]["score"], 100)
        self.assertNotIn("recent_refresh_audit_failures", status["warnings"])

    def test_recent_failed_audit_still_downgrades_quality(self):
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "custom_slip": {"action": "BUILD_SLIP", "leg_count": 1, "legs": []},
        }
        recent_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            append_jsonl(audit_path, {"event": "refresh", "ok": False, "finished_at": recent_at})
            status = build_quality_status(payload, audit_path, Path(tmp) / "errors.jsonl")

        self.assertEqual(status["status"], "WATCH")
        self.assertIn("recent_refresh_audit_failures", status["warnings"])

    def test_dashboard_renders_data_quality_panel(self):
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "custom_slip": {"action": "BUILD_SLIP", "leg_count": 1, "legs": [], "sports": []},
        }
        # The refresh control is admin-gated, so the panel is rendered for an
        # admin here; the reader-facing variant is covered in
        # tests/test_dashboard_render.py.
        rendered = render_dashboard(
            payload,
            principal=AuthPrincipal(username="tester", role="admin", auth_method="session"),
        )
        self.assertIn("Live Status", rendered)
        self.assertIn("Track Record", rendered)
        self.assertIn("80c+ Market Tier", rendered)
        self.assertIn("Fresh market data, manual review packets, no account automation.", rendered)
        self.assertIn("Hawknetic<strong>Predictions</strong>", rendered)
        self.assertIn('class="app-frame"', rendered)
        self.assertIn('class="prediction-drawer"', rendered)
        self.assertIn('class="mobile-bottom-nav"', rendered)
        # refresh-label is the hook the script swaps while a refresh runs.
        self.assertIn('id="refresh-slip"', rendered)
        self.assertIn('class="refresh-label"', rendered)
        self.assertIn("Kalshi contracts", rendered)
        self.assertIn("Live Kalshi market review", rendered)
        self.assertIn("Decision support only", rendered)
        self.assertIn("Fresh data", rendered)
        self.assertIn('aria-live="polite"', rendered)
        self.assertIn("Skip to slips", rendered)
        self.assertNotIn('<div class="holo-stage"', rendered)
        # Styles and script are served as fingerprinted files, so the page
        # links them instead of inlining them.
        self.assertIn('<link rel="stylesheet" href="/assets/app.', rendered)
        self.assertIn('<script src="/assets/app.', rendered)
        self.assertNotIn("<style>", rendered)
        # Freshness polling must use the endpoint every signed-in role may
        # read; /quality.json is admin-only and left other roles stale.
        script = SCRIPT.body.decode("utf-8")
        self.assertIn("LIVE_DATA_POLL_SECONDS", script)
        self.assertIn("/freshness.json", script)
        self.assertNotIn("/quality.json", script)
        self.assertNotIn("System details", rendered)
        self.assertNotIn("Backend checks", rendered)
        self.assertNotIn("Metric Guardrails", rendered)
        self.assertNotIn("Upgrade to Pro", rendered)
        self.assertNotIn("Payment method", rendered)
        with tempfile.TemporaryDirectory() as tmp:
            controls = build_quality_status(payload, Path(tmp) / "audit.jsonl", Path(tmp) / "errors.jsonl")["controls"]
        self.assertIn("/research-record.json", controls["api"])
        self.assertIn("manual", rendered.lower())

    def test_dashboard_distinguishes_blocked_data_from_fresh_no_slip(self):
        fresh_payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "custom_slip": {"action": "NO_SLIP", "reason": "No qualifying legs.", "leg_count": 0},
        }
        fresh_rendered = render_dashboard(fresh_payload)
        self.assertIn("No qualifying legs", fresh_rendered)
        self.assertIn("No slip", fresh_rendered)

        blocked_payload = {
            "generated_at": "2026-07-01T00:00:00+00:00",
            "custom_slip": {"action": "BUILD_SLIP", "leg_count": 1, "legs": []},
        }
        blocked_rendered = render_dashboard(blocked_payload)
        self.assertIn("Review blocked", blocked_rendered)
        self.assertIn("Waiting for fresh data", blocked_rendered)
        self.assertNotIn("<span>Live data</span>", blocked_rendered)

    def test_dashboard_explains_fresh_contract_data_when_a_tier_has_no_slip(self):
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "custom_slip": {"action": "NO_SLIP", "reason": "No qualifying exact contract.", "leg_count": 0},
            "combo_source_summary": {
                "active_kxmve_market_count": 210,
                "verified_current_day_contract_count": 13,
                "tiers": {"primary": {"eligible_exact_combo_count": 0}},
            },
        }

        rendered = render_dashboard(payload)

        # The counts still surface; the sentence carrying them dropped the ticker
        # prefix and the internal vocabulary a reader does not share.
        self.assertIn("Kalshi has 210 combo contracts open", rendered)
        self.assertIn("13 are confirmed for today&#x27;s games", rendered)
        self.assertIn("None fit this tier", rendered)

    def test_dashboard_market_browser_uses_only_source_backed_contract_values(self):
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "custom_slip": {"action": "NO_SLIP", "reason": "No qualifying exact contract.", "leg_count": 0},
            "markets": [
                {
                    "ticker": "KXMVE-EXACT-1",
                    "title": "Exact listed combo contract",
                    "real_data_ready": True,
                    "yes_ask_cents": 47,
                    "no_ask_cents": 54,
                    "volume_24h": "321",
                    "close_time": "2099-07-03T20:00:00+00:00",
                    "real_data_warning": "Market-implied public source values.",
                    "leg_details": [
                        {
                            "display_event": "Detroit vs Texas",
                            "side": "yes",
                            "subtitle": "Over 3.5 runs",
                            "market_implied_probability": 0.81,
                        }
                    ],
                }
            ],
        }

        rendered = render_dashboard(payload)

        self.assertIn("KXMVE-EXACT-1", rendered)
        self.assertIn("47.00c", rendered)
        self.assertIn("Detroit vs Texas", rendered)
        self.assertIn("81.0%", rendered)
        self.assertNotIn("Add to Slip", rendered)

    def test_dashboard_labels_unpriced_combo_as_rfq_watchlist_with_visible_leg_prices(self):
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "custom_slip": {"action": "NO_SLIP", "reason": "Public combo quote required.", "leg_count": 0},
            "combo_source_summary": {
                "active_kxmve_market_count": 1,
                "verified_current_day_contract_count": 1,
                "tradable_kxmve_market_count": 0,
                "rfq_required_kxmve_market_count": 1,
                "tiers": {"primary": {"eligible_exact_combo_count": 0, "rfq_watchlist_count": 1}},
            },
            "markets": [
                {
                    "ticker": "KXMVE-RFQ-1",
                    "title": "Exact RFQ combination",
                    "status": "active",
                    "real_data_ready": True,
                    "yes_ask_cents": 0,
                    "yes_bid_cents": 0,
                    "no_ask_cents": 100,
                    "no_bid_cents": 100,
                    "close_time": "2099-07-03T20:00:00+00:00",
                    "leg_details": [
                        {
                            "display_event": "Detroit vs Texas",
                            "side": "yes",
                            "subtitle": "Over 3.5 runs",
                            "market_implied_probability": 0.81,
                        }
                    ],
                }
            ],
        }

        rendered = render_dashboard(payload)

        self.assertIn("1 awaiting an RFQ before a combo price exists", rendered)
        self.assertIn("RFQ required", rendered)
        self.assertIn("1 priced underlying legs · 81.0%", rendered)
        self.assertNotIn("0.00c", rendered)
        self.assertIn("watchlist-only until an RFQ supplies an executable combo price", rendered)

    def test_dashboard_uses_the_postgres_collector_snapshot(self):
        payload = self._refresh_fixture_payload()
        payload["dashboard_snapshot"] = {"source": "postgres"}
        with patch.dict(os.environ, {"DASHBOARD_PAYLOAD_SOURCE": "postgres"}, clear=False):
            with patch("kalshi_research_bot.paper_server.load_latest_kalshi_snapshot", return_value=payload):
                loaded = load_current_payload(Path("unused.json"))

        self.assertEqual(loaded["generated_at"], payload["generated_at"])
        self.assertEqual(loaded["dashboard_snapshot"]["source"], "postgres")

    def test_hosted_dashboard_never_uses_the_generated_file_as_its_source(self):
        with patch.dict(
            os.environ,
            {"DASHBOARD_PAYLOAD_SOURCE": "file", "RAILWAY_PROJECT_ID": "project"},
            clear=False,
        ):
            loaded = load_current_payload(Path("unused.json"))

        self.assertEqual(loaded["refresh_error"], "hosted_file_payload_forbidden")
        self.assertEqual(loaded["markets"], [])

    def test_stale_kalshi_snapshot_hides_market_quotes(self):
        payload = self._refresh_fixture_payload()
        payload["generated_at"] = "2020-01-01T00:00:00+00:00"
        payload["markets"] = [
            {
                "ticker": "KXMVE-STALE",
                "title": "Stale contract",
                "yes_ask_cents": 47,
                "real_data_ready": True,
            }
        ]

        rendered = render_dashboard(payload)

        self.assertIn("Kalshi contracts temporarily hidden", rendered)
        self.assertNotIn("KXMVE-STALE", rendered)

    def test_refresh_payload_logs_hosted_predictions_to_research_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_path = root / "today.json"
            settings = test_settings()
            reset_database(settings)
            with patch.dict(os.environ, {"RESEARCH_DATA_DIR": str(root), "KALSHI_RUN_ID": "hosted_test"}, clear=False):
                with patch("kalshi_research_bot.today.write_today_payload", return_value=self._refresh_fixture_payload()):
                    status = refresh_payload(
                        data_path=data_path,
                        yyyymmdd="20260703",
                        target_probability=0.8,
                        min_leg_probability=None,
                        max_leg_probability=0.985,
                        min_legs=1,
                        max_legs=20,
                        stake_dollars=5,
                        leverage_min_leg_probability=0.75,
                        public_intel_path=None,
                    )

            self.assertTrue(status["ok"])
            self.assertTrue(status["ledger_ok"])
            self.assertEqual(status["ledger_run_id"], "hosted_test")
            self.assertEqual(status["ledger_attempted_predictions"], 1)
            self.assertEqual(status["ledger_logged_predictions"], 1)
            self.assertEqual(status["ledger_rejected_predictions"], 0)
            with connection_pool(settings).connection() as connection:
                count = connection.execute("SELECT COUNT(*) AS count FROM app.prediction_logs").fetchone()["count"]
                run_count = connection.execute("SELECT COUNT(*) AS count FROM app.paper_test_runs WHERE run_id = %s", ("hosted_test",)).fetchone()["count"]
            self.assertEqual(count, 1)
            self.assertEqual(run_count, 1)

    def test_refresh_payload_keeps_slip_live_when_ledger_logging_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("kalshi_research_bot.today.write_today_payload", return_value=self._refresh_fixture_payload()):
                with patch("kalshi_research_bot.paper_server.log_refresh_predictions", side_effect=RuntimeError("db locked")):
                    status = refresh_payload(
                        data_path=Path(tmp) / "today.json",
                        yyyymmdd="20260703",
                        target_probability=0.8,
                        min_leg_probability=None,
                        max_leg_probability=0.985,
                        min_legs=1,
                        max_legs=20,
                        stake_dollars=5,
                        leverage_min_leg_probability=0.75,
                        public_intel_path=None,
                    )

        self.assertTrue(status["ok"])
        self.assertFalse(status["ledger_ok"])
        self.assertIn("RuntimeError: db locked", status["ledger_error"])
        self.assertEqual(status["primary_leg_count"], 1)

    def test_refresh_payload_fails_closed_when_postgres_snapshot_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("kalshi_research_bot.today.write_today_payload", return_value=self._refresh_fixture_payload()):
                with patch(
                    "kalshi_research_bot.kalshi_ingestion.persist_kalshi_snapshot",
                    side_effect=RuntimeError("database unavailable"),
                ):
                    with patch("kalshi_research_bot.paper_server.log_refresh_predictions") as ledger:
                        status = refresh_payload(
                            data_path=Path(tmp) / "today.json",
                            yyyymmdd="20260703",
                            target_probability=0.8,
                            min_leg_probability=None,
                            max_leg_probability=0.985,
                            min_legs=1,
                            max_legs=20,
                            stake_dollars=5,
                            leverage_min_leg_probability=0.75,
                            public_intel_path=None,
                        )

        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "postgres_snapshot_persistence_failed")
        ledger.assert_not_called()

    def test_prediction_quality_handles_postgres_timestamp_columns(self):
        settings = test_settings()
        reset_database(settings)
        with connection_pool(settings).connection() as connection:
            result = evaluate_prediction_table(
                connection,
                table_name="sports_prediction_logs",
                run_id="quality_timestamp_test",
                asset_class="sports",
            )

        self.assertEqual(result["total_rows"], 0)
        self.assertEqual(result["issue_counts"], {"no_prediction_rows": 1})

    def test_evaluate_source_records_requires_hash_and_timestamps(self):
        gate = evaluate_source_records(
            [{"api_fetched_at": "2026-07-06T12:00:00Z"}],
            required_fields=("api_fetched_at", "source_snapshot_hash"),
            max_age_seconds=60,
            now=datetime(2026, 7, 6, 12, 0, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(gate["status"], "WATCH")
        self.assertEqual(gate["issue_counts"]["missing_source_snapshot_hash"], 1)

    def test_zero_heartbeat_diagnosis_treats_fresh_unchanged_as_no_material_change(self):
        payload = {
            "records": [
                {
                    "asset_class": "crypto",
                    "exchange": "coinbase",
                    "symbol": "BTC-USD",
                    "timeframe": "1m",
                    "candle_open_time": "2026-07-06T11:59:00Z",
                    "candle_close_time": "2026-07-06T12:00:00Z",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                    "api_fetched_at": "2026-07-06T12:00:00Z",
                    "source_snapshot_hash": "abc",
                }
            ],
            "errors": [],
        }
        report_text = "Heartbeat status: no_material_change\nLogged predictions: 0\nRejected predictions: 0\nSettled rows: 0"
        diagnosis = build_zero_heartbeat_diagnosis(
            crypto_payload=payload,
            crypto_report_text=report_text,
            now=datetime(2026, 7, 6, 12, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(diagnosis["status"], "OK")
        self.assertEqual(diagnosis["diagnosis"], "unchanged_repeat_guard_or_no_eligible_settlement")

    def test_data_quality_report_and_cli_do_not_require_live_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dashboard = root / "today.json"
            dashboard.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-06T12:00:00Z",
                        "custom_slip": {"leg_count": 2},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "quality.txt"
            json_output = root / "quality.json"
            report = build_data_quality_report(
                dashboard_payload_path=dashboard,
                audit_path=root / "audit.jsonl",
                error_path=root / "errors.jsonl",
                now=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
            )
            rendered = render_data_quality_report(report)
            self.assertIn("Private Research Data Quality Report", rendered)
            self.assertIn("Metric contamination checks", rendered)
            self.assertIn("Core platform quality", rendered)
            self.assertIn("workflow_quality_scores", report)
            self.assertIn("optional_capability_status", report)
            self.assertFalse(report["deployment_readiness"]["ready"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = main(
                    [
                        "data-quality",
                        "--dashboard-payload",
                        str(dashboard),
                        "--audit-path",
                        str(root / "audit.jsonl"),
                        "--error-path",
                        str(root / "errors.jsonl"),
                        "--output",
                        str(output),
                        "--json-output",
                        str(json_output),
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            self.assertTrue(json_output.exists())
            self.assertIn("no auto-betting", buffer.getvalue())

    def test_http_client_uses_cache(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"ok": true}'

        with tempfile.TemporaryDirectory() as tmp:
            client = HttpClient(cache_dir=tmp, cache_ttl_seconds=60)
            with patch("urllib.request.urlopen", return_value=FakeResponse()) as opener:
                first = client.get_text("https://example.com/data")
                second = client.get_text("https://example.com/data")
        self.assertEqual(json.loads(first.text), {"ok": True})
        self.assertEqual(second.text, first.text)
        self.assertEqual(opener.call_count, 1)

    def test_http_client_identifies_the_project_transparently(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"ok": true}'

        with tempfile.TemporaryDirectory() as tmp:
            client = HttpClient(cache_dir=tmp, cache_ttl_seconds=0)
            with patch("urllib.request.urlopen", return_value=FakeResponse()) as opener:
                client.get_text("https://example.com/data")

        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), client.user_agent)
        self.assertIn("HawkNeticResearchBot", client.user_agent)
        self.assertIn("github.com/Dhawkins223/HawkNeticSportsTools", client.user_agent)
        self.assertNotIn("Mozilla", client.user_agent)

    def test_http_client_retries_rate_limit_once(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"ok": true}'

        error = urllib.error.HTTPError("https://example.com/data", 429, "Too Many Requests", {}, None)
        with tempfile.TemporaryDirectory() as tmp:
            client = HttpClient(
                cache_dir=tmp,
                cache_ttl_seconds=60,
                max_retries=1,
                retry_backoff_seconds=0,
            )
            with patch("urllib.request.urlopen", side_effect=[error, FakeResponse()]) as opener:
                response = client.get_text("https://example.com/data")
        self.assertEqual(json.loads(response.text), {"ok": True})
        self.assertFalse(response.from_cache)
        self.assertEqual(opener.call_count, 2)

    def test_http_client_stale_fallback_is_marked(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"ok": true}'

        url = "https://example.com/data"
        with tempfile.TemporaryDirectory() as tmp:
            client = HttpClient(cache_dir=tmp, cache_ttl_seconds=60)
            with patch("urllib.request.urlopen", return_value=FakeResponse()):
                client.get_text(url)
            cache_path = client._cache_path(url)
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["saved_at"] = time.time() - 120
            cached["fetched_at"] = "2026-07-01T00:00:00Z"
            cache_path.write_text(json.dumps(cached), encoding="utf-8")
            fallback_client = HttpClient(
                cache_dir=tmp,
                cache_ttl_seconds=1,
                max_retries=0,
                allow_stale_on_error=True,
                max_stale_seconds=3600,
            )
            error = urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            with patch("urllib.request.urlopen", side_effect=error):
                response = fallback_client.get_text(url)
        self.assertTrue(response.from_cache)
        self.assertTrue(response.stale)
        self.assertEqual(response.stale_reason, "http_429")
        self.assertEqual(response.fetched_at, "2026-07-01T00:00:00Z")
        self.assertEqual(fallback_client.cache_status()["stale_fallback_count"], 1)

    def test_http_response_hash_and_freshness_are_explicit(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b'{"ok": true}'

        with tempfile.TemporaryDirectory() as tmp:
            client = HttpClient(cache_dir=tmp, cache_ttl_seconds=0)
            with patch("urllib.request.urlopen", return_value=FakeResponse()):
                first = client.get_text("https://example.com/data")
            with patch("urllib.request.urlopen", return_value=FakeResponse()):
                second = client.get_text("https://example.com/data")
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.freshness_state, "fresh")
        self.assertEqual(first.received_at, first.fetched_at)

    def test_http_client_rejects_oversized_response_without_fake_data(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b"x" * 2048

        with tempfile.TemporaryDirectory() as tmp:
            client = HttpClient(cache_dir=tmp, cache_ttl_seconds=0, max_response_bytes=1024, max_retries=0)
            with patch("urllib.request.urlopen", return_value=FakeResponse()):
                with self.assertRaisesRegex(ResponseTooLargeError, "response_too_large"):
                    client.get_text("https://example.com/oversized")

    def test_quality_gate_flags_stale_cache_fallback(self):
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "custom_slip": {"leg_count": 2},
            "source_cache_status": {"stale_fallback_count": 1},
        }
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            error_path = Path(tmp) / "errors.jsonl"
            append_jsonl(audit_path, {"event": "refresh", "ok": True})
            status = build_quality_status(payload, audit_path, error_path)
        self.assertEqual(status["status"], "WATCH")
        self.assertIn("stale_cache_fallback_used", status["source_quality_gate"]["reasons"])

    def test_http_cache_prune_removes_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            old_file = cache_dir / "old.json"
            new_file = cache_dir / "new.json"
            old_file.write_text("old", encoding="utf-8")
            new_file.write_text("new", encoding="utf-8")
            now = time.time()
            os.utime(old_file, (now - 7200, now - 7200))
            os.utime(new_file, (now, now))

            result = prune_http_cache(cache_dir, max_age_seconds=3600, max_bytes=0, now=now)

            self.assertTrue(result["ok"])
            self.assertFalse(old_file.exists())
            self.assertTrue(new_file.exists())
            self.assertEqual(result["deleted_files"], 1)

    def test_http_cache_prune_enforces_size_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            files = []
            now = time.time()
            for index in range(5):
                path = cache_dir / f"{index}.json"
                path.write_bytes(b"x" * 100)
                os.utime(path, (now - (10 - index), now - (10 - index)))
                files.append(path)

            result = prune_http_cache(cache_dir, max_age_seconds=0, max_bytes=300, now=now)

            self.assertTrue(result["ok"])
            self.assertLessEqual(result["remaining_bytes"], 300)
            self.assertGreaterEqual(result["deleted_files"], 2)
            self.assertFalse(files[0].exists())


if __name__ == "__main__":
    unittest.main()
