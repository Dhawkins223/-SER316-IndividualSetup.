from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ..private_research import utc_now_iso


def airtable_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get("AIRTABLE_ENABLED", "false")).lower() in {"1", "true", "yes", "on"}


def airtable_configured(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return airtable_enabled(values) and bool(values.get("AIRTABLE_API_KEY")) and bool(values.get("AIRTABLE_BASE_ID"))


def top_rejection_reason(rejection_reasons: Any) -> str | None:
    if not rejection_reasons:
        return None
    if isinstance(rejection_reasons, Mapping):
        return str(max(rejection_reasons.items(), key=lambda item: int(item[1] or 0))[0])
    if isinstance(rejection_reasons, (list, tuple)):
        for item in rejection_reasons:
            if isinstance(item, Mapping):
                reason = item.get("reason") or item.get("rejection_reason")
                if reason:
                    return str(reason)
            elif str(item).strip():
                return str(item).strip()
    return None


def bot_run_payload(
    report: Mapping[str, Any],
    *,
    bot_name: str,
    asset_class: str,
    stage: str,
    mode: str,
) -> dict[str, Any]:
    settled = int(report.get("settled_deduped_exposures") or report.get("settled_deduped_market_exposures") or 0)
    rejected = int(report.get("rejected_predictions") or report.get("invalid_rejected_predictions") or 0)
    unresolved = int(report.get("unresolved_predictions") or 0)
    return {
        "bot_name": bot_name,
        "asset_class": asset_class,
        "run_id": report.get("run_id") or (report.get("run") or {}).get("run_id"),
        "stage": stage,
        "mode": mode,
        "valid_rows": int(report.get("total_raw_predictions") or report.get("new_valid_rows") or report.get("new_predictions_logged") or 0),
        "settled_deduped": settled,
        "unresolved_rows": unresolved,
        "rejected_rows": rejected,
        "top_rejection_reason": top_rejection_reason(
            report.get("rejection_reason_counts") or report.get("rejection_reasons")
        ),
        "duplicate_exposure_count": int(report.get("duplicate_exposure_warnings") or 0)
        if isinstance(report.get("duplicate_exposure_warnings"), int)
        else len(report.get("duplicate_exposure_warnings") or []),
        "last_successful_run": utc_now_iso(),
        "next_gate": report.get("sample_size_status") or report.get("deduped_sample_status") or report.get("sample_status"),
        "gate_status": report.get("gate_result") or report.get("stage3b_gate_status"),
        "claims_allowed": False,
        "ml_allowed": False,
        "public_allowed": False,
    }


def stage_gate_payload(
    *,
    bot_name: str,
    run_id: str,
    current_stage: str,
    current_count: int,
    required_count: int,
    gate_status: str,
    next_action: str,
) -> dict[str, Any]:
    return {
        "bot_name": bot_name,
        "run_id": run_id,
        "current_stage": current_stage,
        "current_count": current_count,
        "required_count": required_count,
        "gate_status": gate_status,
        "next_action": next_action,
    }


def open_issue_payload(
    *,
    severity: str,
    bot_name: str,
    issue_type: str,
    description: str,
    status: str = "open",
) -> dict[str, Any]:
    return {
        "severity": severity.lower(),
        "bot_name": bot_name,
        "issue_type": issue_type,
        "description": description,
        "created_at": utc_now_iso(),
        "resolved_at": None,
        "status": status,
    }


def sync_status(
    payloads: Mapping[str, list[dict[str, Any]]],
    *,
    env: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    if not airtable_configured(env) or client is None:
        return {"status": "airtable_sync_skipped_unavailable", "synced_count": 0, "failures": []}
    values = os.environ if env is None else env
    table_map = {
        "bot_runs": values.get("AIRTABLE_BOT_RUNS_TABLE") or "Bot Runs",
        "source_health": values.get("AIRTABLE_SOURCE_HEALTH_TABLE") or "Source Health",
        "stage_gates": values.get("AIRTABLE_STAGE_GATES_TABLE") or "Stage Gates",
        "open_issues": values.get("AIRTABLE_OPEN_ISSUES_TABLE") or "Open Issues",
    }
    synced = 0
    failures: list[dict[str, Any]] = []
    for key, rows in payloads.items():
        table = table_map.get(key, key)
        for row in rows:
            try:
                client.upsert(table, row)
                synced += 1
            except Exception as exc:  # noqa: BLE001 - optional connector must not fail cycles
                failures.append({"table": table, "reason": str(exc)})
    return {"status": "airtable_sync_complete" if not failures else "airtable_sync_partial_failure", "synced_count": synced, "failures": failures}
