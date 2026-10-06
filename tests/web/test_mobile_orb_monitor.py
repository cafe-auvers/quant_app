"""Price confirmations remain visible independently of sizing and quote age."""
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.utils.market_calendar import US_MARKET_ZONE
from src.web.assets import ReleaseAssets


@pytest.mark.parametrize("width", [320, 390])
def test_mobile_orb_filter_and_completed_range_explanation(width, tmp_path, browser_event_loop):
    playwright = pytest.importorskip("playwright.sync_api")
    root = Path(__file__).resolve().parents[2]
    static = root / "src/web/static"
    assets = ReleaseAssets(static, root / "src/ui/static/vendor")
    now = datetime.now(US_MARKET_ZONE)
    rows = []
    for symbol, price_status, position_status, stale in [
        ("PASS_UNKNOWN", "PASS", "UNKNOWN", False),
        ("PASS_FAIL", "PASS", "FAIL", False),
        ("PASS_STALE", "PASS", "PASS", True),
        ("WAIT", "WAITING", "PASS", False),
    ]:
        rows.append({
            "symbol": symbol, "name": symbol, "watchlist_member": True,
            "breakout_price": 101, "version": 1, "current_price": 103,
            "quote_as_of": (now - timedelta(minutes=5) if stale else now).isoformat(),
            "quote_status": "STALE" if stale else "CURRENT", "orb_session_date": now.date().isoformat(),
            "orb": [{"window": "1m", "price_status": price_status,
                     "position_status": position_status, "breakout_trigger": 104,
                     "price_reason": "Waiting for price above breakout and ORH",
                     "position_reason": "Sizing unavailable" if position_status == "UNKNOWN" else "ADR bounds failed"}],
        })
    session = {"mode": "CONNECTED", "csrf_token": "test",
               "market_status": {"state": "OPEN", "phase": "REGULAR"}}
    errors, mutations = [], []

    def handle(route):
        path = urlparse(route.request.url).path
        if route.request.method == "POST":
            mutations.append(path)
            route.fulfill(status=405)
        elif path.startswith("/api/"):
            if path.endswith("session") or path.endswith("status"):
                value = session
            elif path == "/api/v1/planning":
                value = {"rows": rows}
            elif path == "/api/v1/intraday-monitor":
                value = {"rows": rows, "as_of": now.isoformat(), "enabled": True, "advisory": True}
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

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 844}, is_mobile=True)
            page.route("**/*", handle)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://localhost:8779/")
            page.locator("#mobile-list-menu").click()
            waiting = page.locator('[data-mobile-symbol="WAIT"]')
            waiting.wait_for()
            assert "Await breakout" in waiting.inner_text()
            assert "Range complete · waiting above $104.00" in waiting.inner_text()
            page.locator('[data-monitor-filter="orb"]').click()
            assert sorted(page.locator(".mobile-monitor-row").evaluate_all("rows => rows.map(row => row.dataset.mobileSymbol)")) == ["PASS_FAIL", "PASS_STALE", "PASS_UNKNOWN"]
            assert "Passed earlier · latest quote stale" in page.locator('[data-mobile-symbol="PASS_STALE"]').inner_text()
            assert "Unavailable" in page.locator('[data-mobile-symbol="PASS_UNKNOWN"]').inner_text()
            assert not errors and not mutations
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(tmp_path / f"orb-monitor-{width}.png"))
        finally:
            browser.close()
