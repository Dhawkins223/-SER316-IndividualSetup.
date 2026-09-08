from __future__ import annotations

from collections import defaultdict
from typing import Any


BOT_COMPANY_GUARDRAILS: dict[str, bool | str] = {
    "private_local_research_only": True,
    "auto_trade_enabled": False,
    "auto_bet_enabled": False,
    "kalshi_order_upload_enabled": False,
    "real_money_execution_enabled": False,
    "public_ui_enabled": False,
    "ml_training_enabled": False,
    "profitability_claims_allowed": False,
    "account_handoff_policy": "manual_review_only",
}


def bot_company_roster() -> list[dict[str, Any]]:
    return [
        {
            "id": "platform_foreman",
            "name": "Platform Foreman",
            "department": "Operations",
            "cadence": "every_5_minutes",
            "task": "Keep the local dashboard alive, refresh stale data, and log watchdog status.",
            "command": "powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\dashboard_watchdog.ps1 -Port 8765",
            "task_name": "DashboardWatchdog",
        },
        {
            "id": "source_health_sentinel",
            "name": "Source Health Sentinel",
            "department": "Operations",
            "cadence": "every_15_minutes",
            "task": "Record dashboard/source health and surface stale-data or source-blocked states.",
            "command": "cmd /c scripts\\source_health.cmd",
            "task_name": "SourceHealthSentinel",
        },
        {
            "id": "crypto_market_scout",
            "name": "Crypto Market Scout",
            "department": "Crypto Research",
            "cadence": "every_15_minutes",
            "task": "Fetch Coinbase/Kraken data, log valid changed snapshots, settle eligible crypto rows, update reports.",
            "command": "cmd /c scripts\\crypto_cycle.cmd --run-id crypto_private_20260704",
            "task_name": "CryptoStage3A",
        },
        {
            "id": "crypto_diagnostics_analyst",
            "name": "Crypto Diagnostics Analyst",
            "department": "Crypto Research",
            "cadence": "daily",
            "task": "Regenerate Stage 4 diagnostic reports without changing live logic or training models.",
            "command": "cmd /c scripts\\crypto_diagnostics.cmd --run-id crypto_private_20260704",
            "task_name": "CryptoDiagnosticsDaily",
        },
        {
            "id": "sports_public_source_scout",
            "name": "Sports Public Source Scout",
            "department": "Sports Research",
            "cadence": "hourly",
            "task": "Run scraper-first sports collection, log valid rows, settle official finals when available.",
            "command": "cmd /c scripts\\sports_cycle.cmd --run-id sports_private_20260704",
            "task_name": "SportsScraperStage3A",
        },
        {
            "id": "kalshi_settlement_clerk",
            "name": "Kalshi Settlement Clerk",
            "department": "Kalshi Research",
            "cadence": "every_12_hours",
            "task": "Import official Kalshi settlements, refresh daily reports, and regenerate Stage 3B audit.",
            "command": "cmd /c scripts\\kalshi_passive_check.cmd --run-id stage3a_20260703_170707",
            "task_name": "KalshiPassiveCheck",
        },
        {
            "id": "feature_export_librarian",
            "name": "Feature Export Librarian",
            "department": "Research Data",
            "cadence": "daily",
            "task": "Export ML-ready feature/label files without leakage; no training.",
            "command": "cmd /c scripts\\feature_exports.cmd",
            "task_name": "FeatureExportsDaily",
        },
        {
            "id": "quality_auditor",
            "name": "Quality Auditor",
            "department": "Quality",
            "cadence": "daily",
            "task": "Run the full test suite and write local logs; never mutates model logic.",
            "command": "cmd /c scripts\\qa_daily.cmd",
            "task_name": "QualityAuditDaily",
        },
        {
            "id": "report_archivist",
            "name": "Report Archivist",
            "department": "Operations",
            "cadence": "daily",
            "task": "Archive reports to optional Google Drive connector when enabled; skip safely when unavailable.",
            "command": "cmd /c scripts\\archive_reports.cmd",
            "task_name": "ReportArchiveDaily",
        },
        {
            "id": "status_sync_clerk",
            "name": "Status Sync Clerk",
            "department": "Operations",
            "cadence": "hourly",
            "task": "Sync research-only status to optional Airtable connector when enabled; local DB remains source of truth.",
            "command": "cmd /c scripts\\status_sync.cmd",
            "task_name": "StatusSyncHourly",
        },
        {
            "id": "daily_briefing_chief",
            "name": "Daily Briefing Chief",
            "department": "Executive",
            "cadence": "daily",
            "task": "Generate a local company status brief from dashboard, reports, scheduled tasks, and guardrails.",
            "command": "cmd /c scripts\\company_brief.cmd",
            "task_name": "CompanyBriefDaily",
        },
    ]


def bot_company_summary() -> dict[str, Any]:
    roster = bot_company_roster()
    departments: dict[str, list[str]] = defaultdict(list)
    cadences: dict[str, list[str]] = defaultdict(list)
    for bot in roster:
        departments[bot["department"]].append(bot["name"])
        cadences[bot["cadence"]].append(bot["name"])
    return {
        "bot_count": len(roster),
        "departments": dict(sorted(departments.items())),
        "cadences": dict(sorted(cadences.items())),
        "guardrails": BOT_COMPANY_GUARDRAILS,
        "roster": roster,
    }


def render_bot_company(summary: dict[str, Any] | None = None) -> str:
    data = summary or bot_company_summary()
    lines = [
        "Private Research Bot Company",
        f"Bot count: {data['bot_count']}",
        "",
        "Departments:",
    ]
    for department, names in data["departments"].items():
        lines.append(f"- {department}: {', '.join(names)}")
    lines.extend(["", "Cadences:"])
    for cadence, names in data["cadences"].items():
        lines.append(f"- {cadence}: {', '.join(names)}")
    lines.extend(["", "Roster:"])
    for bot in data["roster"]:
        lines.append(f"- {bot['name']} [{bot['cadence']}]: {bot['task']}")
    lines.extend(["", "Guardrails:"])
    for key, value in data["guardrails"].items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)
