# Quant App Architecture

This document describes the current architecture of the PyQt5 trading dashboard as implemented by `main.py` and `src/`. It is the maintenance map for the live codebase.

## Product Scope

Quant App is a desktop trading dashboard for US-market swing trading, scanner review, watchlist analysis, ORB planning, KIS account visibility, and guarded KIS order submission.

The application is not a headless service. `main.py` creates a `QApplication`, installs a small Qt warning filter, imports `src.ui.main_window.MainWindow`, and starts the PyQt event loop.

## Runtime Entry Flow

```text
main.py
  -> QApplication
  -> src.ui.main_window.MainWindow
      -> load local JSON state
      -> initialize optional MySQL engine
      -> build tabs, sidebar, status log, and menus
      -> preload KIS profiles and account data
      -> run scanner/watchlist/chart workers through QThread
      -> reconcile any open broker orders from the local order ledger
```

Long-running work runs in `QThread` workers so the PyQt UI remains responsive.

## Directory Layout

```text
quant_app/
  main.py                         Application entry point
  src/
    ui/                           PyQt windows, workers, chart bridge, UI constants
    core/                         Trading domain models and pure business logic
    services/                     App-state persistence and order lifecycle services
    utils/                        Storage, configuration, market-data, and MySQL helpers
    api/                          KIS API adapters and order/account helpers
  data/                           Local JSON state and ticker universe files
  rulebooks/                      Markdown trading rules used by review workflows
  tests/                          Pytest regression suite
  config/                         Non-secret configuration template
  md_archive/                     Historical implementation notes and completed plans
```

Generated files such as `__pycache__/` and `.pytest_cache/` are not part of the architecture and should remain ignored.

## UI Layer

`src/ui/main_window.py` owns the `MainWindow` shell: application state, startup ordering, tab registration, menus, status/progress helpers, persistence entry points, and shared parsing/formatting helpers. Domain-heavy UI behavior is split into plain Python mixins inherited by `MainWindow`; the mixins do not inherit Qt classes and do not import `MainWindow`.

Current inheritance shape:

```text
MainWindow(
  SidebarMixin,
  DashboardMixin,
  ScannerMixin,
  WatchlistMixin,
  BuylistMixin,
  ChartsControllerMixin,
  ChartsRenderMixin,
  QMainWindow,
)
```

Supporting UI modules:

| Module | Responsibility |
|---|---|
| `src/ui/main_window.py` | Main shell, startup ordering, local state loading/saving, tab registration, menus, status log, shared helpers |
| `src/ui/dialogs.py` | Settings dialog and scanner filter dialog |
| `src/ui/mixins/sidebar_mixin.py` | Left sidebar source switching, selected-symbol routing, and sidebar actions |
| `src/ui/mixins/dashboard_mixin.py` | Dashboard tab, KIS account snapshots/profile selection, FX refresh, account-size application, summary widgets |
| `src/ui/mixins/scanner_mixin.py` | Scanner tab, scanner setup/rule UI, scanner worker orchestration, scanner result actions |
| `src/ui/mixins/watchlist_mixin.py` | Watchlist tab, breakout-price persistence, ORB planning UI, AI review, move-to-buylist flow |
| `src/ui/mixins/buylist_mixin.py` | Buy dashboard, monitoring controls, order submission UI, order reconciliation callbacks |
| `src/ui/mixins/charts_render_mixin.py` | Chart HTML/SVG generation, TradingView symbol formatting, indicator panel rendering, chart data normalization helpers |
| `src/ui/mixins/charts_controller_mixin.py` | Chart tabs, symbol controls, chart fetch/cache orchestration, drawing callbacks, chart bridge actions |
| `src/ui/workers.py` | `QThread` workers for refreshes, KIS snapshots, KIS order submission, order reconciliation, intraday fetches, scanner runs, and review jobs |
| `src/ui/chart_bridge.py` | `QWebChannel` bridge used by chart JavaScript to persist drawings and breakout-price markers |
| `src/ui/filter_catalog.py` | Default scanner setups, scanner metric labels, tab defaults, and settings defaults |

