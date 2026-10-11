"""Keep the mobile review cursor and ordering through membership edits."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.web.assets import ReleaseAssets


@pytest.fixture
def continuity_browser(browser_event_loop):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


def open_lists(page, mode, *, count=50, reject=False, hold=False, draft=False, fail_refresh=False):
    root = Path(__file__).resolve().parents[2]
    static = root / "src/web/static"
    assets = ReleaseAssets(static, root / "src/ui/static/vendor")
    cards = {
        f"S{index:03d}": {
            "symbol": f"S{index:03d}", "name": f"Stock {index}",
            "stage": "WATCHLIST" if mode == "watchlist" else "BUYLIST",
            "canonical_stage": "BUY_TODAY" if mode == "buy_today" and not draft
            else "WATCHLIST" if mode == "watchlist" else "BUYLIST",
            "watchlist_member": True, "buylist_member": mode != "watchlist",
            "buy_today_member": mode == "buy_today" and not draft,
            "breakout_price": 25.0, "version": 1,
        }
        for index in range(1, count + 1)
    }
    drafts = set(cards) if mode == "buy_today" else set()
    state = {"cards": cards, "posts": [], "errors": [], "held": None, "reads": 0}
    session = {
        "mode": "CONNECTED", "csrf_token": "test", "planning_writable": True,
        "operator": {"delegated": True, "operations": ["activate_buy_today", "deactivate_buy_today"]},
        "market_status": {"state": "CLOSED", "phase": "CLOSED"},
    }

    def mutate(route):
        payload = route.request.post_data_json
        symbol = urlparse(route.request.url).path.split("/")[4]
        card = cards[symbol]
        if reject:
            route.fulfill(status=409, json={"detail": "Save rejected", "current": dict(card)})
            return
        assert payload["expected_revision"] == card["version"]
        operation = payload.get("operation")
        if operation == "remove_watchlist":
            card["watchlist_member"] = False
        elif operation in {"remove_buylist", "promote_buylist"}:
            card["buylist_member"] = operation == "promote_buylist"
            card["stage"] = card["canonical_stage"] = "BUYLIST" if card["buylist_member"] else "WATCHLIST"
        elif operation == "add_watchlist":
            card["watchlist_member"] = True
        elif route.request.url.endswith("activate-buy-today"):
            card["buy_today_member"] = payload["enabled"]
            card["canonical_stage"] = "BUY_TODAY" if payload["enabled"] else "BUYLIST"
            if payload["enabled"]:
                drafts.add(symbol)
            else:
                drafts.discard(symbol)
        elif route.request.method == "DELETE":
            drafts.discard(symbol)
        else:
            raise AssertionError(f"Unexpected mutation: {route.request.url}")
        card["version"] += 1
        route.fulfill(json={"card": dict(card), "status": "SAVED", "queued": False})

    def handle(route):
        path = urlparse(route.request.url).path
        if route.request.method in {"POST", "DELETE"}:
            state["posts"].append(route.request.post_data_json)
            if hold:
                state["held"] = route
            else:
                mutate(route)
            return
        if path.startswith("/api/"):
            if path.endswith("session") or path.endswith("status"):
                value = session
            elif path == "/api/v1/planning":
                state["reads"] += 1
                if fail_refresh and state["posts"]:
                    route.fulfill(status=502, json={"detail": "Read refresh unavailable"})
                    return
                # Follow-up reads deliberately change server order.
                rows = list(cards.values())
                if state["posts"]:
                    rows.reverse()
                value = {"rows": [dict(card) for card in rows]}
            elif path.startswith("/api/v1/planning/"):
                value = {"card": dict(cards[path.rsplit("/", 1)[-1]])}
            elif path == "/api/v1/buy-today-drafts":
                symbols = sorted(drafts, reverse=bool(state["posts"]))
                value = {"rows": [{**cards[symbol], "card_version": cards[symbol]["version"]} for symbol in symbols]}
            elif path.startswith("/api/v1/scanner"):
                value = {"rows": [{"symbol": symbol, "rank": index + 1} for index, symbol in enumerate(cards)], "total": count}
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
    choose_list(page, mode)
    state["complete"] = lambda: mutate(state["held"])
    return state


def choose_list(page, mode):
    if page.viewport_size["width"] <= 900:
        page.locator("#mobile-list-menu").click()
        page.locator(f'[data-mobile-list="{mode}"]').click()
    else:
        page.locator(f'.list-tab[data-list="{mode}"]').click()


def select_row(page, symbol):
    mobile = page.viewport_size["width"] <= 900
    selector = f'[data-mobile-symbol="{symbol}"]' if mobile else f'.stock-row[data-symbol="{symbol}"]'
    page.locator(selector).click()
    page.wait_for_function("symbol => document.querySelector('#active-symbol').textContent === symbol", arg=symbol)
    page.wait_for_function("!document.querySelector('#quick-watchlist').disabled")


def list_symbols(page):
    selector = "#mobile-list-items [data-mobile-symbol]" if page.viewport_size["width"] <= 900 else "#stock-list [data-symbol]"
    return page.locator(selector).evaluate_all("nodes => nodes.map(node => node.dataset.mobileSymbol || node.dataset.symbol)")


def assert_active_list(page, mode):
    selector = f'[data-mobile-list="{mode}"]' if page.viewport_size["width"] <= 900 else f'.list-tab[data-list="{mode}"]'
    assert "active" in page.locator(selector).get_attribute("class").split()


@pytest.mark.parametrize("width", [320, 390, 1400])
@pytest.mark.parametrize("mode", ["watchlist", "buylist", "buy_today"])
def test_removal_selects_previous_21st_row_in_same_list(continuity_browser, width, mode):
    page = continuity_browser.new_page(viewport={"width": width, "height": 844})
    state = open_lists(page, mode)
    select_row(page, "S020")
    before = state["reads"]
    button = "#quick-buy-today" if mode == "buy_today" else f"#quick-{mode}"
    with page.expect_response("**/api/v1/planning"):
        page.locator(button).click()
    page.wait_for_function("document.querySelector('#active-symbol').textContent === 'S021'")
    assert state["reads"] > before
    assert_active_list(page, mode)
    expected = [f"S{index:03d}" for index in range(1, 51) if index != 20]
    assert list_symbols(page) == expected
    if width <= 900:
        page.locator("#mobile-list-menu").click()
        assert_active_list(page, mode)
        assert list_symbols(page) == expected
    assert state["errors"] == []
    page.close()


@pytest.mark.parametrize("mode,target", [("watchlist", "buylist"), ("buylist", "buy-today")])
@pytest.mark.parametrize("width", [390, 1400])
def test_addition_keeps_35th_row_and_next_reviews_36th(continuity_browser, mode, target, width):
    page = continuity_browser.new_page(viewport={"width": width, "height": 844})
    state = open_lists(page, mode)
    select_row(page, "S035")
    page.locator(f"#quick-{target}").click()
    if target == "buy-today":
        page.locator("#operator-confirm-submit").click()
    page.wait_for_function("document.querySelector('#quick-plan-message').classList.contains('success')")
    page.wait_for_function("!document.querySelector('#quick-buylist').disabled")
    assert page.locator("#active-symbol").inner_text() == "S035"
    assert_active_list(page, mode)
    assert list_symbols(page)[34:36] == ["S035", "S036"]
    page.locator("#mobile-next-symbol" if width <= 900 else "#next-symbol").click()
    page.wait_for_function("document.querySelector('#active-symbol').textContent === 'S036'")
    assert state["errors"] == []
    page.close()


@pytest.mark.parametrize("mode", ["watchlist", "buylist", "buy_today"])
def test_last_row_removal_selects_previous_row_and_final_removal_leaves_empty_list(continuity_browser, mode):
    page = continuity_browser.new_page(viewport={"width": 390, "height": 844})
    state = open_lists(page, mode, count=2)
    select_row(page, "S002")
    button = "#quick-buy-today" if mode == "buy_today" else f"#quick-{mode}"
    page.locator(button).click()
    page.wait_for_function("document.querySelector('#active-symbol').textContent === 'S001'")
    page.wait_for_function(f"!document.querySelector('{button}').disabled")
    page.locator(button).click()
    page.wait_for_function("document.querySelector('#mobile-list-count').textContent === '0'")
    assert_active_list(page, mode)
    assert list_symbols(page) == []
    assert state["errors"] == []
    page.close()


@pytest.mark.parametrize("mode,draft", [("watchlist", False), ("buylist", False), ("buy_today", False), ("buy_today", True)])
def test_rejected_removal_retains_selection_and_order(continuity_browser, mode, draft):
    page = continuity_browser.new_page(viewport={"width": 390, "height": 844})
    state = open_lists(page, mode, reject=True, draft=draft)
    select_row(page, "S020")
    button = "#quick-buy-today" if mode == "buy_today" else f"#quick-{mode}"
    page.locator(button).click()
    page.wait_for_function("document.querySelector('#quick-plan-message').textContent.includes('Save rejected')")
    assert page.locator("#active-symbol").inner_text() == "S020"
    assert_active_list(page, mode)
    assert list_symbols(page) == [f"S{index:03d}" for index in range(1, 51)]
    page.locator("#mobile-next-symbol").click()
    page.wait_for_function("document.querySelector('#active-symbol').textContent === 'S021'")
    assert state["errors"] == []
    page.close()


def test_next_during_pending_removal_uses_old_position_and_completion_keeps_new_selection(continuity_browser):
    page = continuity_browser.new_page(viewport={"width": 390, "height": 844})
    state = open_lists(page, "watchlist", hold=True)
    select_row(page, "S020")
    page.locator("#quick-watchlist").click()
    page.wait_for_function("document.querySelector('#quick-watchlist').classList.contains('pending')")
    page.locator("#mobile-next-symbol").click()
    page.wait_for_function("document.querySelector('#active-symbol').textContent === 'S021'")
    state["complete"]()
    page.wait_for_function("document.querySelector('#mobile-list-count').textContent === '49'")
    assert page.locator("#active-symbol").inner_text() == "S021"
    assert_active_list(page, "watchlist")
    assert state["errors"] == []
    page.close()


@pytest.mark.parametrize("mode", ["watchlist", "buylist", "buy_today"])
def test_confirmed_removal_advances_even_when_followup_read_fails(continuity_browser, mode):
    page = continuity_browser.new_page(viewport={"width": 390, "height": 844})
    state = open_lists(page, mode, fail_refresh=True)
    select_row(page, "S020")
    button = "#quick-buy-today" if mode == "buy_today" else f"#quick-{mode}"
    with page.expect_response("**/api/v1/planning") as refreshed:
        page.locator(button).click()
    assert refreshed.value.status == 502
    page.wait_for_function("document.querySelector('#active-symbol').textContent === 'S021'")
    assert_active_list(page, mode)
    assert list_symbols(page)[19] == "S021"
    assert len(list_symbols(page)) == 49
    assert state["errors"] == []
    page.close()


@pytest.mark.parametrize("width", [390, 1400])
def test_removal_retains_list_scroll_position(continuity_browser, width):
    page = continuity_browser.new_page(viewport={"width": width, "height": 844})
    state = open_lists(page, "watchlist")
    select_row(page, "S020")
    mobile = width <= 900
    if mobile:
        page.locator("#mobile-list-menu").click()
    list_id = "mobile-list-items" if mobile else "stock-list"
    selector = '[data-mobile-symbol="S020"]' if mobile else '.stock-row[data-symbol="S020"]'
    page.locator(selector).scroll_into_view_if_needed()
    page.wait_for_function("id => document.getElementById(id).scrollTop > 0", arg=list_id)
    before = page.locator(f"#{list_id}").evaluate("node => node.scrollTop")
    if mobile:
        page.locator("#mobile-list-menu").click()
    with page.expect_response("**/api/v1/planning"):
        page.locator("#quick-watchlist").click()
    page.wait_for_function("document.querySelector('#active-symbol').textContent === 'S021'")
    if mobile:
        page.locator("#mobile-list-menu").click()
    page.wait_for_function(
        "({id, before}) => Math.abs(document.getElementById(id).scrollTop - before) < 70",
        arg={"id": list_id, "before": before},
    )
    assert_active_list(page, "watchlist")
    assert state["errors"] == []
    page.close()
