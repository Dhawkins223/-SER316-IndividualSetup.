from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Any, Protocol

from ..private_research import deterministic_hash, parse_aware_timestamp, utc_now_iso
from .firecrawl import fetch_public_page
from .http import HttpClient


RETRIEVAL_METHODS = (
    "official_api",
    "http_json",
    "http_csv",
    "rss",
    "http_html",
    "embedded_json",
    "browser_network",
    "browser_dom",
    "firecrawl",
)
RETRIEVAL_PRIORITY = {method: index for index, method in enumerate(RETRIEVAL_METHODS)}


@dataclass(frozen=True)
class SourceRequest:
    resource: str
    parser_version: str
    freshness_seconds: int
    private_resource: str | None = None
    expected_content_type: str = "application/json"
    timeout_seconds: int = 20


@dataclass
class SourceCollectionResult:
    source_name: str
    requested_resource: str
    retrieval_method: str
    source_observation_time: str | None
    received_time: str
    http_status: int | None
    content_type: str | None
    content_hash: str | None
    parser_version: str
    freshness_deadline: str | None
    freshness_state: str
    raw_evidence_reference: str | None
    validation_state: str
    normalized_record_count: int = 0
    rejection_count: int = 0
    failure_reason: str | None = None
    raw_result: Any = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.validation_state == "valid" and self.freshness_state == "fresh"

    def evidence(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "requested_resource": self.requested_resource,
            "retrieval_method": self.retrieval_method,
            "source_observation_time": self.source_observation_time,
            "received_time": self.received_time,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "content_hash": self.content_hash,
            "parser_version": self.parser_version,
            "freshness_deadline": self.freshness_deadline,
            "freshness_state": self.freshness_state,
            "raw_evidence_reference": self.raw_evidence_reference,
            "validation_state": self.validation_state,
            "normalized_record_count": self.normalized_record_count,
            "rejection_count": self.rejection_count,
            "failure_reason": self.failure_reason,
            "raw_payload": self.raw_result,
            "details": self.details,
        }


class SourceAdapter(Protocol):
    source_name: str
    retrieval_method: str

    def collect(self, request: SourceRequest) -> SourceCollectionResult:
        ...


