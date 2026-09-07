"""Exercise the real dashboard and bundled assets in Chromium, without a database.

Run with SMOKE=1 python scripts/browser_smoke.py after installing .[browser]
and `python -m playwright install chromium`. Nothing contacts a hosted service.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from kalshi_research_bot.auth import AuthPrincipal
from kalshi_research_bot.browser_fixtures import (
    BROWSER_FIXTURE_STATES,
    browser_fixture_refresh_status,
    build_browser_fixture_payload,
    make_verified_fixture_payload,
    make_fixture_sports_board,
    make_fixture_sports_clv_report,
    make_fixture_research_record,
)
from kalshi_research_bot.dashboard_assets import ASSETS
from kalshi_research_bot.paper_server import render_dashboard

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "tests" / "browser" / "vendor"
ROLES = ("read_only", "admin")
WIDTHS = (1440, 768, 390, 320)


def fixture_pages() -> dict[str, bytes]:
    pages = {}
    for role in ROLES:
        for state in BROWSER_FIXTURE_STATES:
            payload = build_browser_fixture_payload(make_verified_fixture_payload(), state)
            board = make_fixture_sports_board()
            clv = make_fixture_sports_clv_report()
            record = make_fixture_research_record()
            if state in {"empty", "stale", "error"}:
                board.update(events=[], event_count=0, is_current=False, board_state=state)
                clv.update(graded_rows=0, by_market=[], by_bookmaker=[])
            with patch("kalshi_research_bot.paper_server.safe_sports_board", return_value=board), \
                 patch("kalshi_research_bot.paper_server.safe_sports_clv_report", return_value=clv), \
                 patch("kalshi_research_bot.paper_server.build_research_record", return_value=record):
                pages[f"/{role}/{state}"] = render_dashboard(
                    payload,
                    principal=AuthPrincipal(username=role, role=role, auth_method="local_unprotected"),
                ).encode()
    return pages


def assert_no_overflow(page) -> None:
    sizes = page.evaluate("({page: document.documentElement.scrollWidth, viewport: innerWidth})")
    assert sizes["page"] == sizes["viewport"], f"horizontal overflow: {sizes}"


def check_accessibility(page) -> None:
    results = page.evaluate("async () => (await axe.run(document)).violations")
    failures = [
        {"id": item["id"], "impact": item["impact"], "targets": [node["target"] for node in item["nodes"]]}
        for item in results if item["impact"] in {"serious", "critical"}
    ]
    assert not failures, json.dumps(failures)


def exercise_sheet(page, *, check_a11y: bool) -> None:
    toggle = page.locator("#mobile-slip-toggle")
    drawer = page.locator("#prediction-drawer")
    assert drawer.is_hidden(), "mobile slip must start collapsed"
    toggle.focus()
    page.keyboard.press("Enter")
    assert drawer.is_visible()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert page.locator("#prediction-drawer-title").evaluate("node => node === document.activeElement")
    page.keyboard.press("Shift+Tab")
    assert drawer.evaluate("node => node.contains(document.activeElement)"), "focus escaped the slip"
    page.keyboard.press("Tab")
    assert drawer.evaluate("node => node.contains(document.activeElement)")
    assert_no_overflow(page)
    if check_a11y:
        check_accessibility(page)
    page.keyboard.press("Escape")
    assert drawer.is_hidden()
    assert toggle.get_attribute("aria-expanded") == "false"
    assert toggle.evaluate("node => node === document.activeElement"), "focus was not restored"
    toggle.click()
    page.locator("#close-prediction-drawer").click()
    assert drawer.is_hidden()
    # An in-page link must land on visible, focusable content after the sheet closes.
    toggle.click()
    page.locator('.drawer-header a[href="#primary"]').click()
    assert drawer.is_hidden()
    assert page.locator("#primary").evaluate("node => node === document.activeElement")


def main() -> int:
    if os.environ.get("SMOKE") != "1":
        print("Browser checks are opt-in locally: set SMOKE=1.")
        return 0
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    print("Rendering dashboard fixtures", flush=True)
    pages = fixture_pages()
    axe = gzip.decompress((VENDOR / "axe.min.js.gz").read_bytes())
    expected_hash = (VENDOR / "axe.sha256").read_text().strip()
    assert hashlib.sha256(axe).hexdigest() == expected_hash, "vendored axe-core checksum mismatch"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            asset = ASSETS.get(path)
            if asset:
                body, media = asset.body, asset.content_type
            elif path == "/__smoke__/axe.min.js":
                body, media = axe, "text/javascript"
            elif path == "/refresh-status":
                state = urlsplit(self.headers.get("Referer", "")).path.rsplit("/", 1)[-1]
                status = browser_fixture_refresh_status(state if state in BROWSER_FIXTURE_STATES else "live")
                body, media = json.dumps(status).encode(), "application/json"
            elif path == "/freshness.json":
                body, media = b'{"status":"ready"}', "application/json"
            elif path in pages:
                body, media = pages[path], "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    artifact_dir = Path(os.environ.get("SMOKE_ARTIFACT_DIR") or tempfile.mkdtemp(prefix="hawknetic-browser-"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    checked = []
    try:
        with sync_playwright() as playwright:
            launch = {"headless": True, "timeout": 15000}
            if os.environ.get("CHROMIUM_EXECUTABLE"):
                launch["executable_path"] = os.environ["CHROMIUM_EXECUTABLE"]
                launch["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
            browser = playwright.chromium.launch(**launch)
            print("Chromium started", flush=True)
            for role in ROLES:
                for state in BROWSER_FIXTURE_STATES:
                    for width in WIDTHS:
                        label = f"{role}/{state}/{width}"
                        print(f"Checking {label}", flush=True)
                        context = browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
                        page = context.new_page()
                        page.set_default_timeout(10000)
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                        page.goto(f"{origin}/{role}/{state}", wait_until="networkidle")
                        page.evaluate("document.fonts.ready")
                        page.add_script_tag(url=f"{origin}/__smoke__/axe.min.js")
                        try:
                            assert_no_overflow(page)
                            check_accessibility(page)
                            if width <= 900:
                                exercise_sheet(page, check_a11y=width == 320)
                            assert not errors, errors
                        except Exception as error:
                            page.screenshot(path=str(artifact_dir / f"failure-{role}-{state}-{width}.png"), full_page=True)
                            raise AssertionError(f"{label}: {error}") from error
                        if state == "live" and width in {1440, 390}:
                            page.screenshot(path=str(artifact_dir / f"{role}-{width}.png"), full_page=True)
                        checked.append(label)
                        context.close()

            # Prove the overflow gate fails on a known defect, not just that
            # today's markup passes. This exists only in the disposable page.
            page = browser.new_page(viewport={"width": 390, "height": 900})
            page.goto(f"{origin}/read_only/live")
            page.evaluate("document.documentElement.style.minWidth = '395px'")
            try:
                assert_no_overflow(page)
            except AssertionError:
                pass
            else:
                raise AssertionError("the deliberate 5px overflow was not detected")
            page.close()

            for role in ROLES:
                context = browser.new_context(java_script_enabled=False, viewport={"width": 320, "height": 900})
                page = context.new_page()
                page.goto(f"{origin}/{role}/live")
                assert page.locator("#prediction-drawer").is_visible()
                assert page.locator("#mobile-slip-toggle").is_hidden()
                assert page.locator('a[href="#prediction-drawer"]').count() > 0
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    elapsed = time.monotonic() - started
    assert elapsed < 180, f"browser checks exceeded three minutes: {elapsed:.1f}s"
    (artifact_dir / "results.json").write_text(json.dumps({"checks": checked, "seconds": elapsed}, indent=2))
    print(f"Passed {len(checked)} role/state/width checks, mobile keyboard checks, no-JS fallback, and overflow negative control in {elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
