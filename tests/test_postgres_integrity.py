from __future__ import annotations

import concurrent.futures
import hashlib
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from urllib.parse import urlparse, urlunparse

from kalshi_research_bot.auth import LocalAuthStore
from kalshi_research_bot.collection_ledger import CollectionLedger
from kalshi_research_bot.business_store import create_store
from kalshi_research_bot.database import DatabaseSettings, close_connection_pools, connection_pool, json_default
from kalshi_research_bot.db_migrations import apply_postgres_migrations
from kalshi_research_bot.evaluation.backtest import build_backtest_report, render_backtest_report
from kalshi_research_bot.evaluation.paper_live import build_daily_report, start_paper_test_run
from kalshi_research_bot.operator_inbox import OperatorInbox
from kalshi_research_bot.postgres_import import ImportConflictError, canonical_row_hash, import_canonical_rows, record_import_lineage
from kalshi_research_bot.monitoring import WorkerMonitorStore

from tests.postgres_support import PostgresTestCase, reset_database, test_settings


class PostgresIntegrityTests(PostgresTestCase):
    def test_cold_pool_serves_simultaneous_borrowers(self) -> None:
        close_connection_pools()
        pool = connection_pool(replace(self.settings, pool_min_size=1, pool_max_size=4))
        start = Barrier(4)
        borrowed = Barrier(4)

        def query(_index):
            start.wait(timeout=10)
            with pool.connection() as connection:
                pid = connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
                # All four connections must be usable at the same time, even
                # though the first borrower consumes the minimum idle pool.
                borrowed.wait(timeout=10)
                return pid

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            pids = list(executor.map(query, range(4)))
        self.assertEqual(len(set(pids)), 4)

    @contextmanager
    def _temporary_database(self):
        import psycopg
        from psycopg import sql

        database_name = f"hawknetic_lock_{uuid.uuid4().hex}"
        parsed = urlparse(self.settings.require_url())
        admin_url = urlunparse(parsed._replace(path="/postgres"))
        target_url = urlunparse(parsed._replace(path=f"/{database_name}"))
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        try:
            yield target_url
        finally:
            with psycopg.connect(admin_url, autocommit=True) as admin:
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))

    def test_migration_lock_serializes_concurrent_migrators(self) -> None:
        with self._temporary_database() as url:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(apply_postgres_migrations, [url, url]))
        self.assertTrue(all(result["ready"] for result in results))
        self.assertTrue(all(not result["pending_versions"] for result in results))
        self.assertEqual(sum(bool(result["newly_applied"]) for result in results), 1)

    def test_migration_failure_releases_lock_for_corrected_retry(self) -> None:
        with self._temporary_database() as url, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0001_base.sql").write_text("CREATE SCHEMA app; CREATE TABLE app.lock_probe (id INTEGER);", encoding="utf-8")
            broken = root / "0002_probe.sql"
            broken.write_text("THIS IS INVALID SQL;", encoding="utf-8")
            with self.assertRaises(Exception):
                apply_postgres_migrations(url, directory=root)
            broken.write_text("CREATE TABLE app.lock_retry (id INTEGER);", encoding="utf-8")
            result = apply_postgres_migrations(url, directory=root)
        self.assertTrue(result["ready"])
        self.assertEqual(result["applied_versions"], ["0001", "0002"])

    def test_migration_statement_timeout_is_configurable(self) -> None:
        with self._temporary_database() as url, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0001_timeout.sql").write_text(
                """
                DO $$
                BEGIN
                    IF current_setting('statement_timeout')::interval <> INTERVAL '75 seconds' THEN
                        RAISE EXCEPTION 'unexpected_migration_statement_timeout';
                    END IF;
                END $$;
                """,
                encoding="utf-8",
            )
            result = apply_postgres_migrations(url, directory=root, statement_timeout_ms=75000)

        self.assertTrue(result["ready"])
        self.assertEqual(result["applied_versions"], ["0001"])

    def test_staging_numeric_compatibility_migration_upgrades_to_current_head(self) -> None:
        migration_root = Path(__file__).resolve().parents[1] / "migrations" / "postgres"
        compatibility_migration = migration_root / "0006_exact_numeric_runtime_compatibility.sql"
        self.assertEqual(
            hashlib.sha256(compatibility_migration.read_bytes()).hexdigest(),
            "ccb1a1f53e7610d9116e6321a8aec8528073f66a6d9b87e5a295f981b2c74d75",
        )

        with self._temporary_database() as url, tempfile.TemporaryDirectory() as directory:
            staging_root = Path(directory) / "staging"
            staging_root.mkdir()
            for filename in (
                "0001_research_schema.sql",
                "0002_operator_messages.sql",
                "0003_authoritative_research_ledger.sql",
                "0004_clear_partial_settlement_false_positive.sql",
                "0005_collection_ledger_compatibility.sql",
                "0006_exact_numeric_runtime_compatibility.sql",
            ):
                shutil.copy2(migration_root / filename, staging_root / filename)

            staging_result = apply_postgres_migrations(url, directory=staging_root)
            upgraded_result = apply_postgres_migrations(url)

            import psycopg

            with psycopg.connect(url) as connection:
                edge_results = connection.execute(
                    "SELECT to_regclass('app.edge_results')"
                ).fetchone()[0]
                legacy_edge_results = connection.execute(
                    "SELECT to_regclass('public.edge_results')"
                ).fetchone()[0]

        self.assertEqual(staging_result["applied_versions"], ["0001", "0002", "0003", "0004", "0005", "0006"])
        self.assertNotIn("0006", upgraded_result["newly_applied"])
        self.assertIn("0007", upgraded_result["newly_applied"])
        self.assertTrue(upgraded_result["ready"])
        self.assertEqual(edge_results, "app.edge_results")
        self.assertIsNone(legacy_edge_results)

    def test_pre_cutover_authentication_and_operator_rows_are_retained(self) -> None:
        migration_root = Path(__file__).resolve().parents[1] / "migrations" / "postgres"
        with self._temporary_database() as url, tempfile.TemporaryDirectory() as directory:
            legacy_root = Path(directory) / "legacy"
            legacy_root.mkdir()
            for filename in (
                "0001_research_schema.sql",
                "0002_operator_messages.sql",
                "0003_authoritative_research_ledger.sql",
                "0004_clear_partial_settlement_false_positive.sql",
                "0005_collection_ledger_compatibility.sql",
            ):
                shutil.copy2(migration_root / filename, legacy_root / filename)
            apply_postgres_migrations(url, directory=legacy_root)

            import psycopg

            with psycopg.connect(url) as connection:
                user_id = connection.execute(
                    """
                    INSERT INTO public.app_users
                        (username, password_hash, password_salt, password_algorithm, role)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    ("retained-upgrade-user", "hash", "salt", "scrypt", "researcher"),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO public.app_sessions
                        (session_id_hash, user_id, csrf_token_hash, created_at, expires_at, last_seen_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "retained-session",
                        user_id,
                        "csrf-hash",
                        "2026-07-18T00:00:00+00:00",
                        "2026-07-19T00:00:00+00:00",
                        "2026-07-18T00:00:00+00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO public.login_audit
                        (username, attempted_at, successful, failure_reason)
                    VALUES (%s, %s, %s, %s)
                    """,
                    ("retained-upgrade-user", "2026-07-18T00:00:00+00:00", False, "invalid_password"),
                )
                connection.execute(
                    """
                    INSERT INTO public.operator_messages
                        (message_id, created_at, updated_at, created_by, title, body, priority, target, status, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "retained-upgrade-message",
                        "2026-07-18T00:00:00+00:00",
                        "2026-07-18T00:00:00+00:00",
                        "owner",
                        "retain",
                        "operator history",
                        "normal",
                        "operations",
                        "queued",
                        "dashboard",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO public.worker_status (
                        worker_name, asset_class, current_run_id, status, heartbeat_at, details_json
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "retained-worker",
                        "kalshi",
                        "retained-worker-run",
                        "healthy",
                        "2026-07-18T00:00:00+00:00",
                        '{"retained":false}',
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO public.worker_runs (
                        worker_name, run_id, idempotency_key, attempted_at,
                        finished_at, status, records_processed, details_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "retained-worker",
                        "retained-worker-run",
                        "retained-worker-idempotency",
                        "2026-07-18T00:00:00+00:00",
                        "2026-07-18T00:01:00+00:00",
                        "success",
                        7,
                        '{"retained":false}',
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO public.ingestion_batches (
                        batch_id, idempotency_key, source, endpoint, worker_name,
                        worker_version, collector_version, collection_mode,
                        request_parameters_json, started_at, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "retained-legacy-batch",
                        "retained-legacy-idempotency",
                        "legacy-source",
                        "/legacy",
                        "legacy-worker",
                        "v1",
                        "v1",
                        "historical",
                        '{"include":false}',
                        "2026-07-18T00:00:00+00:00",
                        "completed",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO public.raw_source_payloads (
                        payload_id, batch_id, source, entity_type,
                        source_identifier, received_at, content_hash, payload_json,
                        parser_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "retained-legacy-payload",
                        "retained-legacy-batch",
                        "legacy-source",
                        "market",
                        "legacy-market",
                        "2026-07-18T00:00:00+00:00",
                        "legacy-content-hash",
                        '{"enabled":false}',
                        "v1",
                    ),
                )
            apply_postgres_migrations(url)
            with psycopg.connect(url) as connection:
                user = connection.execute(
                    "SELECT username, role FROM auth.app_users WHERE username = %s",
                    ("retained-upgrade-user",),
                ).fetchone()
                session = connection.execute(
                    "SELECT session_id_hash FROM auth.app_sessions WHERE session_id_hash = %s",
                    ("retained-session",),
                ).fetchone()
                audit = connection.execute(
                    "SELECT failure_reason FROM auth.login_audit WHERE username = %s",
                    ("retained-upgrade-user",),
                ).fetchone()
                message = connection.execute(
                    "SELECT body FROM ops.operator_messages WHERE message_id = %s",
                    ("retained-upgrade-message",),
                ).fetchone()
                batch = connection.execute(
                    "SELECT legacy_batch_id FROM raw.ingestion_batches WHERE idempotency_key = %s",
                    ("retained-legacy-idempotency",),
                ).fetchone()
                payload = connection.execute(
                    "SELECT legacy_payload_id, payload_json FROM raw.source_payloads WHERE legacy_payload_id = %s",
                    ("retained-legacy-payload",),
                ).fetchone()
                public_ledger = connection.execute(
                    "SELECT to_regclass('public.ingestion_batches')"
                ).fetchone()
                archived_ledger = connection.execute(
                    "SELECT to_regclass('archive.legacy_0005_ingestion_batches')"
                ).fetchone()
                worker_status = connection.execute(
                    "SELECT status, details_json FROM ops.worker_status WHERE worker_name = %s",
                    ("retained-worker",),
                ).fetchone()
                worker_run = connection.execute(
                    "SELECT status, records_written, details_json FROM ops.worker_runs WHERE idempotency_key = %s",
                    ("retained-worker-idempotency",),
                ).fetchone()
        self.assertEqual(user, ("retained-upgrade-user", "researcher"))
        self.assertEqual(session, ("retained-session",))
        self.assertEqual(audit, ("invalid_password",))
        self.assertEqual(message, ("operator history",))
        self.assertEqual(batch, ("retained-legacy-batch",))
        self.assertEqual(payload, ("retained-legacy-payload", {"enabled": False}))
        self.assertEqual(public_ledger, (None,))
        self.assertEqual(archived_ledger, ("archive.legacy_0005_ingestion_batches",))
        self.assertEqual(worker_status, ("healthy", {"retained": False}))
        self.assertEqual(worker_run, ("completed", 7, {"retained": False}))

    def test_pre_cutover_ledger_conflict_fails_instead_of_silently_skipping(self) -> None:
        migration_root = Path(__file__).resolve().parents[1] / "migrations" / "postgres"
        with self._temporary_database() as url, tempfile.TemporaryDirectory() as directory:
            legacy_root = Path(directory) / "legacy"
            legacy_root.mkdir()
            for filename in (
                "0001_research_schema.sql",
                "0002_operator_messages.sql",
                "0003_authoritative_research_ledger.sql",
                "0004_clear_partial_settlement_false_positive.sql",
                "0005_collection_ledger_compatibility.sql",
            ):
                shutil.copy2(migration_root / filename, legacy_root / filename)
            apply_postgres_migrations(url, directory=legacy_root)

            import psycopg

            batch_values = (
                "conflict-batch",
                "conflict-idempotency",
                "legacy-source",
                "/legacy",
                "legacy-worker",
                "v1",
                "v1",
                "historical",
                "{}",
                "2026-07-18T00:00:00+00:00",
                "completed",
            )
            with psycopg.connect(url) as connection:
                connection.execute(
                    """
                    INSERT INTO public.ingestion_batches (
                        batch_id, idempotency_key, source, endpoint, worker_name,
                        worker_version, collector_version, collection_mode,
                        request_parameters_json, started_at, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    batch_values,
                )
                connection.execute(
                    """
                    INSERT INTO raw.ingestion_batches (
                        idempotency_key, source, endpoint, worker_name,
                        worker_version, collector_version, collection_mode,
                        request_parameters, started_at, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        "conflict-idempotency",
                        "different-source",
                        "/legacy",
                        "legacy-worker",
                        "v1",
                        "v1",
                        "historical",
                        "{}",
                        "2026-07-18T00:00:00+00:00",
                        "completed",
                    ),
                )
            with self.assertRaisesRegex(Exception, "legacy_collection_batch_content_conflict"):
                apply_postgres_migrations(url)

    def test_preexisting_payload_retains_legacy_rejection_lineage(self) -> None:
        migration_root = Path(__file__).resolve().parents[1] / "migrations" / "postgres"
        with self._temporary_database() as url, tempfile.TemporaryDirectory() as directory:
            legacy_root = Path(directory) / "legacy"
            legacy_root.mkdir()
            for filename in (
                "0001_research_schema.sql",
                "0002_operator_messages.sql",
                "0003_authoritative_research_ledger.sql",
                "0004_clear_partial_settlement_false_positive.sql",
                "0005_collection_ledger_compatibility.sql",
            ):
                shutil.copy2(migration_root / filename, legacy_root / filename)
            apply_postgres_migrations(url, directory=legacy_root)

            import psycopg

            recorded_at = "2026-07-18T00:00:00+00:00"
            with psycopg.connect(url) as connection:
                connection.execute(
                    """
                    INSERT INTO public.ingestion_batches (
                        batch_id, idempotency_key, source, endpoint, worker_name,
                        worker_version, collector_version, collection_mode,
                        request_parameters_json, started_at, completed_at, status,
                        records_received, records_accepted, records_rejected,
                        records_duplicated, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "legacy-lineage-batch",
                        "legacy-lineage-idempotency",
                        "legacy-source",
                        "/legacy",
                        "legacy-worker",
                        "v1",
                        "v1",
                        "historical",
                        "{}",
                        recorded_at,
                        recorded_at,
                        "completed_with_rejections",
                        1,
                        0,
                        1,
                        0,
                        recorded_at,
                    ),
                )
                batch_id = connection.execute(
                    """
                    INSERT INTO raw.ingestion_batches (
                        idempotency_key, source, endpoint, worker_name,
                        worker_version, collector_version, collection_mode,
                        request_parameters, started_at, completed_at, status,
                        records_received, records_accepted, records_rejected,
                        records_duplicated, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        "legacy-lineage-idempotency",
                        "legacy-source",
                        "/legacy",
                        "legacy-worker",
                        "v1",
                        "v1",
                        "historical",
                        "{}",
                        recorded_at,
                        recorded_at,
                        "completed_with_rejections",
                        1,
                        0,
                        1,
                        0,
                        recorded_at,
                    ),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO public.raw_source_payloads (
                        payload_id, batch_id, source, entity_type,
                        source_identifier, observed_at, received_at, content_hash,
                        payload_json, parser_version, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "legacy-lineage-payload",
                        "legacy-lineage-batch",
                        "legacy-source",
                        "market",
                        "legacy-market",
                        recorded_at,
                        recorded_at,
                        "legacy-lineage-hash",
                        '{"valid":false}',
                        "v1",
                        recorded_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO raw.source_payloads (
                        batch_id, source, entity_type, source_identifier,
                        observed_at, received_at, content_hash, payload_json,
                        parser_version, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        batch_id,
                        "legacy-source",
                        "market",
                        "legacy-market",
                        recorded_at,
                        recorded_at,
                        "legacy-lineage-hash",
                        '{"valid":false}',
                        "v1",
                        recorded_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO public.rejected_records (
                        rejection_id, batch_id, payload_id, entity_type,
                        rejection_code, rejection_detail, parser_version,
                        rejected_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "legacy-lineage-rejection",
                        "legacy-lineage-batch",
                        "legacy-lineage-payload",
                        "market",
                        "parse_failed",
                        "invalid fixture",
                        "v1",
                        recorded_at,
                    ),
                )

            apply_postgres_migrations(url)
            with psycopg.connect(url) as connection:
                lineage = connection.execute(
                    """
                    SELECT payload.legacy_payload_id, rejection.raw_payload_id, payload.id
                    FROM raw.rejected_records AS rejection
                    JOIN raw.source_payloads AS payload ON payload.id = rejection.raw_payload_id
                    WHERE rejection.legacy_rejection_id = %s
                    """,
                    ("legacy-lineage-rejection",),
                ).fetchone()

        self.assertEqual(lineage[0], "legacy-lineage-payload")
        self.assertEqual(lineage[1], lineage[2])

    def test_numeric_round_trip_is_exact(self) -> None:
        exact_value = Decimal("9999999999.12345678")
        balancing_value = Decimal("0.87654322")
        with connection_pool(self.settings).connection() as connection:
            connection.execute(
                """
                INSERT INTO app.edge_results (
                    ticker, game_id, side, model_probability, entry_price_cents,
                    fair_price_cents, expected_value_cents, title, notes_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                ("EXACT", "event", "yes", Decimal("0.5"), exact_value, exact_value, exact_value, "exact", "{}"),
            )
            connection.execute(
                """
                INSERT INTO app.edge_results (
                    ticker, game_id, side, model_probability, entry_price_cents,
                    fair_price_cents, expected_value_cents, title, notes_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                ("EXACT-BALANCE", "event", "yes", Decimal("0.5"), balancing_value, balancing_value, balancing_value, "balance", "{}"),
            )
            row = connection.execute(
                "SELECT entry_price_cents, fair_price_cents, expected_value_cents FROM app.edge_results WHERE ticker = %s",
                ("EXACT",),
            ).fetchone()
            aggregate = connection.execute(
                "SELECT SUM(entry_price_cents) AS total FROM app.edge_results WHERE ticker IN (%s, %s)",
                ("EXACT", "EXACT-BALANCE"),
            ).fetchone()["total"]
        self.assertIsNotNone(row)
        self.assertEqual(row["entry_price_cents"], exact_value)
        self.assertEqual(row["fair_price_cents"], exact_value)
        self.assertEqual(row["expected_value_cents"], exact_value)
        self.assertEqual(aggregate, Decimal("10000000000.00000000"))
        self.assertEqual(json_default(aggregate), "10000000000.000000000000")

    def test_persisted_financial_columns_do_not_use_binary_float_types(self) -> None:
        forbidden = self.query_all(
            """
            SELECT table_schema, table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema IN ('app', 'core', 'research', 'ops', 'raw', 'reporting', 'auth')
              AND data_type IN ('double precision', 'real')
            ORDER BY table_schema, table_name, column_name
            """
        )
        json_columns = self.query_all(
            """
            SELECT table_schema, table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema IN ('app', 'core', 'research', 'ops', 'raw', 'reporting', 'auth')
              AND column_name LIKE %s
              AND data_type <> 'jsonb'
            ORDER BY table_schema, table_name, column_name
            """,
            ("%json%",),
        )
        self.assertEqual([dict(row) for row in forbidden], [])
        self.assertEqual([dict(row) for row in json_columns], [])

    def test_backtest_reports_exact_decimal_values_without_float_coercion(self) -> None:
        exact_value = Decimal("9999999999.12345678")
        rows = [
            {
                "event_id": f"exact-event-{index}",
                "market": f"EXACT-{index}",
                "side": "yes",
                "strategy": "exact-decimal",
                "timestamp": "2026-07-18T12:00:00+00:00",
                "settlement_state": "win",
                "actual_outcome": True,
                "entry_price_cents": exact_value,
                "profit_loss_cents": Decimal("0.87654322"),
                "confidence_score": Decimal("0.9"),
                "predicted_probability": Decimal("0.9"),
            }
            for index in range(100)
        ]
        report = build_backtest_report(rows)
        self.assertEqual(report["average_entry_price_cents"], exact_value)
        self.assertIn("9999999999.12345678c", render_backtest_report(report))

    def test_jsonb_preserves_falsey_values(self) -> None:
        values = [None, False, True, 0, 0.0, "", [], [0], {}, {"value": False}]
        with connection_pool(self.settings).connection() as connection:
            for index, value in enumerate(values):
                connection.execute(
                    """
                    INSERT INTO app.source_records (source, kind, url, title, text, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    ("test", "json", f"https://example.test/{index}", f"row-{index}", "body", __import__("json").dumps(value)),
                )
            rows = connection.execute(
                "SELECT metadata_json FROM app.source_records ORDER BY id"
            ).fetchall()
        round_tripped = [row["metadata_json"] for row in rows]
        self.assertEqual(round_tripped, values)
        self.assertEqual([type(value) for value in round_tripped], [type(value) for value in values])

    def test_runtime_search_path_excludes_public(self) -> None:
        with connection_pool(self.settings).connection() as connection:
            search_path = connection.execute("SHOW search_path").fetchone()[0]
            schemas = connection.execute("SELECT current_schemas(false)").fetchone()[0]
            public_tables = connection.execute(
                "SELECT COUNT(*) AS count FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE' AND table_schema = 'public'"
            ).fetchone()["count"]
        self.assertEqual(search_path, "app,pg_catalog")
        self.assertEqual(schemas, ["app", "pg_catalog"])
        self.assertEqual(public_tables, 0)

    def test_connection_context_rolls_back_partial_writes(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "forced_transaction_failure"):
            with connection_pool(self.settings).connection() as connection:
                connection.execute(
                    """
                    INSERT INTO app.source_records (source, kind, url, title, text, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    ("rollback-test", "fixture", "https://example.test/rollback", "rollback", "body", "{}"),
                )
                raise RuntimeError("forced_transaction_failure")
        count = self.query_one(
            "SELECT COUNT(*) AS count FROM app.source_records WHERE source = %s",
            ("rollback-test",),
        )["count"]
        self.assertEqual(count, 0)

    def test_operator_claim_is_atomic(self) -> None:
        inbox = OperatorInbox(self.settings)
        message = inbox.add(title="claim", body="review", created_by="owner", message_id="claim-once")

        def claim(agent: str):
            try:
                return inbox.claim(message["message_id"], agent=agent)
            except ValueError as exc:
                return str(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ["agent-one", "agent-two"]))
        claimed = [result for result in results if isinstance(result, dict)]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["status"], "claimed")

    def test_failed_login_increment_is_atomic(self) -> None:
        auth = LocalAuthStore(self.settings)
        auth.create_user("counter-user", "correct-password", role="researcher")

        def fail_login(_: int):
            return auth.authenticate_password(
                "counter-user",
                "incorrect-password",
                maximum_failures=100,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(fail_login, [1, 2]))
        row = self.query_one("SELECT failed_login_count FROM auth.app_users WHERE username = %s", ("counter-user",))
        self.assertEqual(row["failed_login_count"], 2)

    def test_authentication_and_operator_records_survive_pool_reopen(self) -> None:
        auth = LocalAuthStore(self.settings)
        inbox = OperatorInbox(self.settings)
        auth.create_user("retained-user", "correct-password", role="researcher")
        message = inbox.add(
            title="retained operator instruction",
            body="retain this operational record",
            created_by="owner",
            message_id="retained-message",
        )
        close_connection_pools()
        self.assertIsNotNone(LocalAuthStore(self.settings).authenticate_password("retained-user", "correct-password"))
        retained = OperatorInbox(self.settings).get(message["message_id"])
        self.assertEqual(retained["title"], "retained operator instruction")

    def test_batch_completion_and_checkpoint_are_one_time(self) -> None:
        ledger = CollectionLedger(self.settings)
        batch = ledger.start_batch(
            idempotency_key="batch-once",
            source="test",
            endpoint="endpoint",
            worker_name="worker",
            worker_version="v1",
            collector_version="v1",
        )

        def complete(_: int) -> bool:
            return ledger.complete_batch(
                batch_id=batch.batch_id,
                records_received=1,
                records_accepted=1,
                records_rejected=0,
                records_duplicated=0,
                checkpoint={"source": "test", "endpoint": "endpoint", "cursor": "cursor-1"},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(complete, [1, 2]))
        self.assertEqual(sum(results), 1)
        checkpoint = ledger.checkpoint(source="test", endpoint="endpoint")
        self.assertEqual(checkpoint["cursor"], "cursor-1")
        self.assertEqual(checkpoint["batch_id"], int(batch.batch_id))

    def test_late_worker_completion_cannot_overwrite_newer_run_status(self) -> None:
        monitor = WorkerMonitorStore(self.settings)
        self.assertTrue(
            monitor.start_run(
                worker_name="overlap-worker",
                asset_class="kalshi",
                run_id="run-one",
                idempotency_key="overlap-one",
                attempted_at="2026-07-18T00:00:00+00:00",
            )
        )
        self.assertTrue(
            monitor.start_run(
                worker_name="overlap-worker",
                asset_class="kalshi",
                run_id="run-two",
                idempotency_key="overlap-two",
                attempted_at="2026-07-18T00:01:00+00:00",
            )
        )
        self.assertTrue(
            monitor.finish_success(
                worker_name="overlap-worker",
                idempotency_key="overlap-one",
                finished_at="2026-07-18T00:02:00+00:00",
                records_processed=1,
                details={},
            )
        )
        active = self.query_one(
            "SELECT current_run_id, current_idempotency_key, status FROM ops.worker_status WHERE worker_name = %s",
            ("overlap-worker",),
        )
        self.assertEqual(dict(active), {"current_run_id": "run-two", "current_idempotency_key": "overlap-two", "status": "running"})
        self.assertTrue(
            monitor.finish_success(
                worker_name="overlap-worker",
                idempotency_key="overlap-two",
                finished_at="2026-07-18T00:03:00+00:00",
                records_processed=1,
                details={},
            )
        )
        self.assertEqual(
            self.query_one("SELECT status FROM ops.worker_status WHERE worker_name = %s", ("overlap-worker",))["status"],
            "healthy",
        )

    def test_older_worker_start_cannot_steal_newer_run_status(self) -> None:
        monitor = WorkerMonitorStore(self.settings)
        self.assertTrue(
            monitor.start_run(
                worker_name="ordered-worker",
                asset_class="kalshi",
                run_id="newer-run",
                idempotency_key="newer-owner",
                attempted_at="2026-07-18T00:02:00+00:00",
            )
        )
        self.assertTrue(
            monitor.start_run(
                worker_name="ordered-worker",
                asset_class="kalshi",
                run_id="older-run",
                idempotency_key="older-owner",
                attempted_at="2026-07-18T00:01:00+00:00",
            )
        )

        active = self.query_one(
            "SELECT current_run_id, current_idempotency_key FROM ops.worker_status WHERE worker_name = %s",
            ("ordered-worker",),
        )
        self.assertEqual(
            dict(active),
            {"current_run_id": "newer-run", "current_idempotency_key": "newer-owner"},
        )

    def test_stale_worker_heartbeat_cannot_refresh_newer_owner(self) -> None:
        monitor = WorkerMonitorStore(self.settings)
        self.assertTrue(
            monitor.start_run(
                worker_name="heartbeat-owner",
                asset_class="kalshi",
                run_id="older-run",
                idempotency_key="older-owner",
                attempted_at="2026-07-18T00:01:00+00:00",
            )
        )
        self.assertTrue(
            monitor.start_run(
                worker_name="heartbeat-owner",
                asset_class="kalshi",
                run_id="newer-run",
                idempotency_key="newer-owner",
                attempted_at="2026-07-18T00:02:00+00:00",
            )
        )

        self.assertFalse(monitor.heartbeat("heartbeat-owner", idempotency_key="older-owner"))
        self.assertTrue(monitor.heartbeat("heartbeat-owner", idempotency_key="newer-owner"))

    def test_import_identical_rows_are_reported_without_rewrite(self) -> None:
        row = {
            "username": "import-user",
            "password_hash": "hash",
            "password_salt": "salt",
            "password_algorithm": "scrypt",
            "role": "read_only",
            "is_disabled": False,
            "failed_login_count": 0,
            "created_at": datetime(2026, 7, 18, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 7, 18, tzinfo=timezone.utc),
        }
        with connection_pool(self.settings).connection() as connection:
            first = import_canonical_rows(connection, table="auth.app_users", key_columns=("username",), rows=[row])
            second = import_canonical_rows(connection, table="auth.app_users", key_columns=("username",), rows=[row])
        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.identical_duplicates, 1)

    def test_canonical_import_hash_normalizes_equivalent_utc_timestamps(self) -> None:
        source = {"source": "espn_summary", "freshness_deadline": "2026-07-16T20:38:05Z"}
        target = {
            "source": "espn_summary",
            "freshness_deadline": datetime(2026, 7, 16, 20, 38, 5, tzinfo=timezone.utc),
        }

        self.assertEqual(canonical_row_hash(source), canonical_row_hash(target))

    def test_import_same_count_different_content_fails(self) -> None:
        base = {
            "username": "conflict-user",
            "password_hash": "hash",
            "password_salt": "salt",
            "password_algorithm": "scrypt",
            "role": "read_only",
            "is_disabled": False,
            "failed_login_count": 0,
            "created_at": datetime(2026, 7, 18, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 7, 18, tzinfo=timezone.utc),
        }
        changed = {**base, "role": "admin"}
        with connection_pool(self.settings).connection() as connection:
            import_canonical_rows(connection, table="auth.app_users", key_columns=("username",), rows=[base])
            with self.assertRaises(ImportConflictError):
                import_canonical_rows(connection, table="auth.app_users", key_columns=("username",), rows=[changed])

    def test_import_lineage_rejects_changed_content(self) -> None:
        original_hash = canonical_row_hash({"record": "original"})
        with connection_pool(self.settings).connection() as connection:
            self.assertTrue(
                record_import_lineage(
                    connection,
                    import_id="local-history-20260718",
                    source_system="local_archive",
                    source_table="evaluation.prediction_logs",
                    source_key="42",
                    target_table="app.prediction_logs",
                    target_key="42",
                    content_hash=original_hash,
                )
            )
            self.assertFalse(
                record_import_lineage(
                    connection,
                    import_id="local-history-20260718",
                    source_system="local_archive",
                    source_table="evaluation.prediction_logs",
                    source_key="42",
                    target_table="app.prediction_logs",
                    target_key="42",
                    content_hash=original_hash,
                )
            )
            with self.assertRaises(ImportConflictError):
                record_import_lineage(
                    connection,
                    import_id="local-history-20260718",
                    source_system="local_archive",
                    source_table="evaluation.prediction_logs",
                    source_key="42",
                    target_table="app.prediction_logs",
                    target_key="42",
                    content_hash=canonical_row_hash({"record": "changed"}),
                )

    def test_reporting_day_uses_documented_timezone_boundary(self) -> None:
        store = self.store("timezone-boundary")
        start_paper_test_run(store, run_id="timezone-boundary")
        inserted = 0
        for market_id, timestamp in (("before", "2026-01-01T04:30:00+00:00"), ("after", "2026-01-01T05:30:00+00:00")):
            inserted += store.insert_prediction_logs(
                [
                    {
                        "run_id": "timezone-boundary",
                        "timestamp": timestamp,
                        "event": "event",
                        "event_id": market_id,
                        "market": "market",
                        "market_id": market_id,
                        "side": "yes",
                        "strategy": "primary_80",
                        "model_version": "market_implied_slip_v1",
                        "confidence_score": Decimal("0.8"),
                        "confidence_label": "high",
                        "predicted_outcome": "yes",
                        "event_start_time": "2026-01-01T11:00:00+00:00",
                        "market_close_time": "2026-01-01T12:00:00+00:00",
                        "api_fetched_at": timestamp,
                        "source_snapshot_hash": market_id,
                        "entry_price_cents": Decimal("80"),
                        "implied_probability": Decimal("0.8"),
                        "validation_status": "valid",
                        "validation_errors": [],
                    }
                ]
            )
        self.assertEqual(inserted, 2)
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) AS total FROM app.prediction_logs WHERE run_id = %s",
                ("timezone-boundary",),
            )["total"],
            2,
        )
        prior_day = build_daily_report(store, run_id="timezone-boundary", date="2025-12-31")
        next_day = build_daily_report(store, run_id="timezone-boundary", date="2026-01-01")
        self.assertEqual(prior_day["new_valid_rows"], 1)
        self.assertEqual(next_day["new_valid_rows"], 1)

    def test_harness_resets_the_same_database_the_code_under_test_writes(self) -> None:
        """The harness truncates one database; the code under test opens another.

        ``.env.example`` ships ``DATABASE_URL`` and ``TEST_DATABASE_URL`` pointing
        at different databases. Left unbound, ``setUp`` truncates the test database
        while every store built from the environment writes to the development one:
        rows survive the reset, later assertions read another test's rows, and a
        test run mutates development data.
        """

        with patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://someone@127.0.0.1:1/development"},
            clear=False,
        ):
            settings = test_settings()
            self.assertEqual(
                DatabaseSettings.from_env().require_url(),
                settings.require_url(),
                "resolving test settings must bind DATABASE_URL to the test database",
            )

        store = create_store(settings=DatabaseSettings.from_env())
        start_paper_test_run(store, run_id="harness-isolation")
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) AS total FROM app.paper_test_runs WHERE run_id = %s",
                ("harness-isolation",),
            )["total"],
            1,
            "a store built from the environment must land in the database the harness truncates",
        )
        reset_database(self.settings)
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) AS total FROM app.paper_test_runs WHERE run_id = %s",
                ("harness-isolation",),
            )["total"],
            0,
            "reset_database must clear what the code under test wrote",
        )