Current tab construction in `_setup_tabs()`:

| Tab key | Label | Builder |
|---|---|---|
| `dashboard` | Dashboard | `_build_dashboard_tab()` |
| `scanner` | Scanner | `_build_scanner_tab()` |
| `watchlist` | Watchlist | `_build_watchlist_tab()` |
| `buylist` | Buy Dashboard | `_build_buylist_tab()` |
| `charts` | Charts | `_build_charts_tab()` |
| `tradingview` | TradingView Chart | `_build_tradingview_tab()` |
| `intraday_charts` | Intraday Charts | `_build_intraday_charts_tab()` |

`data/tab_options.json` persists tab visibility. The legacy `_build_trade_plan_tab()` method still exists for compatibility and tests, but it is not currently added by `_setup_tabs()`.

## Worker Layer

Workers live in `src/ui/workers.py`.

| Worker | Purpose |
|---|---|
| `RefreshWorker` | Refresh daily price history and indicators into MySQL |
| `HourlyRefreshWorker` | Refresh hourly price history into MySQL |
| `KisAccountWorker` | Fetch one KIS account snapshot |
| `KisStartupAccountsWorker` | Preload configured KIS SIM/PROD account profiles |
| `FxRateWorker` | Resolve USD/KRW from KIS snapshot data or fallback sources |
| `KisOrderWorker` | Submit KIS overseas orders and emit broker acceptance/rejection state |
| `OrderReconciliationWorker` | Fetch account snapshots and reconcile open broker orders against holdings deltas |
| `IntradayFetchWorker` | Fetch one symbol's intraday bars |
| `IntradayBulkFetchWorker` | Fetch intraday bars for multiple symbols |
| `ScannerWorker` | Run scanner rules over loaded metrics |
| `WatchlistAiWorker` | Review watchlist items in batch |
| `SingleStockAiWorker` | Review one stock/setup |

## Service Layer

| Module | Responsibility |
|---|---|
| `src/services/app_state.py` | `StateSaveManager`, save-result tracking, metadata writes, and compatibility helpers for watchlist, buylist, trade plans, scanner setups, drawings, and tab options |
| `src/services/intraday_provider.py` | Provider-neutral request/result contracts and OHLCV normalization/resampling helpers |
| `src/services/intraday_data_service.py` | KIS-first intraday orchestration, yfinance fallback, and best-source cache loading |
| `src/services/kis_intraday_provider.py` | KIS intraday provider wrapper using existing SIM/PROD account config |
| `src/services/yfinance_intraday_provider.py` | yfinance intraday fallback provider preserving existing retry behavior |
| `src/services/order_ledger.py` | Persistent local order ledger stored at `data/orders.json` |
| `src/services/order_execution_service.py` | Guarded KIS order submission with durable idempotency before and after API calls |
| `src/services/order_reconciliation.py` | Conservative account-snapshot reconciliation for accepted/working broker orders |

The service layer contains persistence and lifecycle logic that is not specific to one widget.

## Core Domain Modules

| Module | Responsibility |
|---|---|
| `src/core/scanner.py` | `StockScanner`, `ScanRule`, and comparison operators for rule-based filtering |
| `src/core/watchlist.py` | `Watchlist`, `TradePlanManager`, `BuylistManager`, and persistence-ready dataclasses |
| `src/core/position_sizer.py` | Fixed-risk and fixed-dollar position sizing calculations |
| `src/core/orb.py` | Opening range breakout range calculation, signal evaluation, and intraday resampling |
| `src/core/trade_reviewer.py` | Rulebook-backed trade setup review model |
| `src/core/scoring.py` | Deterministic scoring, optional OpenAI review, fallback analysis, and HTML rendering |
| `src/core/order_state.py` | Broker order lifecycle enums and `BrokerOrder` persistence model |

The buylist is the local monitoring model. Broker orders are now tracked separately in the order ledger and only applied back to buylist positions after fill evidence is confirmed.

