from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from .contracts import EdgeResult, SourceRecord
from .database import DatabaseSession, DatabaseSettings, connection_pool


NON_TRADABLE_MARKET_STATUSES = {
    "closed",
    "settled",
    "resolved",
    "canceled",
    "cancelled",
    "void",
    "inactive",
}

def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _json(value: Any, *, default: Any = None) -> str:
    payload = default if value is None and default is not None else value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _parse_timestamp(value: Any):
    from datetime import datetime, timezone

    if not value:
        return None
    if isinstance(value, datetime):
        timestamp = value
    else:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)


def _prediction_validation_errors(log: dict[str, Any]) -> list[str]:
    timestamp = _parse_timestamp(log.get("timestamp") or log.get("prediction_timestamp"))
    event_start = _parse_timestamp(log.get("event_start_time"))
    market_close = _parse_timestamp(log.get("market_close_time"))
    market_status = str(log.get("market_status") or log.get("status") or "").strip().lower()
    errors = list(log.get("validation_errors") or [])
    if not log.get("run_id"):
        errors.append("missing_run_id")
    if timestamp is None:
        errors.append("missing_prediction_timestamp")
    if event_start is None:
        errors.append("missing_event_start_time")
    if market_close is None:
        errors.append("missing_market_close_time")
    if timestamp and event_start and timestamp >= event_start:
        errors.append("prediction_after_event_start")
    if timestamp and market_close and timestamp >= market_close:
        errors.append("prediction_after_market_close")
    if event_start and market_close and market_close < event_start:
        errors.append("market_closes_before_event_start")
    if market_status in NON_TRADABLE_MARKET_STATUSES:
        errors.append(f"market_not_tradable:{market_status}")
    return sorted(set(errors))


def _validated_log(log: dict[str, Any]) -> dict[str, Any]:
    validated = dict(log)
    errors = _prediction_validation_errors(validated)
    validated["validation_errors"] = errors
    validated["validation_status"] = "invalid" if errors else "valid"
    validated["market_id"] = validated.get("market_id") or validated.get("market")
    validated["event_id"] = validated.get("event_id") or validated.get("event_ticker")
    validated["strategy"] = validated.get("strategy") or validated.get("slip_name")
    validated["source_snapshot_hash"] = validated.get("source_snapshot_hash") or validated.get("source_snapshot_id")
    if validated.get("settlement_state", "unresolved") == "unresolved":
        validated["actual_outcome"] = None
        validated["profit_loss_cents"] = None
    return validated


class PostgresStore:
    """The application persistence API backed exclusively by PostgreSQL."""

    def __init__(self, namespace: str | None = None, *, settings: DatabaseSettings | None = None) -> None:
        self.settings = settings or DatabaseSettings.from_env()
        self.namespace = namespace

    def initialize(self) -> None:
        from .business_store import ensure_database_ready

        ensure_database_ready(self.settings)

    @contextmanager
    def connect(self) -> Iterator[DatabaseSession]:
        with connection_pool(self.settings).connection() as connection:
            yield connection

    def insert_source_records(self, records: list[SourceRecord]) -> None:
        if not records:
            return
        rows = [
            (record.source, record.kind, record.url, record.title, record.text, _json(record.metadata, default={}))
            for record in records
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO app.source_records (source, kind, url, title, text, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                rows,
            )

    def insert_edge_results(self, edges: list[EdgeResult]) -> None:
        if not edges:
            return
        rows = [
            (
                edge.ticker,
                edge.game_id,
                edge.side,
                _decimal(edge.model_probability),
                _decimal(edge.entry_price_cents),
                _decimal(edge.fair_price_cents),
                _decimal(edge.expected_value_cents),
                edge.title,
                _json(edge.notes, default={}),
            )
            for edge in edges
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO app.edge_results
                    (ticker, game_id, side, model_probability, entry_price_cents,
                     fair_price_cents, expected_value_cents, title, notes_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                rows,
            )

    def insert_prediction_logs(self, logs: list[dict[str, Any]]) -> int:
        if not logs:
            return 0
        validated_logs = [_validated_log(log) for log in logs]
        inserted = 0
        with self.connect() as connection:
            for log in validated_logs:
                cursor = connection.execute(
                    """
                    INSERT INTO app.prediction_logs (
                        run_id, prediction_timestamp, event, event_id, market, market_id, side, strategy,
                        input_data_json, odds_json, model_version, confidence_score, confidence_label,
                        predicted_outcome, event_start_time, market_close_time, api_fetched_at,
                        source_updated_at, source_snapshot_id, source_snapshot_hash, snapshot_sequence,
                        entry_price_cents, implied_probability, reason_features_json, validation_status,
                        validation_errors_json, settlement_state, actual_outcome, profit_loss_cents, slip_name
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                        %s, %s::jsonb, %s, %s, %s, %s
                    ) ON CONFLICT DO NOTHING
                    """,
                    (
                        log.get("run_id"), log.get("timestamp", ""), log.get("event", ""), log.get("event_id"),
                        log.get("market", ""), log.get("market_id"), log.get("side", ""), log.get("strategy"),
                        _json(log.get("input_data_used"), default={}), _json(log.get("odds_used"), default={}),
                        log.get("model_version", ""), _decimal(log.get("confidence_score") or 0), log.get("confidence_label", ""),
                        log.get("predicted_outcome", ""), log.get("event_start_time"), log.get("market_close_time"),
                        log.get("api_fetched_at"), log.get("source_updated_at"), log.get("source_snapshot_id"),
                        log.get("source_snapshot_hash"), int(log.get("snapshot_sequence") or 1),
                        _decimal(log.get("entry_price_cents")), _decimal(log.get("implied_probability")),
                        _json(log.get("reason_features"), default={}), log.get("validation_status", "invalid"),
                        _json(log.get("validation_errors"), default=[]), log.get("settlement_state", "unresolved"),
                        None if log.get("actual_outcome") is None else bool(log.get("actual_outcome")),
                        _decimal(log.get("profit_loss_cents")), log.get("slip_name", ""),
                    ),
                )
                inserted += max(0, cursor.rowcount)
        return inserted

    def create_paper_test_run(self, run: dict[str, Any]) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO app.paper_test_runs (run_id, started_at, status, model_versions_json, config_json, config_hash)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (run_id) DO NOTHING
                RETURNING run_id
                """,
                (
                    run["run_id"], run["started_at"], run.get("status", "active"),
                    _json(run.get("model_versions"), default={}), _json(run.get("config"), default={}), run["config_hash"],
                ),
            ).fetchone()
        return row is not None

    def insert_prediction_rejections(self, rejections: list[dict[str, Any]]) -> None:
        if not rejections:
            return
        rows = [
            (
                rejection.get("run_id", ""), rejection.get("timestamp", ""), rejection.get("event", ""),
                rejection.get("event_id"), rejection.get("market", ""), rejection.get("market_id"),
                rejection.get("side", ""), rejection.get("strategy"),
                _json(rejection.get("validation_errors"), default=[]), _json(rejection.get("raw_log"), default={}),
            )
            for rejection in rejections
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO app.prediction_rejections (
                    run_id, prediction_timestamp, event, event_id, market, market_id,
                    side, strategy, validation_errors_json, raw_log_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                rows,
            )
