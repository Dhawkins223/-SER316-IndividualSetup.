from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from ..config import repo_path


ArchiveUploader = Callable[[Path, str], Any]
DEFAULT_REPORT_FOLDER = "Research Bots"


def google_drive_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get("GOOGLE_DRIVE_ENABLED", "false")).lower() in {"1", "true", "yes", "on"}


def drive_folder_for_path(path: str | Path, *, env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    root = values.get("GOOGLE_DRIVE_REPORT_FOLDER") or DEFAULT_REPORT_FOLDER
    text = str(path).replace("\\", "/").lower()
    name = Path(path).name.lower()
    if "handoff" in name:
        child = "Handoffs"
    elif "feature" in text or "_features" in name or "_labels" in name:
        child = "Feature Exports"
    elif "audit" in name or "diagnostic" in name:
        child = "Audits"
    elif "crypto_runs" in text:
        child = "Crypto"
    elif "sports_runs" in text:
        child = "Sports"
    elif "paper_runs" in text or "kalshi" in text:
        child = "Kalshi"
    else:
        child = ""
    return f"{root}/{child}" if child else root


def archive_files(
    paths: Iterable[str | Path],
    *,
    env: Mapping[str, str] | None = None,
    uploader: ArchiveUploader | None = None,
) -> dict[str, Any]:
    if not google_drive_enabled(env) or uploader is None:
        return {
            "status": "archive_skipped_google_drive_unavailable",
            "uploaded_count": 0,
            "failed_count": 0,
            "uploaded": [],
            "failures": [],
        }
    uploaded: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for item in paths:
        path = Path(item)
        if not path.exists():
            failures.append({"path": str(path), "reason": "missing_local_file"})
            continue
        folder = drive_folder_for_path(path, env=env)
        try:
            uploaded.append({"path": str(path), "folder": folder, "result": uploader(path, folder)})
        except Exception as exc:  # noqa: BLE001 - connector failure must not break bot cycles
            failures.append({"path": str(path), "folder": folder, "reason": str(exc)})
    return {
        "status": "archive_complete" if not failures else "archive_partial_failure",
        "uploaded_count": len(uploaded),
        "failed_count": len(failures),
        "uploaded": uploaded,
        "failures": failures,
    }


def default_report_paths() -> list[Path]:
    roots = [repo_path("data", "paper_runs"), repo_path("data", "crypto_runs"), repo_path("data", "sports_runs")]
    patterns = ["*_report.txt", "*_audit.txt", "*_diagnostic.txt", "*_features.csv", "*_labels.csv"]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            files.extend(sorted(root.glob(pattern)))
    handoff = repo_path("data", "connectors_handoff.md")
    if handoff.exists():
        files.append(handoff)
    return files
