"""P&L estimates and usable scrolling across narrow mobile position cards."""
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import urlparse

import pytest

from src.web.assets import ReleaseAssets
from .test_mobile_list_addition import list_browser


def open_board(page, *, stale=False, missing=False, nav_status="ARMED"):
    root = Path(__file__).resolve().parents[2]
    static = root / "src/web/static"
    assets = ReleaseAssets(static, root / "src/ui/static/vendor")
    now = datetime.now(timezone.utc)
    rows = []
    for symbol, stage in (("OPEN", "OPEN_POSITION"), ("PART", "PARTIAL_SELL"), ("SELL", "SELL_ALL")):
        rows.append(dict(symbol=symbol, name="A company with a long name and important position information",
                         board_status=stage, version=1, broker_quantity=100, orderable_quantity=75,
                         average_entry_price=None if missing else 10,
                         last_reported_price=None if missing else 11,
                         price_as_of="2026-01-01T00:00:00Z" if stale else now.isoformat(),
                         active_stop_price=9.3, stop_quantity=100,
                         pending_partial_sell_quantity=25, pending_partial_sell_price=12,
                         warnings=["A long warning that should wrap and remain readable on a narrow screen"],
                         exit_order_pending=False, nav_profit_exit=dict(status=nav_status, nav_usd=11000,
                             target_profit_usd=220, target_quantity=50, filled_quantity=20)))
    session = dict(mode="CONNECTED", csrf_token="test",
                   operator={"delegated": True, "operations": ["request_partial_sell", "request_sell_all"]},
                   market_status={"state": "OPEN", "phase": "REGULAR"})
    state = {"posts": [], "errors": [], "rows": rows}

    def handle(route):
        path = urlparse(route.request.url).path
        if route.request.method != "GET":
            state["posts"].append(route.request.post_data_json)
            route.fulfill(status=409, json={"detail": "Display-only fixture"})
        elif path == "/api/v1/buyboard":
            route.fulfill(json={"rows": rows, "revision": "1"})
        elif path.startswith("/api/"):
            route.fulfill(json=session if path.endswith("session") or path.endswith("status") else {"rows": []})
        elif path == "/":
            route.fulfill(body=assets.render_page(static / "dashboard.html"), content_type="text/html")
        elif path.startswith("/release-static/"):
            _, body, kind = assets.assets[path.rsplit("/", 1)[-1]]
            route.fulfill(body=body, content_type=kind)
        else:
            route.fulfill(status=404)

    page.route("**/*", handle)
    page.on("pageerror", lambda error: state["errors"].append(str(error)))
    page.goto("http://localhost:8779/")
    page.get_by_role("button", name="Buy Board", exact=True).click()
    return state


@pytest.mark.parametrize("width,height", [(320, 568), (390, 844), (820, 844)])
def test_position_pnl_and_exit_scenarios_are_prominent_and_scrollable(list_browser, tmp_path, width, height):
    page = list_browser.new_page(viewport={"width": width, "height": height}, is_mobile=True, has_touch=True)
    state = open_board(page)
    for tab, amount, basis in (("Open", "+$100.00 (+10.00%)", "100 sh"),
                               ("Partial", "+$50.00 (+20.00%)", "25 sh @ $12.00"),
                               ("Sell All", "+$100.00 (+10.00%)", "100 sh")):
        page.get_by_role("tab", name=re.compile(rf"^{tab} ")).click()
        card = page.locator(".mobile-kanban-card")
        pnl = card.locator(".board-pnl-fact")
        assert pnl.count() == 3
        assert "+$100.00 (+10.00%)" in pnl.nth(0).inner_text()
        assert amount in pnl.nth(1).inner_text()
        assert basis in pnl.nth(1).inner_text()
        assert "−$70.00 (−7.00%)" in pnl.nth(2).inner_text().replace("(-", "(−")
        assert page.locator("#buy-board-cards").evaluate("el => el.scrollHeight <= el.clientHeight + 1")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        for fact in pnl.all():
            assert fact.evaluate("el => el.scrollWidth <= el.clientWidth")
        card.click()
        page.get_by_role("button", name="Close", exact=True).click()
    page.screenshot(path=str(tmp_path / f"position-pnl-{width}.png"), full_page=True)
    assert not state["posts"] and not state["errors"]
    page.close()


