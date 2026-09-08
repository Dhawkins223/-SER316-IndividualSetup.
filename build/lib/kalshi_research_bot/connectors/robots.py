from __future__ import annotations

import urllib.parse
import urllib.robotparser


def robots_url_for(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))


def can_fetch(url: str, user_agent: str) -> bool:
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url_for(url))
    try:
        parser.read()
    except Exception:
        return False
    return parser.can_fetch(user_agent, url)
