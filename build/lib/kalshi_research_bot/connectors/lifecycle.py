from __future__ import annotations

from pathlib import Path
from typing import Any

from .airtable_status import bot_run_payload, stage_gate_payload, sync_status
from .google_drive_archive import archive_files
from .slack_alerts import build_alert_payload, send_alert


def apply_post_report_connectors(
    report: dict[str, Any],
    *,
    report_paths: list[str | Path],
    bot_name: str,
    asset_class: str,
    run_id: str,
    stage: str,
    mode: str,
) -> dict[str, Any]:
    decorated = dict(report)
    archive_status = archive_files(report_paths)
    settled = int(report.get("settled_deduped_exposures") or report.get("settled_deduped_market_exposures") or 0)
    airtable_status = sync_status(
        {
            "bot_runs": [bot_run_payload(report, bot_name=bot_name, asset_class=asset_class, stage=stage, mode=mode)],
            "stage_gates": [
                stage_gate_payload(
                    bot_name=bot_name,
                    run_id=run_id,
                    current_stage=stage,
                    current_count=settled,
                    required_count=300 if settled >= 100 else 100,
                    gate_status=str(report.get("gate_result") or report.get("stage3b_gate_status") or "unknown"),
                    next_action=str(report.get("next_automatic_action") or "continue research-only collection"),
                )
            ],
        }
    )
    slack_status = _maybe_send_milestone_alert(report, bot_name=bot_name, asset_class=asset_class, run_id=run_id, report_paths=report_paths)
    decorated["connector_actions"] = {
        "google_drive_archive": archive_status["status"],
        "airtable_status_sync": airtable_status["status"],
        "slack_alert": slack_status["status"],
    }
    return decorated


def _maybe_send_milestone_alert(
    report: dict[str, Any],
    *,
    bot_name: str,
    asset_class: str,
    run_id: str,
    report_paths: list[str | Path],
) -> dict[str, Any]:
    settled = int(report.get("settled_deduped_exposures") or report.get("settled_deduped_market_exposures") or 0)
    event_type: str | None = None
    if asset_class == "crypto" and settled in {500, 1000}:
        event_type = f"crypto_{settled}_settled_deduped"
    elif asset_class == "kalshi" and settled >= 300:
        event_type = "kalshi_300_deduped"
    elif asset_class == "sports" and report.get("source_mode") in {"scraper", "scraper_first"} and not report.get("blockers"):
        event_type = "sports_scraper_started"
    if event_type is None:
        return {"status": "slack_alert_not_applicable", "sent": False}
    alert = build_alert_payload(
        bot_name=bot_name,
        asset_class=asset_class,
        run_id=run_id,
        severity="info",
        event_type=event_type,
        message=f"{asset_class} research gate/state changed at {settled} settled de-duped exposures",
        report_path=str(report_paths[0]) if report_paths else None,
        next_action=str(report.get("next_automatic_action") or "review research-only report"),
    )
    return send_alert(alert)

