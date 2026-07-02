# main_window.py Split Plan

> Status: Completed and superseded by the live architecture map. The current codebase has the mixin split plus `src/ui/controllers/` for account, scanner, watchlist, buylist execution, and chart-data workflows. Use `PROJECT_ARCHITECTURE.md` as the canonical maintenance document.

## Current Assessment

The original mixin plan is directionally valid as a low-risk first refactor: it keeps one `MainWindow` object, preserves existing `self.*` state, and avoids redesigning PyQt signal ownership while reducing file size.

It is not risk-free. The split can cause breakage if methods are moved by line number, if imports are missed, if shared helpers are moved into the wrong domain, or if PyQt signals are connected more than once. Treat this as a mechanical extraction only. Do not change scanner rules, ORB logic, order execution behavior, chart behavior, or saved JSON formats during the split.

Current `src/ui/main_window.py` is still about 10k lines. The method line numbers in older notes are already stale, so this plan is symbol-based rather than line-number-based.

## Main Risks During The Split

1. **Missing module imports after moving methods**
   Python methods resolve globals in the module where they are defined. If a method using `pd`, `dt`, `QColor`, `QMessageBox`, `QTimer`, `QWebEngineView`, `json`, `html`, `quote`, etc. is moved into a mixin file, that mixin file must import those names itself.

2. **Shared helpers placed in one domain mixin**
   Helpers such as `_parse_float`, `_parse_int`, `_set_html_or_text`, `append_log`, status/progress methods, shortcut setup, and market/date formatting are used across several domains. Putting them inside `scanner_mixin.py` or `charts_mixin.py` creates hidden coupling. Keep them in `main_window.py` initially or move them into a small shared mixin.

3. **MRO and duplicate method names**
   Mixins are resolved left-to-right before `QMainWindow`. A duplicate method name in two mixins may silently call the wrong implementation. Before each phase, scan for duplicate method names in all mixins and `main_window.py`.

4. **PyQt multiple inheritance mistakes**
   Mixins must be plain Python classes. They must not inherit `QObject`, `QWidget`, or `QMainWindow`, and they should not define `__init__` unless it is cooperative and deliberately called. The only Qt base class should remain `QMainWindow` in `MainWindow`.

5. **Signal connection duplication**
   `_setup_tabs` and tab builder methods connect many signals. Moving code must not add extra connections. Do not reconnect signals outside the moved methods.

6. **Worker lifetime and async callbacks**
   Worker attributes such as `self.kis_order_worker`, `self.order_reconciliation_worker`, `self.intraday_fetch_worker`, `self.scanner_worker`, and related cleanup methods must remain on the same `MainWindow` instance. Moving methods is fine, but changing worker ownership is not.

7. **Static method tests**
   Tests call many helpers as `MainWindow._method(...)`. If a helper is converted into a standalone function, those tests and external callers break. During this split, keep these as inherited methods or provide compatibility wrappers.

8. **Cross-domain chart/scanner/watchlist coupling**
   Scanner preview, sidebar selection, watchlist ORB, and chart symbol controls call each other. Move by method ownership, not by nearby line range.

9. **Startup ordering**
   `__init__` initializes state, builds tabs, builds the status log, applies unresolved order startup state, starts timers, runs scans, and schedules KIS preload/reconciliation. Do not reorder this flow during the split.

10. **No Git repository**
   The workspace currently has no Git metadata. The old "commit after every phase" instruction is only valid if a repo is initialized. Without Git, create a backup copy or complete one phase at a time with tests before continuing.

## Refactor Rules

