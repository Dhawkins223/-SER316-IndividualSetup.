from __future__ import annotations

from ..config import load_json


class SourceBot:
    def load_enabled_sources(self, config_path: str) -> list[dict]:
        config = load_json(config_path)
        sources = config.get("sources", [])
        return [source for source in sources if source.get("enabled", True)]

    def validate_source(self, source: dict) -> None:
        if "name" not in source or "kind" not in source:
            raise ValueError("source requires name and kind")
        if source["kind"] in {"rss", "web_page"} and "url" not in source:
            raise ValueError(f"{source['name']} requires url")
        if source["kind"] == "kalshi" and "base_url" not in source:
            raise ValueError(f"{source['name']} requires base_url")
