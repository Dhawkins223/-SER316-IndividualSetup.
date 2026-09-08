from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from kalshi_research_bot.collection_ledger import CollectionLedger
from kalshi_research_bot.kalshi_ingestion import load_latest_kalshi_snapshot
from kalshi_research_bot.monitoring import actionable_monitoring_events
from kalshi_research_bot.retention import (
    DEFAULT_RETENTION_DAYS,
    DEFAULT_VOLUME_CAPACITY_BYTES,
    MINIMUM_RETENTION_DAYS,
    TOMBSTONE_KEY,
    RetentionWindowTooShort,
    database_capacity_state,
    prune_source_payload_bodies,
    render_storage_report,
    source_payload_storage_report,
    volume_capacity_bytes,
)

from tests.postgres_support import PostgresTestCase


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DatabaseCapacityTests(unittest.TestCase):
    """The alarm that was missing when the volume filled.

    Production reported `database_bytes` every retention cycle while the number
    climbed to the volume ceiling, and nothing ever escalated it. PostgreSQL
    then ran out of space mid-write, PANICked, and could not finish WAL recovery
    to restart. These tests pin the thresholds that fire while there is still
    room to prune.
    """

    def test_an_empty_database_is_not_an_alarm(self) -> None:
        state = database_capacity_state(0)
        self.assertEqual(state["state"], "ok")
        self.assertEqual(state["used_ratio"], 0.0)
        self.assertEqual(state["headroom_bytes"], DEFAULT_VOLUME_CAPACITY_BYTES)

    def test_three_quarters_full_warns_while_there_is_still_room(self) -> None:
        state = database_capacity_state(int(DEFAULT_VOLUME_CAPACITY_BYTES * 0.76))
        self.assertEqual(state["state"], "warning")
        self.assertGreater(state["headroom_bytes"], 0)

    def test_ninety_percent_is_critical(self) -> None:
        state = database_capacity_state(int(DEFAULT_VOLUME_CAPACITY_BYTES * 0.91))
        self.assertEqual(state["state"], "critical")

    def test_the_volume_that_actually_died_reports_critical(self) -> None:
        # 4.99 GB of a 5 GB Railway Hobby volume: the measured state of
        # production on 2026-09-08, after the PANIC.
        state = database_capacity_state(4_994_793_472)
        self.assertEqual(state["state"], "critical")
        self.assertGreater(state["used_ratio"], 0.99)

    def test_capacity_is_configurable_for_a_resized_volume(self) -> None:
        with mock.patch.dict("os.environ", {"DATABASE_VOLUME_CAPACITY_BYTES": "50000000000"}):
            self.assertEqual(volume_capacity_bytes(), 50_000_000_000)
            # The same byte count that is critical on 5 GB is unremarkable on 50.
            self.assertEqual(database_capacity_state(4_994_793_472)["state"], "ok")

    def test_a_nonsense_capacity_falls_back_rather_than_disabling_the_alarm(self) -> None:
        for value in ("", "   ", "not-a-number", "0", "-1"):
            with mock.patch.dict("os.environ", {"DATABASE_VOLUME_CAPACITY_BYTES": value}):
                self.assertEqual(volume_capacity_bytes(), DEFAULT_VOLUME_CAPACITY_BYTES)

    def test_a_critical_volume_escalates_past_the_default_severity(self) -> None:
        events = actionable_monitoring_events(
            {
                "status": "ready",
                "anomalies": [{"type": "database_capacity", "state": "critical", "used_ratio": 0.99}],
            }
        )
        self.assertEqual([event["severity"] for event in events], ["critical"])
        self.assertIn("RAW_RETENTION_DAYS", events[0]["next_action"])

    def test_a_warning_volume_stays_a_warning(self) -> None:
        events = actionable_monitoring_events(
            {
                "status": "ready",
                "anomalies": [{"type": "database_capacity", "state": "warning", "used_ratio": 0.78}],
            }
        )
        self.assertEqual([event["severity"] for event in events], ["warning"])


class RetentionWindowDefaultTests(unittest.TestCase):
    def test_the_default_window_fits_the_volume_it_runs_against(self) -> None:
        """A window is only real if the data can age into it.

        At the ~166 MB/day of raw payloads production measured, the default
        window has to leave room for the core tables on the same volume. Thirty
        days wanted ~5 GB of payloads alone on a 5 GB volume, so nothing ever
        became eligible and retention pruned nothing.
        """

        measured_daily_payload_bytes = 166_000_000
        steady_state = measured_daily_payload_bytes * DEFAULT_RETENTION_DAYS
        self.assertLess(steady_state, DEFAULT_VOLUME_CAPACITY_BYTES * 0.5)
        self.assertGreaterEqual(DEFAULT_RETENTION_DAYS, MINIMUM_RETENTION_DAYS)


