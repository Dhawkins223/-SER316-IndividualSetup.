from __future__ import annotations

import xml.etree.ElementTree as ET

from ..contracts import SourceRecord
from .http import HttpClient


class RssCollector:
    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    def fetch(self, source_name: str, url: str, limit: int = 20) -> list[SourceRecord]:
        response = self.http.get_text(url)
        root = ET.fromstring(response.text)
        records: list[SourceRecord] = []
        for item in root.findall(".//item")[:limit]:
            title = item.findtext("title") or ""
            link = item.findtext("link") or url
            description = item.findtext("description") or ""
            records.append(
                SourceRecord(
                    source=source_name,
                    kind="rss",
                    url=link,
                    title=title.strip(),
                    text=description.strip(),
                )
            )
        return records
