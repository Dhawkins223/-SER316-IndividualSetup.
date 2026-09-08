from __future__ import annotations

from ..connectors import KalshiPublicClient, RssCollector, WebPageCollector
from ..contracts import SourceRecord
from .source_bot import SourceBot


class ScrapeBot:
    def __init__(self) -> None:
        self.source_bot = SourceBot()
        self.rss = RssCollector()
        self.web_page = WebPageCollector()

    def collect(self, config_path: str) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for source in self.source_bot.load_enabled_sources(config_path):
            self.source_bot.validate_source(source)
            kind = source["kind"]
            if kind == "rss":
                records.extend(self.rss.fetch(source["name"], source["url"]))
            elif kind == "web_page":
                records.append(
                    self.web_page.fetch(
                        source["name"],
                        source["url"],
                        respect_robots=source.get("respect_robots", True),
                    )
                )
            elif kind == "kalshi":
                client = KalshiPublicClient(source["base_url"])
                payload = client.markets(limit=source.get("limit", 10), query=source.get("query"))
                records.append(
                    SourceRecord(
                        source=source["name"],
                        kind="kalshi",
                        url=source["base_url"],
                        title="Kalshi public markets",
                        text=str(payload)[:4000],
                        metadata={"market_count": len(payload.get("markets", []))},
                    )
                )
            else:
                raise ValueError(f"unsupported source kind: {kind}")
        return records
