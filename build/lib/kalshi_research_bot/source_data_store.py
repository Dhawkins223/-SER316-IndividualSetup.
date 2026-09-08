"""PostgreSQL read model and durable queue for cloud source data."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4

from .business_store import ensure_database_ready
from .collection_ledger import canonical_json
from .database import DatabaseSession, DatabaseSettings, connection_pool


SUPPORTED_REFRESH_SOURCES = frozenset(
    {"kalshi_current", "kalshi_reference", "polymarket", "sports_current"}
)
MAX_SOURCE_DATA_PAGE_SIZE = 200


def utc_iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat()


class SourceDataStore:
    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self.settings = ensure_database_ready(settings)

    @contextmanager
    def _connect(self) -> Iterator[DatabaseSession]:
        with connection_pool(self.settings).connection() as connection:
            yield connection

    def summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM core.source_sports) AS sports,
                    (SELECT COUNT(*) FROM core.source_entities) AS entities,
                    (SELECT COUNT(*) FROM core.source_entities WHERE entity_type = 'team') AS teams,
                    (SELECT COUNT(*) FROM core.source_entities WHERE entity_type LIKE '%%_player') AS players,
                    (SELECT COUNT(*) FROM core.source_milestones) AS milestones,
                    (SELECT COUNT(*) FROM core.source_live_snapshots) AS live_snapshots,
                    (SELECT COUNT(*) FROM core.source_assets) AS assets,
                    (SELECT COUNT(*) FROM core.external_markets) AS external_markets,
                    (SELECT MAX(last_seen_at) FROM core.source_entities) AS entities_fresh_at,
                    (SELECT MAX(observed_at) FROM core.source_live_snapshots) AS live_fresh_at,
                    (SELECT MAX(observed_at) FROM core.external_market_observations) AS markets_fresh_at,
                    (SELECT COUNT(*) FROM ops.source_refresh_requests WHERE status = 'queued') AS queued_refreshes,
                    (SELECT COUNT(*) FROM ops.source_refresh_requests WHERE status = 'running') AS running_refreshes
                """
            ).fetchone()
        data = dict(row or {})
        counts = {
            key: int(data.get(key) or 0)
            for key in (
                "sports", "entities", "teams", "players", "milestones",
                "live_snapshots", "assets", "external_markets",
            )
        }
        return {
            "status": "ready" if any(counts.values()) else "empty",
            "counts": counts,
            "freshness": {
                "entities": data.get("entities_fresh_at"),
                "live": data.get("live_fresh_at"),
                "markets": data.get("markets_fresh_at"),
            },
            "refresh_queue": {
                "queued": int(data.get("queued_refreshes") or 0),
                "running": int(data.get("running_refreshes") or 0),
            },
        }

    def list_entities(
        self,
        *,
        source: str | None = None,
        entity_type: str | None = None,
        competition: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
        max_age_seconds: int = 43200,
    ) -> dict[str, Any]:
        conditions = ["entity.last_seen_at >= CURRENT_TIMESTAMP - make_interval(secs => %s)"]
        parameters: list[Any] = [max(60, int(max_age_seconds))]
        if entity_type == "player":
            conditions.append("RIGHT(entity.entity_type, 7) = '_player'")
        elif entity_type:
            conditions.append("entity.entity_type = %s")
            parameters.append(str(entity_type))
        for column, value in (
            ("entity.source", source),
            ("entity.competition", competition),
        ):
            if value:
                conditions.append(f"{column} = %s")
                parameters.append(str(value))
        if search:
            conditions.append("entity.display_name ILIKE %s")
            parameters.append(f"%{str(search).strip()}%")
        bounded_limit = max(1, min(int(limit), MAX_SOURCE_DATA_PAGE_SIZE))
        bounded_offset = max(0, int(offset))
        parameters.extend((bounded_limit, bounded_offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT entity.source, entity.source_entity_id, entity.entity_type,
                       entity.display_name, entity.competition, entity.source_ids,
                       entity.details, entity.source_updated_at, entity.last_seen_at,
                       asset.asset_kind, asset.asset_url,
                       COUNT(*) OVER () AS total_count
                FROM core.source_entities AS entity
                LEFT JOIN LATERAL (
                    SELECT asset_kind, asset_url
                    FROM core.source_assets
                    WHERE source = entity.source
                      AND owner_type = 'entity'
                      AND owner_source_id = entity.source_entity_id
                    ORDER BY observed_at DESC, id DESC
                    LIMIT 1
                ) AS asset ON TRUE
                WHERE {' AND '.join(conditions)}
                ORDER BY entity.competition NULLS LAST, entity.display_name, entity.source_entity_id
                LIMIT %s OFFSET %s
                """,
                tuple(parameters),
            ).fetchall()
        total = int(rows[0]["total_count"] or 0) if rows else 0
        items = []
        for row in rows:
            item = dict(row)
            item.pop("total_count", None)
            items.append(item)
        return {
            "status": "ready" if items else "empty",
            "total_count": total,
            "returned_count": len(items),
            "offset": bounded_offset,
            "limit": bounded_limit,
            "next_offset": bounded_offset + len(items) if bounded_offset + len(items) < total else None,
            "max_age_seconds": max(60, int(max_age_seconds)),
            "items": items,
        }

    def list_external_markets(
        self,
        *,
        venue: str = "polymarket",
        market_type: str | None = None,
        search: str | None = None,
        starts_within_hours: int | None = None,
        limit: int = 50,
        offset: int = 0,
        max_age_seconds: int = 7200,
    ) -> dict[str, Any]:
        conditions = [
            "market.venue = %s",
            "market.closed = FALSE",
            "market.active IS DISTINCT FROM FALSE",
            "observation.observed_at >= CURRENT_TIMESTAMP - make_interval(secs => %s)",
        ]
        parameters: list[Any] = [venue, max(60, int(max_age_seconds))]
        if market_type:
            conditions.append("market.market_type = %s")
            parameters.append(str(market_type))
        if search:
            conditions.append("(market.question ILIKE %s OR market.slug ILIKE %s)")
            needle = f"%{str(search).strip()}%"
            parameters.extend((needle, needle))
        if starts_within_hours is not None:
            conditions.append("market.game_start_time BETWEEN CURRENT_TIMESTAMP AND CURRENT_TIMESTAMP + make_interval(hours => %s)")
            parameters.append(max(1, min(int(starts_within_hours), 168)))
        bounded_limit = max(1, min(int(limit), MAX_SOURCE_DATA_PAGE_SIZE))
        bounded_offset = max(0, int(offset))
        parameters.extend((bounded_limit, bounded_offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT market.venue, market.source_market_id, market.source_event_id,
                       market.condition_id, market.game_id, market.slug, market.question,
                       market.description, market.market_type, market.line,
                       market.game_start_time, market.start_time, market.end_time,
                       market.source_updated_at, market.last_seen_at,
                       observation.observed_at, observation.best_bid, observation.best_ask,
                       observation.spread, observation.last_trade_price, observation.price_sum,
                       observation.volume, observation.liquidity, observation.normalization,
                       outcomes.items AS outcomes, asset.asset_url,
                       COUNT(*) OVER () AS total_count
                FROM core.external_markets AS market
                JOIN LATERAL (
                    SELECT * FROM core.external_market_observations
                    WHERE external_market_id = market.id
                    ORDER BY observed_at DESC, id DESC
                    LIMIT 1
                ) AS observation ON TRUE
                LEFT JOIN LATERAL (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'name', outcome_name,
                            'price', price,
                            'probability', normalized_probability,
                            'token_id', source_token_id
                        ) ORDER BY outcome_position
                    ) AS items
                    FROM core.external_market_outcomes
                    WHERE observation_id = observation.id
                ) AS outcomes ON TRUE
                LEFT JOIN LATERAL (
                    SELECT asset_url
                    FROM core.source_assets
                    WHERE source = market.venue
                      AND owner_type = 'market'
                      AND owner_source_id = market.source_market_id
                    ORDER BY observed_at DESC, id DESC
                    LIMIT 1
                ) AS asset ON TRUE
                WHERE {' AND '.join(conditions)}
                ORDER BY market.game_start_time NULLS LAST, market.question, market.source_market_id
                LIMIT %s OFFSET %s
                """,
                tuple(parameters),
            ).fetchall()
        total = int(rows[0]["total_count"] or 0) if rows else 0
        items = []
        for row in rows:
            item = dict(row)
            item.pop("total_count", None)
            items.append(item)
        return {
            "status": "ready" if items else "empty",
            "total_count": total,
            "returned_count": len(items),
            "offset": bounded_offset,
            "limit": bounded_limit,
            "next_offset": bounded_offset + len(items) if bounded_offset + len(items) < total else None,
            "max_age_seconds": max(60, int(max_age_seconds)),
            "items": items,
        }

    def list_live_data(
        self,
        *,
        competition: str | None = None,
        limit: int = 50,
        max_age_seconds: int = 900,
    ) -> dict[str, Any]:
        conditions = ["observed_at >= CURRENT_TIMESTAMP - make_interval(secs => %s)"]
        parameters: list[Any] = [max(60, int(max_age_seconds))]
        if competition:
            conditions.append("competition = %s")
            parameters.append(str(competition))
        bounded_limit = max(1, min(int(limit), MAX_SOURCE_DATA_PAGE_SIZE))
        parameters.append(bounded_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT ON (source, source_milestone_id)
                       source, source_milestone_id, live_data_type, competition,
                       observed_at, details, player_stats
                FROM core.source_live_snapshots
                WHERE {' AND '.join(conditions)}
                ORDER BY source, source_milestone_id, observed_at DESC, id DESC
                LIMIT %s
                """,
                tuple(parameters),
            ).fetchall()
        items = [dict(row) for row in rows]
        return {
            "status": "ready" if items else "empty",
            "returned_count": len(items),
            "limit": bounded_limit,
            "max_age_seconds": max(60, int(max_age_seconds)),
            "items": items,
        }

    def enqueue_refresh(
        self,
        *,
        sources: Sequence[str],
        reason: str,
        requested_by: str,
        scope: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        priority: int = 100,
        not_before: str | None = None,
    ) -> dict[str, Any]:
        normalized_sources = tuple(dict.fromkeys(str(source).strip() for source in sources if str(source).strip()))
        unsupported = sorted(set(normalized_sources) - SUPPORTED_REFRESH_SOURCES)
        if not normalized_sources:
            raise ValueError("refresh_sources_required")
        if unsupported:
            raise ValueError(f"unsupported_refresh_sources:{','.join(unsupported)}")
        if reason not in {"manual", "scheduled", "pregame"}:
            raise ValueError(f"unsupported_refresh_reason:{reason}")
        request_id = str(uuid4())
        key = str(idempotency_key or f"refresh:{request_id}")
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO ops.source_refresh_requests (
                    request_id, idempotency_key, sources, scope, reason,
                    requested_by, priority, not_before
                ) VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, COALESCE(%s, CURRENT_TIMESTAMP))
                ON CONFLICT (idempotency_key) DO UPDATE SET
                    updated_at = ops.source_refresh_requests.updated_at
                RETURNING *
                """,
                (
                    request_id, key, canonical_json(normalized_sources),
                    canonical_json(scope or {}), reason, requested_by,
                    max(1, min(int(priority), 1000)), not_before,
                ),
            ).fetchone()
        return dict(row or {})

    def claim_refresh(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT request_id
                    FROM ops.source_refresh_requests
                    WHERE (
                        (status = 'queued' AND not_before <= CURRENT_TIMESTAMP)
                        OR (status = 'running' AND lease_expires_at < CURRENT_TIMESTAMP)
                    )
                      AND attempt_count < max_attempts
                    ORDER BY priority ASC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE ops.source_refresh_requests AS request
                SET status = 'running',
                    claimed_by = %s,
                    claimed_at = CURRENT_TIMESTAMP,
                    lease_expires_at = CURRENT_TIMESTAMP + make_interval(secs => %s),
                    attempt_count = attempt_count + 1,
                    error_code = NULL,
                    updated_at = CURRENT_TIMESTAMP
                FROM candidate
                WHERE request.request_id = candidate.request_id
                RETURNING request.*
                """,
                (worker_id, max(60, int(lease_seconds))),
            ).fetchone()
        return dict(row) if row else None

    def finish_refresh(
        self,
        request_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "blocked"}:
            raise ValueError(f"unsupported_refresh_completion:{status}")
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE ops.source_refresh_requests
                SET status = %s,
                    completed_at = CURRENT_TIMESTAMP,
                    lease_expires_at = NULL,
                    result = %s::jsonb,
                    error_code = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = %s
                RETURNING *
                """,
                (status, canonical_json(result or {}), error_code, request_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"refresh_request_not_found:{request_id}")
        return dict(row)

    def record_refresh_failure(
        self,
        request_id: str,
        *,
        result: Mapping[str, Any],
        error_code: str,
        retry_delay_seconds: int = 60,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE ops.source_refresh_requests
                SET status = CASE WHEN attempt_count < max_attempts THEN 'queued' ELSE 'failed' END,
                    not_before = CURRENT_TIMESTAMP + make_interval(secs => %s),
                    completed_at = CASE
                        WHEN attempt_count < max_attempts THEN NULL
                        ELSE CURRENT_TIMESTAMP
                    END,
                    lease_expires_at = NULL,
                    result = %s::jsonb,
                    error_code = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = %s
                RETURNING *
                """,
                (
                    max(5, min(int(retry_delay_seconds), 3600)),
                    canonical_json(result), error_code[:2000], request_id,
                ),
            ).fetchone()
        if row is None:
            raise KeyError(f"refresh_request_not_found:{request_id}")
        return dict(row)

    def get_refresh(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ops.source_refresh_requests WHERE request_id = %s",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def schedule_pregame_refreshes(
        self,
        *,
        now: datetime | None = None,
        requested_by: str = "pregame-planner",
    ) -> list[dict[str, Any]]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        horizon = current + timedelta(minutes=65)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT game_start_time
                FROM core.external_markets
                WHERE closed = FALSE
                  AND active IS DISTINCT FROM FALSE
                  AND game_start_time BETWEEN %s AND %s
                ORDER BY game_start_time
                """,
                (current.isoformat(), horizon.isoformat()),
            ).fetchall()
        scheduled = []
        for row in rows:
            start = row["game_start_time"]
            if start is None:
                continue
            minutes = max(0, int((start - current).total_seconds() // 60))
            if minutes <= 5:
                bucket = "t05"
                priority = 10
            elif minutes <= 20:
                bucket = "t20"
                priority = 20
            else:
                bucket = "t65"
                priority = 30
            key = f"pregame:{start.astimezone(timezone.utc).isoformat()}:{bucket}"
            scheduled.append(
                self.enqueue_refresh(
                    sources=("polymarket", "kalshi_current", "sports_current"),
                    reason="pregame",
                    requested_by=requested_by,
                    scope={"game_start_time": start.astimezone(timezone.utc).isoformat(), "window": bucket},
                    idempotency_key=key,
                    priority=priority,
                )
            )
        return scheduled
