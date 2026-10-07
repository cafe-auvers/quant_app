"""The compact monitor shows one line per stock and the best complete ORB."""
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.utils.market_calendar import US_MARKET_ZONE
from src.web.assets import ReleaseAssets


@pytest.mark.parametrize("width", [320, 390, 680])
def test_monitor_table_best_orb_filter_and_completed_range_explanation(width, tmp_path, browser_event_loop):
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
        ("BEST", "PASS", "PASS", False),
        ("LOW_VOL", "PASS", "PASS", False),
        ("WAIT", "WAITING", "PASS", False),
    ]:
        rows.append({
            "symbol": symbol, "name": symbol, "watchlist_member": True,
            "breakout_price": 101, "version": 1, "current_price": 103,
            "change_percent": -2.5 if symbol == "WAIT" else 10,
            "return_1m": 0, "return_3m": 40.2,
            "quote_as_of": (now - timedelta(minutes=5) if stale else now).isoformat(),
            "quote_status": "STALE" if stale else "CURRENT", "orb_session_date": now.date().isoformat(),
            "breakout_status": "UNKNOWN" if stale else "WAITING" if symbol == "WAIT" else "ABOVE",
            "broke_out_today": None if stale else symbol != "WAIT",
            "orb": [{"window": "1m", "price_status": price_status,
                     "position_status": position_status, "breakout_trigger": 104, "risk_percent": 0.5,
                     "liquidity_status": "FAIL" if symbol == "LOW_VOL" else "PASS", "score": 70,
                     "liquidity_reason": "Low opening liquidity: 100 shares/min < 200 required",
                     "price_reason": "Waiting for price above breakout and ORH",
                     "position_reason": "Sizing unavailable" if position_status == "UNKNOWN" else "ADR bounds failed"}],
        })
    best = next(row for row in rows if row["symbol"] == "BEST")
    best["orb"].append({**best["orb"][0], "window": "5m", "risk_percent": 0.0175 * 100, "score": 90})
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
            assert waiting.locator("td").count() == 7
            assert "Waiting" in waiting.inner_text()
            assert "Range complete · waiting above $104.00" in waiting.locator(".mobile-monitor-result").get_attribute("title")
            assert waiting.locator(".mobile-monitor-percent").all_inner_texts() == ["-2.5%", "+0.0%", "+40.2%"]
            assert page.locator(".mobile-monitor-table th").all_inner_texts() == ["Stock", "Today %", "Breakout", "Best ORB", "1M %", "3M %", "+"]
            assert page.locator('[data-mobile-symbol="BEST"] .mobile-monitor-result').inner_text() == "5m · 1.75%"
            assert page.locator('[data-mobile-symbol="BEST"] .mobile-monitor-breakout').inner_text() == "Yes"
            assert page.locator('[data-mobile-symbol="LOW_VOL"] .mobile-monitor-result').inner_text() == "Low vol"
            assert max(page.locator(".mobile-monitor-row").evaluate_all("rows => rows.map(row => row.getBoundingClientRect().height)")) <= 42
            page.screenshot(path=str(tmp_path / f"monitor-table-{width}.png"))
            page.locator("#mobile-list-items").evaluate("node => {node.scrollLeft = 80}")
            page.locator('[data-monitor-filter="orb"]').click()
            assert sorted(page.locator(".mobile-monitor-row").evaluate_all("rows => rows.map(row => row.dataset.mobileSymbol)")) == ["BEST", "PASS_STALE"]
            stale = page.locator('[data-mobile-symbol="PASS_STALE"] .mobile-monitor-result')
            assert stale.inner_text() == "1m · 0.5%*"
            assert "Passed earlier · latest quote stale" in stale.get_attribute("title")
            assert stale.get_attribute("data-status") == "STALE"
            assert page.locator('[data-mobile-symbol="PASS_STALE"] .mobile-monitor-percent.stale').count() == 3
            if width < 420:
                assert page.locator("#mobile-list-items").evaluate("node => node.scrollLeft") > 0
            assert not errors and not mutations
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(tmp_path / f"monitor-orb-filter-{width}.png"))
        finally:
            browser.close()