## Data and Persistence

Local JSON state is read/written through `src/utils/storage.py` and service helpers. Writes use a temp file followed by atomic replace. When an existing JSON file is overwritten, `save_json()` first keeps a rolling `.bak` copy; `load_json()` falls back to that backup if the main file is missing or malformed.

| File | Purpose |
|---|---|
| `data/watchlist.json` | User watchlist items |
| `data/buylist.json` | Buy dashboard and monitoring items |
| `data/trade_plans.json` | Saved trade plans |
| `data/scanner_setups.json` | Named scanner rule presets |
| `data/chart_drawings.json` | Saved chart line drawings; watchlist breakout prices are persisted in `data/watchlist.json` |
| `data/tab_options.json` | Tab visibility settings |
| `data/orders.json` | Local broker-order ledger, created when the first order is recorded |
| `data/state_metadata.json` | Optional sidecar with last successful/failed app-state save time, last error, and files written |
| `data/us_kis_tickers.csv` | Cached KIS-registered US stock universe used by scanner refreshes |
| `data/sp500_tickers.csv` | Cached S&P 500 fallback universe |

`data/settings.json` may be created when settings or shortcuts are saved.

Critical local state files keep one rolling `.bak` backup beside the JSON file, including watchlist, buylist, trade plans, orders, and execution queue state. The app does not wrap existing JSON payloads in a schema envelope, so legacy loaders keep their current formats.

`MainWindow.closeEvent()` requests interruption for active background workers, waits with one shared bounded shutdown budget, then attempts a final synchronous app-state save and waits briefly for pending background saves. Normal UI save calls still schedule background saves through `save_app_state()`, but those threads are tracked and non-daemon.

## Market Data Layer

`src/utils/data_loader.py` provides Yahoo Finance based market data:

- KIS overseas master loading/caching for the default US stock universe, with S&P 500 fallback.
- Price history download through Yahoo chart endpoints.
- Multi-symbol history normalization.
- Technical metric calculation used by scanning and charts.

`src/utils/db_loader.py` provides optional MySQL-backed caching:

- `price_history` for daily and interval-aware historical data.
- `hourly_price_history` for hourly chart data.
- `intraday_price_history` for 1m/5m intraday bars, keyed by `source`.
- `chart_indicators` for RS/TI65-style chart overlays.
- `scanner_metrics` for scanner-ready metrics.

The app can run without MySQL. When MySQL is configured, refresh and scanner workflows use cached tables for speed and freshness checks.

## Intraday Source Architecture

Intraday data is provider-based and source-explicit:

```text
IntradayFetchWorker / IntradayBulkFetchWorker
  -> IntradayRequest
  -> fetch_intraday_with_fallback()
      -> KIS provider if enabled/configured/working
      -> yfinance provider if KIS is disabled, unavailable, or returns no usable bars and fallback is allowed
  -> save_intraday_history_to_db(..., source="kis" or "yfinance")
  -> ORB/chart workflows load best cached source
```

Source priority for cached ORB/chart reads:

1. `source="kis"`
2. `source="yfinance"`
3. legacy/unfiltered rows for backward compatibility

KIS intraday is disabled by default. `src/api/kis_intraday.py` does not hardcode unverified endpoint paths, TR IDs, raw output names, or raw OHLCV field names. Enabling it requires explicit `.env` endpoint/TR ID/field mappings verified from official KIS documentation or a successful manual API test.

The ORB engine in `src/core/orb.py` remains source-agnostic. It consumes normalized `Open`, `High`, `Low`, `Close`, `Volume` DataFrames for the existing 1m, 5m, and 30m windows.

## KIS Integration

