"""Read-only Kalshi reference data used by the sports research catalog.

The exchange exposes structured targets (players, teams, and other named
participants), sports milestones, and event artwork without authentication.
This module keeps the source identifiers intact and rejects incomplete rows;
it does not guess cross-provider identities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import urlencode

from .http import HttpClient, non_live_response_reason


KALSHI_PUBLIC_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PARSER_VERSION = "kalshi_public_catalog_v1"


@dataclass(frozen=True)
class PublicCatalogResponse:
    payload: Any
    fetched_at: str
    status: int
    not_live_reason: str | None
    request_url: str


@dataclass
class CatalogNormalization:
    records: list[dict[str, Any]] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)


def _timestamp(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def fetch_kalshi_catalog(
    path: str,
    *,
    parameters: Mapping[str, Any] | None = None,
    client: HttpClient | None = None,
    base_url: str = KALSHI_PUBLIC_BASE_URL,
    timeout_seconds: int = 20,
) -> PublicCatalogResponse:
    """Fetch one public endpoint and preserve whether the body was live."""
    clean_path = "/" + str(path).lstrip("/")
    query = urlencode(
        [(str(key), value) for key, value in (parameters or {}).items() if value is not None],
        doseq=True,
    )
    request_url = f"{base_url.rstrip('/')}{clean_path}"
    if query:
        request_url = f"{request_url}?{query}"
    try:
        response = (client or HttpClient()).get_text(request_url, timeout=timeout_seconds)
    except HTTPError as exc:
        # A missing metadata document is an ordinary source-level rejection for
        # a milestone whose event was retired. Return the status so the caller
        # can record that evidence instead of crashing the entire catalog cycle.
        status = int(exc.code)
        exc.close()
        return PublicCatalogResponse(
            payload=None,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            status=status,
            not_live_reason=None,
            request_url=request_url,
        )
    return PublicCatalogResponse(
        payload=response.json() if int(getattr(response, "status", 200)) == 200 else None,
        fetched_at=str(getattr(response, "fetched_at", "")),
        status=int(getattr(response, "status", 200)),
        not_live_reason=non_live_response_reason(response),
        request_url=request_url,
    )


def normalize_structured_targets(payload: Any) -> CatalogNormalization:
    result = CatalogNormalization()
    rows = payload.get("structured_targets") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        result.rejections.append({"source_entity_id": None, "reason": "missing_structured_targets"})
        return result
    for row in rows:
        if not isinstance(row, Mapping):
            result.rejections.append({"source_entity_id": None, "reason": "target_not_an_object"})
            continue
        source_entity_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        entity_type = str(row.get("type") or "").strip()
        if not source_entity_id or not name or not entity_type:
            result.rejections.append(
                {"source_entity_id": source_entity_id or None, "reason": "missing_target_identity"}
            )
            continue
        details = _mapping(row.get("details"))
        result.records.append(
            {
                "source": "kalshi",
                "source_entity_id": source_entity_id,
                "entity_type": entity_type,
                "display_name": name,
                "competition": str(details.get("league") or details.get("competition") or "").strip() or None,
                "source_id": str(row.get("source_id") or "").strip() or None,
                "source_ids": _mapping(row.get("source_ids")),
                # Kalshi currently includes player_stats and series_stats for
                # some player types. Retaining the complete object preserves
                # those categories without pretending every sport shares a
                # fixed stat schema.
                "details": details,
                "source_updated_at": _timestamp(row.get("last_updated_ts")),
            }
        )
        for key in ("image_url", "photo_url", "headshot_url", "avatar_url"):
            url = str(details.get(key) or "").strip()
            if url.startswith(("https://", "http://")):
                result.assets.append(
                    {
                        "source": "kalshi",
                        "owner_type": "entity",
                        "owner_source_id": source_entity_id,
                        "asset_kind": key.removesuffix("_url"),
                        "asset_url": url,
                    }
                )
    return result


def normalize_milestones(payload: Any) -> CatalogNormalization:
    result = CatalogNormalization()
    rows = payload.get("milestones") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        result.rejections.append({"source_milestone_id": None, "reason": "missing_milestones"})
        return result
    for row in rows:
        if not isinstance(row, Mapping):
            result.rejections.append({"source_milestone_id": None, "reason": "milestone_not_an_object"})
            continue
        milestone_id = str(row.get("id") or "").strip()
        category = str(row.get("category") or "").strip()
        milestone_type = str(row.get("type") or "").strip()
        title = str(row.get("title") or "").strip()
        if not milestone_id or not category or not milestone_type or not title:
            result.rejections.append(
                {"source_milestone_id": milestone_id or None, "reason": "missing_milestone_identity"}
            )
            continue
        details = _mapping(row.get("details"))
        result.records.append(
            {
                "source": "kalshi",
                "source_milestone_id": milestone_id,
                "category": category,
                "milestone_type": milestone_type,
                "title": title,
                "notification_message": str(row.get("notification_message") or "").strip() or None,
                "competition": str(details.get("league") or details.get("competition") or "").strip() or None,
                "start_time": _timestamp(row.get("start_date")),
                "end_time": _timestamp(row.get("end_date")),
                "primary_event_tickers": _string_list(row.get("primary_event_tickers")),
                "related_event_tickers": _string_list(row.get("related_event_tickers")),
                "details": details,
                "source_id": str(row.get("source_id") or "").strip() or None,
                "source_ids": _mapping(row.get("source_ids")),
                "source_updated_at": _timestamp(row.get("last_updated_ts")),
            }
        )
    return result


def normalize_event_metadata(event_ticker: str, payload: Any) -> CatalogNormalization:
    """Extract only source-declared event and market artwork URLs."""
    result = CatalogNormalization()
    if not isinstance(payload, Mapping):
        result.rejections.append({"event_ticker": event_ticker, "reason": "metadata_not_an_object"})
        return result
    for key in ("image_url", "featured_image_url"):
        url = str(payload.get(key) or "").strip()
        if url.startswith(("https://", "http://")):
            result.assets.append(
                {
                    "source": "kalshi",
                    "owner_type": "event",
                    "owner_source_id": event_ticker,
                    "asset_kind": key.removesuffix("_url"),
                    "asset_url": url,
                }
            )
    market_details = payload.get("market_details")
    if isinstance(market_details, list):
        for row in market_details:
            if not isinstance(row, Mapping):
                continue
            market_ticker = str(row.get("market_ticker") or "").strip()
            url = str(row.get("image_url") or "").strip()
            if market_ticker and url.startswith(("https://", "http://")):
                result.assets.append(
                    {
                        "source": "kalshi",
                        "owner_type": "market",
                        "owner_source_id": market_ticker,
                        "asset_kind": "image",
                        "asset_url": url,
                    }
                )
    if not result.assets:
        result.rejections.append({"event_ticker": event_ticker, "reason": "metadata_has_no_assets"})
    return result


def normalize_live_data(milestone_id: str, payload: Any) -> CatalogNormalization:
    """Preserve Kalshi's current milestone data, including declared player stats."""
    result = CatalogNormalization()
    live_data = payload.get("live_data") if isinstance(payload, Mapping) else None
    if not isinstance(live_data, Mapping):
        result.rejections.append(
            {"milestone_id": milestone_id, "reason": "missing_live_data"}
        )
        return result
    details = _mapping(live_data.get("details"))
    declared_milestone = str(live_data.get("milestone_id") or milestone_id).strip()
    if not declared_milestone:
        result.rejections.append(
            {"milestone_id": milestone_id or None, "reason": "missing_live_milestone_identity"}
        )
        return result
    player_stats = details.get("player_stats")
    if not isinstance(player_stats, (Mapping, list)):
        player_stats = {}
    result.records.append(
        {
            "source": "kalshi",
            "source_milestone_id": declared_milestone,
            "live_data_type": str(live_data.get("type") or "").strip() or None,
            "details": details,
            "player_stats": player_stats,
        }
    )
    return result