class RetentionTests(PostgresTestCase):
    def _store_payload(
        self,
        *,
        source: str = "kalshi_public_api",
        entity_type: str = "kalshi_today_snapshot",
        identifier: str,
        age_days: float,
        body: dict | None = None,
    ) -> str:
        """Write one collected payload as though it arrived `age_days` ago."""
        received = (_now() - timedelta(days=age_days)).isoformat()
        ledger = CollectionLedger(self.settings)
        batch = ledger.start_batch(
            idempotency_key=f"batch-{identifier}",
            source=source,
            endpoint="test",
            worker_name="test-worker",
            worker_version="v1",
            collector_version="v1",
            request_parameters={},
            started_at=received,
        )
        stored = ledger.store_payload(
            batch_id=batch.batch_id,
            source=source,
            entity_type=entity_type,
            source_identifier=identifier,
            observed_at=received,
            received_at=received,
            parser_version="v1",
            payload=body if body is not None else {"identifier": identifier, "filler": "x" * 4000},
        )
        ledger.complete_batch(
            batch_id=batch.batch_id,
            records_received=1,
            records_accepted=1,
            records_rejected=0,
            records_duplicated=0,
            payload_hash_value=stored["content_hash"],
        )
        return stored["payload_id"]

    def test_dry_run_reports_candidates_without_writing(self) -> None:
        self._store_payload(identifier="old", age_days=60)
        self._store_payload(identifier="new", age_days=1)

        result = prune_source_payload_bodies(older_than_days=30, settings=self.settings)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["pruned"], 0)
        self.assertGreater(result["reclaimable_bytes"], 0)

        rows = self.query_all("SELECT payload_json FROM raw.source_payloads")
        for row in rows:
            self.assertNotIn(TOMBSTONE_KEY, row["payload_json"])

    def test_apply_replaces_only_bodies_past_the_window(self) -> None:
        self._store_payload(identifier="old", age_days=60)
        self._store_payload(identifier="recent", age_days=1)

        result = prune_source_payload_bodies(older_than_days=30, dry_run=False, settings=self.settings)
        self.assertEqual(result["pruned"], 1)

        old = self.query_one(
            "SELECT payload_json FROM raw.source_payloads WHERE source_identifier = %s", ("old",)
        )
        recent = self.query_one(
            "SELECT payload_json FROM raw.source_payloads WHERE source_identifier = %s", ("recent",)
        )
        self.assertIn(TOMBSTONE_KEY, old["payload_json"])
        self.assertEqual(old["payload_json"][TOMBSTONE_KEY]["state"], "body_pruned")
        self.assertGreater(old["payload_json"][TOMBSTONE_KEY]["original_bytes"], 0)
        self.assertNotIn(TOMBSTONE_KEY, recent["payload_json"])

    def test_evidence_survives_the_prune(self) -> None:
        payload_id = self._store_payload(identifier="old", age_days=90)
        self._store_payload(identifier="newest", age_days=60)
        before = self.query_one(
            "SELECT batch_id, source, entity_type, source_identifier, observed_at, received_at,"
            " content_hash, parser_version FROM raw.source_payloads WHERE id = %s",
            (int(payload_id),),
        )

        prune_source_payload_bodies(older_than_days=30, dry_run=False, settings=self.settings)

        after = self.query_one(
            "SELECT batch_id, source, entity_type, source_identifier, observed_at, received_at,"
            " content_hash, parser_version, payload_json FROM raw.source_payloads WHERE id = %s",
            (int(payload_id),),
        )
        for column in (
            "batch_id",
            "source",
            "entity_type",
            "source_identifier",
            "observed_at",
            "received_at",
            "content_hash",
            "parser_version",
        ):
            self.assertEqual(after[column], before[column], f"{column} must survive the prune")
        # The tombstone carries the hash forward so the row remains self-describing.
        self.assertEqual(after["payload_json"][TOMBSTONE_KEY]["content_hash"], before["content_hash"])

    def test_the_newest_payload_per_source_is_never_pruned(self) -> None:
        # Everything is far past the window, so only the newest-per-source guard
        # can keep the dashboard's current snapshot readable.
        self._store_payload(identifier="older", age_days=90)
        self._store_payload(identifier="newest", age_days=60)

        result = prune_source_payload_bodies(older_than_days=30, dry_run=False, settings=self.settings)
        self.assertEqual(result["pruned"], 1)

        newest = self.query_one(
            "SELECT payload_json FROM raw.source_payloads WHERE source_identifier = %s", ("newest",)
        )
        self.assertNotIn(TOMBSTONE_KEY, newest["payload_json"])

    def test_the_dashboard_snapshot_still_loads_after_a_prune(self) -> None:
        self._store_payload(identifier="older", age_days=90)
        self._store_payload(
            identifier="current",
            age_days=60,
            body={"generated_at": "2026-08-15T00:00:00+00:00", "markets": [], "games": []},
        )

        prune_source_payload_bodies(older_than_days=30, dry_run=False, settings=self.settings)

        snapshot = load_latest_kalshi_snapshot(settings=self.settings)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["generated_at"], "2026-08-15T00:00:00+00:00")
        self.assertNotIn(TOMBSTONE_KEY, snapshot)

    def test_pruning_is_idempotent(self) -> None:
        self._store_payload(identifier="old_a", age_days=90)
        self._store_payload(identifier="old_b", age_days=80)
        self._store_payload(identifier="newest", age_days=60)

        first = prune_source_payload_bodies(older_than_days=30, dry_run=False, settings=self.settings)
        second = prune_source_payload_bodies(older_than_days=30, dry_run=False, settings=self.settings)
        self.assertEqual(first["pruned"], 2)
        self.assertEqual(second["pruned"], 0)
        self.assertEqual(second["candidates"], 0)

    def test_a_short_window_is_refused(self) -> None:
        with self.assertRaises(RetentionWindowTooShort):
            prune_source_payload_bodies(older_than_days=1, settings=self.settings)

    def test_limit_bounds_a_pass_and_reports_the_remainder(self) -> None:
        for index in range(4):
            self._store_payload(identifier=f"old_{index}", age_days=90 - index)
        self._store_payload(identifier="newest", age_days=60)

        result = prune_source_payload_bodies(
            older_than_days=30, limit=2, dry_run=False, settings=self.settings
        )
        self.assertEqual(result["pruned"], 2)
        self.assertEqual(result["remaining_after_limit"], 2)

    def test_source_filter_scopes_the_prune(self) -> None:
        self._store_payload(source="kalshi_public_api", identifier="kalshi_old", age_days=90)
        self._store_payload(source="kalshi_public_api", identifier="kalshi_new", age_days=60)
        self._store_payload(source="espn_scoreboard", identifier="espn_old", age_days=90)
        self._store_payload(source="espn_scoreboard", identifier="espn_new", age_days=60)

        result = prune_source_payload_bodies(
            older_than_days=30, source="espn_scoreboard", dry_run=False, settings=self.settings
        )
        self.assertEqual(result["pruned"], 1)

        kalshi_old = self.query_one(
            "SELECT payload_json FROM raw.source_payloads WHERE source_identifier = %s", ("kalshi_old",)
        )
        self.assertNotIn(TOMBSTONE_KEY, kalshi_old["payload_json"])

    def test_storage_report_attributes_space_by_source(self) -> None:
        self._store_payload(source="kalshi_public_api", identifier="kalshi_old", age_days=90)
        self._store_payload(source="kalshi_public_api", identifier="kalshi_new", age_days=1)
        self._store_payload(source="espn_scoreboard", identifier="espn_old", age_days=90)

        report = source_payload_storage_report(settings=self.settings, older_than_days=30)
        self.assertEqual(report["total_rows"], 3)
        self.assertEqual(report["tombstoned_rows"], 0)
        self.assertGreater(report["total_body_bytes"], 0)
        # espn_old is that source's only payload, so the newest-per-source guard
        # protects it and the report says so rather than over-promising.
        self.assertEqual(report["prunable_rows"], 1)
        self.assertEqual({entry["source"] for entry in report["by_source"]}, {"kalshi_public_api", "espn_scoreboard"})

        rendered = render_storage_report(report)
        self.assertIn("Raw Source Payload Storage", rendered)
        self.assertIn("kalshi_public_api", rendered)

    def test_retention_worker_prunes_and_reports_what_it_did(self) -> None:
        from kalshi_research_bot.worker_services import SERVICE_SPECS, build_service_operation

        self._store_payload(identifier="old_a", age_days=90)
        self._store_payload(identifier="old_b", age_days=80)
        self._store_payload(identifier="newest", age_days=60)

        self.assertIn("raw-retention", SERVICE_SPECS)
        operation = build_service_operation(
            "raw-retention", kalshi_run_id="k", crypto_run_id="c", sports_run_id="s"
        )
        result = operation()

        self.assertFalse(result["dry_run"], "a retention worker that only reports leaves the volume filling")
        self.assertEqual(result["payload_bodies_pruned"], 2)
        self.assertEqual(result["records_processed"], 2)
        self.assertFalse(result["no_material_change"])
        self.assertGreater(result["reclaimable_bytes"], 0)
        self.assertEqual(result["still_eligible"], 0)

        # Nothing left to prune is the healthy steady state, not a failure.
        steady = operation()
        self.assertEqual(steady["payload_bodies_pruned"], 0)
        self.assertTrue(steady["no_material_change"])

    def test_a_window_wider_than_the_data_is_reported_as_not_biting(self) -> None:
        # Production's thirty-day window pruned nothing and looked broken. It was
        # simply wider than the data's own age, which is a state worth naming.
        self._store_payload(identifier="recent_a", age_days=3)
        self._store_payload(identifier="recent_b", age_days=1)

        result = prune_source_payload_bodies(older_than_days=30, settings=self.settings)
        self.assertFalse(result["window_bites"])
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["retained_span_days"], 2.0)
        self.assertIsNotNone(result["oldest_received_at"])

    def test_a_window_inside_the_data_age_is_reported_as_biting(self) -> None:
        self._store_payload(identifier="old", age_days=40)
        self._store_payload(identifier="newest", age_days=1)

        result = prune_source_payload_bodies(older_than_days=30, settings=self.settings)
        self.assertTrue(result["window_bites"])
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["retained_span_days"], 39.0)

    def test_retention_worker_surfaces_whether_its_window_bites(self) -> None:
        from kalshi_research_bot.worker_services import build_service_operation

        self._store_payload(identifier="recent", age_days=2)
        self._store_payload(identifier="newest", age_days=1)
        operation = build_service_operation(
            "raw-retention", kalshi_run_id="k", crypto_run_id="c", sports_run_id="s"
        )
        result = operation()

        self.assertFalse(result["window_bites"], "a 30-day window over 2 days of data cannot bite")
        self.assertEqual(result["payload_bodies_pruned"], 0)
        self.assertEqual(result["retained_span_days"], 1.0)

    def test_storage_census_names_the_largest_relations(self) -> None:
        from kalshi_research_bot.retention import database_storage_census

        for index in range(3):
            self._store_payload(identifier=f"census_{index}", age_days=index)

        census = database_storage_census(settings=self.settings, limit=10)
        self.assertGreater(census["database_bytes"], 0)
        self.assertTrue(census["largest_relations"])

        names = [row["relation"] for row in census["largest_relations"]]
        self.assertIn("raw.source_payloads", names)
        sizes = [row["total_bytes"] for row in census["largest_relations"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True), "largest relation must come first")

    def test_retention_worker_reports_where_the_space_is(self) -> None:
        from kalshi_research_bot.worker_services import build_service_operation

        self._store_payload(identifier="only", age_days=1)
        operation = build_service_operation(
            "raw-retention", kalshi_run_id="k", crypto_run_id="c", sports_run_id="s"
        )
        result = operation()

        # Nothing is old enough to prune, so the census is the only thing that
        # explains a database that is still growing.
        self.assertEqual(result["payload_bodies_pruned"], 0)
        self.assertGreater(result["database_bytes"], 0)
        self.assertTrue(any("raw.source_payloads=" in entry for entry in result["largest_relations"]))

    def test_retention_worker_window_is_configurable_and_floored(self) -> None:
        from unittest import mock

        from kalshi_research_bot.worker_services import build_service_operation

        self._store_payload(identifier="old", age_days=20)
        self._store_payload(identifier="newest", age_days=15)

        with mock.patch.dict("os.environ", {"RAW_RETENTION_DAYS": "10"}):
            operation = build_service_operation(
                "raw-retention", kalshi_run_id="k", crypto_run_id="c", sports_run_id="s"
            )
            widened = operation()
        self.assertEqual(widened["retention_days"], 10)
        self.assertEqual(widened["payload_bodies_pruned"], 1)

        # A window under the floor is raised to it rather than honoured.
        with mock.patch.dict("os.environ", {"RAW_RETENTION_DAYS": "1"}):
            operation = build_service_operation(
                "raw-retention", kalshi_run_id="k", crypto_run_id="c", sports_run_id="s"
            )
            floored = operation()
        self.assertEqual(floored["retention_days"], 7)

    def test_report_counts_tombstones_separately_after_a_prune(self) -> None:
        self._store_payload(identifier="old", age_days=90)
        self._store_payload(identifier="newest", age_days=60)
        prune_source_payload_bodies(older_than_days=30, dry_run=False, settings=self.settings)

        report = source_payload_storage_report(settings=self.settings, older_than_days=30)
        self.assertEqual(report["total_rows"], 2)
        self.assertEqual(report["tombstoned_rows"], 1)
        self.assertEqual(report["prunable_rows"], 0)

    def test_duplication_report_counts_only_copies_past_the_first(self) -> None:
        """Storing an unchanged response again is what the report has to catch.

        Uniqueness on `raw.source_payloads` is per batch, and every cycle opens a
        new batch, so a source returning identical content stores the whole body
        again without any constraint objecting.
        """

        from kalshi_research_bot.retention import source_payload_duplication_report

        unchanged = {"scoreboard": "no games", "filler": "y" * 4000}
        for index in range(3):
            self._store_payload(identifier=f"repeat_{index}", age_days=index, body=unchanged)
        self._store_payload(identifier="distinct", age_days=1, body={"scoreboard": "one game"})

        report = source_payload_duplication_report(settings=self.settings)
        self.assertEqual(report["retained_rows"], 4)
        # Three identical bodies plus one different one is two distinct bodies.
        self.assertEqual(report["distinct_bodies"], 2)
        # The first copy of each body is evidence; only the other two are redundant.
        self.assertEqual(report["redundant_rows"], 2)
        self.assertGreater(report["redundant_bytes"], 0)
        self.assertLess(report["redundant_bytes"], report["body_bytes"])

        entry = report["by_source"][0]
        self.assertEqual(entry["most_copies_of_one_body"], 3)

    def test_identical_content_from_two_sources_is_not_redundant(self) -> None:
        """Two sources agreeing is two observations, not one stored twice."""

        from kalshi_research_bot.retention import source_payload_duplication_report

        same = {"final": "Home 4 Away 2", "filler": "z" * 2000}
        self._store_payload(identifier="a", age_days=1, source="espn_scoreboard", body=same)
        self._store_payload(identifier="b", age_days=1, source="the_odds_api", body=same)

        report = source_payload_duplication_report(settings=self.settings)
        self.assertEqual(report["redundant_rows"], 0)
        self.assertEqual(report["redundant_bytes"], 0)
        self.assertEqual(report["redundant_share"], "0.0000")

    def test_pruned_bodies_are_not_counted_as_duplicates(self) -> None:
        """A tombstone has no body left to deduplicate."""

        from kalshi_research_bot.retention import source_payload_duplication_report

        unchanged = {"scoreboard": "no games", "filler": "y" * 4000}
        self._store_payload(identifier="old_a", age_days=90, body=unchanged)
        self._store_payload(identifier="old_b", age_days=80, body=unchanged)
        self._store_payload(identifier="newest", age_days=1, body=unchanged)
        prune_source_payload_bodies(older_than_days=30, dry_run=False, settings=self.settings)

        report = source_payload_duplication_report(settings=self.settings)
        self.assertEqual(report["retained_rows"], 1)
        self.assertEqual(report["redundant_rows"], 0)

    def test_empty_table_reports_no_share_rather_than_dividing_by_zero(self) -> None:
        from kalshi_research_bot.retention import source_payload_duplication_report

        report = source_payload_duplication_report(settings=self.settings)
        self.assertEqual(report["retained_rows"], 0)
        self.assertIsNone(report["redundant_share"])
        self.assertEqual(report["by_source"], [])

    def test_worker_measures_duplication_only_when_asked(self) -> None:
        from unittest import mock

        from kalshi_research_bot.worker_services import build_service_operation

        unchanged = {"scoreboard": "no games", "filler": "y" * 4000}
        self._store_payload(identifier="one", age_days=1, body=unchanged)
        self._store_payload(identifier="two", age_days=2, body=unchanged)

        operation = build_service_operation(
            "raw-retention", kalshi_run_id="k", crypto_run_id="c", sports_run_id="s"
        )
        quiet = operation()
        # The scan reads every retained body, so it must stay out of the hourly cycle.
        self.assertNotIn("duplication_measured", quiet)
        self.assertNotIn("redundant_bytes", quiet)

        with mock.patch.dict("os.environ", {"RAW_RETENTION_DUPLICATION_CENSUS": "true"}):
            operation = build_service_operation(
                "raw-retention", kalshi_run_id="k", crypto_run_id="c", sports_run_id="s"
            )
            measured = operation()
        self.assertTrue(measured["duplication_measured"])
        self.assertEqual(measured["redundant_rows"], 1)
        self.assertTrue(measured["duplication_by_source"])
