from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..config import repo_path
from ..private_research import deterministic_hash, utc_now_iso, write_json


CONNECTOR_NAME = "firecrawl"
DEFAULT_BASE_URL = "https://api.firecrawl.dev"
BLOCKED_STATUS_CODES = {401, 403, 429}
BLOCKED_CONTENT_MARKERS = {
    "captcha": "captcha_or_login_required",
    "cloudflare": "captcha_or_login_required",
    "sign in": "captcha_or_login_required",
    "log in": "captcha_or_login_required",
    "login required": "captcha_or_login_required",
    "paywall": "paywall_detected",
    "subscribe to continue": "paywall_detected",
    "subscription required": "paywall_detected",
}


FetchResult = Mapping[str, Any]
Fetcher = Callable[[str, int], FetchResult]


def is_firecrawl_configured(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return bool(values.get("FIRECRAWL_API_KEY"))


def detect_blocked_page(*, status_code: int | None = None, raw_text: str | None = None, raw_html: str | None = None) -> str | None:
    if status_code in BLOCKED_STATUS_CODES:
        return "source_blocked"
    content = f"{raw_text or ''}\n{raw_html or ''}".lower()
    for marker, reason in BLOCKED_CONTENT_MARKERS.items():
        if marker in content:
            return reason
    return None


def build_firecrawl_snapshot(
    *,
    source_url: str,
    api_fetched_at: str,
    raw_text: str | None = None,
    raw_html: str | None = None,
    status_code: int | None = None,
    block_reason: str | None = None,
    error_reason: str | None = None,
) -> dict[str, Any]:
    snapshot = {
        "connector_name": CONNECTOR_NAME,
        "source_url": source_url,
        "api_fetched_at": api_fetched_at,
        "raw_text": raw_text or "",
        "raw_html": raw_html or "",
        "status_code": status_code,
        "blocked": bool(block_reason or error_reason),
        "block_reason": block_reason,
        "error_reason": error_reason,
    }
    snapshot["source_snapshot_hash"] = deterministic_hash(
        {
            "connector_name": CONNECTOR_NAME,
            "source_url": source_url,
            "raw_text": snapshot["raw_text"],
            "raw_html": snapshot["raw_html"],
            "status_code": status_code,
            "block_reason": block_reason,
            "error_reason": error_reason,
        }
    )
    return snapshot


def fetch_public_page(
    url: str,
    *,
    env: Mapping[str, str] | None = None,
    fetcher: Fetcher | None = None,
    timeout_seconds: int = 20,
    backoff_seconds: float = 0.5,
    retries: int = 1,
    cache_snapshot: bool = True,
) -> dict[str, Any]:
    values = os.environ if env is None else env
    if fetcher is None and not is_firecrawl_configured(values):
        return build_firecrawl_snapshot(
            source_url=url,
            api_fetched_at=utc_now_iso(),
            block_reason="source_blocked",
            error_reason="firecrawl_unconfigured",
        )

    last_error: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        try:
            raw = dict(fetcher(url, timeout_seconds) if fetcher else _firecrawl_api_fetch(url, env=values, timeout_seconds=timeout_seconds))
            api_fetched_at = str(raw.get("api_fetched_at") or utc_now_iso())
            raw_text = raw.get("raw_text") or raw.get("markdown") or raw.get("text") or ""
            raw_html = raw.get("raw_html") or raw.get("html") or ""
            status_code = _coerce_status(raw.get("status_code"))
            block_reason = raw.get("block_reason") or detect_blocked_page(
                status_code=status_code,
                raw_text=str(raw_text or ""),
                raw_html=str(raw_html or ""),
            )
            if not block_reason and not str(raw_text or raw_html).strip():
                block_reason = "empty_content"
            snapshot = build_firecrawl_snapshot(
                source_url=url,
                api_fetched_at=api_fetched_at,
                raw_text=str(raw_text or ""),
                raw_html=str(raw_html or ""),
                status_code=status_code,
                block_reason=block_reason,
                error_reason=raw.get("error_reason"),
            )
            if cache_snapshot:
                _cache_snapshot(snapshot)
            return snapshot
        except TimeoutError:
            return build_firecrawl_snapshot(source_url=url, api_fetched_at=utc_now_iso(), block_reason="timeout", error_reason="timeout")
        except urllib.error.HTTPError as exc:
            return build_firecrawl_snapshot(
                source_url=url,
                api_fetched_at=utc_now_iso(),
                status_code=exc.code,
                block_reason="source_blocked" if exc.code in BLOCKED_STATUS_CODES else "parse_failed",
                error_reason=f"http_{exc.code}",
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff_seconds)
                continue
            reason = "timeout" if isinstance(exc, TimeoutError) else "parse_failed"
            return build_firecrawl_snapshot(source_url=url, api_fetched_at=utc_now_iso(), block_reason=reason, error_reason=str(exc))
    return build_firecrawl_snapshot(
        source_url=url,
        api_fetched_at=utc_now_iso(),
        block_reason="parse_failed",
        error_reason=str(last_error or "unknown_firecrawl_error"),
    )


def _firecrawl_api_fetch(url: str, *, env: Mapping[str, str], timeout_seconds: int) -> dict[str, Any]:
    base_url = (env.get("FIRECRAWL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    api_url = f"{base_url}/v1/scrape"
    request_payload = json.dumps({"url": url, "formats": ["markdown", "html"]}).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=request_payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {env.get('FIRECRAWL_API_KEY', '')}",
            "Content-Type": "application/json",
            "User-Agent": "kalshi-research-bot private-research/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
        data = payload.get("data") or payload
        return {
            "status_code": getattr(response, "status", None),
            "raw_text": data.get("markdown") or data.get("text") or "",
            "raw_html": data.get("html") or "",
            "api_fetched_at": utc_now_iso(),
        }


def _coerce_status(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cache_snapshot(snapshot: dict[str, Any]) -> Path | None:
    try:
        target = repo_path("data", "source_snapshots", CONNECTOR_NAME, f"{snapshot['source_snapshot_hash']}.json")
        write_json(target, snapshot)
        return target
    except OSError:
        return None
