from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..config import repo_path


DEFAULT_USER_AGENT = (
    "HawkNeticResearchBot/1.0 "
    "(+https://github.com/Dhawkins223/HawkNeticSportsTools)"
)


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    text: str
    fetched_at: str
    from_cache: bool = False
    stale: bool = False
    stale_reason: str = ""

    def json(self) -> Any:
        return json.loads(self.text, parse_float=Decimal)

    @property
    def content_hash(self) -> str:
        import hashlib

        return f"sha256:{hashlib.sha256(self.text.encode('utf-8')).hexdigest()}"

    @property
    def received_at(self) -> str:
        return self.fetched_at

    @property
    def freshness_state(self) -> str:
        if self.stale:
            return "stale"
        return "fresh"


class ResponseTooLargeError(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        cache_dir: str | Path | None = None,
        cache_ttl_seconds: int | None = None,
        max_retries: int | None = None,
        retry_backoff_seconds: float | None = None,
        min_interval_seconds: float | None = None,
        allow_stale_on_error: bool | None = None,
        max_stale_seconds: int | None = None,
        max_response_bytes: int | None = None,
        retry_jitter_seconds: float | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.cache_dir = Path(cache_dir) if cache_dir else repo_path("data", "http_cache")
        self.cache_ttl_seconds = max(0, _env_int("KALSHI_HTTP_CACHE_TTL_SECONDS", 120) if cache_ttl_seconds is None else int(cache_ttl_seconds))
        self.max_retries = max(0, _env_int("KALSHI_HTTP_MAX_RETRIES", 1) if max_retries is None else int(max_retries))
        self.retry_backoff_seconds = max(
            0.0,
            _env_float("KALSHI_HTTP_BACKOFF_SECONDS", 1.5)
            if retry_backoff_seconds is None
            else float(retry_backoff_seconds),
        )
        self.min_interval_seconds = max(
            0.0,
            _env_float("KALSHI_HTTP_MIN_INTERVAL_SECONDS", 0.0)
            if min_interval_seconds is None
            else float(min_interval_seconds),
        )
        self.allow_stale_on_error = (
            _env_bool("KALSHI_HTTP_ALLOW_STALE_ON_ERROR", False)
            if allow_stale_on_error is None
            else bool(allow_stale_on_error)
        )
        self.max_stale_seconds = max(
            0,
            _env_int("KALSHI_HTTP_MAX_STALE_SECONDS", 60 * 60)
            if max_stale_seconds is None
            else int(max_stale_seconds),
        )
        self.max_response_bytes = max(
            1024,
            _env_int("KALSHI_HTTP_MAX_RESPONSE_BYTES", 25 * 1024 * 1024)
            if max_response_bytes is None
            else int(max_response_bytes),
        )
        self.retry_jitter_seconds = max(
            0.0,
            _env_float("KALSHI_HTTP_RETRY_JITTER_SECONDS", 0.25)
            if retry_jitter_seconds is None
            else float(retry_jitter_seconds),
        )
        self._last_request_at = 0.0
        self.cache_hit_count = 0
        self.live_fetch_count = 0
        self.stale_fallback_events: list[dict[str, Any]] = []

    def get_text(self, url: str, timeout: int = 20) -> HttpResponse:
        cache_path = self._cache_path(url)
        cached = self._read_cache(cache_path)
        if cached and self.cache_ttl_seconds and time.time() - float(cached.get("saved_at", 0)) <= self.cache_ttl_seconds:
            self.cache_hit_count += 1
            return _response_from_cache(url, cached)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            self._pace_request()
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw_body = response.read()
                    if len(raw_body) > self.max_response_bytes:
                        raise ResponseTooLargeError(
                            f"response_too_large:{len(raw_body)}>{self.max_response_bytes}"
                        )
                    body = raw_body.decode("utf-8", errors="replace")
                    result = HttpResponse(url=url, status=response.status, text=body, fetched_at=_iso_from_epoch(time.time()))
                    self.live_fetch_count += 1
                    self._write_cache(cache_path, result)
                    return result
            except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
                last_error = exc
                if not _should_retry(exc) or attempt >= self.max_retries:
                    break
                time.sleep(
                    _retry_delay(exc, self.retry_backoff_seconds, attempt)
                    + random.uniform(0.0, self.retry_jitter_seconds)
                )
        stale_response = self._stale_response(url, cached, last_error)
        if stale_response is not None:
            return stale_response
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"HTTP request failed without an error: {url}")

    def cache_status(self) -> dict[str, Any]:
        return {
            "cache_hit_count": self.cache_hit_count,
            "live_fetch_count": self.live_fetch_count,
            "stale_fallback_count": len(self.stale_fallback_events),
            "stale_fallback_events": self.stale_fallback_events[-10:],
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "max_stale_seconds": self.max_stale_seconds,
            "min_interval_seconds": self.min_interval_seconds,
            "max_response_bytes": self.max_response_bytes,
            "retry_jitter_seconds": self.retry_jitter_seconds,
        }

    def _cache_path(self, url: str) -> Path:
        import hashlib

        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, cache_path: Path) -> dict[str, Any] | None:
        if not cache_path.exists():
            return None
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _write_cache(self, cache_path: Path, response: HttpResponse) -> None:
        if not self.cache_ttl_seconds:
            return
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "url": response.url,
                        "status": response.status,
                        "saved_at": time.time(),
                        "fetched_at": response.fetched_at,
                        "text": response.text,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            return

    def _pace_request(self) -> None:
        if not self.min_interval_seconds:
            return
        elapsed = time.time() - self._last_request_at
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self._last_request_at = time.time()

    def _stale_response(
        self,
        url: str,
        cached: dict[str, Any] | None,
        error: BaseException | None,
    ) -> HttpResponse | None:
        if not self.allow_stale_on_error or not cached:
            return None
        saved_at = float(cached.get("saved_at", 0))
        age_seconds = max(0, int(time.time() - saved_at))
        if age_seconds > self.max_stale_seconds:
            return None
        reason = _error_reason(error)
        self.cache_hit_count += 1
        self.stale_fallback_events.append(
            {
                "url": url,
                "reason": reason,
                "cached_age_seconds": age_seconds,
                "cached_fetched_at": str(cached.get("fetched_at") or _iso_from_epoch(saved_at)),
            }
        )
        return _response_from_cache(url, cached, stale=True, stale_reason=reason)


