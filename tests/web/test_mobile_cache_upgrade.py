"""Real HTTP/service-worker regression; also runnable with Playwright installed."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web.assets import ReleaseAssets
from src.web.market_data import DemoMarketDataSource


LEGACY_WORKER = """
const CACHE = 'quant-web-static-v81';
const ASSETS = ['/live-static/app.css', '/live-static/app.js'];
self.addEventListener('install', e => {e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS))); self.skipWaiting();});
self.addEventListener('activate', e => {e.waitUntil(self.clients.claim());});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method === 'GET' && u.origin === self.location.origin && ASSETS.includes(u.pathname))
    e.respondWith(caches.match(u.pathname).then(hit => hit || fetch(e.request)));
});
"""


def test_mobile_upgrade_bypasses_old_cache_and_recovers_price_timeout(tmp_path, browser_event_loop):
    playwright = pytest.importorskip("playwright.sync_api")
    static = ROOT / "src/web/static"
    vendor = ROOT / "src/ui/static/vendor"
    assets = ReleaseAssets(static, vendor)
    dashboard = assets.render_page(static / "dashboard.html")
    plans = [{"symbol": "NVDA", "name": "NVIDIA", "breakout_price": 180.0, "watchlist_member": True, "buylist_member": True, "version": 1}]
    quotes = [{**plans[0], "current_price": 185.25, "quote_as_of": "2026-10-02T16:00:00-04:00", "quote_status": "CLOSED", "breakout_status": "CLOSED", "broke_out_today": None, "orb": []}]
    session = {
        "mode": "SANDBOX", "csrf_token": "test", "operator": {"operations": []},
        "market_status": {"state": "CLOSED", "phase": "CLOSED", "compact_label": "Market Closed", "watchlist_session_date": "2026-10-05"},
        "executor_health": "HEALTHY", "executor_state": "ACTIVE", "executor_reason": "Executor heartbeat is current", "executor_hostname": "PC",
    }
    state = {"updated": False, "monitor_calls": 0, "hang": False, "urls": []}
    release_hang = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            state["urls"].append(path)
            content_type = "application/json"
            status = 200
            if path == "/":
                content_type = "text/html"
                body = dashboard if state["updated"] else "<script>navigator.serviceWorker.register('/service-worker.js')</script>"
            elif path == "/service-worker.js":
                content_type = "application/javascript"
                body = (static / "service_worker.js").read_text(encoding="utf-8") if state["updated"] else LEGACY_WORKER
            elif path.startswith("/release-static/"):
                digest, name = path.split("/")[2:]
                expected, body, content_type = assets.assets[name]
                assert digest == expected
            elif path == "/live-static/app.js":
                content_type, body = "application/javascript", "window.__legacyAssetLoaded = true;"
            elif path == "/live-static/app.css":
                content_type, body = "text/css", "body { background: red; }"
            elif path.startswith("/web-static/"):
                file = static / path.rsplit("/", 1)[-1]
                body = file.read_bytes()
                content_type = "image/svg+xml" if file.suffix == ".svg" else "application/manifest+json"
            elif path.startswith("/api/"):
                if path.endswith("session") or path.endswith("status"):
                    value = session
                elif path == "/api/v1/intraday-monitor":
                    state["monitor_calls"] += 1
                    if state["hang"]:
                        release_hang.wait(timeout=30)
                    pending = state["monitor_calls"] < 2
                    value = {"rows": [] if pending else quotes, "enabled": True, "as_of": None if pending else "2026-10-04T07:00:00-04:00", "refreshing": pending}
                elif path == "/api/v1/planning":
                    value = {"rows": plans}
                elif path == "/api/v1/buy-today-drafts":
                    value = {"rows": [{**plans[0], "card_version": 1}]}
                elif path.startswith("/api/v1/planning/"):
                    value = {"card": plans[0]}
                elif path.startswith("/api/v1/charts/"):
                    pieces = path.split("/")
                    value = DemoMarketDataSource().chart_bundle("NVDA", pieces[-1], daily_bars=100, hourly_months=1)
                elif path.startswith("/api/v1/scanner"):
                    value = {"rows": [{"symbol": "NVDA", "rank": 1}], "total": 1}
                else:
                    value = {"rows": []}
                body = json.dumps(value)
            else:
                status, body = 404, "{}"
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    errors = []
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin)
            page.evaluate("navigator.serviceWorker.ready")
            page.wait_for_function("navigator.serviceWorker.controller !== null")
            assert page.evaluate("caches.has('quant-web-static-v81')")
            # Demonstrate the exact bug: a newer query still returns old code.
            assert "__legacyAssetLoaded" in page.evaluate("fetch('/live-static/app.js?v=91').then(r => r.text())")
            page.evaluate("caches.open('unrelated-cache')")
            state["updated"] = True
            page.clock.install()
            with page.expect_response("**/api/v1/intraday-monitor") as initial:
                page.reload()
            assert initial.value.json()["as_of"] is None
            page.locator("#mobile-list-menu").click()
            page.wait_for_function("document.querySelector('.mobile-monitor-row') !== null")
            page.clock.fast_forward(5_001)
            page.wait_for_function("document.querySelector('#mobile-monitor-status').textContent.includes('Market closed')")
            assert not page.evaluate("Boolean(window.__legacyAssetLoaded)")
            assert "$185.25" in page.locator(".mobile-monitor-stock button").get_attribute("title")
            assert "$180" in page.locator(".mobile-monitor-breakout").get_attribute("title")
            assert page.locator(".mobile-monitor-row").evaluate("n => n.getBoundingClientRect().height <= 42")
            assert "good" in page.locator("#executor-dot").get_attribute("class")
            page.wait_for_function("caches.keys().then(keys => keys.includes('quant-web-static-v92') && !keys.includes('quant-web-static-v81'))")
            assert page.evaluate("caches.has('unrelated-cache')")
            page.screenshot(path=str(tmp_path / "cache-upgrade-390.png"))
            before = state["monitor_calls"]
            state["hang"] = True
            page.clock.fast_forward(60_001)
            # Wait for the request to reach the server before advancing its timeout.
            page.wait_for_timeout(100)
            assert state["monitor_calls"] > before
            page.clock.fast_forward(25_001)
            page.wait_for_function("document.querySelector('#mobile-monitor-status').textContent.includes('timed out')")
            state["hang"] = False
            release_hang.set()
            page.clock.fast_forward(60_001)
            page.wait_for_function("document.querySelector('#mobile-monitor-status').textContent.includes('Market closed')")
            page.set_viewport_size({"width": 320, "height": 740})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.locator(".mobile-monitor-row").evaluate("n => n.getBoundingClientRect().height <= 42")
            page.screenshot(path=str(tmp_path / "cache-upgrade-320.png"))
            assert any(url.startswith("/release-static/") for url in state["urls"])
            assert not errors, errors
            browser.close()
    finally:
        release_hang.set()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    output = ROOT / "artifacts/monitor_qa"
    output.mkdir(parents=True, exist_ok=True)
    test_mobile_upgrade_bypasses_old_cache_and_recovers_price_timeout(output)
    print(json.dumps({"checks": "passed", "legacy_cache_upgrade": True, "price_timeout_recovery": True, "widths": [390, 320]}))