def test_partial_sell_live_preview_updates_price_quantity_and_preserves_draft_on_poll(list_browser, tmp_path):
    page = list_browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    state = open_board(page)
    page.get_by_role("tab", name=re.compile(r"^Open ")).click()
    page.locator(".mobile-kanban-card").click()
    page.get_by_role("button", name="Partial Sell", exact=True).click()
    quantity = page.locator("#buy-board-action-value")
    price = page.locator("#buy-board-partial-price")
    preview = page.locator("#buy-board-exit-preview")
    quantity.fill("25")
    assert "+$25.00 (+10.00%)" in preview.inner_text()
    price.fill("12.00")
    assert "+$50.00 (+20.00%)" in preview.inner_text()
    assert "$300.00" in preview.inner_text()
    quantity.fill("50")
    assert "+$100.00 (+20.00%)" in preview.inner_text()
    quantity.fill("80")
    assert "+$" not in preview.inner_text()
    quantity.fill("25")
    price.fill("12.005")
    assert "valid sell price" in preview.inner_text()
    price.fill("12.00")
    page.clock.install()
    page.clock.fast_forward(65_000)
    assert quantity.input_value() == "25"
    assert price.input_value() == "12.00"
    assert preview.is_visible()
    assert "+$50.00 (+20.00%)" in preview.inner_text()
    page.screenshot(path=str(tmp_path / "partial-preview.png"), full_page=True)
    page.locator("#buy-board-action-input-submit").click()
    assert "+$50.00 (+20.00%)" in page.locator("#operator-confirm-copy").inner_text()
    assert not state["posts"] and not state["errors"]
    page.close()


@pytest.mark.parametrize("stale,missing", [(True, False), (False, True)])
def test_missing_and_stale_prices_never_appear_as_fresh_profit(list_browser, stale, missing):
    page = list_browser.new_page(viewport={"width": 320, "height": 568}, is_mobile=True)
    state = open_board(page, stale=stale, missing=missing)
    page.get_by_role("tab", name=re.compile(r"^Open ")).click()
    pnl = page.locator(".mobile-kanban-card .board-pnl-fact")
    assert ("Unavailable" if missing else "stale") in pnl.nth(0).inner_text()
    assert ("Unavailable" if missing else "stale") in pnl.nth(1).inner_text()
    page.get_by_role("tab", name=re.compile(r"^Partial ")).click()
    if not missing:
        assert "+$50.00 (+20.00%)" in pnl.nth(1).inner_text()
        assert "stale" not in pnl.nth(1).inner_text()
    assert not state["posts"] and not state["errors"]
    page.close()


def test_poll_closes_an_exit_draft_when_the_position_enters_another_stage(list_browser):
    page = list_browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
    state = open_board(page)
    page.get_by_role("tab", name=re.compile(r"^Open ")).click()
    page.locator(".mobile-kanban-card").click()
    page.get_by_role("button", name="Partial Sell", exact=True).click()
    page.locator("#buy-board-action-value").fill("25")
    page.locator("#buy-board-partial-price").fill("12")
    state["rows"][0].update(board_status="PARTIAL_SELL", version=2)
    page.clock.install()
    page.clock.fast_forward(65_000)
    page.wait_for_function("document.querySelector('#buy-board-action-stage').textContent === 'Partial Sell'")
    assert page.locator("#buy-board-action-input").is_hidden()
    assert page.locator("#buy-board-exit-preview").is_hidden()
    assert not state["posts"] and not state["errors"]
    page.close()


@pytest.mark.parametrize("status,text", [
    ("ARMED", "Sell 50 shares at +$220.00"),
    ("SELLING", "20 sh / 50 sh sold"),
    ("DONE", "Completed · no repeat"),
    ("OVERRIDDEN", "Manual exit override"),
    ("WAITING", "Waiting for fresh account NAV"),
    ("BLOCKED", "Saved state needs review"),
])
def test_nav_profit_target_and_fill_progress_are_visible_on_mobile(list_browser, status, text):
    page = list_browser.new_page(viewport={"width": 320, "height": 568}, is_mobile=True)
    state = open_board(page, nav_status=status)
    page.get_by_role("tab", name=re.compile(r"^Open ")).click()
    card = page.locator(".mobile-kanban-card")
    assert "Auto take-profit" in card.inner_text()
    assert text in card.inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    card.click()
    assert text in page.locator("#buy-board-action-sheet").inner_text()
    assert not state["posts"] and not state["errors"]
    page.close()
