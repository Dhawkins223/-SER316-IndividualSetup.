"""PostgreSQL persistence for source-backed sports catalog records."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from .business_store import ensure_database_ready
from .collection_ledger import canonical_json, content_hash
from .database import DatabaseSession, DatabaseSettings, connection_pool


class SourceCatalogStore:
    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self.settings = ensure_database_ready(settings)

    @contextmanager
    def _connect(self) -> Iterator[DatabaseSession]:
        with connection_pool(self.settings).connection() as connection:
            yield connection

    def upsert_sports(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        raw_payload_id: str,
        observed_at: str,
    ) -> int:
        accepted = 0
        with self._connect() as connection:
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO core.source_sports (
                        source, source_sport_id, sport_code, display_name, ordering,
                        primary_tag_id, series_id, resolution_url, metadata,
                        first_seen_at, last_seen_at, current_raw_payload_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (source, source_sport_id) DO UPDATE SET
                        sport_code = EXCLUDED.sport_code,
                        display_name = EXCLUDED.display_name,
                        ordering = EXCLUDED.ordering,
                        primary_tag_id = EXCLUDED.primary_tag_id,
                        series_id = EXCLUDED.series_id,
                        resolution_url = EXCLUDED.resolution_url,
                        metadata = EXCLUDED.metadata,
                        last_seen_at = EXCLUDED.last_seen_at,
                        current_raw_payload_id = EXCLUDED.current_raw_payload_id,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        row["source"], row["source_sport_id"], row["sport_code"],
                        row["display_name"], row.get("ordering"), row.get("primary_tag_id"),
                        row.get("series_id"), row.get("resolution_url"),
                        canonical_json(row.get("metadata") or {}), observed_at, observed_at,
                        int(raw_payload_id),
                    ),
                )
                accepted += 1
        return accepted

    def upsert_entities(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        raw_payload_id: str,
        ingestion_batch_id: str,
        observed_at: str,
    ) -> dict[str, int]:
        accepted = 0
        snapshots_inserted = 0
        with self._connect() as connection:
            for row in rows:
                entity = connection.execute(
                    """
                    INSERT INTO core.source_entities (
                        source, source_entity_id, entity_type, display_name,
                        competition, source_id, source_ids, details, source_updated_at,
                        first_seen_at, last_seen_at, current_raw_payload_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (source, source_entity_id) DO UPDATE SET
                        entity_type = EXCLUDED.entity_type,
                        display_name = EXCLUDED.display_name,
                        competition = EXCLUDED.competition,
                        source_id = EXCLUDED.source_id,
                        source_ids = EXCLUDED.source_ids,
                        details = EXCLUDED.details,
                        source_updated_at = EXCLUDED.source_updated_at,
                        last_seen_at = EXCLUDED.last_seen_at,
                        current_raw_payload_id = EXCLUDED.current_raw_payload_id,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING id
                    """,
                    (
                        row["source"], row["source_entity_id"], row["entity_type"],
                        row["display_name"], row.get("competition"), row.get("source_id"),
                        canonical_json(row.get("source_ids") or {}),
                        canonical_json(row.get("details") or {}), row.get("source_updated_at"),
                        observed_at, observed_at, int(raw_payload_id),
                    ),
                ).fetchone()
                if entity is None:  # pragma: no cover - RETURNING guarantees a row
                    raise RuntimeError("source_entity_upsert_failed")
                snapshot_hash = content_hash(
                    {
                        "details": row.get("details") or {},
                        "source_updated_at": row.get("source_updated_at"),
                    }
                )
                inserted = connection.execute(
                    """
                    INSERT INTO core.source_entity_snapshots (
                        source_entity_id, observed_at, source_updated_at, details,
                        snapshot_hash, raw_payload_id, ingestion_batch_id
                    ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (source_entity_id, snapshot_hash) DO NOTHING
                    RETURNING id
                    """,
                    (
                        entity["id"], observed_at, row.get("source_updated_at"),
                        canonical_json(row.get("details") or {}), snapshot_hash,
                        int(raw_payload_id), int(ingestion_batch_id),
                    ),
                ).fetchone()
                snapshots_inserted += int(inserted is not None)
                accepted += 1
        return {"accepted": accepted, "snapshots_inserted": snapshots_inserted}

    def upsert_milestones(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        raw_payload_id: str,
        observed_at: str,
    ) -> int:
        accepted = 0
        with self._connect() as connection:
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO core.source_milestones (
                        source, source_milestone_id, category, milestone_type, title,
                        notification_message, competition, start_time, end_time,
                        primary_event_tickers, related_event_tickers, details,
                        source_id, source_ids, source_updated_at, first_seen_at,
                        last_seen_at, current_raw_payload_id
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s
                    )
                    ON CONFLICT (source, source_milestone_id) DO UPDATE SET
                        category = EXCLUDED.category,
                        milestone_type = EXCLUDED.milestone_type,
                        title = EXCLUDED.title,
                        notification_message = EXCLUDED.notification_message,
                        competition = EXCLUDED.competition,
                        start_time = EXCLUDED.start_time,
                        end_time = EXCLUDED.end_time,
                        primary_event_tickers = EXCLUDED.primary_event_tickers,
                        related_event_tickers = EXCLUDED.related_event_tickers,
                        details = EXCLUDED.details,
                        source_id = EXCLUDED.source_id,
                        source_ids = EXCLUDED.source_ids,
                        source_updated_at = EXCLUDED.source_updated_at,
                        last_seen_at = EXCLUDED.last_seen_at,
                        current_raw_payload_id = EXCLUDED.current_raw_payload_id,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        row["source"], row["source_milestone_id"], row["category"],
                        row["milestone_type"], row["title"], row.get("notification_message"),
                        row.get("competition"), row.get("start_time"), row.get("end_time"),
                        canonical_json(row.get("primary_event_tickers") or []),
                        canonical_json(row.get("related_event_tickers") or []),
                        canonical_json(row.get("details") or {}), row.get("source_id"),
                        canonical_json(row.get("source_ids") or {}), row.get("source_updated_at"),
                        observed_at, observed_at, int(raw_payload_id),
                    ),
                )
                accepted += 1
        return accepted

    def upsert_live_snapshots(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        raw_payload_id: str,
        ingestion_batch_id: str,
        observed_at: str,
        competition: str | None = None,
    ) -> int:
        inserted = 0
        with self._connect() as connection:
            for row in rows:
                snapshot_hash = content_hash(
                    {
                        "type": row.get("live_data_type"),
                        "details": row.get("details") or {},
                        "player_stats": row.get("player_stats") or {},
                    }
                )
                result = connection.execute(
                    """
                    INSERT INTO core.source_live_snapshots (
                        source, source_milestone_id, live_data_type, competition,
                        observed_at, details, player_stats, snapshot_hash,
                        raw_payload_id, ingestion_batch_id
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (source, source_milestone_id, snapshot_hash) DO NOTHING
                    RETURNING id
                    """,
                    (
                        row["source"], row["source_milestone_id"],
                        row.get("live_data_type"), row.get("competition") or competition,
                        observed_at, canonical_json(row.get("details") or {}),
                        canonical_json(row.get("player_stats") or {}), snapshot_hash,
                        int(raw_payload_id), int(ingestion_batch_id),
                    ),
                ).fetchone()
                inserted += int(result is not None)
        return inserted

    def upsert_assets(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        raw_payload_id: str,
        observed_at: str,
        source_page_url: str | None = None,
    ) -> int:
        accepted = 0
        with self._connect() as connection:
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO core.source_assets (
                        source, owner_type, owner_source_id, asset_kind, asset_url,
                        source_page_url, observed_at, raw_payload_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source, owner_type, owner_source_id, asset_kind, asset_url)
                    DO UPDATE SET
                        source_page_url = EXCLUDED.source_page_url,
                        observed_at = EXCLUDED.observed_at,
                        raw_payload_id = EXCLUDED.raw_payload_id,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        row["source"], row["owner_type"], row["owner_source_id"],
                        row["asset_kind"], row["asset_url"], source_page_url,
                        observed_at, int(raw_payload_id),
                    ),
                )
                accepted += 1
        return accepted

    def upsert_external_markets(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        raw_payload_id: str,
        ingestion_batch_id: str,
        observed_at: str,
    ) -> dict[str, int]:
        accepted = 0
        observations_inserted = 0
        outcomes_inserted = 0
        with self._connect() as connection:
            for row in rows:
                source_market_id = str(row.get("market_id") or row.get("condition_id") or "").strip()
                if not source_market_id:
                    raise ValueError("external_market_source_id_required")
                market = connection.execute(
                    """
                    INSERT INTO core.external_markets (
                        venue, source_market_id, source_event_id, condition_id, game_id,
                        slug, question, description, market_type, line, active, closed,
                        game_start_time, start_time, end_time, source_updated_at,
                        first_seen_at, last_seen_at, current_raw_payload_id
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (venue, source_market_id) DO UPDATE SET
                        source_event_id = EXCLUDED.source_event_id,
                        condition_id = EXCLUDED.condition_id,
                        game_id = EXCLUDED.game_id,
                        slug = EXCLUDED.slug,
                        question = EXCLUDED.question,
                        description = EXCLUDED.description,
                        market_type = EXCLUDED.market_type,
                        line = EXCLUDED.line,
                        active = EXCLUDED.active,
                        closed = EXCLUDED.closed,
                        game_start_time = EXCLUDED.game_start_time,
                        start_time = EXCLUDED.start_time,
                        end_time = EXCLUDED.end_time,
                        source_updated_at = EXCLUDED.source_updated_at,
                        last_seen_at = EXCLUDED.last_seen_at,
                        current_raw_payload_id = EXCLUDED.current_raw_payload_id,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING id
                    """,
                    (
                        row["venue"], source_market_id, row.get("source_event_id"),
                        row.get("condition_id"), row.get("game_id"), row.get("slug"),
                        row.get("question") or source_market_id, row.get("description"),
                        row.get("sports_market_type"), row.get("line"), row.get("active"),
                        bool(row.get("closed")), row.get("game_start_time"), row.get("start_date"),
                        row.get("end_date"), row.get("source_updated_at"), observed_at,
                        observed_at, int(raw_payload_id),
                    ),
                ).fetchone()
                if market is None:  # pragma: no cover
                    raise RuntimeError("external_market_upsert_failed")
                # Polling an unchanged book every cycle should not turn into a
                # large fake time series. Keep a new observation only when a
                # quoted price changes; volume/liquidity remain attached to that
                # price state but do not create rows by themselves.
                observation_hash = content_hash(
                    {
                        "best_bid": row.get("best_bid"),
                        "best_ask": row.get("best_ask"),
                        "spread": row.get("spread"),
                        "last_trade_price": row.get("last_trade_price"),
                        "price_sum": row.get("price_sum"),
                        "outcomes": row.get("outcomes") or [],
                    }
                )
                latest = connection.execute(
                    """
                    SELECT snapshot_hash
                    FROM core.external_market_observations
                    WHERE external_market_id = %s
                    ORDER BY observed_at DESC, id DESC
                    LIMIT 1
                    """,
                    (market["id"],),
                ).fetchone()
                observation = None
                if latest is None or str(latest["snapshot_hash"]) != observation_hash:
                    observation = connection.execute(
                        """
                        INSERT INTO core.external_market_observations (
                            external_market_id, observed_at, best_bid, best_ask, spread,
                            last_trade_price, price_sum, volume, liquidity, normalization,
                            snapshot_hash, raw_payload_id, ingestion_batch_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (external_market_id, observed_at, snapshot_hash) DO NOTHING
                        RETURNING id
                        """,
                        (
                            market["id"], observed_at, row.get("best_bid"), row.get("best_ask"),
                            row.get("spread"), row.get("last_trade_price"), row["price_sum"],
                            row.get("volume"), row.get("liquidity"), row["normalization"],
                            observation_hash, int(raw_payload_id), int(ingestion_batch_id),
                        ),
                    ).fetchone()
                if observation is not None:
                    observations_inserted += 1
                    for position, outcome in enumerate(row.get("outcomes") or []):
                        connection.execute(
                            """
                            INSERT INTO core.external_market_outcomes (
                                observation_id, outcome_position, outcome_name, price,
                                normalized_probability, source_token_id
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (
                                observation["id"], position, outcome["outcome"], outcome["price"],
                                outcome["normalized_probability"], outcome.get("clob_token_id"),
                            ),
                        )
                        outcomes_inserted += 1
                accepted += 1
        return {
            "accepted": accepted,
            "observations_inserted": observations_inserted,
            "outcomes_inserted": outcomes_inserted,
        }

    def summary(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM core.source_sports) AS sports,
                    (SELECT COUNT(*) FROM core.source_entities) AS entities,
                    (SELECT COUNT(*) FROM core.source_entity_snapshots) AS entity_snapshots,
                    (SELECT COUNT(*) FROM core.source_milestones) AS milestones,
                    (SELECT COUNT(*) FROM core.source_live_snapshots) AS live_snapshots,
                    (SELECT COUNT(*) FROM core.source_assets) AS assets,
                    (SELECT COUNT(*) FROM core.external_markets) AS external_markets,
                    (SELECT COUNT(*) FROM core.external_market_observations) AS market_observations
                """
            ).fetchone()
        return {key: int(value or 0) for key, value in dict(row or {}).items()}
