# PyQt5 Trading Dashboard

A desktop trading dashboard for US-market swing trading with scanner workflows, Buy Board ORB planning, chart review, KIS account visibility, and guarded KIS order submission.

New to the project? Open the large-picture
[Project Tour](docs/project_tour.html), or use its
[plain-language text version](docs/project_tour.md). It explains what the two
computers do, what is synchronized, and why an enabled-looking screen still
cannot place an order unless every safety gate agrees.

## Current Capabilities

- KIS production account snapshots with account selection.
- Guarded KIS overseas order submission with a durable local order ledger.
- Conservative fill reconciliation from account snapshots; broker acceptance is not treated as a fill.
- Rule-based scanner presets backed by a KIS-registered US universe, Yahoo/KIS data paths, and MySQL caches.
- A read-only Market Pulse tab with cached broad-market, sector, industry, and thematic ETF performance ranked by completed-session daily returns.
- A precomputed Leadership and Market Context overlay on the TradingView-style chart, with an expandable calculation audit and no chart-time provider calls.
- A persisted, cross-device Watchlist planning stage available from the stock sidebar, Scanner, and TradingView; the former full Watchlist tab is not built.
- Chart-based `breakout_price` planning and Buy Board ORB execution, with an explicit Watchlist -> Buylist -> Buy Today progression.
- A read-only Buy Today `ORB Combinations...` comparison covering all 24 risk/window cases, kept separate from the optimized pre-market `Refresh / Select ORB Plans...` selector; the optimized view is read-only during regular market hours.
- ORB planning where entry is valid only after price clears both ORB high and the persisted buffered breakout price.
- A strategy-neutral `MarketSnapshot -> Strategy -> Signal` interface, with the existing ORB behavior as the first plugin.
- An append-only, redacted trading event journal and a read-only Health tab for KIS, MySQL, mirror freshness, and reconciliation status.
- Buy Board monitoring with partial-exit and EMA-close exit workflow support.
- A six-column operator-facing Kanban Buy Board (`Buylist` through `Sell All`) backed by one durable trade-card aggregate per production account and symbol. `WATCHLIST` remains a hidden, passive planning stage shown in the sidebar; `CLOSED` remains hidden for history.
- Typed, revision-fenced board commands: drag/drop records intent, while the background runtime and broker reconciliation own order effects and automatic lifecycle moves.
- Guarded Kanban entry, partial-exit, sell-all, stop-management, ownership, failover/readiness, and external-order review paths. The engine remains fail-closed unless its production gates are explicitly satisfied.
- Daily, hourly, TradingView, and intraday chart views with persisted drawings and breakout markers.
- Shutdown-safe local JSON persistence with atomic writes, rolling `.bak` recovery, and save-status metadata.

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
    buyboard/                   Kanban columns/cards, typed command dispatch, projections, runtime worker
    dialogs.py                  Settings and scanner-filter dialogs
    controllers/                Testable workflow controllers for UI-owned workflows
    health/                     Production health panel and background probe
    mixins/                     Tab rendering, widget callbacks, and UI glue inherited by MainWindow
  api/                          KIS account, order, intraday, and daily-price adapters
  core/                         Scanner, ORB, trade-card/Kanban, order, and execution models
  infrastructure/               Database schemas, repositories, refresh, and local-mirror support
  strategy/                     Strategy contracts and the built-in ORB plugin
  risk/                         Position sizing and final pre-trade approval
  services/                     App-state, Kanban runtime, execution gateway, reconciliation, persistence
  utils/                        Storage, config, Yahoo data loading, MySQL cache helpers
