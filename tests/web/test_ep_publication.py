from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.risk.orb_position import OrbSettings
from src.web.assets import ReleaseAssets


@pytest.fixture
def ep_browser(browser_event_loop):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


def open_ep_workspace(page, *, editable=True):
    root = Path(__file__).resolve().parents[2]
    assets = ReleaseAssets(root / "src/web/static", root / "src/ui/static/vendor")
    card = {"symbol": "EPX", "name": "EP candidate", "version": 1, "card_version": 1,
        "stage": "BUYLIST", "canonical_stage": "BUYLIST", "display_stage": "BUYLIST",
        "board_status": "BUYLIST", "buylist_member": True, "watchlist_member": False,
        "buy_today_member": False, "breakout_price": 99, "is_ep": False}
    state = {"card": card, "mutations": [], "errors": [], "settings": OrbSettings().to_dict(), "revision": 1}
    session = {"mode": "CONNECTED", "csrf_token": "test", "planning_writable": True,
        "planning_revision": "1", "market_status": {"state": "OPEN", "phase": "REGULAR"},
        "operator": {"delegated": True, "operator_control": "Mobile Web", "execution_owner": "PC",
                     "targets": [{"key": "MOBILE_WEB", "label": "Mobile Web", "available": True, "selected": True},
                                 {"key": "PC", "label": "PC", "available": True, "selected": False}],
                     "operations": ["activate_buy_today", "deactivate_buy_today"] + (["update_orb_settings"] if editable else [])}}

    def handle(route):
        path = urlparse(route.request.url).path
        if path.startswith("/api/"):
            if route.request.method != "GET":
                payload = route.request.post_data_json
                state["mutations"].append({"path": path, "payload": payload})
                if path.endswith("orb-settings"):
                    state["settings"] = {key: payload[key] for key in state["settings"]}
                    state["revision"] += 1
                    value = {"settings": state["settings"], "revision": state["revision"]}
                elif path.endswith("activate-buy-today") or path.endswith("board-actions"):
                    card.update(is_ep=payload.get("is_ep", False), stage="BUY_TODAY", canonical_stage="BUY_TODAY",
                        display_stage="BUY_TODAY", board_status="BUY_TODAY", buy_today_member=True, version=2)
                    value = {"card": card, "status": "BUY TODAY ACTIVATED", "queued": False, "broker_order_placed": False}
                else:
                    route.fulfill(status=400)
                    return
            elif path.endswith("session") or path.endswith("status"):
                value = {**session, "planning_revision": "1"}
            elif path.endswith("orb-settings"):
                value = {"settings": state["settings"], "revision": state["revision"]}
            elif path == "/api/v1/buyboard":
                value = {"rows": [card], "revision": "1"}
            elif path == "/api/v1/planning" or path == "/api/v1/buy-today-drafts":
                value = {"rows": [card] if path.endswith("planning") or card["buy_today_member"] else [], "revision": "1"}
            elif path.startswith("/api/v1/planning/"):
                value = {"card": card}
            elif path.startswith("/api/v1/scanner"):
                value = {"rows": [card], "total": 1}
            else:
                value = {"rows": []}
            route.fulfill(json=value)
        elif path == "/":
            route.fulfill(body=assets.render_page(root / "src/web/static/dashboard.html"), content_type="text/html")
        elif path.startswith("/release-static/"):
            _, body, content_type = assets.assets[path.rsplit("/", 1)[-1]]
            route.fulfill(body=body, content_type=content_type)
        else:
            route.fulfill(status=404)

    page.route("**/*", handle)
    page.on("pageerror", lambda error: state["errors"].append(str(error)))
    page.goto("http://localhost:8781/")
    page.wait_for_function("document.querySelector('#quick-buy-today').disabled === false && document.querySelector('#plan-stage').textContent === 'BUYLIST' && document.querySelector('#orb-settings-revision').textContent === 'Shared r1'")
    return state


@pytest.mark.parametrize("width", [320, 390, 1400])
@pytest.mark.parametrize("choice", ["cancel", "submit", "ep"])
def test_publish_confirmation_offers_three_choices_and_saves_selected_profile(ep_browser, width, choice):
    page = ep_browser.new_page(viewport={"width": width, "height": 844})
    state = open_ep_workspace(page)
    page.locator("#quick-buy-today").click()
    page.locator("#operator-confirm-ep").wait_for(state="visible")
    assert page.locator("#operator-confirm-title").inner_text() == "Publish EPX to Buy Today?"
    assert "Normal: 15% / 65% / 90%" in page.locator("#operator-confirm-copy").inner_text()
    assert "EP: 50% / 100% / 150%" in page.locator("#operator-confirm-copy").inner_text()
    for button in ("cancel", "submit", "ep"):
        bounds = page.locator(f"#operator-confirm-{button}").bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    page.locator(f"#operator-confirm-{choice}").click()
    if choice == "cancel":
        assert not state["mutations"]
    else:
        page.wait_for_function("document.querySelector('#plan-stage').textContent.includes('BUY_TODAY')")
        assert state["mutations"][0]["payload"]["is_ep"] is (choice == "ep")
        assert state["card"]["is_ep"] is (choice == "ep")
        assert page.locator("#chart-entry-profile").is_visible() is (choice == "ep")
    assert not state["errors"]
    page.close()


