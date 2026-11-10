from pathlib import Path
import re
from urllib.parse import urlparse

import pytest

from src.web.assets import ReleaseAssets
from .test_mobile_list_addition import list_browser


def open_partial_sell(page, *, reject=False):
    root = Path(__file__).resolve().parents[2]
    static = root / "src/web/static"
    assets = ReleaseAssets(static, root / "src/ui/static/vendor")
    row = {"symbol": "TEST", "name": "TEST", "board_status": "OPEN_POSITION",
           "version": 1, "broker_quantity": 100, "orderable_quantity": 100,
           "average_entry_price": 3.0, "active_stop_price": 2.8}
    state = {"posts": [], "errors": [], "row": row}
    session = {"mode": "CONNECTED", "csrf_token": "test",
               "operator": {"delegated": True, "operations": ["request_partial_sell", "set_manual_stop"]},
               "market_status": {"state": "OPEN", "phase": "REGULAR"}}

    def handle(route):
        path = urlparse(route.request.url).path
        if path == "/api/v1/operator/board-actions":
            payload = route.request.post_data_json
            state["posts"].append(payload)
            if reject:
                route.fulfill(status=409, json={"detail": "Outstanding KIS sell must be reconciled"})
            else:
                row.update(board_status="PARTIAL_SELL", version=2,
                           pending_partial_sell_quantity=payload["quantity"],
                           pending_partial_sell_price=payload.get("price"))
                route.fulfill(json={"queued": False, "card": row, "board_revision": "2"})
        elif path == "/api/v1/buyboard":
            route.fulfill(json={"rows": [row], "revision": str(row["version"])})
        elif path.startswith("/api/"):
            route.fulfill(json=session if path.endswith("session") or path.endswith("status") else {"rows": []})
        elif path == "/":
            route.fulfill(body=assets.render_page(static / "dashboard.html"), content_type="text/html")
        elif path.startswith("/release-static/"):
            _, body, content_type = assets.assets[path.rsplit("/", 1)[-1]]
            route.fulfill(body=body, content_type=content_type)
        else:
            route.fulfill(status=404)

    page.route("**/*", handle)
    page.on("pageerror", lambda error: state["errors"].append(str(error)))
    page.goto("http://localhost:8779/")
    page.get_by_role("button", name="Buy Board", exact=True).click()
    page.get_by_role("tab", name=re.compile(r"^Open ")).click()
    page.locator(".mobile-kanban-card").click()
    page.get_by_role("button", name="Partial Sell", exact=True).click()
    return state


@pytest.mark.parametrize("width", [320, 390])
@pytest.mark.parametrize("price", [None, "3.50"])
def test_mobile_partial_sell_optional_price_and_confirmation(list_browser, width, price):
    page = list_browser.new_page(viewport={"width": width, "height": 844}, is_mobile=True, has_touch=True)
    state = open_partial_sell(page)
    page.locator("#buy-board-action-value").fill("30")
    if price:
        page.locator("#buy-board-partial-price").fill(price)
    assert page.locator("#buy-board-partial-price-fields").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    if width == 390 and price:
        screenshot = Path(__file__).resolve().parents[2] / "tmp" / "partial_sell_mobile.png"
        screenshot.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(screenshot), full_page=True)
    page.locator("#buy-board-action-input-submit").click()
    copy = page.locator("#operator-confirm-copy").inner_text()
    assert ("$3.50 or higher" if price else "immediately") in copy
    assert state["posts"] == []
    page.locator("#operator-confirm-submit").click()
    page.wait_for_function("document.querySelector('#buy-board-action-status').textContent === 'Saved.'")
    assert len(state["posts"]) == 1
    assert state["posts"][0]["quantity"] == 30
    assert state["posts"][0].get("price") == (3.5 if price else None)
    assert state["posts"][0]["expected_revision"] == 1
    assert not state["errors"]
    page.close()


def test_invalid_limit_stays_in_editor_and_rejection_does_not_show_a_saved_exit(list_browser):
    page = list_browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
    state = open_partial_sell(page, reject=True)
    page.locator("#buy-board-action-value").fill("30")
    page.locator("#buy-board-partial-price").fill("3.505")
    page.locator("#buy-board-action-input-submit").click()
    assert state["posts"] == []
    assert page.locator("#buy-board-action-input").is_visible()
    page.locator("#buy-board-partial-price").fill("3.50")
    page.locator("#buy-board-action-input-submit").click()
    page.locator("#operator-confirm-submit").click()
    page.wait_for_function("document.querySelector('#buy-board-action-status').textContent.includes('Not saved')")
    assert state["row"]["board_status"] == "OPEN_POSITION"
    assert not state["errors"]
    page.close()