- One phase per change set.
- Do not change behavior while moving methods.
- Do not move methods by stale line ranges. Move by exact method/class names.
- Preserve method names, signatures, decorators, and class/static method status.
- Keep all `self.*` state declarations in `MainWindow.__init__`.
- Keep `_setup_tabs` in `main_window.py` until all tab builders are extracted and verified.
- Keep `_create_menu_bar`, `_build_status_log`, `append_log`, `update_progress`, `show_ready`, `show_refresh_error`, `show_refresh_complete`, `show_settings_dialog`, `_apply_shortcuts`, `show_about`, and `save_local_data` in `main_window.py` until the end. They are cross-cutting, not chart-specific.
- Keep `_parse_float` and `_parse_int` in `main_window.py` or a `shared_mixin.py`; do not place them only in `scanner_mixin.py`.
- Mixins must not import `MainWindow`.
- Run validation after every phase:
  - `python -m compileall main.py src tests -q`
  - `pytest -q`
- For UI smoke checks, also start the app manually and check the touched tab/workflow.

## Target File Structure

Recommended first-pass structure:

```text
src/ui/
  main_window.py
  dialogs.py
  mixins/
    __init__.py
    sidebar_mixin.py
    dashboard_mixin.py
    scanner_mixin.py
    watchlist_mixin.py
    buylist_mixin.py
    charts_controller_mixin.py
    charts_render_mixin.py
```

Recommended final `MainWindow` inheritance shape:

```python
class MainWindow(
    SidebarMixin,
    DashboardMixin,
    ScannerMixin,
    WatchlistMixin,
    BuylistMixin,
    ChartsControllerMixin,
    ChartsRenderMixin,
    QMainWindow,
):
    ...
```

`ChartsRenderMixin` should contain static HTML/SVG/rendering helpers. `ChartsControllerMixin` should contain tab widgets, chart state, symbol controls, fetch orchestration, bridge callbacks, and redraw commands. Keeping these separate makes the largest split safer and keeps renderer tests easier to reason about.

## What Should Stay In main_window.py Initially

Keep these in the shell until the domain mixins are stable:

- Imports and constants: `REFERENCE_SYMBOL`, `KST_ZONE`, `US_MARKET_ZONE`, market timing constants.
- `MainWindow.__init__`
- `_apply_global_stylesheet`
- `_apply_unresolved_order_startup_state`
- `_load_watchlist`, `_load_buylist`, `_load_trade_plans`, `_load_chart_drawings`, `_load_tab_options`, `_load_scanner_setups`
- `_save_state`
- `_normalize_tab_options`, `_normalize_scanner_setups`
- `closeEvent`
- `_clear_worker_reference`
- `_setup_tabs`
- `_add_configured_tab`
- `_create_menu_bar`
- `_build_status_log`
- `append_log`, progress/status display helpers
- settings/about/shortcut/save-local-data helpers
- `_parse_float`, `_parse_int`

These can be moved later into `shell_mixin.py` or `shared_mixin.py` only after the domain split is stable.

## Phase 0 - Baseline Inventory And Guards

Before moving anything:

1. Run:
   - `python -m compileall main.py src tests -q`
   - `pytest -q`
2. Save a method inventory:
   - `rg -n "^    def |^    @staticmethod|^    @classmethod|^class " src/ui/main_window.py`
3. Record all tests that import `MainWindow` or call `MainWindow._...`; those calls must remain valid.
4. Check current `main_window.py` line count only as a progress metric, not as an extraction guide.

## Phase 1 - Extract Dialogs

Target: `src/ui/dialogs.py`

Move:

- `SettingsDialog`
- `AddFilterDialog`

Important imports for `dialogs.py`:

- PyQt widgets used by the dialogs, including `QDialog`, `QVBoxLayout`, `QHBoxLayout`, `QGroupBox`, `QFormLayout`, `QPushButton`, `QLineEdit`, `QKeySequenceEdit`, `QMessageBox`, `QTreeWidget`, `QTreeWidgetItem`, `QAbstractItemView`, `QHeaderView`, `QLabel`
- `Qt`, `QColor`, `QKeySequence`
- `DEFAULT_SETTINGS`, `FILTER_CATALOG`, `SETTINGS_FILE`, `load_json`, `save_json`

