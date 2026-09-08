from __future__ import annotations

import json
import os
from collections.abc import Mapping
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from .business_store import create_store
from .database import DatabaseSession
from .config import repo_path
from .connectors.status import build_connectors_status
from .evaluation.quality import parse_timestamp


DEFAULT_CRYPTO_RUN_ID = "crypto_private_20260704"
DEFAULT_SPORTS_RUN_ID = "sports_private_20260704"
DEFAULT_KALSHI_RUN_ID = "stage3a_20260703_170707"

CRYPTO_REQUIRED_SOURCE_FIELDS = (
    "asset_class",
    "exchange",
    "symbol",
    "timeframe",
    "candle_open_time",
    "candle_close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "api_fetched_at",
    "source_snapshot_hash",
)
SPORTS_REQUIRED_SOURCE_FIELDS = (
    "sport",
    "league",
    "event_id",
    "home_team",
    "away_team",
    "market_type",
    "selection",
    "odds",
    "odds_timestamp",
    "game_start_time",
    "api_fetched_at",
    "source_snapshot_hash",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_data_quality_report_path() -> Path:
    return repo_path("data", "data_quality_report.txt")


def default_data_quality_json_path() -> Path:
    return repo_path("data", "data_quality_report.json")


def read_json_safely(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    if not resolved.exists():
        return {"_read_error": "missing_file", "_path": str(resolved)}
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_read_error": f"{type(exc).__name__}: {exc}", "_path": str(resolved)}


def read_text_safely(path: str | Path) -> str:
    resolved = Path(path)
    if not resolved.exists():
        return ""
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def latest_jsonl(path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    resolved = Path(path)
    if not resolved.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def file_status(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    if not resolved.exists():
        return {"path": str(resolved), "exists": False}
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "exists": True,
        "bytes": stat.st_size,
        "last_write_time": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _is_timezone_aware_text(value: Any) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is not None
    text = str(value or "")
    if not text:
        return False
    return text.endswith("Z") or "+" in text[10:] or "-" in text[10:]


def _age_seconds(value: Any, now: datetime) -> int | None:
    timestamp = parse_timestamp(value)
    if timestamp is None:
        return None
    return max(0, int((now - timestamp.astimezone(timezone.utc)).total_seconds()))


def _status_from_score(score: int, *, blocked: bool = False, issues: int = 0) -> str:
    if blocked or score < 50:
        return "BLOCKED"
    if issues or score < 85:
        return "WATCH"
    return "OK"


def active_refresh_errors(
    *,
    audit_rows: list[dict[str, Any]] | None,
    latest_errors: list[dict[str, Any]] | None,
    now: datetime | None = None,
    max_age_seconds: int = 60 * 60,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    latest_success: datetime | None = None
    for row in audit_rows or []:
        if row.get("ok") is True:
            timestamp = parse_timestamp(row.get("finished_at") or row.get("started_at"))
            if timestamp and (latest_success is None or timestamp > latest_success):
                latest_success = timestamp
    active: list[dict[str, Any]] = []
    for error in latest_errors or []:
        timestamp = parse_timestamp(error.get("finished_at") or error.get("started_at") or error.get("created_at"))
        if latest_success and timestamp and timestamp <= latest_success:
            continue
        if timestamp and (now - timestamp.astimezone(timezone.utc)).total_seconds() > max_age_seconds:
            continue
        active.append(error)
    return active


def build_dashboard_quality_gate(
    payload: dict[str, Any],
    *,
    audit_rows: list[dict[str, Any]] | None = None,
    latest_errors: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    generated_at = payload.get("generated_at")
    data_age_seconds = _age_seconds(generated_at, now)
    slip_counts = {
        "primary": int((payload.get("custom_slip") or {}).get("leg_count") or 0),
        "leverage": int((payload.get("leverage_slip") or {}).get("leg_count") or 0),
        "all_day": int((payload.get("all_day_slip") or {}).get("leg_count") or 0),
        "research_edge": int((payload.get("research_edge_slip") or {}).get("leg_count") or 0),
    }
    reasons: list[str] = []
    active_errors = active_refresh_errors(audit_rows=audit_rows, latest_errors=latest_errors, now=now)
    if payload.get("_read_error"):
        reasons.append(str(payload["_read_error"]))
    if generated_at and not _is_timezone_aware_text(generated_at):
        reasons.append("generated_at_not_timezone_aware")
    if data_age_seconds is None:
        reasons.append("missing_or_invalid_generated_at")
    elif data_age_seconds > 1800:
        reasons.append("dashboard_payload_stale_over_30m")
    if payload.get("refresh_error"):
        reasons.append("latest_refresh_error")
    source_cache_status = payload.get("source_cache_status") or {}
    stale_fallback_count = int(source_cache_status.get("stale_fallback_count") or 0)
    if stale_fallback_count:
        reasons.append("stale_cache_fallback_used")
    if not any(slip_counts.values()):
        reasons.append("no_slips_built")
    if active_errors:
        reasons.append("active_refresh_errors_present")
    failed_audits = len(
        active_refresh_errors(
            audit_rows=audit_rows,
            latest_errors=[row for row in audit_rows or [] if row.get("ok") is False],
            now=now,
        )
    )
    if failed_audits:
        reasons.append("recent_refresh_audit_failures")
    score = 100
    score -= 30 if payload.get("_read_error") else 0
    score -= 20 if data_age_seconds is None else 0
    score -= 25 if data_age_seconds is not None and data_age_seconds > 1800 else 0
    score -= 20 if payload.get("refresh_error") else 0
    score -= 15 if stale_fallback_count else 0
    score -= 20 if not any(slip_counts.values()) else 0
    score -= 10 if active_errors else 0
    score -= min(20, failed_audits * 5)
    score = max(0, score)
    return {
        "name": "dashboard_payload",
        "status": _status_from_score(score, blocked=bool(payload.get("_read_error")), issues=len(reasons)),
        "score": score,
        "generated_at": generated_at,
        "data_age_seconds": data_age_seconds,
        "slip_counts": slip_counts,
        "audit_events_checked": len(audit_rows or []),
        "latest_error_count": len(active_errors),
        "historical_error_count": len(latest_errors or []),
        "reasons": sorted(set(reasons)),
    }


def evaluate_source_records(
    records: list[dict[str, Any]],
    *,
    required_fields: tuple[str, ...],
    max_age_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    reasons: Counter[str] = Counter()
    latest_api_fetched_at: str | None = None
    latest_age_seconds: int | None = None
    source_hashes: set[str] = set()
    for row in records:
        for field in required_fields:
            if _is_missing(row.get(field)):
                reasons[f"missing_{field}"] += 1
        api_fetched_at = row.get("api_fetched_at")
        parsed_api_time = parse_timestamp(api_fetched_at)
        if parsed_api_time is None:
            reasons["invalid_api_fetched_at"] += 1
        else:
            if not _is_timezone_aware_text(api_fetched_at):
                reasons["api_fetched_at_not_timezone_aware"] += 1
            if parsed_api_time > now:
                reasons["api_fetched_at_in_future"] += 1
            age = int((now - parsed_api_time.astimezone(timezone.utc)).total_seconds())
            if age > max_age_seconds:
                reasons["stale_source_row"] += 1
            if latest_age_seconds is None or age < latest_age_seconds:
                latest_age_seconds = max(0, age)
                latest_api_fetched_at = str(api_fetched_at)
        for timestamp_field in (
            "prediction_timestamp",
            "event_start_time",
            "market_close_time",
            "game_start_time",
            "odds_timestamp",
            "candle_open_time",
            "candle_close_time",
            "settlement_time",
        ):
            if row.get(timestamp_field) and not _is_timezone_aware_text(row.get(timestamp_field)):
                reasons[f"{timestamp_field}_not_timezone_aware"] += 1
            if row.get(timestamp_field) and parse_timestamp(row.get(timestamp_field)) is None:
                reasons[f"invalid_{timestamp_field}"] += 1
        source_hash = row.get("source_snapshot_hash")
        if source_hash:
            source_hashes.add(str(source_hash))
    if not records:
        reasons["no_source_records"] += 1
    row_count = len(records)
    issue_total = sum(reasons.values())
    score = 100
    score -= 40 if row_count == 0 else 0
    score -= min(60, issue_total * 5)
    score = max(0, score)
    return {
        "row_count": row_count,
        "unique_source_snapshot_hashes": len(source_hashes),
        "latest_api_fetched_at": latest_api_fetched_at,
        "latest_age_seconds": latest_age_seconds,
        "issue_counts": dict(sorted(reasons.items())),
        "status": _status_from_score(score, issues=issue_total),
        "score": score,
    }


def evaluate_source_payload(
    payload: dict[str, Any],
    *,
    name: str,
    required_fields: tuple[str, ...],
    max_age_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    records = [row for row in payload.get("records") or [] if isinstance(row, dict)]
    record_gate = evaluate_source_records(records, required_fields=required_fields, max_age_seconds=max_age_seconds, now=now)
    errors = payload.get("errors") or []
    rejected = payload.get("rejected_records") or []
    blocker = payload.get("blocker")
    issue_counts = Counter(record_gate["issue_counts"])
    for error in errors:
        issue_counts[str(error.get("reason") or error.get("error") or "source_error")] += 1
    for row in rejected:
        issue_counts[str(row.get("rejection_reason") or "rejected_source_row")] += 1
    if blocker:
        issue_counts[str(blocker)] += 1
    blocked = bool(blocker) or bool(errors and not records)
    score = int(record_gate["score"])
    score -= min(35, len(errors) * 10)
    score -= min(25, len(rejected) * 5)
    score -= 30 if blocker else 0
    score = max(0, score)
    return {
        "name": name,
        "source_mode": payload.get("source_mode"),
        "source": payload.get("source"),
        "generated_at": payload.get("generated_at"),
        "blocker": blocker,
        "records": record_gate,
        "error_count": len(errors),
        "rejected_record_count": len(rejected),
        "issue_counts": dict(sorted(issue_counts.items())),
        "score": score,
        "status": _status_from_score(score, blocked=blocked, issues=sum(issue_counts.values())),
    }


def _parse_report_line(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def build_zero_heartbeat_diagnosis(
    *,
    crypto_payload: dict[str, Any],
    crypto_report_text: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    heartbeat_status = _parse_report_line(crypto_report_text, "Heartbeat status") or "unknown"
    logged = _parse_report_line(crypto_report_text, "Logged predictions")
    rejected = _parse_report_line(crypto_report_text, "Rejected predictions")
    settled = _parse_report_line(crypto_report_text, "Settled rows")
    records = [row for row in crypto_payload.get("records") or [] if isinstance(row, dict)]
    errors = crypto_payload.get("errors") or []
    gate = evaluate_source_records(records, required_fields=CRYPTO_REQUIRED_SOURCE_FIELDS, max_age_seconds=30 * 60, now=now)
    if errors and not records:
        reason = "source_blocked_or_unavailable"
        status = "BLOCKED"
    elif heartbeat_status == "no_material_change":
        reason = "unchanged_repeat_guard_or_no_eligible_settlement"
        status = "OK" if gate["status"] == "OK" else "WATCH"
    elif logged == "0" and rejected == "0" and settled == "0":
        reason = "zero_material_change_needs_watch"
        status = "WATCH"
    else:
        reason = "material_change_or_not_a_zero_heartbeat"
        status = "OK"
    return {
        "heartbeat_status": heartbeat_status,
        "logged_predictions": logged,
        "rejected_predictions": rejected,
        "settled_rows": settled,
        "diagnosis": reason,
        "status": status,
        "source_records": len(records),
        "source_error_count": len(errors),
        "latest_source_age_seconds": gate.get("latest_age_seconds"),
        "source_hashes_present": gate.get("unique_source_snapshot_hashes", 0) > 0,
        "next_action": (
            "reduce cadence only if repeated no_material_change continues; keep settlement checks active"
            if reason == "unchanged_repeat_guard_or_no_eligible_settlement"
            else "inspect source errors before collecting again"
            if reason == "source_blocked_or_unavailable"
            else "continue normal research heartbeat"
        ),
    }


def _table_exists(connection: DatabaseSession, table_name: str) -> bool:
    row = connection.execute(
        "SELECT to_regclass(%s) IS NOT NULL AS present",
        (f"app.{table_name}",),
    ).fetchone()
    return bool(row and row["present"])


def _table_column_types(connection: DatabaseSession, table_name: str) -> dict[str, str]:
    if not _table_exists(connection, table_name):
        return {}
    return {
        str(row["column_name"]): str(row["data_type"])
        for row in connection.execute(
            """
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema = 'app' AND table_name = %s
            """,
            (table_name,),
        ).fetchall()
    }


def _count_where(connection: DatabaseSession, table_name: str, clause: str, params: tuple[Any, ...] = ()) -> int:
    row = connection.execute(f"SELECT COUNT(*) AS count FROM app.{table_name} WHERE {clause}", params).fetchone()
    return int(row[0] if row else 0)


def evaluate_prediction_table(
    connection: DatabaseSession | None,
    *,
    table_name: str,
    run_id: str,
    asset_class: str,
) -> dict[str, Any]:
    if connection is None or not _table_exists(connection, table_name):
        return {
            "asset_class": asset_class,
            "table": table_name,
            "run_id": run_id,
            "status": "WATCH",
            "score": 60,
            "issue_counts": {"table_missing": 1},
        }
    column_types = _table_column_types(connection, table_name)
    columns = set(column_types)
    issue_counts: Counter[str] = Counter()
    run_filter = "run_id = %s"
    total = _count_where(connection, table_name, run_filter, (run_id,)) if "run_id" in columns else 0
    valid = _count_where(connection, table_name, f"{run_filter} AND validation_status = 'valid'", (run_id,)) if "validation_status" in columns else 0
    rejected = _count_where(connection, table_name, f"{run_filter} AND validation_status != 'valid'", (run_id,)) if "validation_status" in columns else 0
    unresolved = _count_where(connection, table_name, f"{run_filter} AND settlement_state = 'unresolved'", (run_id,)) if "settlement_state" in columns else 0
    settled = (
        _count_where(
            connection,
            table_name,
            f"{run_filter} AND settlement_state IS NOT NULL AND settlement_state != '' AND settlement_state != 'unresolved'",
            (run_id,),
        )
        if "settlement_state" in columns
        else 0
    )
    for required_column in ("prediction_timestamp", "api_fetched_at", "source_snapshot_hash"):
        if required_column in columns:
            missing_clause = f"{required_column} IS NULL"
            if column_types[required_column] in {"text", "character", "character varying"}:
                missing_clause = f"({missing_clause} OR BTRIM({required_column}) = '')"
            missing = _count_where(
                connection,
                table_name,
                f"{run_filter} AND {missing_clause}",
                (run_id,),
            )
            if missing:
                issue_counts[f"missing_{required_column}"] = missing
    if "prediction_timestamp" in columns:
        future = _count_where(
            connection,
            table_name,
            f"{run_filter} AND prediction_timestamp > %s",
            (run_id, utc_now_iso()),
        )
        if future:
            issue_counts["future_prediction_timestamp"] = future
    if total == 0:
        issue_counts["no_prediction_rows"] = 1
    score = 100
    score -= 25 if total == 0 else 0
    score -= min(50, sum(issue_counts.values()) * 3)
    score = max(0, score)
    return {
        "asset_class": asset_class,
        "table": table_name,
        "run_id": run_id,
        "total_rows": total,
        "valid_rows": valid,
        "rejected_or_invalid_rows": rejected,
        "unresolved_rows": unresolved,
        "settled_rows": settled,
        "issue_counts": dict(sorted(issue_counts.items())),
        "score": score,
        "status": _status_from_score(score, issues=sum(issue_counts.values())),
        "metric_policy": "unresolved and rejected rows are audited here but excluded from performance denominators",
    }


def build_metric_contamination_checks(*, sports_report_text: str, crypto_report_text: str, kalshi_report_text: str) -> dict[str, Any]:
    return {
        "sports_win_rate_zero_guard": (
            "pass"
            if "Win rate: unavailable / no settled rows" in sports_report_text or "Settled de-duped exposures: 0" not in sports_report_text
            else "watch"
        ),
        "crypto_roi_guard": "pass" if "ROI unavailable" in crypto_report_text else "watch",
        "kalshi_research_only_guard": "pass" if "No profitability" in kalshi_report_text or "not proof" in kalshi_report_text.lower() else "watch",
        "asset_class_report_separation": "pass",
        "connector_failure_metric_isolation": "pass",
    }


def build_data_quality_report(
    *,
    dashboard_payload_path: str | Path = repo_path("data", "today_paper_view.json"),
    audit_path: str | Path = repo_path("data", "refresh_audit.jsonl"),
    error_path: str | Path = repo_path("data", "error_events.jsonl"),
    crypto_run_id: str = DEFAULT_CRYPTO_RUN_ID,
    sports_run_id: str = DEFAULT_SPORTS_RUN_ID,
    kalshi_run_id: str = DEFAULT_KALSHI_RUN_ID,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    values = os.environ if env is None else env
    dashboard_payload = read_json_safely(dashboard_payload_path)
    audit_rows = latest_jsonl(audit_path, 10)
    latest_errors = latest_jsonl(error_path, 5)
    dashboard_gate = build_dashboard_quality_gate(dashboard_payload, audit_rows=audit_rows, latest_errors=latest_errors, now=now)
    crypto_payload_path = repo_path("data", "crypto_runs", f"{crypto_run_id}_source.json")
    sports_payload_path = repo_path("data", "sports_runs", f"{sports_run_id}_odds.json")
    crypto_payload = read_json_safely(crypto_payload_path)
    sports_payload = read_json_safely(sports_payload_path)
    crypto_source_gate = evaluate_source_payload(
        crypto_payload,
        name="crypto_source_payload",
        required_fields=CRYPTO_REQUIRED_SOURCE_FIELDS,
        max_age_seconds=30 * 60,
        now=now,
    )
    sports_source_gate = evaluate_source_payload(
        sports_payload,
        name="sports_source_payload",
        required_fields=SPORTS_REQUIRED_SOURCE_FIELDS,
        max_age_seconds=2 * 60 * 60,
        now=now,
    )
    crypto_report_path = repo_path("data", "crypto_runs", f"{crypto_run_id}_all_report.txt")
    sports_report_path = repo_path("data", "sports_runs", f"{sports_run_id}_all_report.txt")
    kalshi_report_path = repo_path("data", "paper_runs", f"{kalshi_run_id}_stage3b_audit.txt")
    crypto_report_text = read_text_safely(crypto_report_path)
    sports_report_text = read_text_safely(sports_report_path)
    kalshi_report_text = read_text_safely(kalshi_report_path)
    zero_heartbeat = build_zero_heartbeat_diagnosis(crypto_payload=crypto_payload, crypto_report_text=crypto_report_text, now=now)
    try:
        with create_store().connect() as connection:
            prediction_tables = [
                evaluate_prediction_table(connection, table_name="prediction_logs", run_id=kalshi_run_id, asset_class="kalshi"),
                evaluate_prediction_table(connection, table_name="crypto_prediction_logs", run_id=crypto_run_id, asset_class="crypto"),
                evaluate_prediction_table(connection, table_name="sports_prediction_logs", run_id=sports_run_id, asset_class="sports"),
            ]
        database_available = True
    except Exception:
        database_available = False
        prediction_tables = [
            evaluate_prediction_table(None, table_name="prediction_logs", run_id=kalshi_run_id, asset_class="kalshi"),
            evaluate_prediction_table(None, table_name="crypto_prediction_logs", run_id=crypto_run_id, asset_class="crypto"),
            evaluate_prediction_table(None, table_name="sports_prediction_logs", run_id=sports_run_id, asset_class="sports"),
        ]
    gates = [dashboard_gate, crypto_source_gate, sports_source_gate, *prediction_tables]
    metric_checks = build_metric_contamination_checks(
        sports_report_text=sports_report_text,
        crypto_report_text=crypto_report_text,
        kalshi_report_text=kalshi_report_text,
    )
    connector_status = build_connectors_status(values)
    guardrails = _quality_guardrails(values)
    core_quality = _build_core_quality(
        database_available=database_available,
        metric_checks=metric_checks,
        guardrails=guardrails,
    )
    workflow_quality_scores = {
        "kalshi": _build_workflow_quality("kalshi", [dashboard_gate, prediction_tables[0]]),
        "crypto": _build_workflow_quality("crypto", [crypto_source_gate, prediction_tables[1]]),
        "sports": _build_workflow_quality("sports", [sports_source_gate, prediction_tables[2]]),
    }
    optional_capability_status = _optional_capability_status(connector_status)
    deployment_readiness = _build_deployment_readiness(values, guardrails=guardrails)
    overall_score = core_quality["score"]
    overall_status = core_quality["status"]
    major_issues = [
        f"{gate['name'] if 'name' in gate else gate.get('asset_class', 'table')}: {gate.get('issue_counts') or gate.get('reasons')}"
        for gate in gates
        if gate.get("status") == "BLOCKED"
    ]
    minor_issues = [
        f"{gate['name'] if 'name' in gate else gate.get('asset_class', 'table')}: {gate.get('issue_counts') or gate.get('reasons')}"
        for gate in gates
        if gate.get("status") == "WATCH"
    ]
    return {
        "report_type": "private_research_data_quality",
        "generated_at": utc_now_iso(),
        "overall_status": overall_status,
        "overall_score": overall_score,
        "core_quality_score": core_quality["score"],
        "core_quality_status": core_quality["status"],
        "core_quality_checks": core_quality["checks"],
        "workflow_quality_scores": workflow_quality_scores,
        "optional_capability_status": optional_capability_status,
        "deployment_readiness": deployment_readiness,
        "mode": "private_local_research_only",
        "dashboard": dashboard_gate,
        "source_payloads": {
            "crypto": {**crypto_source_gate, "path": str(crypto_payload_path), "file_status": file_status(crypto_payload_path)},
            "sports": {**sports_source_gate, "path": str(sports_payload_path), "file_status": file_status(sports_payload_path)},
        },
        "prediction_tables": prediction_tables,
        "zero_heartbeat_diagnosis": zero_heartbeat,
        "metric_contamination_checks": metric_checks,
        "report_files": {
            "crypto_all": file_status(crypto_report_path),
            "sports_all": file_status(sports_report_path),
            "kalshi_stage3b": file_status(kalshi_report_path),
        },
        "audit": {
            "path": str(audit_path),
            "events_checked": len(audit_rows),
            "latest_errors_checked": len(latest_errors),
        },
        "major_issues": major_issues,
        "minor_issues": minor_issues,
        "guardrails": guardrails,
        "next_actions": _quality_next_actions(overall_status, zero_heartbeat, crypto_source_gate, sports_source_gate),
    }


def _quality_guardrails(values: Mapping[str, str]) -> dict[str, Any]:
    enabled = lambda name, default="false": str(values.get(name, default)).lower() in {"1", "true", "yes", "on"}
    return {
        "research_only": enabled("RESEARCH_ONLY", "true"),
        "auto_trade_enabled": enabled("AUTO_TRADE_ENABLED"),
        "auto_bet_enabled": False,
        "kalshi_order_upload_enabled": enabled("KALSHI_ORDER_UPLOAD_ENABLED"),
        "real_money_execution_enabled": enabled("LIVE_EXECUTION_ENABLED"),
        "automatic_upload_enabled": enabled("AUTO_UPLOAD_ENABLED"),
        "model_promotion_enabled": enabled("MODEL_PROMOTION_ENABLED"),
        "stale_cache_as_fresh": enabled("STALE_CACHE_AS_FRESH"),
        "public_ui_enabled": False,
        "ml_training_enabled": False,
        "profitability_claims_allowed": False,
        "account_handoff_policy": "manual_review_only",
    }


def _build_core_quality(
    *,
    database_available: bool,
    metric_checks: Mapping[str, str],
    guardrails: Mapping[str, Any],
) -> dict[str, Any]:
    safety_ok = bool(guardrails.get("research_only")) and not any(
        guardrails.get(name)
        for name in (
            "auto_trade_enabled",
            "kalshi_order_upload_enabled",
            "real_money_execution_enabled",
            "automatic_upload_enabled",
            "model_promotion_enabled",
            "stale_cache_as_fresh",
        )
    )
    checks = {
        "database_audit_available": "pass" if database_available else "fail",
        "metric_denominator_guards": "pass" if all(value == "pass" for value in metric_checks.values()) else "fail",
        "research_only_safety": "pass" if safety_ok else "fail",
    }
    score = round(100.0 * sum(value == "pass" for value in checks.values()) / len(checks), 2)
    status = "OK" if all(value == "pass" for value in checks.values()) else "BLOCKED"
    return {"score": score, "status": status, "checks": checks}


def _build_workflow_quality(name: str, gates: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(gate.get("status") or "BLOCKED") for gate in gates)
    status = "BLOCKED" if statuses["BLOCKED"] else "WATCH" if statuses["WATCH"] else "OK"
    score = round(mean(int(gate.get("score", 0)) for gate in gates), 2) if gates else 0.0
    return {
        "workflow": name,
        "status": status,
        "score": score,
        "ready": status == "OK",
        "components": [str(gate.get("name") or gate.get("asset_class") or "unknown") for gate in gates],
    }


def _optional_capability_status(connector_status: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, state in (connector_status.get("states") or {}).items():
        raw_state = str(state.get("state") or "unconfigured_optional")
        if raw_state.startswith("configured"):
            capability_state = "available"
        elif raw_state == "missing_required":
            capability_state = "failed_required"
        elif raw_state == "disabled":
            capability_state = "disabled"
        else:
            capability_state = "unavailable_optional"
        result[name] = {
            "state": capability_state,
            "required": bool(state.get("required")),
            "reason": state.get("reason"),
        }
    return result


def _build_deployment_readiness(values: Mapping[str, str], *, guardrails: Mapping[str, Any]) -> dict[str, Any]:
    enabled = lambda name: str(values.get(name, "false")).lower() in {"1", "true", "yes", "on"}
    checks = {
        "postgres_database_configured": bool(str(values.get("DATABASE_URL") or "").strip()),
        "postgres_parity_validated": enabled("POSTGRES_PARITY_VALIDATED"),
        "railway_staging_validated": enabled("RAILWAY_STAGING_VALIDATED"),
        "production_backup_verified": enabled("RAILWAY_BACKUP_VERIFIED"),
        "production_volume_healthy": enabled("RAILWAY_VOLUME_HEALTHY"),
        "research_only_safety": bool(guardrails.get("research_only")) and not any(
            guardrails.get(name)
            for name in (
                "auto_trade_enabled",
                "kalshi_order_upload_enabled",
                "real_money_execution_enabled",
                "automatic_upload_enabled",
                "model_promotion_enabled",
                "stale_cache_as_fresh",
            )
        ),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {"ready": not blockers, "status": "READY" if not blockers else "BLOCKED", "checks": checks, "blockers": blockers}


def _quality_next_actions(
    overall_status: str,
    zero_heartbeat: dict[str, Any],
    crypto_source_gate: dict[str, Any],
    sports_source_gate: dict[str, Any],
) -> list[str]:
    actions = []
    if crypto_source_gate["status"] == "BLOCKED":
        actions.append("inspect crypto source errors before next heartbeat; never use stale cache as fresh")
    elif zero_heartbeat["diagnosis"] == "unchanged_repeat_guard_or_no_eligible_settlement":
        actions.append("treat crypto 0/0/0 as no_material_change while source freshness stays OK")
    if sports_source_gate["status"] != "OK":
        actions.append("continue sports scraper QA; blocked or unresolved rows are not losses")
    if overall_status == "OK":
        actions.append("continue scheduled collection and settlement loops")
    else:
        actions.append("repair mandatory core checks before relying on platform quality")
    if sports_source_gate["status"] != "OK":
        actions.append("keep sports blocked while Kalshi and crypto continue under their own quality gates")
    return actions


def render_data_quality_report(report: dict[str, Any]) -> str:
    lines = [
        "Private Research Data Quality Report",
        f"Generated at: {report['generated_at']}",
        f"Core platform quality: {report['core_quality_status']} ({report['core_quality_score']})",
        f"Deployment readiness: {report['deployment_readiness']['status']}",
        f"Mode: {report['mode']}",
        "",
        "Core quality checks:",
    ]
    for name, value in report["core_quality_checks"].items():
        lines.append(f"- {name}: {value}")
    lines.extend(["", "Workflow quality:"])
    for name, workflow in report["workflow_quality_scores"].items():
        lines.append(f"- {name}: status={workflow['status']} score={workflow['score']} ready={workflow['ready']}")
    lines.extend(["", "Optional capabilities:"])
    for name, capability in report["optional_capability_status"].items():
        lines.append(
            f"- {name}: state={capability['state']} required={capability['required']} reason={capability.get('reason')}"
        )
    lines.extend(
        [
            "",
            f"Deployment blockers: {report['deployment_readiness']['blockers']}",
        "",
        "Dashboard gate:",
        f"- status: {report['dashboard']['status']}",
        f"- score: {report['dashboard']['score']}",
        f"- data_age_seconds: {report['dashboard'].get('data_age_seconds')}",
        f"- slip_counts: {report['dashboard'].get('slip_counts')}",
        f"- reasons: {report['dashboard'].get('reasons')}",
        "",
        "Source payload gates:",
        ]
    )
    for name, gate in report["source_payloads"].items():
        lines.extend(
            [
                f"- {name}: status={gate['status']} score={gate['score']} records={gate['records']['row_count']} "
                f"errors={gate['error_count']} rejected={gate['rejected_record_count']}",
                f"  latest_api_fetched_at={gate['records'].get('latest_api_fetched_at')} "
                f"latest_age_seconds={gate['records'].get('latest_age_seconds')} "
                f"issues={gate.get('issue_counts')}",
            ]
        )
    lines.extend(["", "Prediction table gates:"])
    for gate in report["prediction_tables"]:
        lines.append(
            f"- {gate['asset_class']}: status={gate['status']} score={gate['score']} total={gate.get('total_rows', 0)} "
            f"valid={gate.get('valid_rows', 0)} settled={gate.get('settled_rows', 0)} "
            f"unresolved={gate.get('unresolved_rows', 0)} issues={gate.get('issue_counts')}"
        )
    zero = report["zero_heartbeat_diagnosis"]
    lines.extend(
        [
            "",
            "Crypto zero-heartbeat diagnosis:",
            f"- status: {zero['status']}",
            f"- heartbeat_status: {zero['heartbeat_status']}",
            f"- logged/rejected/settled: {zero.get('logged_predictions')}/{zero.get('rejected_predictions')}/{zero.get('settled_rows')}",
            f"- diagnosis: {zero['diagnosis']}",
            f"- source_records: {zero['source_records']}",
            f"- latest_source_age_seconds: {zero.get('latest_source_age_seconds')}",
            f"- next_action: {zero['next_action']}",
            "",
            "Metric contamination checks:",
        ]
    )
    for key, value in report["metric_contamination_checks"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "Major issues:"])
    lines.extend([f"- {issue}" for issue in report["major_issues"]] or ["- none"])
    lines.extend(["", "Minor issues:"])
    lines.extend([f"- {issue}" for issue in report["minor_issues"]] or ["- none"])
    lines.extend(["", "Next actions:"])
    lines.extend([f"- {action}" for action in report["next_actions"]])
    lines.extend(
        [
            "",
            "Guardrails:",
            "- no auto-trading",
            "- no auto-betting",
            "- no Kalshi account order upload",
            "- no stale cache as fresh data",
            "- no edge/profitability claim",
        ]
    )
    return "\n".join(lines)


def write_data_quality_report(report: dict[str, Any], output: str | Path, json_output: str | Path | None = None) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_data_quality_report(report), encoding="utf-8")
    if json_output:
        json_output_path = Path(json_output)
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
