from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def repo_path(*parts: str) -> Path:
    if parts and parts[0] == "data":
        data_dir = os.getenv("RESEARCH_DATA_DIR")
        if data_dir:
            return Path(data_dir).expanduser().joinpath(*parts[1:])
    return Path(__file__).resolve().parents[2].joinpath(*parts)