def configured_retrieval_plan(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        requested = ["official_api", "http_json", "firecrawl"]
    elif isinstance(value, str):
        requested = [item.strip().lower() for item in value.split(",") if item.strip()]
    else:
        requested = [str(item).strip().lower() for item in value if str(item).strip()]
    unknown = sorted(set(requested) - set(RETRIEVAL_METHODS))
    if unknown:
        raise ValueError(f"unsupported_retrieval_methods:{','.join(unknown)}")
    return sorted(dict.fromkeys(requested), key=RETRIEVAL_PRIORITY.__getitem__)


def collect_from_plan(
    request: SourceRequest,
    *,
    plan: Sequence[str],
    adapters: Mapping[str, SourceAdapter],
) -> tuple[SourceCollectionResult | None, list[dict[str, Any]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for method in configured_retrieval_plan(plan):
        adapter = adapters.get(method)
        if adapter is None:
            attempts.append({"retrieval_method": method, "status": "skipped", "reason": "adapter_unavailable"})
            continue
        result = adapter.collect(request)
        evidence.append(result.evidence())
        attempts.append(
            {
                "retrieval_method": method,
                "source_name": result.source_name,
                "status": "accepted" if result.usable else "rejected",
                "reason": result.failure_reason,
                "freshness_state": result.freshness_state,
                "content_hash": result.content_hash,
            }
        )
        if result.usable:
            return result, attempts, evidence
    return None, attempts, evidence


class HttpJsonSourceAdapter:
    def __init__(
        self,
        *,
        source_name: str,
        http: HttpClient,
        retrieval_method: str = "http_json",
    ) -> None:
        if retrieval_method not in {"official_api", "http_json"}:
            raise ValueError(f"invalid_json_retrieval_method:{retrieval_method}")
        self.source_name = source_name
        self.http = http
        self.retrieval_method = retrieval_method

    def collect(self, request: SourceRequest) -> SourceCollectionResult:
        received_time = utc_now_iso()
        try:
            response = self.http.get_text(request.private_resource or request.resource, timeout=request.timeout_seconds)
        except Exception as exc:
            return _failed_result(
                source_name=self.source_name,
                request=request,
                retrieval_method=self.retrieval_method,
                received_time=received_time,
                failure_reason=_safe_failure_reason(exc),
            )
        fetched_at = str(getattr(response, "fetched_at", received_time))
        status = int(getattr(response, "status", 200))
        stale = bool(getattr(response, "stale", False))
        try:
            payload = response.json()
        except (ValueError, TypeError, json.JSONDecodeError):
            return _failed_result(
                source_name=self.source_name,
                request=request,
                retrieval_method=self.retrieval_method,
                received_time=fetched_at,
                failure_reason="parse_failed",
                http_status=status,
                freshness_state="stale" if stale else "fresh",
                raw_result=str(getattr(response, "text", "")),
            )
        digest = str(getattr(response, "content_hash", "") or deterministic_hash(payload))
        if stale:
            return _failed_result(
                source_name=self.source_name,
                request=request,
                retrieval_method=self.retrieval_method,
                received_time=fetched_at,
                failure_reason="stale_source_response",
                http_status=status,
                freshness_state="stale",
                raw_result=payload,
                content_hash=digest,
            )
        return SourceCollectionResult(
            source_name=self.source_name,
            requested_resource=request.resource,
            retrieval_method=self.retrieval_method,
            source_observation_time=fetched_at,
            received_time=fetched_at,
            http_status=status,
            content_type=request.expected_content_type,
            content_hash=digest,
            parser_version=request.parser_version,
            freshness_deadline=_freshness_deadline(fetched_at, request.freshness_seconds),
            freshness_state="fresh",
            raw_evidence_reference=digest,
            validation_state="valid",
            raw_result=payload,
        )


class FirecrawlJsonSourceAdapter:
    source_name = "firecrawl"
    retrieval_method = "firecrawl"

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        self.env = env

    def collect(self, request: SourceRequest) -> SourceCollectionResult:
        snapshot = fetch_public_page(
            request.resource,
            env=self.env,
            timeout_seconds=request.timeout_seconds,
            cache_snapshot=False,
        )
        received_time = str(snapshot.get("api_fetched_at") or utc_now_iso())
        raw_content = snapshot.get("raw_text") or snapshot.get("raw_html") or ""
        if snapshot.get("blocked"):
            return _failed_result(
                source_name=self.source_name,
                request=request,
                retrieval_method=self.retrieval_method,
                received_time=received_time,
                failure_reason=str(snapshot.get("block_reason") or snapshot.get("error_reason") or "source_blocked"),
                http_status=snapshot.get("status_code"),
                freshness_state="blocked",
                raw_result=raw_content,
                content_hash=snapshot.get("source_snapshot_hash"),
            )
        try:
            payload = json.loads(str(raw_content))
        except (TypeError, ValueError, json.JSONDecodeError):
            return _failed_result(
                source_name=self.source_name,
                request=request,
                retrieval_method=self.retrieval_method,
                received_time=received_time,
                failure_reason="parse_failed",
                http_status=snapshot.get("status_code"),
                raw_result=raw_content,
                content_hash=snapshot.get("source_snapshot_hash"),
            )
        digest = str(snapshot.get("source_snapshot_hash") or deterministic_hash(payload))
        return SourceCollectionResult(
            source_name=self.source_name,
            requested_resource=request.resource,
            retrieval_method=self.retrieval_method,
            source_observation_time=received_time,
            received_time=received_time,
            http_status=snapshot.get("status_code"),
            content_type="application/json",
            content_hash=digest,
            parser_version=request.parser_version,
            freshness_deadline=_freshness_deadline(received_time, request.freshness_seconds),
            freshness_state="fresh",
            raw_evidence_reference=digest,
            validation_state="valid",
            raw_result=payload,
        )


def _failed_result(
    *,
    source_name: str,
    request: SourceRequest,
    retrieval_method: str,
    received_time: str,
    failure_reason: str,
    http_status: int | None = None,
    freshness_state: str = "failed",
    raw_result: Any = None,
    content_hash: str | None = None,
) -> SourceCollectionResult:
    digest = content_hash or (deterministic_hash(raw_result) if raw_result is not None and raw_result != "" else None)
    return SourceCollectionResult(
        source_name=source_name,
        requested_resource=request.resource,
        retrieval_method=retrieval_method,
        source_observation_time=None,
        received_time=received_time,
        http_status=http_status,
        content_type=request.expected_content_type,
        content_hash=digest,
        parser_version=request.parser_version,
        freshness_deadline=None,
        freshness_state=freshness_state,
        raw_evidence_reference=digest,
        validation_state="rejected",
        rejection_count=1,
        failure_reason=failure_reason,
        raw_result=raw_result,
    )


def _freshness_deadline(observed_at: str, freshness_seconds: int) -> str | None:
    parsed = parse_aware_timestamp(observed_at)
    if parsed is None:
        return None
    return (parsed + timedelta(seconds=freshness_seconds)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_failure_reason(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if code in {401, 403}:
        return "authentication_or_permission_blocked"
    if code == 429:
        return "rate_limited"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, (json.JSONDecodeError, ValueError)):
        return "parse_failed"
    return "source_blocked"