| Module | Purpose |
|---|---|
| `src/api/kis_account_snapshot_dual.py` | SIM/PROD config, token handling, domestic/overseas snapshots, account profile discovery |
| `src/api/kis_fetch_all_daily.py` | KIS daily price fetches and domestic master parsing |
| `src/api/kis_intraday.py` | Configuration-gated KIS intraday adapter and raw-row normalization |
| `src/api/kis_order.py` | Overseas order submission wrapper that returns broker acceptance/rejection state |
| `src/api/kis_order_status.py` | Explicit placeholders for direct order status/cancel endpoints until verified TR IDs are implemented |
| `src/api/kis_config.py` | Compatibility loader for legacy PROD env variable access |

KIS credentials are loaded from `.env`, for example:

```text
KIS_SIM_APP_KEY
KIS_SIM_APP_SECRET
KIS_SIM_ACCOUNT_NO
KIS_PROD_APP_KEY
KIS_PROD_APP_SECRET
KIS_PROD_ACCOUNT_NO
```

Multiple accounts can be configured with numbered variables such as `KIS_PROD_ACCOUNT_NO_2`. Token caches are local runtime files and are ignored by git.

KIS intraday activation keys:

```text
KIS_INTRADAY_ENABLED=false
KIS_OVERSEAS_INTRADAY_ENDPOINT=
KIS_OVERSEAS_INTRADAY_TR_ID=
KIS_OVERSEAS_INTRADAY_TIME_FIELD=
KIS_OVERSEAS_INTRADAY_OPEN_FIELD=
KIS_OVERSEAS_INTRADAY_HIGH_FIELD=
KIS_OVERSEAS_INTRADAY_LOW_FIELD=
KIS_OVERSEAS_INTRADAY_CLOSE_FIELD=
KIS_OVERSEAS_INTRADAY_VOLUME_FIELD=
KIS_OVERSEAS_INTRADAY_OUTPUT_FIELD=output2
```

Optional:

```text
KIS_OVERSEAS_INTRADAY_PARAMS_JSON={"SYMB":"{symbol}","EXCD":"{exchange}","DATE":"{date}"}
```

Only enable KIS intraday after the endpoint, TR ID, request params, output field, and OHLCV field names have been verified.

## KIS Order Lifecycle

KIS order handling is intentionally split into submission, local ledgering, and fill reconciliation.

```text
Buy/Sell UI action
  -> KisOrderWorker
  -> submit_guarded_overseas_order()
  -> duplicate-open-order check in data/orders.json by environment, account, symbol, side, and intent
  -> BrokerOrder(status=CREATED) written to data/orders.json before KIS API call
  -> BrokerOrder(status=SUBMITTING) written before request is sent
  -> src.api.kis_order.place_overseas_order()
  -> BrokerOrder(status=ACCEPTED or REJECTED) written with raw submit response/error
  -> UI status becomes BUY_SUBMITTED / SELL_SUBMITTED / PARTIAL_EXIT_SUBMITTED
  -> OrderReconciliationWorker compares KIS holdings snapshots
  -> confirmed fills update buylist shares, cost, sold status, and partial-exit stop behavior
```

Important safety rules:

- A successful KIS API order response means broker acceptance only. It does not mean filled.
- Buylist positions are not marked `BOUGHT`, `SOLD`, or partially exited from submission responses.
- Open-order duplicate checks prevent repeated submission for the same environment, account, symbol, side, and intent.
- Startup loads unresolved orders from `data/orders.json`, marks matching buylist rows as submitted/pending, and blocks duplicate execution after restart.
- SIM and PROD are isolated by `environment`, and multiple accounts are isolated by `account_no`.
- Account snapshot deltas are used as fill evidence. Ambiguous cases remain `WORKING` rather than being treated as filled.
- Partial fills are idempotent through `BrokerOrder.applied_filled_quantity`, so repeated reconciliation cannot double-apply the same fill.
- Cancel requests can be represented locally as `CANCEL_REQUESTED`; direct KIS cancel/status endpoint wrappers intentionally raise until the exact endpoints/TR IDs are verified.

## Charting

The chart experience is generated by `MainWindow` and coordinated with `ChartBridge`:

