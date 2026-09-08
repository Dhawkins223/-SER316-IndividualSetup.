from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..config import repo_path
from ..private_research import deterministic_hash, utc_now_iso, write_json


SlackSender = Callable[[dict[str, Any]], Any]


def slack_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get("SLACK_ALERTS_ENABLED", "false")).lower() in {"1", "true", "yes", "on"} and bool(values.get("SLACK_WEBHOOK_URL"))


def build_alert_payload(
    *,
    bot_name: str,
    asset_class: str,
    run_id: str,
    severity: str,
    event_type: str,
    message: str,
    report_path: str | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    return {
        "bot_name": bot_name,
        "asset_class": asset_class,
        "run_id": run_id,
        "severity": map_severity(severity),
        "event_type": event_type,
        "message": message,
        "report_path": report_path,
        "next_action": next_action,
        "created_at": utc_now_iso(),
    }


def map_severity(value: str) -> str:
    normalized = str(value or "info").lower()
    if normalized in {"critical", "error", "warning", "info"}:
        return normalized
    if normalized in {"warn", "medium"}:
        return "warning"
    if normalized in {"high", "fatal"}:
        return "critical"
    return "info"


def send_alert(
    alert: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    sender: SlackSender | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    values = os.environ if env is None else env
    if not slack_enabled(values) and sender is None:
        return {"status": "slack_alert_skipped_unavailable", "sent": False, "deduped": False}
    key = _alert_key(alert)
    path = Path(state_path) if state_path else repo_path("data", "connector_state", "slack_alerts.json")
    seen = _read_seen(path)
    if key in seen:
        return {"status": "slack_alert_deduped", "sent": False, "deduped": True}
    try:
        if sender is not None:
            result = sender(dict(alert))
        else:
            result = _send_webhook(dict(alert), webhook_url=values["SLACK_WEBHOOK_URL"])
        seen.add(key)
        _write_seen(path, seen)
        return {"status": "slack_alert_sent", "sent": True, "deduped": False, "result": result}
    except Exception as exc:  # noqa: BLE001 - alerting must not break cycles
        return {"status": "slack_alert_failed_nonblocking", "sent": False, "deduped": False, "error": str(exc)}


def _send_webhook(alert: dict[str, Any], *, webhook_url: str) -> dict[str, Any]:
    text = (
        f"[{alert['severity']}] {alert['bot_name']} {alert['run_id']} {alert['event_type']}: "
        f"{alert['message']} Next: {alert.get('next_action') or 'review report'}"
    )
    body = json.dumps({"text": text, "metadata": alert}).encode("utf-8")
    request = urllib.request.Request(webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return {"status_code": getattr(response, "status", None)}


def _alert_key(alert: Mapping[str, Any]) -> str:
    return deterministic_hash(
        {
            "bot_name": alert.get("bot_name"),
            "asset_class": alert.get("asset_class"),
            "run_id": alert.get("run_id"),
            "severity": alert.get("severity"),
            "event_type": alert.get("event_type"),
            "message": alert.get("message"),
        }
    )


def _read_seen(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("sent_alert_keys", []))
    except (OSError, json.JSONDecodeError):
        return set()


def _write_seen(path: Path, seen: set[str]) -> None:
    write_json(path, {"sent_alert_keys": sorted(seen)})