Validation:

- Import `MainWindow`.
- Open Settings.
- Open Add Filter.
- Run compile and pytest.

## Phase 2 - Extract Sidebar

Target: `src/ui/mixins/sidebar_mixin.py`

Move:

- `_build_stock_sidebar`
- `refresh_sidebar_sources`
- `on_tab_changed`
- `_set_sidebar_source_to_watchlist`
- `refresh_stock_sidebar`
- `_get_sidebar_selected_data`
- `_get_sidebar_selected_symbol`
- `_sidebar_symbols`
- `on_sidebar_selection_changed`
- `apply_sidebar_selection_to_current_tab`
- `sidebar_add_selected_to_watchlist`
- `sidebar_load_trade_plan`
- `sidebar_show_chart`

Risks:

- Sidebar calls chart, scanner, watchlist, and trade-plan helpers. Those helpers may still live in `main_window.py` at this phase, which is fine.
- Do not move `_seed_trade_plan_fields` just because sidebar calls it.

Validation:

- Sidebar list populates for scanner/watchlist/buylist.
- Selecting a symbol updates the current tab.
- Add-to-watchlist from sidebar still works.

## Phase 3 - Extract Buylist And Order Execution UI

Target: `src/ui/mixins/buylist_mixin.py`

Move:

- `_build_buylist_env_panel`
- `_build_buylist_tab`
- `populate_buylist_dashboard`
- `_populate_buylist_env_table`
- `_buylist_compute_alerts`
- `_buylist_selected_item`
- `_buylist_activate_selected`
- `_buylist_deactivate_selected`
- `_buylist_sell_half_selected`
- `_buylist_sell_all_selected`
- `_buylist_remove_selected`
- `_buylist_move_to_breakeven_selected`
- `_toggle_buylist_monitor`
- `_run_buylist_monitor_cycle`
- `_buylist_refresh_item_data`
- `_compute_ema`
- `_first_account_no_for_environment`
- `_sell_intent_for_reason`
- `_has_duplicate_open_order`
- `_submit_kis_buy_order`
- `_submit_kis_sell_order`
- `_record_broker_order`
- `_on_buy_order_accepted`
- `_on_sell_order_accepted`
- `_on_buy_order_filled`
- `_on_sell_order_filled`
- `reconcile_open_orders`
- `_on_order_reconciliation_finished`
- `apply_confirmed_order_fills_to_buylist`
- `request_cancel_order`
- `_on_order_error`
- `_cleanup_order_worker`

Risks:

- This area is live-trading safety critical. Do not alter `submit_guarded_overseas_order`, duplicate-order checks, ledger writes, accepted-vs-filled behavior, or reconciliation behavior.
- Imports must include order model/ledger classes, workers, `QTimer`, `QMessageBox`, `QColor`, `datetime`, and any yfinance fallback imports used inside methods.

Validation:

- Existing `tests/test_order_lifecycle.py` must pass.
- Buylist table displays.
- Activate/deactivate do not submit orders unexpectedly.
- Duplicate open-order warning path still logs and blocks.

## Phase 4 - Extract Dashboard And Account/Fx Handling

Target: `src/ui/mixins/dashboard_mixin.py`

Move:

- `_build_dashboard_tab`
- KIS account combo/snapshot methods
- startup KIS preload methods
- FX refresh methods
- account-size application methods
- market-data summary/date formatting methods
- single-stock AI sidebar methods
- score helper methods `_score_growth_rank`, `_score_trend_intensity`, `_score_adr`
- `update_dashboard_summary`

Risks:

- `update_dashboard_summary` reads scanner, watchlist, buylist, trade manager, and DB state. It is cross-domain but can live in dashboard if imports are correct.
- Startup order uses KIS preload and reconciliation timers. Do not reorder timer scheduling in `__init__`.

Validation:

