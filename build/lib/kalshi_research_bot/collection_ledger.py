from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from .business_store import ensure_database_ready
from .database import DatabaseSession, DatabaseSettings, connection_pool


BATCH_STATUSES = {
    "started",
    "completed",
    "completed_with_rejections",
    "failed",
    "blocked",
    "cancelled",
}
FRESHNESS_STATES = {
    "fresh",
    "approaching_stale",
    "stale",
    "missing",
    "blocked",
    "failed",
    "unknown",
}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class BatchStart:
    batch_id: str
    created: bool


class CollectionLedger:
    """Transactional PostgreSQL collection ledger with retained raw evidence."""

    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self.settings = ensure_database_ready(settings)

    @contextmanager
    def _connect(self) -> Iterator[DatabaseSession]:
        with connection_pool(self.settings).connection() as connection:
            yield connection

    def start_batch(
        self,
        *,
        idempotency_key: str,
        source: str,
        endpoint: str,
        worker_name: str,
        worker_version: str,
        collector_version: str,
        collection_mode: str = "live",
        request_parameters: Mapping[str, Any] | None = None,
        cursor_start: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        started_at: str | None = None,
    ) -> BatchStart:
        request_parameters_json = canonical_json(dict(request_parameters or {}))
        effective_started_at = started_at or utc_iso()
        with self._connect() as connection:
            created = connection.execute(
                """
                INSERT INTO raw.ingestion_batches (
                    idempotency_key, source, endpoint, worker_name,
                    worker_version, collector_version, collection_mode,
                    request_parameters, cursor_start, window_start, window_end,
                    started_at, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, 'started')
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                (
                    idempotency_key,
                    source,
                    endpoint,
                    worker_name,
                    worker_version,
                    collector_version,
                    collection_mode,
                    request_parameters_json,
                    cursor_start,
                    window_start,
                    window_end,
                    effective_started_at,
                ),
            ).fetchone()
            if created is not None:
                return BatchStart(batch_id=str(created["id"]), created=True)
            existing = connection.execute(
                """
                SELECT id, source, endpoint, worker_name, worker_version,
                       collector_version, collection_mode, request_parameters,
                       cursor_start, window_start, window_end
                FROM raw.ingestion_batches
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            ).fetchone()
        if existing is None:  # pragma: no cover - unique conflict guarantees an existing row
            raise RuntimeError("ingestion_batch_identity_missing")
        expected_identity = {
            "source": source,
            "endpoint": endpoint,
            "worker_name": worker_name,
            "worker_version": worker_version,
            "collector_version": collector_version,
            "collection_mode": collection_mode,
            "request_parameters": request_parameters_json,
            "cursor_start": cursor_start,
            "window_start": canonical_timestamp(window_start),
            "window_end": canonical_timestamp(window_end),
        }
        actual_identity = {
            "source": existing["source"],
            "endpoint": existing["endpoint"],
            "worker_name": existing["worker_name"],
            "worker_version": existing["worker_version"],
            "collector_version": existing["collector_version"],
            "collection_mode": existing["collection_mode"],
            "request_parameters": canonical_json(existing["request_parameters"]),
            "cursor_start": existing["cursor_start"],
            "window_start": canonical_timestamp(existing["window_start"]),
            "window_end": canonical_timestamp(existing["window_end"]),
        }
        # A batch's identity is *what* it collected -- source, endpoint, worker,
        # versions, request parameters, cursor and window. Not the instant the
        # attempt began. Comparing started_at made every retry a conflict, because
        # a second attempt of the same logical collection necessarily starts later.
        # The stored started_at still records the first attempt.
        if actual_identity != expected_identity:
            raise RuntimeError("ingestion_batch_content_conflict")
        return BatchStart(batch_id=str(existing["id"]), created=False)

    def store_payload(
        self,
        *,
        batch_id: str,
        source: str,
        entity_type: str,
        payload: Any,
        parser_version: str,
        source_identifier: str | None = None,
        observed_at: str | None = None,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        digest = content_hash(payload)
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT INTO raw.source_payloads (
                    batch_id, source, entity_type, source_identifier,
                    observed_at, received_at, content_hash, payload_json, parser_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    int(batch_id),
                    source,
                    entity_type,
                    source_identifier,
                    observed_at,
                    received_at or utc_iso(),
                    digest,
                    canonical_json(payload),
                    parser_version,
                ),
            ).fetchone()
            if inserted is not None:
                return {"payload_id": str(inserted["id"]), "content_hash": digest, "duplicate": False}
            existing = connection.execute(
                """
                SELECT id FROM raw.source_payloads
                WHERE batch_id = %s
                  AND source_identifier IS NOT DISTINCT FROM %s
                  AND content_hash = %s
                """,
                (int(batch_id), source_identifier, digest),
            ).fetchone()
        if existing is None:
            raise RuntimeError("raw_payload_conflict_without_matching_content")
        return {"payload_id": str(existing["id"]), "content_hash": digest, "duplicate": True}

    def reject(
        self,
        *,
        batch_id: str,
        entity_type: str,
        rejection_code: str,
        parser_version: str,
        payload_id: str | None = None,
        rejection_detail: str | None = None,
        rejected_at: str | None = None,
    ) -> str:
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO raw.rejected_records (
                    batch_id, raw_payload_id, entity_type, rejection_code,
                    rejection_detail, parser_version, rejected_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    int(batch_id),
                    int(payload_id) if payload_id is not None else None,
                    entity_type,
                    rejection_code,
                    rejection_detail,
                    parser_version,
                    rejected_at or utc_iso(),
                ),
            ).fetchone()
        if row is None:  # pragma: no cover - PostgreSQL RETURNING guarantees a row
            raise RuntimeError("rejection_record_create_failed")
        return str(row["id"])

    def complete_batch(
        self,
        *,
        batch_id: str,
        records_received: int,
        records_accepted: int,
        records_rejected: int,
        records_duplicated: int,
        payload_hash_value: str | None = None,
        http_status: int | None = None,
        cursor_end: str | None = None,
        checkpoint: Mapping[str, Any] | None = None,
        completed_at: str | None = None,
    ) -> bool:
        status = "completed_with_rejections" if records_rejected else "completed"
        finished = completed_at or utc_iso()
        with self._connect() as connection:
            completed = connection.execute(
                """
                UPDATE raw.ingestion_batches
                SET completed_at = %s, status = %s, http_status = %s, records_received = %s,
                    records_accepted = %s, records_rejected = %s, records_duplicated = %s,
                    payload_hash = %s, cursor_end = %s
                WHERE id = %s AND status = 'started'
                RETURNING id
                """,
                (
                    finished,
                    status,
                    http_status,
                    records_received,
                    records_accepted,
                    records_rejected,
                    records_duplicated,
                    payload_hash_value,
                    cursor_end,
                    int(batch_id),
                ),
            ).fetchone()
            if completed is None:
                return False
            if checkpoint:
                connection.execute(
                    """
                    INSERT INTO ops.collection_checkpoints (
                        source, endpoint, partition_scope, cursor, window_start, window_end,
                        last_successful_item_time, ingestion_batch_id, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source, endpoint, partition_scope) DO UPDATE SET
                        cursor = EXCLUDED.cursor,
                        window_start = EXCLUDED.window_start,
                        window_end = EXCLUDED.window_end,
                        last_successful_item_time = EXCLUDED.last_successful_item_time,
                        ingestion_batch_id = EXCLUDED.ingestion_batch_id,
                        updated_at = EXCLUDED.updated_at
                    WHERE ops.collection_checkpoints.ingestion_batch_id <= EXCLUDED.ingestion_batch_id
                    """,
                    (
                        checkpoint["source"],
                        checkpoint["endpoint"],
                        checkpoint.get("partition_scope", ""),
                        checkpoint.get("cursor"),
                        checkpoint.get("window_start"),
                        checkpoint.get("window_end"),
                        checkpoint.get("last_successful_item_time"),
                        int(batch_id),
                        finished,
                    ),
                )
        return True

    def fail_batch(
        self,
        *,
        batch_id: str,
        error_code: str,
        error_message: str | None = None,
        blocked: bool = False,
        completed_at: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE raw.ingestion_batches
                SET completed_at = %s, status = %s, error_code = %s, error_message = %s
                WHERE id = %s AND status = 'started'
                RETURNING id
                """,
                (
                    completed_at or utc_iso(),
                    "blocked" if blocked else "failed",
                    error_code,
                    error_message,
                    int(batch_id),
                ),
            ).fetchone()
        return row is not None

    def update_source_health(
        self,
        *,
        source: str,
        last_attempted_at: str,
        freshness_state: str,
        last_successful_at: str | None = None,
        freshness_deadline: str | None = None,
        last_error: str | None = None,
    ) -> None:
        if freshness_state not in FRESHNESS_STATES:
            raise ValueError(f"invalid_freshness_state:{freshness_state}")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ops.source_health (
                    source, last_attempted_at, last_successful_at, freshness_deadline,
                    freshness_state, consecutive_failures, last_error, updated_at
                ) VALUES (%s, %s, %s, %s, %s,
                    CASE WHEN %s IN ('fresh', 'approaching_stale') THEN 0 ELSE 1 END,
                    %s, %s)
                ON CONFLICT (source) DO UPDATE SET
                    last_attempted_at = EXCLUDED.last_attempted_at,
                    last_successful_at = COALESCE(EXCLUDED.last_successful_at, ops.source_health.last_successful_at),
                    freshness_deadline = EXCLUDED.freshness_deadline,
                    freshness_state = EXCLUDED.freshness_state,
                    consecutive_failures = CASE
                        WHEN EXCLUDED.freshness_state IN ('fresh', 'approaching_stale') THEN 0
                        ELSE ops.source_health.consecutive_failures + 1
                    END,
                    last_error = EXCLUDED.last_error,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    source,
                    last_attempted_at,
                    last_successful_at,
                    freshness_deadline,
                    freshness_state,
                    freshness_state,
                    last_error,
                    utc_iso(),
                ),
            )

    def checkpoint(self, *, source: str, endpoint: str, partition_scope: str = "") -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT checkpoint.*, checkpoint.ingestion_batch_id AS batch_id, batch.idempotency_key
                FROM ops.collection_checkpoints AS checkpoint
                JOIN raw.ingestion_batches AS batch ON batch.id = checkpoint.ingestion_batch_id
                WHERE checkpoint.source = %s AND checkpoint.endpoint = %s AND checkpoint.partition_scope = %s
                """,
                (source, endpoint, partition_scope),
            ).fetchone()
        return dict(row) if row else None
