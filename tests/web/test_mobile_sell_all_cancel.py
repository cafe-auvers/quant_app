"""Exercise regular-session exit withdrawal in an isolated mobile browser."""
from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import urlparse

import pytest

from src.web.assets import ReleaseAssets


@pytest.mark.parametrize("width", [320, 390])
@pytest.mark.parametrize("submitted", [False, True])
def test_mobile_cancel_sell_all_uses_execution_state_not_session_flag(
    tmp_path, width, submitted, browser_event_loop
):
    playwright = pytest.importorskip("playwright.sync_api")
    root = Path(__file__).resolve().parents[2]
    static = root / "src/web/static"
    assets = ReleaseAssets(static, root / "src/ui/static/vendor")
    row = {
        "symbol": "SVIA", "name": "SVIA", "board_status": "SELL_ALL",
        "version": 1, "broker_quantity": 643, "orderable_quantity": 643,
        "average_entry_price": 4.35, "active_stop_price": 4.24,
        "sell_all_at_market_open": False, "exit_order_pending": submitted,
        "can_cancel_sell_all": not submitted,
    }
    session = {
        "mode": "CONNECTED", "csrf_token": "test",
        "operator": {"delegated": True, "operations": ["cancel_sell_all"]},
        "market_status": {"state": "OPEN", "phase": "REGULAR"},
    }
    posts = []
    errors = []

    def handle(route):
        path = urlparse(route.request.url).path
        if path == "/api/v1/operator/board-actions":
            posts.append(route.request.post_data_json)
            row.update(board_status="OPEN_POSITION", version=2, can_cancel_sell_all=False)
            route.fulfill(json={"queued": False, "card": row, "board_revision": "2"})
        elif path == "/api/v1/buyboard":
            route.fulfill(json={"rows": [row], "revision": str(row["version"])})
        elif path.startswith("/api/"):
            value = session if path.endswith("session") or path.endswith("status") else {"rows": []}
            route.fulfill(json=value)
        elif path == "/":
            route.fulfill(body=assets.render_page(static / "dashboard.html"), content_type="text/html")
        elif path.startswith("/release-static/"):
            _digest, body, content_type = assets.assets[path.rsplit("/", 1)[-1]]
            route.fulfill(body=body, content_type=content_type)
        else:
            route.fulfill(status=404)

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 844}, is_mobile=True, has_touch=True)
        page.route("**/*", handle)
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto("http://localhost:8779/")
            page.get_by_role("button", name="Buy Board", exact=True).click()
            page.get_by_role("tab", name=re.compile(r"Sell All")).click()
            page.locator(".mobile-kanban-card").click()
            cancel = page.get_by_role("button", name="Cancel Sell All", exact=True)
            if submitted:
                assert cancel.is_disabled()
                assert "unsubmitted" in cancel.get_attribute("title")
                assert posts == []
            else:
                assert cancel.is_enabled()
                cancel.click()
                page.wait_for_function("document.querySelector('#buy-board-action-status').textContent === 'Saved.'")
                assert page.locator("#buy-board-action-stage").inner_text().upper() == "OPEN POSITIONS"
                page.locator("#buy-board-action-close").click()
                card = page.locator('.mobile-kanban-card[data-status="OPEN_POSITION"]')
                card.wait_for()
                assert "643 @ 4.35" in card.inner_text()
                assert len(posts) == 1 and posts[0]["action"] == "cancel_sell_all"
                assert posts[0]["expected_revision"] == 1
            assert not errors, errors
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(tmp_path / f"sell-all-cancel-{width}-{submitted}.png"))
        finally:
            browser.close()