config/                         Non-secret configuration templates
data/                           Local JSON state and ticker universe files
rulebooks/                      Markdown trading rules used by review workflows
tests/                          Pytest regression suite
md_archive/                     Historical implementation notes and completed plans
```

UI mixins keep PyQt tab construction, widget callbacks, table refreshes, and log/state-save side effects close to the widgets. `src/ui/controllers/` owns workflows that are easier to unit test outside the full `MainWindow`, including KIS account sync, scanner orchestration, chart data loading, and execution-queue refresh/submission coordination.

The **Buy Board** is the operator surface for planning and execution. Its cards are read-only projections of canonical trade-card, ownership, and order state. Board gestures are revision-aware requests; they never call KIS directly or mark an order filled. See [Buy Board ORB Planning](docs/orb_buyboard_planning.md) for the planning controls and [Kanban Logic and Architecture](docs/kanban_architecture.md) for the lifecycle, runtime flow, component boundaries, and safety gates.

## Setup

1. Install the tested dependency graph: `python -m pip install --require-hashes -r requirements.lock`
2. Configure private database/KIS credentials in `.env` and non-secret local overrides in `config/runtime.local.json` when needed.
3. Run the app: `python main.py`
4. Run tests: `pytest -q`

The app can run without MySQL. Database-backed scanning and cache freshness features require valid `MYSQL_*` settings. For PC-independent execution coordination, configure the separate TLS-only `COORD_DB_*` SQL connection described in [TiDB Cloud Coordination Store](docs/tidb_coordination_store.md); historical prices are never uploaded there.

`requirements.txt` contains the intentionally supported direct dependency ranges.
`requirements.lock` pins the full Python 3.11/3.12 dependency graph and package
hashes used by CI and production machines. After intentionally changing a range,
regenerate the lock with `uv pip compile requirements.txt --python-platform
windows --python-version 3.11 --generate-hashes -o
requirements.lock`, then test both
supported Python versions before committing it.

If you're running this across two machines (a dev laptop + an always-on
data-refresh PC, sharing one MySQL database over LAN/Tailscale), see
[docs/pc_sync_data_pipeline.md](docs/pc_sync_data_pipeline.md) for the full
architecture and automation.

For live control, handoff, Buy Today publishing, and the distinction between
**Execution Owner** and **Operator Control**, see
[docs/execution_operator_control.md](docs/execution_operator_control.md).

## Configuration

Database/API credentials and private tokens are local-only and belong in `.env` (gitignored, never commit it). Non-secret hosts, ports, feature flags, limits, timing, and paths live in tracked `config/runtime.json`; workstation-specific overrides belong in gitignored `config/runtime.local.json`.

Every `python main.py` startup synchronizes `.env` with the credential-only
`.env.example` before configuration is loaded. Existing private values win,
new credential keys are added, and recognized legacy runtime keys are moved
without value changes to `config/runtime.local.json`. The same pass regenerates
the credential-only `.env.pc` and blanks every `MYSQL_*` credential for manual
PC configuration. Run
`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_pc_env.ps1`
when you want to apply the synchronization immediately without starting the
app.

The PC morning routine runs this synchronization immediately after its Git
update, so newly pulled credential and runtime schemas are applied before
refresh or app startup. `.env`, `.env.pc`, and `config/runtime.local.json`
remain gitignored. The application combines private credentials with tracked
runtime defaults and local runtime overrides; use `.env.pc` only as the initial
PC credential setup copy.

Only enable KIS intraday after the endpoint, TR ID, request parameters, output field, and raw OHLCV field mappings have been verified.

`QUANT_BACKUP_DIR` is optional -- see [docs/cloud_backup.md](docs/cloud_backup.md) for what it does and why (offsite backup of the gitignored `data/*.json` state files). Auto-detected if unset and a Google Drive for Desktop folder is present.

## Production Safety

- Keep `.env`, token caches, and local account state out of source control.
- Keep the live monitor off until the production account snapshot, order review, and reconciliation paths are verified.
- Treat successful KIS order submission as broker acceptance only.
- Treat every Buy Board gesture as requested intent, not broker confirmation. `Entry Pending` and `Closed` are system-owned columns reached only from reconciled broker truth.
- Keep the guarded Buy Board engine available. Engine availability never authorizes KIS mutations: use `KIS_LIVE_EXECUTION_MODE=DISABLED` for normal mutation-blocked operation, and promote only through the controlled-live runbook after the lease, reconciliation, database, WebSocket, mutation-budget, alerting, capital, and risk fences are verified.
- Preserve the one-owner rule for each `(environment, account_no, symbol)`: Kanban may execute only `KANBAN`-owned symbols for the configured `KANBAN_STRATEGY_INSTANCE_ID`; other cards remain observation-only.
- Manual PROD partial/full sells placed outside the U.S. regular session use a
  broker-held KIS market-on-open reservation. This prioritizes exit execution
  over price protection and still depends on KIS accepting and forwarding the
  reservation.
- Use `data/orders.json` as the durable local order ledger for idempotency and restart protection.
- Use the Health tab next to TradingView Chart to inspect current MySQL connectivity, KIS response age, mirror/reconciliation state, journal write health/free space, and the newest `data/event_journal.jsonl` lifecycle events. Health refreshes do not place orders or call KIS.
- Maintain verified WebSocket symbol keys with `python scripts/manage_kis_ws_symbol_keys.py`; the gitignored `data/kis_ws_symbol_keys.json` hot-reloads independently and the symbol map must not be stored in `.env` or `.env.pc`. See [KIS WebSocket Symbol Keys](docs/kis_ws_symbol_keys.md).
- Keep `data/legacy_non_prod_*.json`; these files preserve filtered paper-trading state without making it actionable.
- Keep local JSON `.bak` files and `data/state_metadata.json` with the rest of local runtime state.
- Do not bypass reconciliation when updating buylist position state after order submission.

## Documentation

- `PROJECT_ARCHITECTURE.md` is the canonical architecture and maintenance map.
- `docs/kanban_architecture.md` explains the Kanban state machine, command/runtime flow, persistence, safety boundaries, and component architecture.
- `docs/orb_buyboard_planning.md` explains Buffer %, the 24-case read-only comparison, Operator-Control-only pre-market ORB selection, market-hours read-only behavior, and published-plan immutability.
- `docs/kanban_production_readiness.md` records the detailed production invariants and rollout evidence requirements.
- `docs/market_pulse.md` documents the Market Pulse universe, EOD calculations, batched refresh, and idempotent cache schema.
- `docs/performance-audit.md` records reproducible responsiveness measurements, implemented optimizations, and remaining limits.
- `docs/wiki/` contains the Wiki-ready operator and maintainer guide set when the GitHub Wiki repository has not yet been initialized.
- `rulebooks/` contains active trading rules used by review workflows.
- `md_archive/` contains completed implementation notes and old planning documents that are not canonical.

## License

Proprietary
