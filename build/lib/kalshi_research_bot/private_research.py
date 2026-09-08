from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


VOLATILE_SOURCE_KEYS = {
    "api_fetched_at",
    "collected_at",
    "collection_timestamp",
    "fetched_at",
    "generated_at",
    "report_generated_at",
}

MIN_SETTLED_DEDUPED_AUDIT = 100
PREFERRED_SETTLED_DEDUPED_AUDIT = 300


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_aware_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        timestamp = value
    else:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if timestamp.tzinfo is None:
        return None
    return timestamp.astimezone(timezone.utc)


def isoformat_utc(timestamp: datetime) -> str:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_horizon(value: str) -> timedelta:
    normalized = str(value).strip().lower()
    if normalized.endswith("m"):
        return timedelta(minutes=int(normalized[:-1]))
    if normalized.endswith("h"):
        return timedelta(hours=int(normalized[:-1]))
    raise ValueError(f"unsupported horizon: {value}")


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def strip_volatile_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_volatile_fields(child)
            for key, child in sorted(value.items())
            if str(key) not in VOLATILE_SOURCE_KEYS
        }
    if isinstance(value, list):
        return [strip_volatile_fields(item) for item in value]
    return value


def deterministic_hash(value: Any, *, ignore_volatile: bool = True) -> str:
    source = strip_volatile_fields(value) if ignore_volatile else value
    return hashlib.sha256(stable_json(source).encode("utf-8")).hexdigest()


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def write_text(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def write_csv(path: str | Path, rows: list[dict[str, Any]], fieldnames: Iterable[str]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in writer.fieldnames or []})


def sample_status(settled_deduped: int) -> str:
    if settled_deduped <= 0:
        return "unavailable_no_settled_deduped_exposures"
    if settled_deduped < MIN_SETTLED_DEDUPED_AUDIT:
        return f"insufficient_sample ({settled_deduped}/100)"
    if settled_deduped < PREFERRED_SETTLED_DEDUPED_AUDIT:
        return f"basic_audit_research_only ({settled_deduped}/300)"
    return "serious_audit_allowed_research_only"


def gate_result(settled_deduped: int) -> str:
    if settled_deduped < MIN_SETTLED_DEDUPED_AUDIT:
        return "blocked_sample_size"
    if settled_deduped < PREFERRED_SETTLED_DEDUPED_AUDIT:
        return "basic_audit_ready_continue_to_300"
    return "preferred_sample_ready_research_only"


def accuracy_status(settled_deduped: int) -> str:
    if settled_deduped <= 0:
        return "accuracy unavailable; no settled de-duped exposures"
    if settled_deduped < MIN_SETTLED_DEDUPED_AUDIT:
        return "metrics withheld; sample too small; research-only"
    if settled_deduped >= PREFERRED_SETTLED_DEDUPED_AUDIT:
        return "serious audit allowed; research-only"
    return "basic audit allowed; research-only"


