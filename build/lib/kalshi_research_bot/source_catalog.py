"""Cloud-ready collection of Polymarket sports and Kalshi reference data.

Every request is retained in the raw ledger before normalized records are
written. Cached, stale, malformed, and non-200 responses are blocked rather
than relabeled as current data.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .collection_ledger import CollectionLedger, content_hash
from .connectors.http import HttpClient, non_live_response_reason
from .connectors.kalshi_catalog import (
    PARSER_VERSION as KALSHI_PARSER_VERSION,
    PublicCatalogResponse,
    fetch_kalshi_catalog,
    normalize_event_metadata,
    normalize_live_data,
    normalize_milestones,
    normalize_structured_targets,
)
from .connectors.polymarket import (
    MARKETS_ENDPOINT,
    PARSER_VERSION as POLYMARKET_PARSER_VERSION,
    SPORTS_ENDPOINT,
    TEAMS_ENDPOINT,
    fetch_polymarket_markets,
    normalize_polymarket_markets,
    normalize_polymarket_sports,
    normalize_polymarket_teams,
)
from .source_catalog_store import SourceCatalogStore
from .worker_runtime import current_worker_idempotency_key


KALSHI_PLAYER_TYPES = (
    "basketball_player",
    "football_player",
    "baseball_player",
    "hockey_player",
    "soccer_player",
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(1, min(value, maximum))


def _configured_player_types() -> tuple[str, ...]:
    configured = str(os.environ.get("KALSHI_PLAYER_TARGET_TYPES") or "").strip()
    if not configured:
        return KALSHI_PLAYER_TYPES
    return tuple(dict.fromkeys(item.strip() for item in configured.split(",") if item.strip()))


def _cycle_key() -> str:
    return current_worker_idempotency_key() or utc_iso()


def _start_batch(
    ledger: CollectionLedger,
    *,
    source: str,
    endpoint: str,
    partition: str,
    parser_version: str,
    request_parameters: Mapping[str, Any],
    cursor_start: str | None = None,
) -> tuple[str, bool]:
    identity = content_hash(
        {
            "worker": "source-catalog",
            "cycle": _cycle_key(),
            "source": source,
            "endpoint": endpoint,
            "partition": partition,
            "parameters": dict(request_parameters),
            "cursor": cursor_start,
        }
    )
    batch = ledger.start_batch(
        idempotency_key=identity,
        source=source,
        endpoint=endpoint,
        worker_name="source-catalog",
        worker_version="source_catalog_v1",
        collector_version=parser_version,
        request_parameters={"partition": partition, **dict(request_parameters)},
        cursor_start=cursor_start,
    )
    return batch.batch_id, batch.created


def _record_rejections(
    ledger: CollectionLedger,
    *,
    batch_id: str,
    payload_id: str | None,
    entity_type: str,
    parser_version: str,
    rejections: list[Mapping[str, Any]],
) -> None:
    for rejection in rejections:
        ledger.reject(
            batch_id=batch_id,
            payload_id=payload_id,
            entity_type=entity_type,
            rejection_code=str(rejection.get("reason") or "normalization_rejected"),
            rejection_detail=str(dict(rejection))[:2000],
            parser_version=parser_version,
        )


def _complete(
    ledger: CollectionLedger,
    *,
    batch_id: str,
    source: str,
    endpoint: str,
    partition: str,
    observed_at: str,
    received: int,
    accepted: int,
    rejected: int,
    duplicated: int,
    payload: Any,
    status: int,
    cursor_end: str | None = None,
) -> None:
    ledger.complete_batch(
        batch_id=batch_id,
        records_received=received,
        records_accepted=accepted,
        records_rejected=rejected,
        records_duplicated=duplicated,
        payload_hash_value=content_hash(payload),
        http_status=status,
        cursor_end=cursor_end,
        checkpoint={
            "source": source,
            "endpoint": endpoint,
            "partition_scope": partition,
            "cursor": cursor_end,
            "last_successful_item_time": observed_at,
        },
    )


def _fetch_polymarket_resource(url: str, *, client: HttpClient | None = None) -> PublicCatalogResponse:
    response = (client or HttpClient()).get_text(url, timeout=20)
    status = int(getattr(response, "status", 200))
    return PublicCatalogResponse(
        payload=response.json() if status == 200 else None,
        fetched_at=str(getattr(response, "fetched_at", "")),
        status=status,
        not_live_reason=non_live_response_reason(response),
        request_url=url,
    )


def _require_live(response: PublicCatalogResponse, source: str) -> None:
    if response.status != 200:
        raise RuntimeError(f"{source}_http_{response.status}")
    if response.not_live_reason:
        raise RuntimeError(f"{source}_response_not_live:{response.not_live_reason}")
    if not response.fetched_at:
        raise RuntimeError(f"{source}_missing_fetch_timestamp")


def collect_polymarket_catalog(
    *,
    ledger: CollectionLedger | None = None,
    store: SourceCatalogStore | None = None,
    client: HttpClient | None = None,
    pages: int | None = None,
    page_size: int | None = None,
    sports_tag_id: str = "1",
) -> dict[str, Any]:
    ledger = ledger or CollectionLedger()
    store = store or SourceCatalogStore()
    pages = pages or _positive_int("POLYMARKET_SPORTS_PAGES", 2, 20)
    page_size = page_size or _positive_int("POLYMARKET_SPORTS_PAGE_SIZE", 250, 500)
    totals = {
        "sports": 0,
        "teams": 0,
        "markets": 0,
        "observations": 0,
        "outcomes": 0,
        "assets": 0,
        "rejected": 0,
    }
    newest = ""

    sports_response = _fetch_polymarket_resource(SPORTS_ENDPOINT, client=client)
    batch_id, _ = _start_batch(
        ledger,
        source="polymarket",
        endpoint="sports",
        partition="directory",
        parser_version=POLYMARKET_PARSER_VERSION,
        request_parameters={},
    )
    try:
        _require_live(sports_response, "polymarket")
    except Exception as exc:
        ledger.fail_batch(batch_id=batch_id, error_code=str(exc), error_message=str(exc), blocked=True)
        raise
    raw = ledger.store_payload(
        batch_id=batch_id,
        source="polymarket",
        entity_type="sports_directory",
        source_identifier=SPORTS_ENDPOINT,
        observed_at=sports_response.fetched_at,
        received_at=sports_response.fetched_at,
        payload=sports_response.payload,
        parser_version=POLYMARKET_PARSER_VERSION,
    )
    normalized_sports = normalize_polymarket_sports(
        sports_response.payload,
        api_fetched_at=sports_response.fetched_at,
    )
    totals["sports"] += store.upsert_sports(
        normalized_sports.markets,
        raw_payload_id=raw["payload_id"],
        observed_at=sports_response.fetched_at,
    )
    sport_assets = [
        {
            "source": "polymarket",
            "owner_type": "sport",
            "owner_source_id": row["source_sport_id"],
            "asset_kind": "image",
            "asset_url": row["image_url"],
        }
        for row in normalized_sports.markets
        if row.get("image_url")
    ]
    totals["assets"] += store.upsert_assets(
        sport_assets,
        raw_payload_id=raw["payload_id"],
        observed_at=sports_response.fetched_at,
        source_page_url=SPORTS_ENDPOINT,
    )
    _record_rejections(
        ledger,
        batch_id=batch_id,
        payload_id=raw["payload_id"],
        entity_type="polymarket_sport",
        parser_version=POLYMARKET_PARSER_VERSION,
        rejections=normalized_sports.rejections,
    )
    totals["rejected"] += len(normalized_sports.rejections)
    _complete(
        ledger,
        batch_id=batch_id,
        source="polymarket",
        endpoint="sports",
        partition="directory",
        observed_at=sports_response.fetched_at,
        received=len(sports_response.payload or []),
        accepted=len(normalized_sports.markets),
        rejected=len(normalized_sports.rejections),
        duplicated=int(raw["duplicate"]),
        payload=sports_response.payload,
        status=sports_response.status,
    )
    newest = sports_response.fetched_at

    team_page_size = _positive_int("POLYMARKET_TEAM_PAGE_SIZE", 500, 1000)
    team_pages = _positive_int("POLYMARKET_TEAM_PAGES", 10, 20)
    for page in range(team_pages):
        offset = page * team_page_size
        team_url = f"{TEAMS_ENDPOINT}?limit={team_page_size}&offset={offset}&order=id&ascending=true"
        team_response = _fetch_polymarket_resource(team_url, client=client)
        partition = f"offset:{offset}"
        team_batch, _ = _start_batch(
            ledger,
            source="polymarket",
            endpoint="teams",
            partition=partition,
            parser_version=POLYMARKET_PARSER_VERSION,
            request_parameters={"limit": team_page_size, "offset": offset},
        )
        try:
            _require_live(team_response, "polymarket")
        except Exception as exc:
            ledger.fail_batch(
                batch_id=team_batch,
                error_code=str(exc),
                error_message=str(exc),
                blocked=True,
            )
            raise
        team_raw = ledger.store_payload(
            batch_id=team_batch,
            source="polymarket",
            entity_type="sports_team_page",
            source_identifier=partition,
            observed_at=team_response.fetched_at,
            received_at=team_response.fetched_at,
            payload=team_response.payload,
            parser_version=POLYMARKET_PARSER_VERSION,
        )
        normalized_teams = normalize_polymarket_teams(
            team_response.payload,
            api_fetched_at=team_response.fetched_at,
        )
        persisted_teams = store.upsert_entities(
            normalized_teams.markets,
            raw_payload_id=team_raw["payload_id"],
            ingestion_batch_id=team_batch,
            observed_at=team_response.fetched_at,
        )
        totals["teams"] += persisted_teams["accepted"]
        team_assets = [
            {
                "source": "polymarket",
                "owner_type": "entity",
                "owner_source_id": row["source_entity_id"],
                "asset_kind": "logo",
                "asset_url": row["details"]["logo_url"],
            }
            for row in normalized_teams.markets
            if row.get("details", {}).get("logo_url")
        ]
        totals["assets"] += store.upsert_assets(
            team_assets,
            raw_payload_id=team_raw["payload_id"],
            observed_at=team_response.fetched_at,
            source_page_url=team_url,
        )
        _record_rejections(
            ledger,
            batch_id=team_batch,
            payload_id=team_raw["payload_id"],
            entity_type="polymarket_team",
            parser_version=POLYMARKET_PARSER_VERSION,
            rejections=normalized_teams.rejections,
        )
        totals["rejected"] += len(normalized_teams.rejections)
        team_rows = team_response.payload if isinstance(team_response.payload, list) else []
        _complete(
            ledger,
            batch_id=team_batch,
            source="polymarket",
            endpoint="teams",
            partition=partition,
            observed_at=team_response.fetched_at,
            received=len(team_rows),
            accepted=persisted_teams["accepted"],
            rejected=len(normalized_teams.rejections),
            duplicated=int(team_raw["duplicate"]),
            payload=team_response.payload,
            status=team_response.status,
        )
        newest = max(newest, team_response.fetched_at)
        if len(team_rows) < team_page_size:
            break

    for page in range(pages):
        offset = page * page_size
        payload, fetched_at, status, not_live = fetch_polymarket_markets(
            client=client,
            limit=page_size,
            offset=offset,
            closed=False,
            tag_id=sports_tag_id,
            order_by="updatedAt",
            url=MARKETS_ENDPOINT,
        )
        response = PublicCatalogResponse(
            payload=payload,
            fetched_at=fetched_at,
            status=status,
            not_live_reason=not_live,
            request_url=(
                f"{MARKETS_ENDPOINT}?limit={page_size}&offset={offset}&closed=false"
                f"&order=updatedAt&ascending=false&tag_id={sports_tag_id}"
            ),
        )
        partition = f"tag:{sports_tag_id}:offset:{offset}"
        page_batch_id, _ = _start_batch(
            ledger,
            source="polymarket",
            endpoint="markets",
            partition=partition,
            parser_version=POLYMARKET_PARSER_VERSION,
            request_parameters={
                "limit": page_size,
                "offset": offset,
                "closed": False,
                "order": "updatedAt",
                "ascending": False,
                "tag_id": sports_tag_id,
            },
        )
        try:
            _require_live(response, "polymarket")
        except Exception as exc:
            ledger.fail_batch(batch_id=page_batch_id, error_code=str(exc), error_message=str(exc), blocked=True)
            raise
        page_raw = ledger.store_payload(
            batch_id=page_batch_id,
            source="polymarket",
            entity_type="sports_market_page",
            source_identifier=partition,
            observed_at=fetched_at,
            received_at=fetched_at,
            payload=payload,
            parser_version=POLYMARKET_PARSER_VERSION,
        )
        normalized = normalize_polymarket_markets(payload, api_fetched_at=fetched_at)
        persisted = store.upsert_external_markets(
            normalized.markets,
            raw_payload_id=page_raw["payload_id"],
            ingestion_batch_id=page_batch_id,
            observed_at=fetched_at,
        )
        totals["markets"] += persisted["accepted"]
        totals["observations"] += persisted["observations_inserted"]
        totals["outcomes"] += persisted["outcomes_inserted"]
        market_assets: list[dict[str, Any]] = []
        for market in normalized.markets:
            owner = str(market.get("market_id") or market.get("condition_id") or "")
            for kind, key in (("image", "image_url"), ("icon", "icon_url")):
                if owner and market.get(key):
                    market_assets.append(
                        {
                            "source": "polymarket",
                            "owner_type": "market",
                            "owner_source_id": owner,
                            "asset_kind": kind,
                            "asset_url": market[key],
                        }
                    )
        totals["assets"] += store.upsert_assets(
            market_assets,
            raw_payload_id=page_raw["payload_id"],
            observed_at=fetched_at,
            source_page_url=response.request_url,
        )
        _record_rejections(
            ledger,
            batch_id=page_batch_id,
            payload_id=page_raw["payload_id"],
            entity_type="polymarket_market",
            parser_version=POLYMARKET_PARSER_VERSION,
            rejections=normalized.rejections,
        )
        totals["rejected"] += len(normalized.rejections)
        _complete(
            ledger,
            batch_id=page_batch_id,
            source="polymarket",
            endpoint="markets",
            partition=partition,
            observed_at=fetched_at,
            received=len(payload or []),
            accepted=persisted["accepted"],
            rejected=len(normalized.rejections),
            duplicated=int(page_raw["duplicate"]),
            payload=payload,
            status=status,
        )
        newest = max(newest, fetched_at)
        if len(payload or []) < page_size:
            break

    ledger.update_source_health(
        source="polymarket",
        last_attempted_at=newest,
        last_successful_at=newest,
        freshness_deadline=(datetime.fromisoformat(newest.replace("Z", "+00:00")) + timedelta(hours=2)).isoformat(),
        freshness_state="fresh",
    )
    return {
        "records_processed": totals["sports"] + totals["teams"] + totals["markets"],
        "source_fresh_at": newest,
        "data_fresh_at": newest,
        **totals,
    }


def collect_kalshi_catalog(
    *,
    ledger: CollectionLedger | None = None,
    store: SourceCatalogStore | None = None,
    client: HttpClient | None = None,
    fetcher: Callable[..., PublicCatalogResponse] = fetch_kalshi_catalog,
    player_types: tuple[str, ...] | None = None,
    player_page_size: int | None = None,
    milestone_page_size: int | None = None,
    event_metadata_limit: int | None = None,
) -> dict[str, Any]:
    ledger = ledger or CollectionLedger()
    store = store or SourceCatalogStore()
    player_types = player_types or _configured_player_types()
    player_page_size = player_page_size or _positive_int("KALSHI_PLAYER_PAGE_SIZE", 1000, 2000)
    milestone_page_size = milestone_page_size or _positive_int("KALSHI_MILESTONE_PAGE_SIZE", 200, 1000)
    event_metadata_limit = event_metadata_limit or _positive_int("KALSHI_EVENT_METADATA_LIMIT", 25, 200)
    totals = {
        "entities": 0,
        "entity_snapshots": 0,
        "milestones": 0,
        "live_snapshots": 0,
        "assets": 0,
        "rejected": 0,
    }
    newest = ""
    event_tickers: list[str] = []

    for target_type in player_types:
        checkpoint = ledger.checkpoint(source="kalshi", endpoint="structured_targets", partition_scope=target_type)
        cursor = str((checkpoint or {}).get("cursor") or "") or None
        parameters = {"type": target_type, "page_size": player_page_size, "cursor": cursor}
        response = fetcher("structured_targets", parameters=parameters, client=client)
        batch_id, _ = _start_batch(
            ledger,
            source="kalshi",
            endpoint="structured_targets",
            partition=target_type,
            parser_version=KALSHI_PARSER_VERSION,
            request_parameters={"type": target_type, "page_size": player_page_size},
            cursor_start=cursor,
        )
        try:
            _require_live(response, "kalshi")
        except Exception as exc:
            ledger.fail_batch(batch_id=batch_id, error_code=str(exc), error_message=str(exc), blocked=True)
            raise
        raw = ledger.store_payload(
            batch_id=batch_id,
            source="kalshi",
            entity_type="structured_target_page",
            source_identifier=target_type,
            observed_at=response.fetched_at,
            received_at=response.fetched_at,
            payload=response.payload,
            parser_version=KALSHI_PARSER_VERSION,
        )
        normalized = normalize_structured_targets(response.payload)
        persisted = store.upsert_entities(
            normalized.records,
            raw_payload_id=raw["payload_id"],
            ingestion_batch_id=batch_id,
            observed_at=response.fetched_at,
        )
        totals["entities"] += persisted["accepted"]
        totals["entity_snapshots"] += persisted["snapshots_inserted"]
        totals["assets"] += store.upsert_assets(
            normalized.assets,
            raw_payload_id=raw["payload_id"],
            observed_at=response.fetched_at,
            source_page_url=response.request_url,
        )
        _record_rejections(
            ledger,
            batch_id=batch_id,
            payload_id=raw["payload_id"],
            entity_type="kalshi_structured_target",
            parser_version=KALSHI_PARSER_VERSION,
            rejections=normalized.rejections,
        )
        totals["rejected"] += len(normalized.rejections)
        rows = response.payload.get("structured_targets", []) if isinstance(response.payload, Mapping) else []
        next_cursor = str(response.payload.get("cursor") or "") if isinstance(response.payload, Mapping) else ""
        _complete(
            ledger,
            batch_id=batch_id,
            source="kalshi",
            endpoint="structured_targets",
            partition=target_type,
            observed_at=response.fetched_at,
            received=len(rows),
            accepted=persisted["accepted"],
            rejected=len(normalized.rejections),
            duplicated=int(raw["duplicate"]),
            payload=response.payload,
            status=response.status,
            cursor_end=next_cursor or None,
        )
        newest = max(newest, response.fetched_at)

    milestone_checkpoint = ledger.checkpoint(source="kalshi", endpoint="milestones", partition_scope="Sports")
    milestone_cursor = str((milestone_checkpoint or {}).get("cursor") or "") or None
    milestone_response = fetcher(
        "milestones",
        parameters={"category": "Sports", "limit": milestone_page_size, "cursor": milestone_cursor},
        client=client,
    )
    milestone_batch, _ = _start_batch(
        ledger,
        source="kalshi",
        endpoint="milestones",
        partition="Sports",
        parser_version=KALSHI_PARSER_VERSION,
        request_parameters={"category": "Sports", "limit": milestone_page_size},
        cursor_start=milestone_cursor,
    )
    try:
        _require_live(milestone_response, "kalshi")
    except Exception as exc:
        ledger.fail_batch(batch_id=milestone_batch, error_code=str(exc), error_message=str(exc), blocked=True)
        raise
    milestone_raw = ledger.store_payload(
        batch_id=milestone_batch,
        source="kalshi",
        entity_type="sports_milestone_page",
        source_identifier="Sports",
        observed_at=milestone_response.fetched_at,
        received_at=milestone_response.fetched_at,
        payload=milestone_response.payload,
        parser_version=KALSHI_PARSER_VERSION,
    )
    normalized_milestones = normalize_milestones(milestone_response.payload)
    totals["milestones"] += store.upsert_milestones(
        normalized_milestones.records,
        raw_payload_id=milestone_raw["payload_id"],
        observed_at=milestone_response.fetched_at,
    )
    for milestone in normalized_milestones.records:
        event_tickers.extend(milestone.get("primary_event_tickers") or [])
    _record_rejections(
        ledger,
        batch_id=milestone_batch,
        payload_id=milestone_raw["payload_id"],
        entity_type="kalshi_milestone",
        parser_version=KALSHI_PARSER_VERSION,
        rejections=normalized_milestones.rejections,
    )
    totals["rejected"] += len(normalized_milestones.rejections)
    milestone_rows = milestone_response.payload.get("milestones", []) if isinstance(milestone_response.payload, Mapping) else []
    milestone_next = str(milestone_response.payload.get("cursor") or "") if isinstance(milestone_response.payload, Mapping) else ""
    _complete(
        ledger,
        batch_id=milestone_batch,
        source="kalshi",
        endpoint="milestones",
        partition="Sports",
        observed_at=milestone_response.fetched_at,
        received=len(milestone_rows),
        accepted=len(normalized_milestones.records),
        rejected=len(normalized_milestones.rejections),
        duplicated=int(milestone_raw["duplicate"]),
        payload=milestone_response.payload,
        status=milestone_response.status,
        cursor_end=milestone_next or None,
    )
    newest = max(newest, milestone_response.fetched_at)

    live_limit = _positive_int("KALSHI_LIVE_MILESTONE_LIMIT", 25, 100)
    fetched_time = datetime.fromisoformat(milestone_response.fetched_at.replace("Z", "+00:00"))
    live_candidates: list[dict[str, Any]] = []
    for milestone in normalized_milestones.records:
        start_text = milestone.get("start_time")
        end_text = milestone.get("end_time")
        start = datetime.fromisoformat(str(start_text).replace("Z", "+00:00")) if start_text else None
        end = datetime.fromisoformat(str(end_text).replace("Z", "+00:00")) if end_text else None
        if start and start > fetched_time + timedelta(hours=6):
            continue
        if end and end < fetched_time - timedelta(hours=2):
            continue
        live_candidates.append(milestone)

    for milestone in live_candidates[:live_limit]:
        milestone_id = str(milestone["source_milestone_id"])
        response = fetcher(
            f"live_data/milestone/{milestone_id}",
            parameters={"include_player_stats": True},
            client=client,
        )
        live_batch, _ = _start_batch(
            ledger,
            source="kalshi",
            endpoint="live_data",
            partition=milestone_id,
            parser_version=KALSHI_PARSER_VERSION,
            request_parameters={"milestone_id": milestone_id, "include_player_stats": True},
        )
        if response.status == 404:
            ledger.fail_batch(
                batch_id=live_batch,
                error_code="live_data_not_found",
                error_message=milestone_id,
                blocked=True,
            )
            continue
        try:
            _require_live(response, "kalshi")
        except Exception as exc:
            ledger.fail_batch(
                batch_id=live_batch,
                error_code=str(exc),
                error_message=str(exc),
                blocked=True,
            )
            totals["rejected"] += 1
            continue
        live_raw = ledger.store_payload(
            batch_id=live_batch,
            source="kalshi",
            entity_type="milestone_live_data",
            source_identifier=milestone_id,
            observed_at=response.fetched_at,
            received_at=response.fetched_at,
            payload=response.payload,
            parser_version=KALSHI_PARSER_VERSION,
        )
        normalized_live = normalize_live_data(milestone_id, response.payload)
        totals["live_snapshots"] += store.upsert_live_snapshots(
            normalized_live.records,
            raw_payload_id=live_raw["payload_id"],
            ingestion_batch_id=live_batch,
            observed_at=response.fetched_at,
            competition=milestone.get("competition"),
        )
        _record_rejections(
            ledger,
            batch_id=live_batch,
            payload_id=live_raw["payload_id"],
            entity_type="kalshi_live_data",
            parser_version=KALSHI_PARSER_VERSION,
            rejections=normalized_live.rejections,
        )
        totals["rejected"] += len(normalized_live.rejections)
        _complete(
            ledger,
            batch_id=live_batch,
            source="kalshi",
            endpoint="live_data",
            partition=milestone_id,
            observed_at=response.fetched_at,
            received=1,
            accepted=len(normalized_live.records),
            rejected=len(normalized_live.rejections),
            duplicated=int(live_raw["duplicate"]),
            payload=response.payload,
            status=response.status,
        )
        newest = max(newest, response.fetched_at)

    for ticker in tuple(dict.fromkeys(event_tickers))[:event_metadata_limit]:
        response = fetcher(f"events/{ticker}/metadata", client=client)
        batch_id, _ = _start_batch(
            ledger,
            source="kalshi",
            endpoint="event_metadata",
            partition=ticker,
            parser_version=KALSHI_PARSER_VERSION,
            request_parameters={"event_ticker": ticker},
        )
        if response.status == 404:
            ledger.fail_batch(
                batch_id=batch_id,
                error_code="event_metadata_not_found",
                error_message=ticker,
                blocked=True,
            )
            totals["rejected"] += 1
            continue
        try:
            _require_live(response, "kalshi")
        except Exception as exc:
            ledger.fail_batch(batch_id=batch_id, error_code=str(exc), error_message=str(exc), blocked=True)
            totals["rejected"] += 1
            continue
        raw = ledger.store_payload(
            batch_id=batch_id,
            source="kalshi",
            entity_type="event_metadata",
            source_identifier=ticker,
            observed_at=response.fetched_at,
            received_at=response.fetched_at,
            payload=response.payload,
            parser_version=KALSHI_PARSER_VERSION,
        )
        normalized = normalize_event_metadata(ticker, response.payload)
        totals["assets"] += store.upsert_assets(
            normalized.assets,
            raw_payload_id=raw["payload_id"],
            observed_at=response.fetched_at,
            source_page_url=response.request_url,
        )
        _record_rejections(
            ledger,
            batch_id=batch_id,
            payload_id=raw["payload_id"],
            entity_type="kalshi_event_metadata",
            parser_version=KALSHI_PARSER_VERSION,
            rejections=normalized.rejections,
        )
        totals["rejected"] += len(normalized.rejections)
        _complete(
            ledger,
            batch_id=batch_id,
            source="kalshi",
            endpoint="event_metadata",
            partition=ticker,
            observed_at=response.fetched_at,
            received=1,
            accepted=len(normalized.assets),
            rejected=len(normalized.rejections),
            duplicated=int(raw["duplicate"]),
            payload=response.payload,
            status=response.status,
        )
        newest = max(newest, response.fetched_at)

    ledger.update_source_health(
        source="kalshi_reference_data",
        last_attempted_at=newest,
        last_successful_at=newest,
        freshness_deadline=(datetime.fromisoformat(newest.replace("Z", "+00:00")) + timedelta(hours=12)).isoformat(),
        freshness_state="fresh",
    )
    return {
        "records_processed": (
            totals["entities"]
            + totals["milestones"]
            + totals["live_snapshots"]
            + totals["assets"]
        ),
        "source_fresh_at": newest,
        "data_fresh_at": newest,
        **totals,
    }
