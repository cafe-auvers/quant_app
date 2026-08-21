# PyQt5 Trading Dashboard

A desktop trading dashboard for US-market swing trading with scanner workflows, watchlist/ORB planning, a durable Kanban Buy Board, chart review, KIS account visibility, and guarded KIS order submission.

## Current Capabilities

- KIS production account snapshots with account selection.
- Guarded KIS overseas order submission with a durable local order ledger.
- Conservative fill reconciliation from account snapshots; broker acceptance is not treated as a fill.
- Rule-based scanner presets backed by a KIS-registered US universe, Yahoo/KIS data paths, and MySQL caches.
- Watchlist management with user-entered `breakout_price` levels for setup validation.
- ORB planning where entry is valid only after price clears both ORB high and the buffered breakout price.
- A strategy-neutral `MarketSnapshot -> Strategy -> Signal` interface, with the existing ORB behavior as the first plugin.
- An append-only, redacted trading event journal and a read-only Health tab for KIS, MySQL, mirror freshness, and reconciliation status.
- Buy dashboard monitoring with partial-exit and EMA-close exit workflow support.
- An eight-column Kanban Buy Board (`Watchlist` through `Closed`) backed by one durable trade-card aggregate per production account and symbol.
- Typed, revision-fenced board commands: drag/drop records intent, while the background runtime and broker reconciliation own order effects and automatic lifecycle moves.
- Guarded Kanban entry, partial-exit, sell-all, stop-management, ownership, failover/readiness, and external-order review paths. The engine remains fail-closed unless its production gates are explicitly satisfied.
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
    buyboard/                   Kanban columns/cards, typed command dispatch, projections, runtime worker
    dialogs.py                  Settings and scanner-filter dialogs
    controllers/                Testable workflow controllers for UI-owned workflows
    health/                     Production health panel and background probe
    mixins/                     Tab rendering, widget callbacks, and UI glue inherited by MainWindow
  api/                          KIS account, order, intraday, and daily-price adapters
  core/                         Scanner, watchlist, trade-card/Kanban, order, and execution models
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

UI mixins keep PyQt tab construction, widget callbacks, table refreshes, and log/state-save side effects close to the widgets. `src/ui/controllers/` owns workflows that are easier to unit test outside the full `MainWindow`, including KIS account sync, scanner orchestration, watchlist ORB refreshes, chart data loading, and buylist execution queue refresh/submission coordination.

The **Buy Board** is separate from the legacy Buy Dashboard. Its UI is a read-only projection of canonical trade-card, ownership, and order state. Board gestures are revision-aware requests; they never call KIS directly or mark an order filled. See [Kanban Logic and Architecture](docs/kanban_architecture.md) for the lifecycle, runtime flow, component boundaries, and safety gates.

## Setup

1. Install the tested dependency graph: `python -m pip install --require-hashes -r requirements.lock`
2. Configure local database and KIS credentials in `.env` when needed.
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

Database and API credentials are local-only and belong in `.env` (gitignored, never commit it). See `.env.example` for the full list of variables to fill in, covering MySQL connection settings, KIS broker API credentials, and optional integration keys.

After changing `.env`, regenerate the PC-specific copy with `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_pc_env.ps1`. The script duplicates every variable into `.env.pc`, preserves every non-MySQL value exactly, and blanks all `MYSQL_*` values for manual entry on the PC. Both `.env` and `.env.pc` are gitignored; copy `.env.pc` to the PC checkout as `.env` after filling the MySQL values.

Only enable KIS intraday after the endpoint, TR ID, request parameters, output field, and raw OHLCV field mappings have been verified.

`QUANT_BACKUP_DIR` is optional -- see [docs/cloud_backup.md](docs/cloud_backup.md) for what it does and why (offsite backup of the gitignored `data/*.json` state files). Auto-detected if unset and a Google Drive for Desktop folder is present.

## Production Safety

- Keep `.env`, token caches, and local account state out of source control.
- Keep the live monitor off until the production account snapshot, order review, and reconciliation paths are verified.
- Treat successful KIS order submission as broker acceptance only.
- Treat every Buy Board gesture as requested intent, not broker confirmation. `Entry Pending` and `Closed` are system-owned columns reached only from reconciled broker truth.
- Keep `BUYBOARD_ENGINE_ENABLED=false` unless the execution lease, account reconciliation, database, KIS WebSocket protocol/capacity, mutation-budget, alerting, and live-execution fences described in `.env.example` have been deliberately configured and verified.
- Preserve the one-owner rule for each `(environment, account_no, symbol)`: Kanban may execute only `KANBAN`-owned symbols for the configured `KANBAN_STRATEGY_INSTANCE_ID`; other cards remain observation-only.
- Manual PROD partial/full sells placed outside the U.S. regular session use a
  broker-held KIS market-on-open reservation. This prioritizes exit execution
  over price protection and still depends on KIS accepting and forwarding the
  reservation.
- Use `data/orders.json` as the durable local order ledger for idempotency and restart protection.
- Use the Health tab next to TradingView Chart to inspect current MySQL connectivity, KIS response age, mirror/reconciliation state, journal write health/free space, and the newest `data/event_journal.jsonl` lifecycle events. Health refreshes do not place orders or call KIS.
- Keep `data/legacy_non_prod_*.json`; these files preserve filtered paper-trading state without making it actionable.
- Keep local JSON `.bak` files and `data/state_metadata.json` with the rest of local runtime state.
- Do not bypass reconciliation when updating buylist position state after order submission.

## Documentation

- `PROJECT_ARCHITECTURE.md` is the canonical architecture and maintenance map.
- `docs/kanban_architecture.md` explains the Kanban state machine, command/runtime flow, persistence, safety boundaries, and component architecture.
- `docs/kanban_production_readiness.md` records the detailed production invariants and rollout evidence requirements.
- `rulebooks/` contains active trading rules used by review workflows.
- `md_archive/` contains completed implementation notes and old planning documents that are not canonical.

## License

Proprietary
