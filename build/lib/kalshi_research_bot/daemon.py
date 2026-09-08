from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from .bot_company import bot_company_roster, bot_company_summary
from .config import repo_path


DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8765"
DEFAULT_CRYPTO_RUN_ID = "crypto_private_20260704"
DEFAULT_SPORTS_RUN_ID = "sports_private_20260704"
DEFAULT_KALSHI_RUN_ID = "stage3a_20260703_170707"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_always_on_tasks(
    *,
    crypto_run_id: str = DEFAULT_CRYPTO_RUN_ID,
    sports_run_id: str = DEFAULT_SPORTS_RUN_ID,
    kalshi_run_id: str = DEFAULT_KALSHI_RUN_ID,
    dashboard_port: int = 8765,
) -> list[dict[str, Any]]:
    tasks = []
    for bot in bot_company_roster():
        command = (
            bot["command"]
            .replace(DEFAULT_CRYPTO_RUN_ID, crypto_run_id)
            .replace(DEFAULT_SPORTS_RUN_ID, sports_run_id)
            .replace(DEFAULT_KALSHI_RUN_ID, kalshi_run_id)
            .replace("-Port 8765", f"-Port {dashboard_port}")
        )
        tasks.append(
            {
                "name": bot["id"],
                "task_name": bot["task_name"],
                "department": bot["department"],
                "cadence": bot["cadence"],
                "purpose": bot["task"],
                "command": command,
            }
        )
    return tasks


def fetch_dashboard_quality(base_url: str = DEFAULT_DASHBOARD_URL, *, opener: Any | None = None) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/quality.json"
    try:
        if opener is None:
            with urlopen(url, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        else:
            payload = opener(url)
        return {
            "status": "available",
            "url": url,
            "quality": payload.get("status"),
            "generated_at": payload.get("generated_at"),
            "data_age_seconds": payload.get("data_age_seconds"),
            "slip_counts": payload.get("slip_counts", {}),
            "warnings": payload.get("warnings", []),
        }
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "url": url,
            "quality": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def report_file_status(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    if not resolved.exists():
        return {"path": str(resolved), "exists": False}
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "exists": True,
        "last_write_time": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "bytes": stat.st_size,
    }


def build_daemon_status(
    *,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    crypto_run_id: str = DEFAULT_CRYPTO_RUN_ID,
    sports_run_id: str = DEFAULT_SPORTS_RUN_ID,
    kalshi_run_id: str = DEFAULT_KALSHI_RUN_ID,
    opener: Any | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "mode": "private_local_research",
        "dashboard": fetch_dashboard_quality(dashboard_url, opener=opener),
        "reports": {
            "crypto_daily": report_file_status(repo_path("data", "crypto_runs", f"{crypto_run_id}_daily_report.txt")),
            "sports_daily": report_file_status(repo_path("data", "sports_runs", f"{sports_run_id}_daily_report.txt")),
            "kalshi_stage3b_audit": report_file_status(repo_path("data", "paper_runs", f"{kalshi_run_id}_stage3b_audit.txt")),
        },
        "tasks": default_always_on_tasks(
            crypto_run_id=crypto_run_id,
            sports_run_id=sports_run_id,
            kalshi_run_id=kalshi_run_id,
        ),
        "guardrails": {
            "auto_trade_enabled": False,
            "auto_bet_enabled": False,
            "kalshi_order_upload_enabled": False,
            "account_handoff_policy": "manual_review_only",
            "public_ui_enabled": False,
            "ml_training_enabled": False,
            "profitability_claims_allowed": False,
        },
        "bot_company": bot_company_summary(),
        "env": {
            "research_daemon_enabled": os.environ.get("RESEARCH_DAEMON_ENABLED", "false"),
            "sports_source_mode": os.environ.get("SPORTS_SOURCE_MODE", "scraper"),
            "sports_scraper_enabled": os.environ.get("SPORTS_SCRAPER_ENABLED", "true"),
            "slack_alerts_enabled": os.environ.get("SLACK_ALERTS_ENABLED", "false"),
            "airtable_enabled": os.environ.get("AIRTABLE_ENABLED", "false"),
            "google_drive_enabled": os.environ.get("GOOGLE_DRIVE_ENABLED", "false"),
        },
    }


def render_daemon_status(status: dict[str, Any]) -> str:
    lines = [
        "Private Research Daemon Status",
        f"Generated at: {status['generated_at']}",
        f"Mode: {status['mode']}",
        "",
        "Dashboard:",
    ]
    dashboard = status["dashboard"]
    lines.append(f"- status: {dashboard.get('status')}")
    lines.append(f"- quality: {dashboard.get('quality')}")
    lines.append(f"- generated_at: {dashboard.get('generated_at')}")
    lines.append(f"- data_age_seconds: {dashboard.get('data_age_seconds')}")
    if dashboard.get("error"):
        lines.append(f"- error: {dashboard['error']}")
    if dashboard.get("warnings"):
        lines.append(f"- warnings: {dashboard['warnings']}")
    lines.extend(["", "Reports:"])
    for name, report in status["reports"].items():
        suffix = f"{report.get('last_write_time')} ({report.get('bytes')} bytes)" if report.get("exists") else "missing"
        lines.append(f"- {name}: {suffix}")
    lines.extend(["", "Always-on tasks:"])
    for task in status["tasks"]:
        lines.append(f"- {task['name']}: {task['cadence']} :: {task['purpose']}")
    lines.extend(["", "Guardrails:"])
    for key, value in status["guardrails"].items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)