- Dashboard loads.
- KIS profile/account combo populates.
- FX refresh and account-size application still work.
- Dashboard summary still updates after scanner/watchlist/buylist changes.

## Phase 5 - Extract Scanner

Target: `src/ui/mixins/scanner_mixin.py`

Move:

- `_build_scanner_tab`
- scanner setup combo/rule UI methods
- scanner worker orchestration
- scanner result table methods
- scanner preview chart method `update_scanner_preview_chart`
- scanner-to-watchlist/trade-plan entry methods

Do not move `_parse_float` and `_parse_int` here unless they are also available to other mixins through a shared base.

Risks:

- Scanner preview uses chart rendering helpers.
- Scanner ORB scoring uses watchlist/ORB sizing helpers.
- Keep scanner rules and scoring behavior unchanged.

Validation:

- Run all scanner tests.
- Run a scanner in the UI.
- Add scanner result to watchlist.

## Phase 6 - Extract Watchlist And ORB Plan UI

Target: `src/ui/mixins/watchlist_mixin.py`

Move:

- `_build_watchlist_tab`
- watchlist table population and selection methods
- watchlist AI review methods
- move-to-buylist methods
- ORB sizing/static helpers
- ORB plan table methods
- watchlist ORB status methods
- breakout-price load/save methods
- `on_watchlist_selection_changed`
- `add_manual_watchlist_item`
- `_seed_trade_plan_fields`
- `update_trade_prices_from_latest`
- dead trade-plan stubs: `calculate_position_size`, `review_trade`, `update_trade_plan_feedback`, `save_trade_plan`, `populate_trade_plan_table`, `load_saved_trade_plan`

Risks:

- Breakout price migration and ORB trigger logic must remain unchanged.
- Several static ORB helpers are tested as `MainWindow._...`; they must remain inherited methods.
- `on_watchlist_selection_changed` is currently near the end of the file, not near the watchlist builder. Move by name.

Validation:

- Watchlist loads.
- Breakout Price field persists.
- ORB panel renders.
- Move to buylist still works.
- ORB-related tests pass.

## Phase 7A - Extract Chart Render Helpers

Target: `src/ui/mixins/charts_render_mixin.py`

Move only static/pure-ish rendering helpers:

- `_normalize_chart_history`
- `_coerce_timestamp_for_index`
- `_get_visible_time_window`
- `_merge_chart_histories`
- `_normalize_chart_merge_index`
- `_generate_message_html`
- `_to_tradingview_symbol`
- `_tradingview_refresh_due`
- `_generate_tradingview_widget_html`
- `_generate_tradingview_chart_url`
- `_get_js_key_condition`
- `_generate_tradingview_lightweight_chart_html`
- `_generate_local_chart_html`
- `_normalize_chart_options`
- `_format_chart_header_metrics`
- `_growth_percent`
- `_format_percent_metric`
- `_future_weekday_dates`
- `_align_chart_indicators`
- `_generate_indicator_panel_svg`
- `_build_price_series`

Risks:

- These methods are heavily tested as `MainWindow._...`.
- They use many globals: `pd`, `dt`, `json`, `html`, `quote`, `QWebEngineView` may not be needed here but chart constants/imports are.

Validation:

- All chart HTML tests pass.
- No UI smoke required beyond chart render if tests pass, but manual chart check is still recommended.

## Phase 7B - Extract Chart Controller/UI

Target: `src/ui/mixins/charts_controller_mixin.py`

Move:

- `_build_charts_tab`
- `_build_tradingview_tab`
- `_build_intraday_charts_tab`
- `_build_trade_plan_tab` (dead/legacy; keep uncalled)
- chart symbol combo methods
- TradingView load/render methods
- intraday chart methods
- intraday fetch/cache methods
- chart mode/button/bridge callbacks
- drawing persistence callbacks
- chart navigation methods
- `plot_selected_symbol`
- `_draw_placeholder_chart`

