from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .collection_ledger import canonical_json, canonical_timestamp, content_hash
from .database import DatabaseSettings, connection_pool


SOURCE_NAME = "kalshi_public_api"
ENDPOINT_NAME = "today_combo_markets"
COLLECTOR_VERSION = "kalshi_today_postgres_v1"


def _required_text(value: Any, error: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(error)
    return text


def _timestamp(value: Any, error: str) -> str:
    try:
        timestamp = canonical_timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(error) from exc
    if timestamp is None:
        raise ValueError(error)
    return timestamp


def _decimal(value: Any, error: str, *, divisor: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(error) from exc
    if divisor is not None:
        number /= divisor
    if not number.is_finite():
        raise ValueError(error)
    return number


def _probability(value: Any, error: str) -> Decimal | None:
    probability = _decimal(value, error, divisor=Decimal("100"))
    if probability is not None and not Decimal("0") <= probability <= Decimal("1"):
        raise ValueError(error)
    return probability


def _nonnegative(value: Any, error: str) -> Decimal | None:
    number = _decimal(value, error)
    if number is not None and number < 0:
        raise ValueError(error)
    return number


def _series_ticker(market: Mapping[str, Any], event_ticker: str) -> str:
    configured = str(market.get("mve_collection_ticker") or "").strip()
    if configured:
        return configured
    prefix = event_ticker.split("-", 1)[0].strip()
    return prefix or "KXMVE"


def _snapshot_key(payload: Mapping[str, Any], worker_name: str) -> str:
    identity = {
        "date": payload.get("date"),
        "generated_at": payload.get("generated_at"),
        "markets": [
            {
                "ticker": market.get("ticker"),
                "api_fetched_at": market.get("api_fetched_at"),
                "source_snapshot_hash": market.get("source_snapshot_hash"),
            }
            for market in payload.get("markets") or []
            if isinstance(market, Mapping)
        ],
    }
    return f"{worker_name}:{content_hash(identity)}"


def _upsert_market(
    connection: Any,
    *,
    market: Mapping[str, Any],
    raw_payload_id: int,
    batch_id: int,
    received_at: str,
) -> bool:
    ticker = _required_text(market.get("ticker"), "missing_market_ticker")
    event_ticker = _required_text(market.get("event_ticker"), "missing_event_ticker")
    observed_at = _timestamp(
        market.get("api_fetched_at") or market.get("source_updated_at") or received_at,
        "invalid_market_observed_at",
    )
    title = str(market.get("title") or ticker).strip() or ticker
    status = str(market.get("status") or "unknown").strip().lower() or "unknown"
    series_ticker = _series_ticker(market, event_ticker)
    category = "sports"
    expected_expiration = canonical_timestamp(market.get("expected_expiration_time"))
    close_time = canonical_timestamp(market.get("close_time"))
    settlement_metadata = canonical_json(
        {
            "source": market.get("source") or SOURCE_NAME,
            "source_url": market.get("source_url"),
        }
    )
    series_row = connection.execute(
        """
        INSERT INTO core.series (
            series_ticker, title, category, settlement_source_metadata,
            first_seen_at, last_seen_at, current_raw_payload_id
        ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
        ON CONFLICT (series_ticker) DO UPDATE SET
            title = EXCLUDED.title,
            category = EXCLUDED.category,
            settlement_source_metadata = EXCLUDED.settlement_source_metadata,
            last_seen_at = GREATEST(core.series.last_seen_at, EXCLUDED.last_seen_at),
            current_raw_payload_id = EXCLUDED.current_raw_payload_id,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            series_ticker,
            series_ticker,
            category,
            settlement_metadata,
            observed_at,
            observed_at,
            raw_payload_id,
        ),
    ).fetchone()
    event_row = connection.execute(
        """
        INSERT INTO core.events (
            event_ticker, series_id, title, category, status, expected_expiration,
            settlement_source_metadata, first_seen_at, last_seen_at, current_raw_payload_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        ON CONFLICT (event_ticker) DO UPDATE SET
            series_id = EXCLUDED.series_id,
            title = EXCLUDED.title,
            category = EXCLUDED.category,
            status = EXCLUDED.status,
            expected_expiration = EXCLUDED.expected_expiration,
            settlement_source_metadata = EXCLUDED.settlement_source_metadata,
            last_seen_at = GREATEST(core.events.last_seen_at, EXCLUDED.last_seen_at),
            current_raw_payload_id = EXCLUDED.current_raw_payload_id,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            event_ticker,
            int(series_row["id"]),
            title,
            category,
            status,
            expected_expiration,
            settlement_metadata,
            observed_at,
            observed_at,
            raw_payload_id,
        ),
    ).fetchone()
    market_row = connection.execute(
        """
        INSERT INTO core.markets (
            market_ticker, event_id, market_type, title, subtitle, status,
            close_time, expiration_time, rules, first_seen_at, last_seen_at,
            current_raw_payload_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (market_ticker) DO UPDATE SET
            event_id = EXCLUDED.event_id,
            market_type = EXCLUDED.market_type,
            title = EXCLUDED.title,
            subtitle = EXCLUDED.subtitle,
            status = EXCLUDED.status,
            close_time = EXCLUDED.close_time,
            expiration_time = EXCLUDED.expiration_time,
            rules = EXCLUDED.rules,
            last_seen_at = GREATEST(core.markets.last_seen_at, EXCLUDED.last_seen_at),
            current_raw_payload_id = EXCLUDED.current_raw_payload_id,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            ticker,
            int(event_row["id"]),
            "combo",
            title,
            market.get("legs_text"),
            status,
            close_time,
            expected_expiration,
            market.get("legs_text"),
            observed_at,
            observed_at,
            raw_payload_id,
        ),
    ).fetchone()
    yes_bid = _probability(market.get("yes_bid_cents"), "invalid_yes_bid")
    yes_ask = _probability(market.get("yes_ask_cents"), "invalid_yes_ask")
    no_bid = _probability(market.get("no_bid_cents"), "invalid_no_bid")
    no_ask = _probability(market.get("no_ask_cents"), "invalid_no_ask")
    if yes_bid is not None and yes_ask is not None and yes_bid > yes_ask:
        raise ValueError("invalid_yes_spread")
    if no_bid is not None and no_ask is not None and no_bid > no_ask:
        raise ValueError("invalid_no_spread")
    inserted = connection.execute(
        """
        INSERT INTO core.market_observations (
            market_id, observed_at, source_received_at, status,
            yes_bid, yes_ask, no_bid, no_ask, volume_24h, liquidity,
            raw_payload_id, ingestion_batch_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (market_id, observed_at, raw_payload_id) DO NOTHING
        RETURNING id
        """,
        (
            int(market_row["id"]),
            observed_at,
            received_at,
            status,
            yes_bid,
            yes_ask,
            no_bid,
            no_ask,
            _nonnegative(market.get("volume_24h"), "invalid_volume_24h"),
            _nonnegative(market.get("liquidity_dollars"), "invalid_liquidity"),
            raw_payload_id,
            batch_id,
        ),
    ).fetchone()
    return inserted is not None


def persist_kalshi_snapshot(
    payload: Mapping[str, Any],
    *,
    worker_name: str,
    idempotency_key: str | None = None,
    settings: DatabaseSettings | None = None,
) -> dict[str, Any]:
    if payload.get("refresh_error"):
        raise ValueError("kalshi_refresh_error_payload_not_persisted")
    received_at = _timestamp(payload.get("generated_at"), "missing_generated_at")
    markets = [market for market in payload.get("markets") or [] if isinstance(market, Mapping)]
    batch_key = idempotency_key or _snapshot_key(payload, worker_name)
    digest = content_hash(payload)
    worker_version = str(os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "local")
    source_identifier = str(payload.get("date") or "current")
    request_parameters = canonical_json({"date": payload.get("date")})
    freshness_seconds = max(60, int(os.environ.get("KALSHI_SOURCE_FRESHNESS_SECONDS", "1800")))
    freshness_deadline = (
        datetime.fromisoformat(received_at).astimezone(timezone.utc) + timedelta(seconds=freshness_seconds)
    ).isoformat()
    configured = settings or DatabaseSettings.from_env()
    with connection_pool(configured).connection() as connection:
        batch = connection.execute(
            """
            INSERT INTO raw.ingestion_batches (
                idempotency_key, source, endpoint, worker_name, worker_version,
                collector_version, collection_mode, request_parameters,
                started_at, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'live', %s::jsonb, %s, 'started')
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                batch_key,
                SOURCE_NAME,
                ENDPOINT_NAME,
                worker_name,
                worker_version,
                COLLECTOR_VERSION,
                request_parameters,
                received_at,
            ),
        ).fetchone()
        if batch is None:
            existing = connection.execute(
                """
                SELECT id, status, payload_hash, records_received, records_accepted,
                       records_rejected, records_duplicated
                FROM raw.ingestion_batches
                WHERE idempotency_key = %s
                """,
                (batch_key,),
            ).fetchone()
            if existing is None:
                raise RuntimeError("kalshi_ingestion_batch_missing")
            if existing["payload_hash"] not in {None, digest}:
                raise RuntimeError("kalshi_ingestion_batch_content_conflict")
            if existing["status"] not in {"completed", "completed_with_rejections"}:
                raise RuntimeError(f"kalshi_ingestion_batch_not_complete:{existing['status']}")
            return {
                "batch_id": str(existing["id"]),
                "created": False,
                "records_received": int(existing["records_received"]),
                "records_accepted": int(existing["records_accepted"]),
                "records_rejected": int(existing["records_rejected"]),
                "records_duplicated": int(existing["records_duplicated"]),
                "payload_hash": digest,
                "freshness_state": "fresh",
            }
        batch_id = int(batch["id"])
        raw_row = connection.execute(
            """
            INSERT INTO raw.source_payloads (
                batch_id, source, entity_type, source_identifier, observed_at,
                received_at, content_hash, payload_json, parser_version
            ) VALUES (%s, %s, 'kalshi_today_snapshot', %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id
            """,
            (
                batch_id,
                SOURCE_NAME,
                source_identifier,
                received_at,
                received_at,
                digest,
                canonical_json(payload),
                COLLECTOR_VERSION,
            ),
        ).fetchone()
        raw_payload_id = int(raw_row["id"])
        accepted = 0
        rejected = 0
        duplicated = 0
        for market in markets:
            try:
                inserted = _upsert_market(
                    connection,
                    market=market,
                    raw_payload_id=raw_payload_id,
                    batch_id=batch_id,
                    received_at=received_at,
                )
            except (TypeError, ValueError) as exc:
                rejected += 1
                connection.execute(
                    """
                    INSERT INTO raw.rejected_records (
                        batch_id, raw_payload_id, entity_type, rejection_code,
                        rejection_detail, parser_version, rejected_at
                    ) VALUES (%s, %s, 'kalshi_market', %s, %s, %s, %s)
                    """,
                    (
                        batch_id,
                        raw_payload_id,
                        str(exc),
                        str(market.get("ticker") or market.get("event_ticker") or "unknown"),
                        COLLECTOR_VERSION,
                        received_at,
                    ),
                )
                continue
            if inserted:
                accepted += 1
            else:
                duplicated += 1
        status = "completed_with_rejections" if rejected else "completed"
        connection.execute(
            """
            UPDATE raw.ingestion_batches
            SET completed_at = %s, status = %s, http_status = 200,
                records_received = %s, records_accepted = %s,
                records_rejected = %s, records_duplicated = %s,
                payload_hash = %s
            WHERE id = %s AND status = 'started'
            """,
            (received_at, status, len(markets), accepted, rejected, duplicated, digest, batch_id),
        )
        connection.execute(
            """
            INSERT INTO ops.collection_checkpoints (
                source, endpoint, partition_scope, last_successful_item_time,
                ingestion_batch_id, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, endpoint, partition_scope) DO UPDATE SET
                last_successful_item_time = EXCLUDED.last_successful_item_time,
                ingestion_batch_id = EXCLUDED.ingestion_batch_id,
                updated_at = EXCLUDED.updated_at
            WHERE ops.collection_checkpoints.ingestion_batch_id <= EXCLUDED.ingestion_batch_id
            """,
            (SOURCE_NAME, ENDPOINT_NAME, source_identifier, received_at, batch_id, received_at),
        )
        connection.execute(
            """
            INSERT INTO ops.source_health (
                source, last_attempted_at, last_successful_at, freshness_deadline,
                freshness_state, consecutive_failures, updated_at
            ) VALUES (%s, %s, %s, %s, 'fresh', 0, %s)
            ON CONFLICT (source) DO UPDATE SET
                last_attempted_at = EXCLUDED.last_attempted_at,
                last_successful_at = EXCLUDED.last_successful_at,
                freshness_deadline = EXCLUDED.freshness_deadline,
                freshness_state = 'fresh',
                consecutive_failures = 0,
                last_error = NULL,
                updated_at = EXCLUDED.updated_at
            """,
            (SOURCE_NAME, received_at, received_at, freshness_deadline, received_at),
        )
    return {
        "batch_id": str(batch_id),
        "created": True,
        "records_received": len(markets),
        "records_accepted": accepted,
        "records_rejected": rejected,
        "records_duplicated": duplicated,
        "payload_hash": digest,
        "freshness_state": "fresh",
    }


def load_latest_kalshi_snapshot(
    *,
    settings: DatabaseSettings | None = None,
) -> dict[str, Any] | None:
    configured = settings or DatabaseSettings.from_env()
    with connection_pool(configured).connection() as connection:
        row = connection.execute(
            """
            SELECT payload.payload_json, payload.content_hash, payload.observed_at,
                   payload.received_at, batch.id AS batch_id, batch.worker_name
            FROM raw.source_payloads AS payload
            JOIN raw.ingestion_batches AS batch ON batch.id = payload.batch_id
            WHERE payload.source = %s
              AND payload.entity_type = 'kalshi_today_snapshot'
              AND batch.status IN ('completed', 'completed_with_rejections')
            ORDER BY payload.observed_at DESC NULLS LAST, payload.id DESC
            LIMIT 1
            """,
            (SOURCE_NAME,),
        ).fetchone()
    if row is None:
        return None
    payload = row["payload_json"]
    if not isinstance(payload, Mapping):
        raise RuntimeError("kalshi_snapshot_payload_not_object")
    result = dict(payload)
    result["dashboard_snapshot"] = {
        "source": "postgres",
        "connector_name": SOURCE_NAME,
        "worker_name": str(row["worker_name"]),
        "batch_id": str(row["batch_id"]),
        "content_hash": str(row["content_hash"]),
        "observed_at": row["observed_at"].isoformat() if row["observed_at"] else None,
        "received_at": row["received_at"].isoformat(),
    }
    return result
