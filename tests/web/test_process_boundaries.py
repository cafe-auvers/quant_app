from __future__ import annotations

import subprocess
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_web_launcher_does_not_import_desktop_or_execution_modules():
    script = """
import sys
from src.web.config import WebConfig
from src.web.api import build_services
services = build_services(WebConfig(data_dir=__import__('pathlib').Path(sys.argv[1])))
blocked_prefixes = (
    'PyQt5',
    'src.ui',
    'src.api.kis',
    'src.services.broker',
    'src.services.buyboard_runtime',
    'src.services.execution_command_gateway',
    'src.services.execution_workflow_service',
)
blocked = [name for name in sys.modules if name == 'main' or name.startswith(blocked_prefixes)]
constructed = [
    f'{type(value).__module__}.{type(value).__name__}'
    for value in vars(services).values()
    if any(token in type(value).__name__.lower() for token in ('broker', 'runtimeworker', 'executiongateway'))
]
print('\\n'.join(blocked))
print('\\n'.join(constructed))
raise SystemExit(1 if blocked or constructed else 0)
"""
    with tempfile.TemporaryDirectory(prefix="quant-web-boundary-") as data_dir:
        result = subprocess.run(
            [sys.executable, "-c", script, data_dir],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr


def test_run_web_imports_only_web_entrypoint():
    source = (ROOT / "scripts" / "run_web.py").read_text(encoding="utf-8")
    assert "import main" not in source
    assert "PyQt" not in source
    assert "KisBroker" not in source
    assert "BuyboardRuntime" not in source


def test_login_form_never_falls_back_to_password_query_string():
    page_source = (ROOT / "src" / "web" / "static" / "login.html").read_text(
        encoding="utf-8"
    )
    script_source = (ROOT / "src" / "web" / "static" / "login.js").read_text(
        encoding="utf-8"
    )
    app_source = (ROOT / "src" / "web" / "application.py").read_text(
        encoding="utf-8"
    )
    assert 'id="login-form" method="post" action="/login"' in page_source
    assert "event.preventDefault()" in script_source
    assert "access_log=False" in app_source
    assert '/live-static/login.js?v=39' in page_source
    assert "from nicegui" not in app_source.lower()
    assert "import nicegui" not in app_source.lower()


def test_dashboard_is_a_static_shell_without_hydration_runtime():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert "function bootApp()" in script_source
    assert "if (!byId('quant-app')) return false" in script_source
    assert "new MutationObserver" not in script_source
    assert "bootApp();" in script_source
    assert 'id="quant-app"' in page_source
    assert re.search(r'<script src="/live-static/app\.js\?v=\d+" defer></script>', page_source)
    assert page_source.index('id="market-summary"') < page_source.index('id="browser-status"')
    assert page_source.index('id="browser-status"') < page_source.index('id="web-status"')
    assert page_source.index('id="web-status"') < page_source.index('id="data-summary"')
    assert page_source.index('id="data-summary"') < page_source.index('id="executor-status"')
    assert '>Browser</span>' in page_source
    assert '>Web</span>' in page_source
    assert '<b id="data-status">Demo</b>' in page_source
    assert '>Executor</span>' in page_source
    assert "function renderStatusStrip(payload = {})" in script_source
    assert "api('/api/v1/status')" in script_source
    assert "window.setInterval(refreshStatusStrip, 10_000)" in script_source
    assert "new WebSocket(`${protocol}//${window.location.host}/live-updates`)" in script_source
    assert "scheduleLivePlanningRefresh(event)" in script_source
    assert "watchlistSessionDate" in script_source
    assert "refreshAfterWatchlistRollover" in script_source
    assert "await refreshPlanningLists()" in script_source
    assert '/live-vendor/lightweight-charts.standalone.production.js' in page_source
    assert 'id="mode-badge"' not in page_source
    assert 'id="data-badge"' not in page_source
    assert "byId('mode-badge')" not in script_source
    assert "byId('data-badge')" not in script_source
    assert "_nicegui" not in page_source.lower()


def test_pwa_registers_current_worker_and_caches_presentation_assets_only():
    app_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    login_source = (
        ROOT / "src" / "web" / "static" / "login.js"
    ).read_text(encoding="utf-8")
    worker_source = (
        ROOT / "src" / "web" / "static" / "service_worker.js"
    ).read_text(encoding="utf-8")
    for source in (app_source, login_source):
        assert "navigator.serviceWorker.register('/service-worker.js'" in source
        assert "updateViaCache: 'none'" in source
        assert "path === '/service-worker.js'" in source
    assert "'/live-static/app.css'" not in worker_source
    assert "'/live-static/app.js'" not in worker_source
    assert "'/live-vendor/lightweight-charts.standalone.production.js'" not in worker_source
    assert "cache.match(request)" in worker_source
    assert "caches.match(url.pathname)" not in worker_source
    assert "'/api/" not in worker_source
    assert "request.method !== 'GET'" in worker_source
    assert "fetch" in worker_source
    assert "indexedDB" not in worker_source
    assert "addEventListener('sync'" not in worker_source
    assert "addEventListener('periodicsync'" not in worker_source


def test_symbol_selection_does_not_rebuild_the_scanner_list():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    selection_body = script_source.split("async function selectSymbol", 1)[1].split(
        "function schedulePrefetch", 1
    )[0]
    assert "updateActiveStockRow()" in selection_body
    assert "renderStockList()" not in selection_body
    assert "bundleInflight" in script_source


def test_mobile_layout_uses_a_scrollable_stock_drawer_and_chart_edges():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    style_source = (ROOT / "src" / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert 'id="mobile-symbol-select"' not in page_source
    assert 'class="mobile-stock-nav"' not in page_source
    assert 'id="mobile-previous-symbol"' in page_source
    assert 'id="mobile-next-symbol"' in page_source
    assert 'class="symbol-stepper mobile-symbol-stepper mobile-segment-control"' in page_source
    assert 'class="timeframe-switch mobile-timeframe-switch mobile-segment-control"' not in page_source
    assert '<span class="mobile-control-caption" aria-hidden="true">Stocks</span>' in page_source
    assert '<span class="mobile-control-caption" aria-hidden="true">Interval</span>' not in page_source
    assert 'data-chart-timeframe-toggle data-current-timeframe="1D"' in page_source
    assert "function updatePaneTimeframeToggle(labelId, timeframe)" in script_source
    assert "switchTimeframe(button.dataset.currentTimeframe === '1D' ? '1H' : '1D')" in script_source
    assert 'class="symbol-stepper chart-symbol-stepper"' in page_source
    assert 'id="mobile-list-items"' in page_source
    assert 'id="mobile-list-count"' in page_source
    assert "renderMobileStockList()" in script_source
    assert "button.dataset.mobileSymbol = row.symbol" in script_source
    assert "byId('mobile-previous-symbol').addEventListener" in script_source
    assert "byId('mobile-next-symbol').addEventListener" in script_source
    assert 'id="mobile-list-menu"' in page_source
    assert 'id="mobile-list-popover"' in page_source
    assert 'data-mobile-list="scanner"' in page_source
    assert 'data-mobile-list="history"' in page_source
    assert 'data-mobile-list="watchlist"' in page_source
    assert 'data-mobile-list="buylist"' in page_source
    assert 'data-mobile-list="buy_today"' in page_source
    assert 'data-list="history"' in page_source
    assert 'id="mobile-history-from"' in page_source
    assert 'id="mobile-history-to"' in page_source
    assert 'id="rail-history-from"' in page_source
    assert "function loadWatchlistHistory(startDate, endDate)" in script_source
    assert "/api/v1/planning-history?start_date=" in script_source
    assert "start.setDate(start.getDate() - 13)" in script_source
    assert 'id="mobile-draw-line"' in page_source
    assert 'id="mobile-menu"' not in page_source
    assert 'id="mobile-menu-popover"' in page_source
    assert 'aria-label="Chart display settings"' in page_source
    assert '<div class="display-settings-heading"><strong>Chart Settings</strong>' in page_source
    menu_control = page_source.index('id="mobile-navigation-menu"')
    home_control = page_source.index('data-mobile-page="summary"', menu_control)
    settings_control = page_source.index('data-mobile-page="settings"', home_control)
    watchlist_control = page_source.index('id="mobile-list-menu"', settings_control)
    chart_control = page_source.index('data-mobile-page="chart"', watchlist_control)
    board_control = page_source.index('data-mobile-page="buy-board"', chart_control)
    stocks_control = page_source.index('id="mobile-previous-symbol"', board_control)
    assert menu_control < home_control < settings_control < watchlist_control < chart_control < board_control < stocks_control
    assert 'data-mobile-page="summary" aria-label="Home"' in page_source
    assert '<span>Home</span>' in page_source
    assert '<span>Settings</span>' in page_source
    assert 'id="mobile-settings-page"' in page_source
    assert '<span id="mobile-list-label">Watchlist</span>' in page_source
    assert 'aria-selected="true" class="active" data-mobile-list="monitor"' in page_source
    assert "listMode: 'monitor'" in script_source
    assert 'id="display-settings-reset"' in page_source
    assert 'id="mobile-orb-settings-form"' in page_source
    assert 'id="orb-capital-max"' in page_source
    assert 'id="orb-stop-adr-max"' in page_source
    assert "capital_max_percent: 30" in script_source
    assert "api('/api/v1/operator/orb-settings'" in script_source
    assert "PC and laptop refresh automatically" in script_source
    assert 'data-display-setting="relativeStrength"' in page_source
    assert 'data-display-setting="marketAlignment"' in page_source
    assert 'id="mobile-logout-button"' in page_source
    assert 'id="mobile-symbol-search-button"' in page_source
    assert page_source.index('id="mobile-symbol-search-button"') < page_source.index('id="active-symbol"')
    assert 'id="active-symbol-search-trigger"' in page_source
    assert 'class="active-symbol-search-trigger"' in page_source
    assert 'id="mobile-symbol-search-dialog"' in page_source
    assert 'id="mobile-symbol-search"' in page_source
    assert 'id="mobile-symbol-search-results"' in page_source
    assert "bindSymbolSearch('mobile-symbol-search', 'mobile-symbol-search-results', closeMobileSymbolSearch)" in script_source
    assert "activeSymbolSearchTrigger.addEventListener('click', openSymbolSearch)" in script_source
    assert "mobileSymbolSearchButton.addEventListener('click', openSymbolSearch)" in script_source
    assert "selectSymbol(item.symbol)" in script_source
    assert ".mobile-symbol-search-button { flex: 0 0 28px; align-self: center;" in style_source
    assert ".symbol-line { min-width: 0; flex: 1 1 0; align-items: center; justify-content: flex-start;" in style_source
    assert "justify-content: space-between; gap: 6px" in style_source
    assert ".metric-chip { min-height: 18px; justify-content: center;" in style_source
    assert ".active-symbol-search-trigger { min-width: 0; max-width: 100%; flex: 1 1 0;" in style_source
    assert ".mobile-symbol-search-dialog { position: fixed; z-index: 70;" in style_source
    assert ".mobile-bottom-nav { grid-row: 4;" in style_source
    assert ".topbar { display: none; }" in style_source
    assert "height: 100vh; height: 100dvh; min-height: 0; grid-template-rows: 32px" in style_source
    assert ".workspace { width: 100%; max-width: 100%; height: 100%; min-width: 0; min-height: 0; display: grid;" in style_source
    assert ".chart-workspace { grid-row: 1; position: relative; width: 100%; max-width: 100%; height: 100%; min-width: 0; min-height: 0;" in style_source
    assert ".mobile-bottom-nav { grid-row: 4; z-index: 50; position: relative; width: 100%; max-width: 100%; min-width: 0; display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));" in style_source
    assert ".mobile-bottom-nav { grid-row: 4; z-index: 50; position: relative;" in style_source
    assert "grid-template-rows: 52px minmax(0, 1fr) auto 76px" in style_source
    assert "calc(17px + env(safe-area-inset-bottom, 0px))" in style_source
    assert "bottom: calc(86px + env(safe-area-inset-bottom, 0px))" in style_source
    assert ".mobile-menu-popover { position: fixed; z-index: 80; left: 6px; right: 6px; bottom: calc(80px + env(safe-area-inset-bottom, 0px));" in style_source
    assert "overflow: visible; border-top: 1px solid #1d3a31" in style_source
    assert ".mobile-segment-buttons { width: 100%; max-width: 112px; min-width: 0; justify-self: center; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in style_source
    assert ".mobile-timeframe-switch" not in style_source
    assert "width: calc(100% - 68px); height: calc(100% - 26px);" in style_source
    assert ".chart-pane.has-relative .drawing-overlay { height: 100%; }" in style_source
    assert "activateListMode(button.dataset.mobileList)" in script_source
    assert "byId('mobile-logout-button').addEventListener('click', logout)" in script_source
    assert "quant-web-display-preferences-v1" in script_source
    assert "function applyDisplayPreferences(announce = false)" in script_source
    assert "state.displayPreferences[input.dataset.displaySetting] = input.checked" in script_source
    assert ".display-settings-scroll { max-height:" in style_source
    assert "fixLeftEdge: true" in script_source
    assert "fixRightEdge: false" in script_source
    assert "rightBarStaysOnScroll: false" in script_source
    assert ".stock-rail { display: none; }" in style_source
    assert ".mobile-list-items { min-height: 0; flex: 1 1 auto; display: block; overflow-y: auto; overflow-x: hidden;" in style_source
    assert "height: min(72dvh, 620px); max-height: calc(100dvh - 96px)" in style_source
    assert ".mobile-list-items { min-height: 0; flex: 1 1 auto; display: block;" in style_source
    assert ".chart-actions { display: none; }" in style_source
    assert "overflow-x: hidden" in style_source
    assert 'class="dashboard-body"' in page_source
    assert "@media (max-width: 900px), (display-mode: standalone)" in style_source
    assert ".dashboard-body { position: fixed; inset: 0; height: 100%; overscroll-behavior: none; }" in style_source
    assert "function lockPageDragging()" in script_source
    assert "compactLayout.matches || standaloneLayout.matches" in script_source
    assert "if (!shouldLock()) return" in script_source
    assert "if (!target?.closest(interactiveSurfaces)) event.preventDefault()" in script_source
    assert "lockPageDragging();" in script_source


def test_mobile_workspace_has_five_primary_bottom_controls():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    style_source = (ROOT / "src" / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    menu = page_source.index('id="mobile-navigation-menu"')
    home = page_source.index('data-mobile-page="summary"', menu)
    settings = page_source.index('data-mobile-page="settings"', home)
    watchlist = page_source.index('id="mobile-list-menu"', settings)
    chart = page_source.index('data-mobile-page="chart"', watchlist)
    board = page_source.index('data-mobile-page="buy-board"', chart)
    stocks = page_source.index('id="mobile-previous-symbol"', board)
    assert menu < home < settings < watchlist < chart < board < stocks
    assert 'data-mobile-page="chart" aria-label="Chart" aria-current="page"' in page_source
    assert 'id="mobile-summary-page"' in page_source
    assert 'id="mobile-market-pulse-page"' not in page_source
    assert 'data-mobile-page="market-pulse"' not in page_source
    assert 'id="mobile-home"' not in page_source
    assert 'id="pulse-context"' in page_source
    assert page_source.index('id="mobile-summary-page"') < page_source.index('id="pulse-context"') < page_source.index('id="mobile-buy-board-page"')
    assert 'id="mobile-buy-board-page"' in page_source
    assert 'id="publish-today-plan"' in page_source
    assert 'id="operator-confirm-dialog"' in page_source
    assert "function setMobilePage(page)" in script_source
    assert "function renderMarketPulsePage()" in script_source
    assert "function renderBuyBoardPage()" in script_source
    assert "const KANBAN_COLUMNS = Object.freeze([" in script_source
    assert "async function applyBuyBoardAction(action, payload = {})" in script_source
    assert "state.buyBoardOptimistic.set(symbol, {row: optimistic})" in script_source
    assert "const BUY_BOARD_FALLBACK_MS = 15_000" in script_source
    assert "BUY_BOARD_FALLBACK_MS," in script_source
    assert 'id="buy-board-tabs"' in page_source
    assert 'id="buy-board-action-sheet"' in page_source
    assert ".mobile-kanban-tabs { width: 100%; min-width: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));" in style_source
    assert 'id="summary-scanner-setups"' in page_source
    assert 'id="operator-control-targets"' in page_source
    assert 'id="operator-control-status"' in page_source
    assert "async function changeOperatorControl(target)" in script_source
    assert "api('/api/v1/operator/control'" in script_source
    assert ".operator-control-targets { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));" in style_source
    assert "async function loadScanner(setup = state.scannerSetup)" in script_source
    assert "function publishTodayPlan()" in script_source
    assert "function toggleCanonicalBuyToday()" in script_source
    assert ".mobile-home-button" not in style_source
    assert ".mobile-floating-draw { position: absolute;" in style_source
    draw_button = page_source[
        page_source.index('id="mobile-draw-line"'):
        page_source.index('</button>', page_source.index('id="mobile-draw-line"'))
    ]
    assert "<span>" not in draw_button
    assert "bottom: calc(140px + env(safe-area-inset-bottom, 0px))" in style_source
    assert "background: rgba(7, 18, 15, .58)" in style_source
    assert ".mobile-page { position: absolute;" in style_source


def test_browser_performance_samples_are_bounded_and_measure_chart_paint():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "window.__quantWebMetrics" in script_source
    assert "if (state.clientTimings.length > 100)" in script_source
    assert "symbol-navigation-chart-paint" in script_source
    assert "timeframe-switch-chart-paint" in script_source
    assert "await nextPaint()" in script_source
    assert "cache_hit: chartCacheHit" in script_source


def test_review_actions_are_inline_and_daily_view_opens_at_six_months():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert "const DEFAULT_VISIBLE_BARS = 63" in script_source
    assert "Math.max(0, bars.length - DEFAULT_VISIBLE_BARS)" in script_source
    assert "to: bars.at(-1).time" in script_source
    assert "setVisibleRange(visibleRange)" in script_source
    assert "pane.chart.priceScale('right').applyOptions({autoScale: true})" in script_source
    assert "pane.rsChart.priceScale('right').applyOptions({autoScale: true})" in script_source
    assert "if (pane.hasRelative) pane.rsChart.timeScale().setVisibleRange(visibleRange)" in script_source
    assert "rightOffset: 0" in script_source
    assert 'id="quick-watchlist"' in page_source
    assert 'id="quick-buylist"' in page_source
    assert 'id="quick-watchlist" class="review-action" aria-pressed="false"' in page_source
    assert "quickWatchlist.disabled = connectedReadOnly || busy" in script_source
    assert "quickBuylist.disabled = connectedReadOnly || busy" in script_source
    assert "watchlistMember ? 'remove_watchlist' : 'add_watchlist'" in script_source
    assert "buylistMember ? 'remove_buylist' : 'promote_buylist'" in script_source
    assert "REMOVED FROM BUYLIST - stock remains in Watchlist." in script_source
    assert "Watchlist was unchanged and breakout was kept." in script_source
    assert 'id="quick-buy-today"' in page_source
    assert "byId('quick-buy-today').addEventListener('click', toggleBuyTodayDraft)" in script_source
    assert "method: 'DELETE'" in script_source
    assert "? 'Cancelling…' : hasBuyToday ? 'Cancel Today' : 'Buy Today'" in script_source
    assert "quickBuyToday.disabled = busy || !buyTodayActionEligible" in script_source
    assert 'data-list="buy_today"' in page_source
    assert 'id="planning-drawer-button"' not in page_source
    assert 'id="chart-coverage"' not in page_source
    assert "byId('chart-coverage')" not in script_source
    assert "% state.visibleRows.length" in script_source


def test_planning_controls_update_optimistically_before_canonical_save():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    planning_command = script_source.split(
        "async function planningCommand", 1
    )[1].split("async function saveBuyTodayDraft", 1)[0]
    optimistic_index = planning_command.index(
        "state.optimisticPlans.set(actionSymbol, optimistic)"
    )
    render_index = planning_command.index("renderPlan(true)", optimistic_index)
    save_index = planning_command.index("await sendPlanningCommand(", render_index)
    assert optimistic_index < render_index < save_index
    assert "renderPlan(true)" in planning_command
    assert "function renderPlan(fast = false)" in script_source
    assert "if (fast) return" in script_source
    assert "applyListMode(state.listMode, listVisible)" in script_source
    assert "state.planningPendingSymbols.add(actionSymbol)" in script_source
    assert "state.planningPendingSymbols.delete(actionSymbol)" in script_source
    assert "const requestedPlanningEpoch = planningEpoch(symbol)" in script_source
    assert "requestedPlanningEpoch === planningEpoch(symbol)" in script_source
    assert script_source.count("bumpPlanningEpoch(actionSymbol)") >= 6
    assert "state.optimisticBuyToday.set(symbol, row)" in script_source
    assert "state.optimisticBuyToday.set(symbol, null)" in script_source
    assert "state.optimisticPlans.get(symbol) || planResult.card" in script_source
    assert "void refreshPlanningLists().catch(() => {})" in script_source
    assert "Saving" in script_source


def test_relative_strength_chart_is_rendered_as_a_linked_lower_pane():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    style_source = (ROOT / "src" / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert 'id="rs-primary"' in page_source
    assert "title: 'Relative vs SPY'" in script_source
    assert "title: 'Relative SMA 50'" in script_source
    assert "subscribeVisibleLogicalRangeChange" in script_source
    assert "pane.root.classList.toggle('has-relative'" in script_source
    assert "function applyCompactRelativeLabels(pane)" in script_source
    assert "lastValueVisible: labelsVisible" in script_source
    assert "axisLabelVisible: labelsVisible" in script_source
    assert "function syncPaneCrosshair(pane, source, param)" in script_source
    assert "targetChart.setCrosshairPosition(price, param.time, targetSeries)" in script_source
    assert "targetChart.clearCrosshairPosition()" in script_source
    assert "rsChart.subscribeCrosshairMove" in script_source
    assert "const coveredRelativeData = bars.map" in script_source
    assert "function positionPaneCrosshairGuide(pane, source, param)" in script_source
    assert ".pane-crosshair-guide" in style_source


def test_chart_metrics_earnings_and_cumulative_memberships_are_visible():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert 'id="adr-chip"' in page_source
    assert 'id="growth-1m-chip"' in page_source
    assert 'id="growth-3m-chip"' in page_source
    assert 'id="earnings-chip"' in page_source
    assert "function updateHeaderMetricChips(symbol, context = {})" in script_source
    assert "setChip('growth-1m-chip', '1M', 'return_1m', true)" in script_source
    assert "setChip('growth-3m-chip', '3M', 'return_3m', true)" in script_source
    assert "pane.candles.setMarkers(preferences.earnings ? earningsMarkers(pane.bundle) : [])" in script_source
    assert "minimumWidth: 68" in script_source
    assert "leftPriceScale: {visible: false}" in script_source
    assert "classList.toggle('current', watchlistMember)" in script_source
    assert "state.planningRows.filter(row => row.watchlist_member !== false)" in script_source


def test_market_alignment_overlay_matches_desktop_summary_and_details():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    style_source = (ROOT / "src" / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert 'id="market-alignment-overlay"' in page_source
    assert 'data-alignment-state="MKT"' in page_source
    assert 'data-alignment-state="SEG"' in page_source
    assert 'data-alignment-state="SEC"' in page_source
    assert 'data-alignment-state="IND"' in page_source
    assert 'id="alignment-stale"' in page_source
    assert 'id="market-alignment-details"' in page_source
    assert "function renderMarketAlignment(value)" in script_source
    assert "renderMarketAlignment(bundle.market_alignment)" in script_source
    assert "details.replaceChildren()" in script_source
    assert ".market-alignment-overlay" in style_source
    assert 'span[data-state="GREEN"] i' in style_source
    assert 'span[data-state="YELLOW"] i' in style_source
    assert 'span[data-state="RED"] i' in style_source


def test_breakout_can_be_placed_dragged_and_entered_manually():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    style_source = (ROOT / "src" / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert 'id="place-breakout"' in page_source
    assert 'id="breakout-input"' in page_source
    assert 'id="breakout-price-popup"' in page_source
    assert 'id="breakout-price-popup-input"' in page_source
    assert page_source.index('id="place-breakout"') > page_source.index('id="planning-panel"')
    assert page_source.count('id="place-breakout"') == 1
    assert "pane.candles.coordinateToPrice(param.point.y)" in script_source
    assert "handle.addEventListener('pointerdown'" in script_source
    assert "handle.setPointerCapture(event.pointerId)" in script_source
    assert "Math.hypot(event.clientX - gesture.startX" in script_source
    assert "openBreakoutPricePopup(gesture.originalPrice)" in script_source
    assert "commitBreakoutPrice(price" in script_source
    assert "const pendingSave = planningCommand(" in script_source
    assert "async function ensureWatchlistPlanForBreakout()" not in script_source
    assert "Added to Watchlist for breakout setup" not in script_source
    assert "byId('quick-plan-details').addEventListener('click', toggleBreakoutPlacement)" in script_source
    assert "byId('place-breakout').addEventListener('click', toggleBreakoutPlacement)" in script_source
    assert "axisLabelVisible: true, title: ''" in script_source
    assert "if (!state.breakoutMode || planningPending() || state.breakoutSaving) return" in script_source
    assert "pane.breakoutHandle.disabled = !state.breakoutMode" in script_source
    assert "byId('quick-plan-details').addEventListener('click'" in script_source
    assert "async function toggleBreakoutPlacement()" in script_source
    assert "planning-panel').classList.add('open')" not in script_source
    assert ".breakout-drag-handle" in style_source
    assert ".chart-pane.breakout-placement .breakout-drag-handle { pointer-events: auto;" in style_source
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in style_source
    assert "#quick-plan-details { grid-column:" not in style_source
    assert "align-items: center; justify-content: center" in style_source


def test_compact_chart_header_stays_on_one_row():
    style_source = (ROOT / "src" / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert ".chart-header { height: 52px; min-height: 52px;" in style_source
    assert ".chart-actions { display: none; }" in style_source
    assert ".company-context { display: none; }" in style_source
    assert 'aria-label="Previous stock"' in page_source
    assert 'aria-label="Next stock"' in page_source
    assert "#adr-chip { display: inline-flex; }" in style_source
    assert "#growth-1m-chip, #growth-3m-chip { display: inline-flex; }" in style_source
    assert "#earnings-chip { display: none; }" in style_source
    assert ".chart-metrics { display: none; }" not in style_source
    assert ".chart-metrics { flex: 0 0 auto; align-items: center; justify-content: flex-end; margin: 0 0 0 auto;" in style_source


def test_line_tool_matches_desktop_two_point_and_edit_workflow():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    style_source = (ROOT / "src" / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    page_source = (
        ROOT / "src" / "web" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert 'id="draw-line"' in page_source
    assert 'id="mobile-draw-line"' in page_source
    assert 'aria-pressed="false"' in page_source
    assert "function drawingChartPoint(pane, event)" in script_source
    assert "pane.candles.coordinateToPrice(point.y)" in script_source
    assert "function placeDrawingAnchor(pane, point)" in script_source
    assert "Line Tool remains active" in script_source
    assert "function hitTestDrawing(pane, point)" in script_source
    assert "persistDrawingUpdate(drag.original, drag.preview" in script_source
    assert "function drawingTimeCoordinate(pane, iso)" in script_source
    assert "const hasSavedDrawings = state.drawings.some(item => !item.deleted)" in script_source
    assert "item.timeframe === pane.timeframe" not in script_source
    assert "A daily point is stored at midnight" in script_source
    assert "1D + 1H ·" in script_source
    assert "event.key.toLowerCase() === 'd'" in script_source
    assert "event.key === 'Delete'" in script_source
    assert ".drawing-overlay" in style_source
    assert ".drawing-overlay[hidden] { display: none !important; }" in style_source
    assert "const overlayNeeded = state.drawMode || hasSavedDrawings" in script_source
    assert "Math.min(2, Math.max(1, window.devicePixelRatio || 1))" in script_source
    assert "chart.applyOptions({layout: chartLayout})" in script_source
    assert ".mobile-nav-button.active" in style_source
    assert "byId('mobile-draw-line').addEventListener('click', toggleLineTool)" in script_source
    assert "function buildFutureWhitespace(bars, timeframe)" in script_source
    assert "const sessionCount = 120" in script_source
    assert "pane.candles.setData([...bars, ...futureWhitespace])" in script_source
    assert "pane.relativeStrength.setData([...coveredRelativeData, ...futureWhitespace])" in script_source
    assert "inset: 0 auto auto 0; width: calc(100% - 68px); height: calc(100% - 26px)" in style_source


def test_mobile_chart_axes_are_touch_scalable_and_resettable():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert script_source.count("axisPressedMouseMove: {time: true, price: true}") == 2
    assert script_source.count("axisDoubleClickReset: {time: true, price: true}") == 2
    assert script_source.count("horzTouchDrag: true, vertTouchDrag: true") == 2
    assert script_source.count("kineticScroll: {mouse: false, touch: true}") == 2
    assert script_source.count("mouseWheel: true, pinch: true") == 2


def test_mobile_page_zoom_is_locked_without_disabling_chart_pinch():
    script_source = (ROOT / "src" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    style_source = (ROOT / "src" / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    for page_name in ("dashboard.html", "login.html"):
        page_source = (
            ROOT / "src" / "web" / "static" / page_name
        ).read_text(encoding="utf-8")
        assert "maximum-scale=1,user-scalable=no" in page_source
    assert "touch-action: pan-x pan-y" in style_source
    assert ".chart-surface {" in style_source
    assert "touch-action: none; background: #081310" in style_source
    assert script_source.count("mouseWheel: true, pinch: true") == 2


def test_desktop_shell_is_viewport_bound_with_internal_panel_scrolling():
    style_source = (ROOT / "src" / "web" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    assert "@media (min-width: 901px)" in style_source
    assert "html, body { height: 100%; min-height: 0; overflow: hidden; }" in style_source
    assert ".app-shell { height: 100vh; height: 100dvh; min-height: 0; overflow: hidden; }" in style_source
    assert ".stock-list { flex: 1 1 auto; min-height: 0; overflow-y: auto;" in style_source
    assert ".chart-pane.has-relative { grid-template-rows: minmax(0, 7fr) minmax(0, 3fr); }" in style_source
