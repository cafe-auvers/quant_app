"""Stage facts and price provenance without broker calls or board mutations."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from urllib.parse import urlparse

import pytest

from src.web.assets import ReleaseAssets


@pytest.mark.parametrize("width", [320, 390, 820])
def test_buyboard_stage_details_and_stale_prices(width, tmp_path, browser_event_loop):
    playwright = pytest.importorskip("playwright.sync_api")
    root = Path(__file__).resolve().parents[2]
    static = root / "src/web/static"
    assets = ReleaseAssets(static, root / "src/ui/static/vendor")
    now = datetime.now(timezone.utc)
    rows = []
    for symbol, stage in [("TODAY", "BUY_TODAY"), ("ENTRY", "ENTRY_PENDING"),
                          ("OPEN", "OPEN_POSITION"), ("PARTIAL", "PARTIAL_SELL"),
                          ("SELL", "SELL_ALL"), ("UNSIZED", "BUY_TODAY"), ("ODD", "BUYLIST")]:
        rows.append({
            "symbol": symbol, "name": symbol, "board_status": stage, "version": 1,
            "breakout_price": 10, "entry_execution_price": 10.1,
            "entry_orb_low": 9.5, "selected_orb_window": "5m",
            "target_position_quantity": 100 if symbol != "UNSIZED" else 0,
            "broker_quantity": 20 if symbol == "ENTRY" else 100 if stage in ("OPEN_POSITION", "PARTIAL_SELL", "SELL_ALL") else 0,
            "orderable_quantity": 75, "average_entry_price": 10,
            "active_stop_price": 9.5, "stop_quantity": 100,
            "pending_partial_sell_quantity": 25, "reserved_sell_quantity": 25,
            "exit_order_pending": stage == "PARTIAL_SELL",
            "last_reported_price": 10.5 if symbol != "UNSIZED" else None,
            "price_as_of": (now - timedelta(seconds=10 if symbol not in ("SELL", "OPEN") else 500)).isoformat(),
            "entry_block_reason": "Waiting for fresh quote and trade events" if symbol == "TODAY" else "",
            "warnings": ["migrated_from_buylist"] if symbol == "TODAY" else [],
            "last_exit_error": "Live Trading is OFF" if symbol == "SELL" else "",
            "buy_today_note": "Buy Today rejected - all ORB plans invalid. 30m: invalid tick" if symbol == "ODD" else "",
        })
    monitor = [{"symbol": "OPEN", "current_price": 11, "quote_as_of": now.isoformat(), "quote_status": "CURRENT"}]
    session = {"mode": "CONNECTED", "csrf_token": "test",
               "market_status": {"state": "OPEN", "phase": "REGULAR"}}
    errors, mutations = [], []

    def handle(route):
        path = urlparse(route.request.url).path
        if route.request.method != "GET":
            mutations.append(path)
            route.fulfill(status=405)
        elif path == "/api/v1/buyboard":
            route.fulfill(json={"rows": rows, "revision": "1"})
        elif path == "/api/v1/intraday-monitor":
            route.fulfill(json={"rows": monitor, "as_of": now.isoformat(), "enabled": True})
        elif path.startswith("/api/"):
            route.fulfill(json=session if path.endswith("session") or path.endswith("status") else {"rows": []})
        elif path == "/":
            route.fulfill(body=assets.render_page(static / "dashboard.html"), content_type="text/html")
        elif path.startswith("/release-static/"):
            _, body, content_type = assets.assets[path.rsplit("/", 1)[-1]]
            route.fulfill(body=body, content_type=content_type)
        else:
            route.fulfill(status=404)

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 844}, is_mobile=True)
            page.route("**/*", handle)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://localhost:8779/")
            page.get_by_role("button", name="Buy Board", exact=True).click()
            for tab, symbol, expected in [
                ("Buylist", "ODD", ["Buy Today rejected", "30m: invalid tick"]),
                ("Today", "TODAY", ["Target shares", "100 sh", "Planned stop", "$9.50", "Entry plan", "$10.10", "Planned risk", "$60.00", "Waiting for fresh quote"]),
                ("Entry", "ENTRY", ["Held / target", "20 / 100 sh", "Average fill", "$10.00"]),
                ("Open", "OPEN", ["Held", "100 sh", "Sellable", "75 sh", "Active stop", "100 / 100 sh", "$11.00", "+$100.00 (+10.00%)", "Yahoo", "indicative"]),
                ("Partial", "PARTIAL", ["Sell requested", "25 sh", "Working sell"]),
                ("Sell All", "SELL", ["Not submitted", "stale", "Price stale", "Live Trading is OFF"]),
            ]:
                page.get_by_role("tab", name=re.compile(rf"^{tab} ")).click()
                card = page.locator(".mobile-kanban-card").filter(has_text=re.compile(rf"^{symbol}"))
                card.wait_for()
                text = card.inner_text()
                for value in expected:
                    assert value in text, (symbol, value, text)
                assert "KST" in text
                if symbol == "TODAY":
                    assert page.locator("#buy-board-column-summary").inner_text() == "2 stocks · 100 target shares · 1 not sized"
                card.click()
                sheet = page.locator("#buy-board-action-facts").inner_text().lower()
                assert "price" in sheet
                if tab != "Buylist":
                    assert ("planned stop" if tab in ("Today", "Entry") else "active stop") in sheet
                if symbol == "ODD":
                    assert "Buy Today rejected" in page.locator("#buy-board-action-warning").inner_text()
                if symbol == "TODAY":
                    assert "Waiting for fresh quote" in page.locator("#buy-board-action-warning").inner_text()
                page.locator("#buy-board-action-close").click()
            page.get_by_role("tab", name=re.compile(r"^Today ")).click()
            unknown = page.locator(".mobile-kanban-card").filter(has_text=re.compile(r"^UNSIZED"))
            assert "Not sized" in unknown.inner_text()
            assert "Price not reported yet" in unknown.inner_text()
            assert not errors and not mutations
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert page.locator(".mobile-kanban-card-details").evaluate_all("items => items.every(item => item.scrollWidth <= item.clientWidth)")
            page.screenshot(path=str(tmp_path / f"buyboard-{width}.png"))
        finally:
            browser.close()