Do not move `_build_status_log`, `append_log`, progress/status methods, settings, shortcuts, about, or `save_local_data` in this phase. They are shell-level.

Risks:

- Chart bridge calls methods by name on `MainWindow`; names must not change.
- `QWebChannel`/`QWebEngineView` optional imports must remain correctly guarded.
- Intraday fetch worker callbacks must keep worker references alive.

Validation:

- Charts tab renders.
- TradingView tab renders.
- Intraday Charts tab renders.
- Set/clear Breakout Price works.
- Draw/update/delete/clear chart lines works.
- Intraday fetch fallback still works.

## Optional Phase 8 - Extract Shell/Shared UI Helpers

Only after all domain mixins are stable, consider a small `shared_mixin.py` or leave these in `main_window.py` permanently:

- `_parse_float`, `_parse_int`
- `_build_status_log`
- `append_log`
- progress/status helpers
- settings/about/shortcuts/save-local-data helpers

This phase is optional. Keeping `main_window.py` as a 1k-2k line shell is acceptable if it owns initialization, tab wiring, state loading/saving, and cross-cutting UI utilities.

## Import Checklist For Every Mixin

For each new mixin module:

1. Import every global referenced by moved methods.
2. Do not rely on imports from `main_window.py`.
3. Do not import `MainWindow`.
4. Preserve `@staticmethod` and `@classmethod` decorators.
5. Preserve type hints only if required imports are cheap and safe. Otherwise use `from __future__ import annotations`.
6. Keep optional WebEngine/WebChannel imports guarded.
7. Keep module-level constants either imported from `main_window.py` only if that does not create cycles, or moved to a neutral `src/ui/constants.py`.

Recommended neutral constants file if needed:

```text
src/ui/constants.py
  REFERENCE_SYMBOL
  KST_ZONE
  US_MARKET_ZONE
  MARKET_DATA_READY_TIME_KST
  LIVE_INTRADAY_REFRESH_INTERVAL_MS
  TRADINGVIEW_REFRESH_INTERVAL_SECONDS
  KIS_DAILY_CHART_FAILURE_COOLDOWN_SECONDS
  US_MARKET_OPEN_TIME
  US_MARKET_CLOSE_TIME
```

Do not create `constants.py` unless imports become awkward. It is optional.

## Validation Matrix

Run after every phase:

```text
python -m compileall main.py src tests -q
pytest -q
```

Targeted manual checks by phase:

| Phase | Manual Check |
|---|---|
| 1 | Settings and Add Filter dialogs |
| 2 | Sidebar source switch, symbol selection, add to watchlist |
| 3 | Buylist table, activate/deactivate, no fill on acceptance, duplicate guard |
| 4 | Dashboard, KIS account snapshot, FX refresh, summary |
| 5 | Scanner run, result selection, add to watchlist |
| 6 | Watchlist table, ORB panel, breakout price, move to buylist |
| 7A | Chart HTML tests, static helper tests |
| 7B | Charts/TradingView/Intraday tabs, drawings, breakout marker, fetch |

## Progress Tracker

| Phase | Target | Status |
|---|---|---|
| 0 | Baseline inventory and validation | Completed |
| 1 | `src/ui/dialogs.py` | Completed |
| 2 | `src/ui/mixins/sidebar_mixin.py` | Completed |
| 3 | `src/ui/mixins/buylist_mixin.py` | Completed |
| 4 | `src/ui/mixins/dashboard_mixin.py` | Completed |
| 5 | `src/ui/mixins/scanner_mixin.py` | Completed |
| 6 | `src/ui/mixins/watchlist_mixin.py` | Completed |
| 7A | `src/ui/mixins/charts_render_mixin.py` | Completed |
| 7B | `src/ui/mixins/charts_controller_mixin.py` | Completed |
| 8 | optional shared/shell helper extraction | Superseded by `src/ui/controllers/` and existing shared shell helpers |
