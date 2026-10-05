"""Exercise Watchlist additions and rejection feedback without live trading."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from urllib.parse import urlparse

import pytest

from src.web.assets import ReleaseAssets


@pytest.fixture
def list_browser():
    playwright = pytest.importorskip("playwright.sync_api")
    previous = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            yield browser
            browser.close()
    finally:
        asyncio.set_event_loop_policy(previous)


def open_watchlist(page, *, delegated=True, reject=""):
    root = Path(__file__).resolve().parents[2]
    static = root / "src/web/static"
    assets = ReleaseAssets(static, root / "src/ui/static/vendor")
    card = {
        "symbol": "SHMD", "name": "SHMD", "stage": "WATCHLIST",
        "canonical_stage": "WATCHLIST", "watchlist_member": True,
        "buylist_member": False, "buy_today_member": False,
        "breakout_price": None, "version": 1,
    }
    state = {"card": card, "posts": [], "errors": [], "draft": False}
    session = {
        "mode": "CONNECTED", "csrf_token": "test", "planning_writable": True,
        "operator": {"delegated": delegated, "operations": ["activate_buy_today", "deactivate_buy_today"]},
        "market_status": {"state": "CLOSED", "phase": "CLOSED"},
    }

    def handle(route):
        path = urlparse(route.request.url).path
        if route.request.method == "POST":
            payload = route.request.post_data_json
            state["posts"].append((path, payload))
            operation = payload.get("operation", "")
            if reject and operation == reject:
                route.fulfill(status=409, json={"detail": "Stale planning revision", "current": dict(card)})
                return
            assert payload["expected_revision"] == card["version"]
            if operation == "set_breakout":
                card.update(breakout_price=payload["breakout_price"], version=card["version"] + 1)
            elif operation == "promote_buylist":
                card.update(stage="BUYLIST", canonical_stage="BUYLIST", buylist_member=True, version=card["version"] + 1)
            elif path.endswith("activate-buy-today"):
                assert payload["enabled"] is True
                card.update(canonical_stage="BUY_TODAY", buy_today_member=True, version=card["version"] + 1)
            elif path.endswith("buy-today-preview"):
                state["draft"] = True
            else:
                raise AssertionError(f"Unexpected mutation: {path}")
            route.fulfill(json={"card": dict(card), "status": "SAVED", "queued": False})
            return
        if path.startswith("/api/"):
            if path.endswith("session") or path.endswith("status"):
                value = session
            elif path == "/api/v1/planning":
                value = {"rows": [dict(card)]}
            elif path == "/api/v1/planning/SHMD":
                value = {"card": dict(card)}
            elif path == "/api/v1/buy-today-drafts":
                value = {"rows": [{**card, "card_version": card["version"]}] if state["draft"] else []}
            elif path.startswith("/api/v1/scanner"):
                value = {"rows": [{"symbol": "SHMD", "rank": 1}], "total": 1}
            else:
                value = {"rows": []}
            route.fulfill(json=value)
        elif path == "/":
            route.fulfill(body=assets.render_page(static / "dashboard.html"), content_type="text/html")
        elif path.startswith("/release-static/"):
            _digest, body, content_type = assets.assets[path.rsplit("/", 1)[-1]]
            route.fulfill(body=body, content_type=content_type)
        else:
            route.fulfill(status=404)

    page.route("**/*", handle)
    page.on("pageerror", lambda error: state["errors"].append(str(error)))
    page.goto("http://localhost:8779/")
    page.wait_for_function("document.querySelector('#quick-watchlist').getAttribute('aria-pressed') === 'true'")
    return state


def assert_feedback_visible(page):
    assert page.locator("#quick-plan-message").is_visible()
    assert page.locator("#quick-plan-message").evaluate("""node => {
        const r = node.getBoundingClientRect();
        const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        return r.top >= 0 && r.bottom <= window.innerHeight && node.contains(top);
    }""")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [320, 390, 1400])
def test_buylist_requests_missing_price_and_persists_addition(list_browser, tmp_path, width):
    page = list_browser.new_page(viewport={"width": width, "height": 844}, has_touch=width < 900)
    state = open_watchlist(page)
    page.locator("#quick-buylist").click()
    assert page.locator("#breakout-price-popup").is_visible()
    assert "SHMD" in page.locator("#breakout-price-title").inner_text()
    assert page.locator("#breakout-price-popup-input").input_value() == ""
    assert state["posts"] == []
    page.locator("#breakout-price-cancel").click()
    assert_feedback_visible(page)
    assert "Set a breakout price" in page.locator("#quick-plan-message").inner_text()
    page.screenshot(path=str(tmp_path / f"missing-price-{width}.png"))
    page.locator("#quick-buylist").click()
    page.locator("#breakout-price-popup-input").fill("2.75")
    page.locator("#breakout-price-form button[type=submit]").click()
    page.wait_for_function("document.querySelector('#quick-buylist').getAttribute('aria-pressed') === 'true' && !document.querySelector('#quick-buylist').disabled")
    assert [payload["operation"] for _, payload in state["posts"]] == ["set_breakout", "promote_buylist"]
    assert [payload["expected_revision"] for _, payload in state["posts"]] == [1, 2]
    assert state["card"]["buylist_member"] is True
    assert state["card"]["buy_today_member"] is False
    assert_feedback_visible(page)
    page.screenshot(path=str(tmp_path / f"buylist-saved-{width}.png"))
    page.reload()
    page.wait_for_function("document.querySelector('#quick-buylist').getAttribute('aria-pressed') === 'true'")
    assert state["errors"] == []
    page.close()


@pytest.mark.parametrize("delegated", [True, False])
def test_buy_today_from_watchlist_promotes_and_requires_operator_confirmation(list_browser, delegated):
    page = list_browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True)
    state = open_watchlist(page, delegated=delegated)
    page.locator("#quick-buy-today").click()
    page.locator("#breakout-price-popup-input").fill("2.75")
    page.locator("#breakout-price-form button[type=submit]").click()
    if delegated:
        page.locator("#operator-confirm-dialog").wait_for(state="visible")
        assert len(state["posts"]) == 2
        page.locator("#operator-confirm-cancel").click()
        assert state["card"]["buy_today_member"] is False
        assert state["card"]["buylist_member"] is True
        page.locator("#quick-buy-today").click()
        page.locator("#operator-confirm-submit").click()
        page.wait_for_function("document.querySelector('#quick-buy-today').textContent === 'Cancel Today' && !document.querySelector('#quick-buy-today').disabled")
        assert state["posts"][2][0].endswith("activate-buy-today")
        assert state["posts"][2][1]["expected_revision"] == 3
        assert state["card"]["buy_today_member"] is True
    else:
        page.wait_for_function("document.querySelector('#quick-plan-message').textContent.includes('SHARED BUY TODAY DRAFT')")
        assert not page.locator("#operator-confirm-dialog").is_visible()
        assert state["posts"][2][0].endswith("buy-today-preview")
        assert state["card"]["buy_today_member"] is False
    assert_feedback_visible(page)
    assert state["errors"] == []
    page.close()


@pytest.mark.parametrize("reject", ["set_breakout", "promote_buylist"])
def test_rejected_save_stays_visible_and_cannot_activate_buy_today(list_browser, reject):
    page = list_browser.new_page(viewport={"width": 320, "height": 740}, has_touch=True)
    state = open_watchlist(page, reject=reject)
    page.locator("#quick-buy-today").click()
    page.locator("#breakout-price-popup-input").fill("2.75")
    page.locator("#breakout-price-form button[type=submit]").click()
    page.wait_for_function("document.querySelector('#quick-plan-message').textContent.includes('Stale planning revision')")
    assert_feedback_visible(page)
    assert not page.locator("#operator-confirm-dialog").is_visible()
    assert state["card"]["buy_today_member"] is False
    assert [payload["operation"] for _, payload in state["posts"]] == (
        ["set_breakout"] if reject == "set_breakout" else ["set_breakout", "promote_buylist"]
    )
    assert state["errors"] == []
    page.close()
