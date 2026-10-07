"""Show confirmed purchases on the chart and keep Buy Today review continuity."""
from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.web.assets import ReleaseAssets


def open_purchase_list(page):
    root = Path(__file__).resolve().parents[2]
    static = root / "src/web/static"
    assets = ReleaseAssets(static, root / "src/ui/static/vendor")
    cards = {
        symbol: {
            "symbol": symbol, "name": f"Company {symbol}", "version": 1, "card_version": 1,
            "stage": "BUYLIST", "canonical_stage": stage, "display_stage": stage,
            "watchlist_member": True, "buylist_member": True,
            "buy_today_member": stage == "BUY_TODAY", "buy_today_display_member": True,
            "breakout_price": 12, "broker_quantity": quantity,
            "entry_remaining_target_quantity": remaining, "average_entry_price": 12.5 if quantity else None,
            "purchase_status": status,
        }
        for symbol, stage, quantity, remaining, status in (
            ("WAIT", "BUY_TODAY", 0, 10, ""), ("ENTRY", "ENTRY_PENDING", 0, 10, "ENTRY_PENDING"),
            ("PART", "OPEN_POSITION", 3, 7, "PARTIALLY_BOUGHT"),
            ("FILL", "OPEN_POSITION", 10, 0, "BOUGHT"), ("SOLD", "CLOSED", 0, 0, "SOLD"),
        )
    }
    state = {"cards": cards, "revision": "1", "errors": [], "mutations": []}
    session = {
        "mode": "CONNECTED", "csrf_token": "test", "planning_writable": True,
        "planning_revision": "1", "market_status": {"phase": "REGULAR"},
        "operator": {"delegated": True, "operations": ["activate_buy_today", "deactivate_buy_today"]},
    }

    def handle(route):
        path = urlparse(route.request.url).path
        if route.request.method != "GET":
            state["mutations"].append(path)
            route.fulfill(status=405)
        elif path.startswith("/api/"):
            if path.endswith("session") or path.endswith("status"):
                value = {**session, "planning_revision": state["revision"]}
            elif path == "/api/v1/planning" or path == "/api/v1/buy-today-drafts":
                value = {"rows": list(cards.values()), "revision": state["revision"]}
            elif path.startswith("/api/v1/planning/"):
                value = {"card": cards[path.rsplit("/", 1)[-1]]}
            elif path.startswith("/api/v1/scanner"):
                value = {"rows": list(cards.values()), "total": len(cards)}
            else:
                value = {"rows": []}
            route.fulfill(json=value)
        elif path == "/":
            route.fulfill(body=assets.render_page(static / "dashboard.html"), content_type="text/html")
        elif path.startswith("/release-static/"):
            _, body, content_type = assets.assets[path.rsplit("/", 1)[-1]]
            route.fulfill(body=body, content_type=content_type)
        else:
            route.fulfill(status=404)

    page.route("**/*", handle)
    page.on("pageerror", lambda error: state["errors"].append(str(error)))
    page.goto("http://localhost:8781/")
    page.wait_for_function("document.querySelector('#quick-watchlist').getAttribute('aria-pressed') === 'true'")
    if page.viewport_size["width"] <= 900:
        page.locator("#mobile-list-menu").click()
        page.locator('[data-mobile-list="buy_today"]').click()
    else:
        page.locator('.list-tab[data-list="buy_today"]').click()
    return state


def select_purchase(page, symbol):
    mobile = page.viewport_size["width"] <= 900
    if mobile and page.locator("#mobile-list-popover").is_hidden():
        page.locator("#mobile-list-menu").click()
    page.locator(f'[data-mobile-symbol="{symbol}"]' if mobile else f'.stock-row[data-symbol="{symbol}"]').click()
    page.wait_for_function("symbol => document.querySelector('#active-symbol').textContent === symbol", arg=symbol)


@pytest.mark.parametrize("width", [320, 390, 1400])
def test_purchase_badge_visible_on_chart_and_list_without_enabling_cancel(width, tmp_path, browser_event_loop):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 844})
        state = open_purchase_list(page)
        for symbol, status in (("FILL", "BOUGHT"), ("PART", "PARTIALLY_BOUGHT"), ("SOLD", "SOLD"), ("ENTRY", "ENTRY_PENDING")):
            selector = f'[data-mobile-symbol="{symbol}"]' if width <= 900 else f'.stock-row[data-symbol="{symbol}"]'
            assert page.locator(selector).locator('[data-purchase-status]').get_attribute("data-purchase-status") == status
            select_purchase(page, symbol)
            badge = page.locator("#chart-purchase-status")
            page.wait_for_function("status => document.querySelector('#chart-purchase-status').dataset.status === status", arg=status)
            assert badge.is_visible()
            assert "Bought" in badge.inner_text() or "bought" in badge.inner_text() or status == "ENTRY_PENDING"
            assert "Cancel Today" not in page.locator("#quick-buy-today").inner_text()
            if status != "SOLD":
                assert page.locator("#quick-buy-today").is_disabled()
            if symbol in {"FILL", "PART"}:
                assert "shares at $12.50 average" in badge.get_attribute("aria-label")
            # The badge must survive the narrow-screen company-name hiding rule.
            assert badge.evaluate("node => {const box=node.getBoundingClientRect(), parent=node.parentElement.getBoundingClientRect(); return box.width>0 && box.left>=parent.left && box.right<=parent.right+1;}")
            if width == 320 and symbol in {"FILL", "PART"}:
                page.screenshot(path=str(tmp_path / f"chart-{symbol.lower()}.png"))
            if width <= 900:
                page.locator("#mobile-list-menu").click()
        select_purchase(page, "WAIT")
        page.wait_for_function("document.querySelector('#chart-purchase-status').hidden")
        assert state["errors"] == [] and state["mutations"] == []
        browser.close()


def test_live_fill_refresh_keeps_selected_buy_today_chart_and_marks_partial_then_bought(browser_event_loop):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.clock.install()
        state = open_purchase_list(page)
        select_purchase(page, "WAIT")
        page.wait_for_function("document.querySelector('#chart-purchase-status').hidden")
        for revision, quantity, remaining, status in ((2, 3, 7, "PARTIALLY_BOUGHT"), (3, 10, 0, "BOUGHT")):
            state["revision"] = str(revision)
            state["cards"]["WAIT"].update(
                version=revision, card_version=revision, broker_quantity=quantity,
                entry_remaining_target_quantity=remaining, average_entry_price=12.5,
                canonical_stage="OPEN_POSITION", display_stage="OPEN_POSITION",
                buy_today_member=False, purchase_status=status,
            )
            page.clock.fast_forward(10_000)
            page.wait_for_function("status => document.querySelector('#chart-purchase-status').dataset.status === status", arg=status)
            assert page.locator("#active-symbol").inner_text() == "WAIT"
            assert page.locator('[data-mobile-list="buy_today"]').get_attribute("aria-selected") == "true"
            assert page.locator("#quick-buy-today").is_disabled()
        page.locator("#mobile-list-menu").click()
        assert page.locator('[data-mobile-symbol="WAIT"] [data-purchase-status="BOUGHT"]').is_visible()
        assert state["errors"] == [] and state["mutations"] == []
        browser.close()
