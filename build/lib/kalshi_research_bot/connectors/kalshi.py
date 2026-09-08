from __future__ import annotations

from urllib.parse import urlencode

from .http import HttpClient
from .kalshi_catalog import KALSHI_PUBLIC_BASE_URL


class KalshiPublicClient:
    def __init__(self, base_url: str = KALSHI_PUBLIC_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = HttpClient()

    def markets(self, limit: int = 20, status: str = "open", query: str | None = None) -> dict:
        params: dict[str, str | int] = {"limit": limit, "status": status}
        if query:
            params["query"] = query
        url = f"{self.base_url}/markets?{urlencode(params)}"
        return self.http.get_text(url).json()

    def structured_targets(
        self,
        *,
        target_type: str,
        page_size: int = 1000,
        cursor: str | None = None,
    ) -> dict:
        params: dict[str, str | int] = {"type": target_type, "page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        return self.http.get_text(f"{self.base_url}/structured_targets?{urlencode(params)}").json()

    def milestones(
        self,
        *,
        category: str = "Sports",
        page_size: int = 200,
        cursor: str | None = None,
    ) -> dict:
        # Unlike structured_targets, the milestones endpoint names its page
        # length parameter `limit`.
        params: dict[str, str | int] = {"category": category, "limit": page_size}
        if cursor:
            params["cursor"] = cursor
        return self.http.get_text(f"{self.base_url}/milestones?{urlencode(params)}").json()

    def event_metadata(self, event_ticker: str) -> dict:
        safe_ticker = str(event_ticker).strip()
        if not safe_ticker or "/" in safe_ticker:
            raise ValueError("kalshi_event_ticker_required")
        return self.http.get_text(f"{self.base_url}/events/{safe_ticker}/metadata").json()
