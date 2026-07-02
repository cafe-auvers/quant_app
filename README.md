# PyQt5 Trading Dashboard

A desktop trading dashboard for US-market swing trading with scanner workflows, watchlist/ORB planning, chart review, KIS account visibility, and guarded KIS order submission.

## Current Capabilities

- KIS SIM/PROD account snapshots with account profile selection.
- Guarded KIS overseas order submission with a durable local order ledger.
- Conservative fill reconciliation from account snapshots; broker acceptance is not treated as a fill.
- Rule-based scanner presets backed by a KIS-registered US universe, Yahoo/KIS data paths, and MySQL caches.
- Watchlist management with user-entered `breakout_price` levels for setup validation.
- ORB planning where entry is valid only after price clears both ORB high and the buffered breakout price.
- Buy dashboard monitoring with partial-exit and EMA-close exit workflow support.
- Daily, hourly, TradingView, and intraday chart views with persisted drawings and breakout markers.
- Shutdown-safe local JSON persistence with atomic writes, rolling `.bak` recovery, and save-status metadata.
- Optional OpenAI-backed trade review with deterministic fallback analysis.

## Strategy Terminology

The app does not use fixed profit targets or R/R-based take-profit levels for the active ORB workflow.

- `breakout_price` is the user-entered daily structural breakout level.
- ORB entry trigger is `max(orb_high, breakout_price * (1 + buffer_pct))`.
- Legacy saved JSON that contains `target_price` and no `breakout_price` is migrated into `breakout_price`.
- Profit management is rule based: first partial exit after 3-5 days if the trade has worked, then hold remaining shares while momentum continues, with final exit on a close below the selected EMA, usually 10 EMA or 20 EMA.

## Project Structure

```text
main.py                         Application entry point
src/
  ui/
    main_window.py              MainWindow shell, state loading, menus, tabs, shared helpers
    dialogs.py                  Settings and scanner-filter dialogs
    controllers/                Testable workflow controllers for UI-owned workflows
    mixins/                     Tab rendering, widget callbacks, and UI glue inherited by MainWindow
  api/                          KIS account, order, intraday, and daily-price adapters
  core/                         Scanner, watchlist, ORB, scoring, sizing, order and execution queue models
  services/                     App-state, intraday, order ledger, execution, reconciliation
  utils/                        Storage, config, Yahoo data loading, MySQL cache helpers
config/                         Non-secret configuration templates
data/                           Local JSON state and ticker universe files
rulebooks/                      Markdown trading rules used by review workflows
tests/                          Pytest regression suite
md_archive/                     Historical implementation notes and completed plans
```

UI mixins keep PyQt tab construction, widget callbacks, table refreshes, and log/state-save side effects close to the widgets. `src/ui/controllers/` owns workflows that are easier to unit test outside the full `MainWindow`, including KIS account sync, scanner orchestration, watchlist ORB refreshes, chart data loading, and buylist execution queue refresh/submission coordination.

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Configure local database and KIS credentials in `.env` when needed.
3. Run the app: `python main.py`
4. Run tests: `pytest -q`

The app can run without MySQL. Database-backed scanning and cache freshness features require valid `MYSQL_*` settings.

## Configuration

Database and API credentials are local-only and belong in `.env`.

```text
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_password_here
MYSQL_DB=quant_app

KIS_SIM_APP_KEY=your_sim_key
KIS_SIM_APP_SECRET=your_sim_secret
KIS_SIM_ACCOUNT_NO=12345678-01
KIS_PROD_APP_KEY=your_prod_key
KIS_PROD_APP_SECRET=your_prod_secret
KIS_PROD_ACCOUNT_NO=87654321-01

KIS_INTRADAY_ENABLED=false
OPENAI_API_KEY=
```

Only enable KIS intraday after the endpoint, TR ID, request parameters, output field, and raw OHLCV field mappings have been verified.

## Production Safety

- Keep `.env`, token caches, and local account state out of source control.
- Smoke-test KIS order workflows in SIM before using PROD.
- Treat successful KIS order submission as broker acceptance only.
- Use `data/orders.json` as the durable local order ledger for idempotency and restart protection.
- Keep local JSON `.bak` files and `data/state_metadata.json` with the rest of local runtime state.
- Do not bypass reconciliation when updating buylist position state after order submission.

## Documentation

- `PROJECT_ARCHITECTURE.md` is the canonical architecture and maintenance map.
- `rulebooks/` contains active trading rules used by review workflows.
- `md_archive/` contains completed implementation notes and old planning documents that are not canonical.

## License

Proprietary