- TradingView Lightweight Charts HTML/JavaScript is generated locally.
- `QWebEngineView` is used when PyQtWebEngine is installed.
- Fallback text is shown when WebEngine is unavailable.
- Chart drawings are saved through `QWebChannel` into `data/chart_drawings.json`.
- Breakout prices are user-entered daily structural levels persisted on watchlist items. Legacy `target_price` JSON values are migrated into `breakout_price` only when no breakout price exists.
- ORB entry validation uses `entry_trigger = max(orb_high, breakout_price * (1 + buffer_pct))`; profit management uses rule-based exits rather than fixed take-profit targets.
- Daily, hourly, and intraday views use normalized OHLCV DataFrames.
- RS/TI65 and growth overlays load from MySQL indicators when available, with local fallbacks.

## Scanner Flow

```text
Ticker universe
  -> Yahoo/KIS/MySQL history sources
  -> compute_stock_metrics / scanner_metrics cache
  -> StockScanner rules
  -> scanner results table
  -> optional add to watchlist or buylist
```

Scanner setups are persisted in `data/scanner_setups.json`. Rules use labels from `src/ui/filter_catalog.py`.

## Watchlist, ORB, and Buylist Flow

```text
Watchlist symbol
  -> latest daily/hourly/intraday history
  -> deterministic score and optional AI review
  -> ORB range from 1m/5m/30m bars
  -> account/risk-aware position plan
  -> optional buylist monitoring
  -> optional guarded KIS order submission
```

Account value comes from the selected KIS profile when a snapshot is available. Otherwise the UI falls back to manual/default account-size values. USD/KRW conversion is tracked in the UI and refreshed separately.

## AI and Rulebooks

Rulebooks live under `rulebooks/` and are loaded by `TradeReviewer`. `src/core/scoring.py` can call OpenAI when `OPENAI_API_KEY` is present. If the key is missing or a request fails, deterministic fallback analysis keeps the UI workflow functional.

Current rulebook files:

- `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md`
- `rulebooks/fundamental_rules.md`
- `rulebooks/risk_management.md`
- `rulebooks/technical_rules.md`

## Configuration

Runtime configuration is environment-driven:

| Key family | Used by | Purpose |
|---|---|---|
| `MYSQL_*` | `src/utils/config.py`, `src/utils/db_loader.py` | Optional MySQL cache connection |
| `KIS_SIM_*`, `KIS_PROD_*` | KIS account/order modules | KIS account snapshots and order workflows |
| `OPENAI_API_KEY` | `src/core/scoring.py` | Optional AI review |

The `.env` file is local-only and ignored by git. `config/template_config.py` remains a non-secret example configuration file.

## Tests

The test suite is pytest-based:

```text
python -m compileall main.py src tests -q
pytest -q
```

Coverage includes scanner rules, scoring, position sizing, ORB logic, watchlist and buylist persistence, local JSON backup/recovery and shutdown flushing, MySQL helper behavior, KIS account config/profile parsing, selected `MainWindow` formatting/helpers, refactor boundaries, and KIS order lifecycle safety.
Intraday provider coverage includes KIS disabled/configuration errors, yfinance fallback behavior, source-priority cache loading, ORB invariance across normalized provider data, 1m-to-5m resampling, and worker signal payload shape.

## Production Safety Notes

- Keep secrets out of source. `.env` and `.kis_token_cache*.json` are local runtime files.
- KIS PROD order paths require valid credentials and should be smoke-tested in SIM before real trading.
- KIS intraday remains configuration-gated. Do not enable it until endpoint/TR ID/request params/raw field mappings are verified.
- yfinance fallback remains available for intraday/ORB workflows when KIS intraday is disabled or unavailable.
- Do not treat KIS order acceptance as a fill. Confirm fills through verified order status endpoints or conservative account snapshot reconciliation.
- MySQL is optional, but production workflows that depend on scanner/cache freshness should configure `MYSQL_*` and validate refresh jobs.
- `data/` files are local state unless intentionally replaced with sanitized sample data.
- Keep generated `.bak` files and `data/state_metadata.json` out of source control with the rest of local runtime state.