@pytest.mark.parametrize("width", [320, 390, 1400])
def test_ep_settings_are_editable_on_phone_and_desktop_web(ep_browser, width):
    page = ep_browser.new_page(viewport={"width": width, "height": 844})
    state = open_ep_workspace(page)
    if width > 900:
        page.locator("#desktop-orb-settings").click()
    else:
        page.locator('#mobile-navigation-menu').click()
        page.locator('[data-mobile-page="settings"]').click()
    page.locator("#orb-ep-stop-adr-min").fill("40")
    page.locator("#orb-ep-stop-adr-ideal").fill("90")
    page.locator("#orb-ep-stop-adr-max").fill("140")
    page.locator("#orb-settings-save").click()
    page.wait_for_function("document.querySelector('#orb-settings-revision').textContent === 'Shared r2'")
    assert state["settings"]["ep_stop_adr_min_percent"] == 40
    assert state["settings"]["ep_stop_adr_ideal_percent"] == 90
    assert state["settings"]["ep_stop_adr_max_percent"] == 140
    assert state["settings"]["stop_adr_max_percent"] == 90
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not state["errors"]
    page.close()


@pytest.mark.parametrize("width", [320, 390, 820])
def test_mobile_menu_separates_settings_and_preserves_unsaved_risk(ep_browser, width, tmp_path):
    page = ep_browser.new_page(viewport={"width": width, "height": 844}, has_touch=True)
    state = open_ep_workspace(page)
    menu = page.locator("#mobile-navigation-menu")
    popup = page.locator("#mobile-navigation-popover")
    assert menu.is_visible() and not popup.is_visible()
    assert page.locator('.mobile-bottom-nav > [data-mobile-page="summary"]').count() == 0
    menu.click()
    assert popup.is_visible() and menu.get_attribute("aria-expanded") == "true"
    assert popup.locator("button").all_inner_texts() == ["Home", "Settings"]
    page.screenshot(path=str(tmp_path / f"navigation-menu-{width}.png"))
    page.keyboard.press("Escape")
    assert not popup.is_visible() and menu.get_attribute("aria-expanded") == "false"
    assert menu.evaluate("node => document.activeElement === node")
    menu.click()
    page.get_by_role("button", name="Home", exact=True).click()
    assert page.locator("#mobile-summary-page").is_visible()
    assert page.locator("#mobile-summary-page #mobile-orb-settings-form").count() == 0
    assert page.locator("#mobile-summary-page #operator-control-targets").count() == 0
    assert page.locator("#summary-open-lists").is_visible()
    assert not page.locator("#mobile-orb-settings-form").is_visible()
    assert not popup.is_visible()
    menu.click()
    page.get_by_role("button", name="Settings", exact=True).click()
    assert page.locator("#mobile-settings-page").is_visible()
    assert page.locator("#mobile-settings-page #operator-control-targets").count() == 1
    assert page.locator("#mobile-settings-page #summary-scanner-setups").count() == 1
    assert page.locator("#orb-opening-volume-min").input_value() == "200.00"
    page.locator("#orb-opening-volume-min").fill("375")
    assert state["settings"]["opening_min_shares_per_minute"] == 200
    menu.click()
    page.get_by_role("button", name="Home", exact=True).click()
    menu.click()
    page.get_by_role("button", name="Settings", exact=True).click()
    assert page.locator("#orb-opening-volume-min").input_value() == "375"
    assert state["mutations"] == []
    page.locator("#mobile-settings-page").evaluate("node => node.scrollTop = 0")
    page.screenshot(path=str(tmp_path / f"settings-page-{width}.png"))
    page.locator("#settings-open-chart").click()
    assert page.locator("#mobile-menu-popover").is_visible()
    menu.click()
    assert not page.locator("#mobile-menu-popover").is_visible()
    page.locator("#mobile-settings-page h1").click()
    assert not popup.is_visible()
    menu.click()
    page.locator("#mobile-list-menu").click()
    assert not popup.is_visible()
    assert page.locator("#mobile-list-popover").is_visible()
    assert page.locator("#mobile-workspace").get_attribute("data-mobile-page") == "chart"
    page.get_by_role("button", name="Chart", exact=True).click()
    page.get_by_role("button", name="Buy Board", exact=True).click()
    assert page.locator("#mobile-buy-board-page").is_visible()
    for selector in ("#mobile-navigation-menu", "#mobile-list-menu", 'button[data-mobile-page="chart"]',
                     'button[data-mobile-page="buy-board"]', "#mobile-previous-symbol", "#mobile-next-symbol"):
        button = page.locator(selector)
        assert button.is_visible()
        bounds = button.bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
        assert bounds["y"] + bounds["height"] <= 844
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not state["errors"] and not state["mutations"]
    page.close()


def test_settings_navigation_keeps_read_only_shared_controls_disabled(ep_browser):
    page = ep_browser.new_page(viewport={"width": 390, "height": 844})
    state = open_ep_workspace(page, editable=False)
    page.locator("#mobile-navigation-menu").click()
    page.get_by_role("button", name="Settings", exact=True).click()
    assert page.locator("#orb-opening-volume-min").is_disabled()
    assert page.locator("#orb-capital-max").is_disabled()
    assert page.locator("#orb-settings-save").is_disabled()
    assert not state["mutations"] and not state["errors"]
    page.close()
