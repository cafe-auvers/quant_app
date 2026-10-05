"""Exercise removal feedback and poll races in an isolated mobile browser."""
from __future__ import annotations

from pathlib import Path
import asyncio
import sys
from urllib.parse import urlparse

import pytest

from src.web.assets import ReleaseAssets


@pytest.fixture
def browser_event_loop():
    previous = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)


@pytest.mark.parametrize("width", [390, 320])
def test_mobile_removal_keeps_rejection_visible_and_ignores_stale_poll(tmp_path, width, browser_event_loop):
    playwright = pytest.importorskip("playwright.sync_api")
    root = Path(__file__).resolve().parents[2]
    static = root / "src/web/static"
    assets = ReleaseAssets(static, root / "src/ui/static/vendor")
    row = {
        "symbol": "BLKB", "name": "Blackbaud", "board_status": "BUYLIST",
        "version": 1, "breakout_price": 45.6, "kanban_priority": 0,
    }
    session = {
        "mode": "CONNECTED", "csrf_token": "test",
        "operator": {"delegated": True, "operations": ["remove_buylist"]},
        "market_status": {"state": "CLOSED", "phase": "CLOSED"},
    }
    state = {
        "reject": True, "removed": False, "hold_poll": False,
        "held": None, "board_reads": 0, "failed_refreshes": 0, "posts": [],
    }
    errors = []

    def handle(route):
        path = urlparse(route.request.url).path
        if path == "/api/v1/operator/board-actions":
            state["posts"].append(route.request.post_data_json)
            if state["reject"]:
                route.fulfill(status=409, json={"detail": "Planning membership cannot change while position evidence exists"})
            else:
                state["removed"] = True
                route.fulfill(json={"queued": False, "card": None, "board_revision": "2"})
            return
        if path == "/api/v1/buyboard":
            state["board_reads"] += 1
            snapshot = {"rows": [] if state["removed"] else [row], "revision": "2" if state["removed"] else "1"}
            if state["hold_poll"]:
                state["hold_poll"] = False
                state["held"] = (route, snapshot)
                return
            route.fulfill(json=snapshot)
            return
        if path.startswith("/api/"):
            if path.endswith("session") or path.endswith("status"):
                value = session
            elif path == "/api/v1/planning" and state["removed"]:
                state["failed_refreshes"] += 1
                route.fulfill(status=502, json={"detail": "Read refresh unavailable"})
                return
            else:
                value = {"rows": []}
            route.fulfill(json=value)
            return
        if path == "/":
            route.fulfill(body=assets.render_page(static / "dashboard.html"), content_type="text/html")
        elif path.startswith("/release-static/"):
            name = path.rsplit("/", 1)[-1]
            _digest, body, content_type = assets.assets[name]
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
            page.locator(".mobile-kanban-card").wait_for()
            page.locator(".mobile-kanban-card").click()
            page.get_by_role("button", name="Remove from Buylist", exact=True).click()
            page.wait_for_function("document.querySelector('#buy-board-action-status').textContent.includes('Not saved:')")
            assert page.locator("#buy-board-action-sheet").is_visible()
            assert page.locator(".mobile-kanban-card").count() == 1
            # A later successful poll must not erase the rejection.
            before = state["board_reads"]
            page.evaluate("document.querySelector('#buy-board-refresh').click()")
            page.wait_for_function("!document.querySelector('.mobile-kanban-card').classList.contains('pending')")
            page.wait_for_timeout(100)
            assert state["board_reads"] > before
            assert "Not saved:" in page.locator("#buy-board-action-status").inner_text()
            assert page.locator("#buy-board-action-status").evaluate("""node => {
                const r = node.getBoundingClientRect();
                const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
                return r.top >= 0 && r.bottom <= window.innerHeight && node.contains(top);
            }""")
            page.screenshot(path=str(tmp_path / f"removal-rejected-{width}.png"))
            # Hold a snapshot taken before the successful removal, then return
            # it after confirmation while the secondary planning read fails.
            state["reject"] = False
            state["hold_poll"] = True
            page.evaluate("document.querySelector('#buy-board-refresh').click()")
            page.wait_for_timeout(100)
            assert state["held"] is not None
            page.get_by_role("button", name="Remove from Buylist", exact=True).click()
            page.locator("#buy-board-action-sheet").wait_for(state="hidden")
            held_route, stale = state["held"]
            held_route.fulfill(json=stale)
            page.wait_for_timeout(200)
            assert state["failed_refreshes"] == 1
            assert page.locator(".mobile-kanban-card").count() == 0
            assert page.locator("#buy-board-column-count").inner_text() == "0"
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert [post["action"] for post in state["posts"]] == ["remove_buylist", "remove_buylist"]
            assert not errors, errors
            page.screenshot(path=str(tmp_path / f"removal-confirmed-{width}.png"))
        finally:
            browser.close()
