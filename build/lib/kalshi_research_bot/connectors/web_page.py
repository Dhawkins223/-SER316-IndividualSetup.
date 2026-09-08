from __future__ import annotations

from html.parser import HTMLParser

from ..contracts import SourceRecord
from .http import HttpClient
from .robots import can_fetch


class TextExtractingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self.in_title:
            self.title_parts.append(cleaned)
        elif len(cleaned) > 2:
            self.text_parts.append(cleaned)


class WebPageCollector:
    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    def fetch(self, source_name: str, url: str, respect_robots: bool = True) -> SourceRecord:
        if respect_robots and not can_fetch(url, self.http.user_agent):
            raise PermissionError(f"robots.txt does not allow fetching {url}")
        response = self.http.get_text(url)
        parser = TextExtractingParser()
        parser.feed(response.text)
        return SourceRecord(
            source=source_name,
            kind="web_page",
            url=url,
            title=" ".join(parser.title_parts).strip(),
            text=" ".join(parser.text_parts[:120]).strip(),
        )