def prune_http_cache(
    cache_dir: str | Path | None = None,
    *,
    max_age_seconds: int | None = None,
    max_bytes: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    root = Path(cache_dir) if cache_dir else repo_path("data", "http_cache")
    if not root.exists():
        return {
            "cache_dir": str(root),
            "deleted_files": 0,
            "deleted_bytes": 0,
            "remaining_files": 0,
            "remaining_bytes": 0,
            "ok": True,
        }
    resolved_max_age_seconds = max(
        0,
        _env_int("KALSHI_HTTP_CACHE_MAX_AGE_SECONDS", 6 * 60 * 60)
        if max_age_seconds is None
        else int(max_age_seconds),
    )
    resolved_max_bytes = max(
        0,
        _env_int("KALSHI_HTTP_CACHE_MAX_BYTES", 256 * 1024 * 1024)
        if max_bytes is None
        else int(max_bytes),
    )
    checked_at = time.time() if now is None else float(now)
    files: list[dict[str, Any]] = []
    deleted_files = 0
    deleted_bytes = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        age_seconds = max(0, int(checked_at - stat.st_mtime))
        if resolved_max_age_seconds and age_seconds > resolved_max_age_seconds:
            try:
                size = stat.st_size
                path.unlink()
                deleted_files += 1
                deleted_bytes += size
            except OSError:
                pass
            continue
        files.append({"path": path, "size": stat.st_size, "mtime": stat.st_mtime})

    remaining_bytes = sum(int(item["size"]) for item in files)
    if resolved_max_bytes and remaining_bytes > resolved_max_bytes:
        target_bytes = int(resolved_max_bytes * 0.8)
        for item in sorted(files, key=lambda value: float(value["mtime"])):
            if remaining_bytes <= target_bytes:
                break
            path = item["path"]
            size = int(item["size"])
            try:
                Path(path).unlink()
            except OSError:
                continue
            deleted_files += 1
            deleted_bytes += size
            remaining_bytes -= size

    remaining_files = 0
    remaining_bytes = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        remaining_files += 1
        remaining_bytes += stat.st_size

    return {
        "cache_dir": str(root),
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
        "remaining_files": remaining_files,
        "remaining_bytes": remaining_bytes,
        "max_age_seconds": resolved_max_age_seconds,
        "max_bytes": resolved_max_bytes,
        "ok": True,
    }


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _response_from_cache(
    url: str,
    cached: dict[str, Any],
    *,
    stale: bool = False,
    stale_reason: str = "",
) -> HttpResponse:
    saved_at = float(cached.get("saved_at", 0))
    return HttpResponse(
        url=url,
        status=int(cached.get("status", 200)),
        text=str(cached.get("text", "")),
        fetched_at=str(cached.get("fetched_at") or _iso_from_epoch(saved_at)),
        from_cache=True,
        stale=stale,
        stale_reason=stale_reason,
    )


def _should_retry(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504}
    return isinstance(error, (TimeoutError, urllib.error.URLError))


def _retry_delay(error: BaseException, backoff_seconds: float, attempt: int) -> float:
    if isinstance(error, urllib.error.HTTPError):
        retry_after = error.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(30.0, float(retry_after)))
            except ValueError:
                pass
    return min(30.0, backoff_seconds * (2**attempt))


def _error_reason(error: BaseException | None) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"http_{error.code}"
    if error is None:
        return "unknown_error"
    return type(error).__name__


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def live_probe_client(**overrides: Any) -> HttpClient:
    """An `HttpClient` that has to reach the network to answer.

    A probe exists to confirm a source responds *now* and in the shape a
    normalizer expects. The ordinary client caches responses and can be
    configured to serve a stale body when a request fails, so a probe built on it
    can return a body fetched minutes ago -- or one fetched before the host went
    down -- and report success without a request leaving the machine. That turns
    a readiness check into a recording of the last time things worked.
    """
    settings: dict[str, Any] = {
        "cache_ttl_seconds": 0,
        "allow_stale_on_error": False,
        "max_stale_seconds": 0,
    }
    settings.update(overrides)
    return HttpClient(**settings)


def non_live_response_reason(response: Any) -> str | None:
    """Name why a response is not evidence the source answered, or None if it is.

    Checked in addition to using `live_probe_client`, because a caller may pass
    its own client and a probe must not accept a cached body from it either.
    """
    if bool(getattr(response, "from_cache", False)):
        return "served_from_cache"
    if bool(getattr(response, "stale", False)):
        reason = str(getattr(response, "stale_reason", "") or "unknown")
        return f"stale_response:{reason}"
    return None
