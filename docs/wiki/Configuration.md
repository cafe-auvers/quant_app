# Configuration

The deployment verified on 2026-10-05 selects `COORD_DB_BACKEND=postgresql`, Session pooler
port 5432, database `postgres`, and private schema `quant_coordination`. Preserve those
local overrides on PC, laptop, and the web deployment; tracked `mysql` defaults support
legacy installations and do not select an active TiDB service. `MYSQL_*` still describes PC
historical prices. Optional web `supabase_enabled` is unrelated to the coordination backend.
See [Supabase Deployment](Supabase-Deployment).

Configuration is split by sensitivity. `.env` contains credentials only, while
non-secret hosts, ports, flags, limits, and timing values belong in
`config/runtime.json` or the gitignored `config/runtime.local.json` override.
`.env.example` is the authoritative credential schema; runtime JSON contains
feature defaults. Startup migrates recognized legacy runtime keys out of `.env`.

| Family | Purpose | Default posture |
|---|---|---|
| `MYSQL_*` | Canonical market-data database | Optional |
| `COORD_DB_*` | Shared control/coordination SQL | Deployed PostgreSQL; optional only for isolated/legacy setup, verified TLS required |
| `SUPABASE_DB_PASSWORD` | Administrator provisioning/migration credential | Not a runtime/browser login |
| `KIS_PROD_*` | Production account/API configuration | No secret defaults |
| `TRADING_ENABLED` | Administrative hard-lock | Fail-closed |
| `BUYBOARD_ENGINE_ENABLED` | Guarded Kanban runtime availability | `true`; not broker authorization |
| `KANBAN_STRATEGY_INSTANCE_ID` | Stable Kanban ownership identity | Must be deliberate |
| `KIS_LIVE_EXECUTION_MODE` | Disabled/controlled/full-live envelope | Disabled |
| `KIS_CONTROLLED_LIVE_MAX_ENTRY_EQUITY_FRACTION` | Controlled-live per-entry share of fresh account NAV | Zero disables this cap |
| `KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL` | Optional additional fixed USD per-entry ceiling | Zero disables this cap; at least one controlled-live cap is required |
| `KIS_WS_*`, `KIS_MARKET_DATA_*` | Verified real-time capability | Fail-closed |
| `KIS_MUTATION_*` | Shared request budgets and spacing | Unknown/zero blocks entry |
| `EXTERNAL_ALERT_*` | Critical out-of-process alerting | Optional |
| `QUANT_BACKUP_DIR` | Offsite local-state backup target | Optional |
| `AUTO_CLAIM_MAIN_ON_HANDOFF` | PC ownership auto-claim | `0` |

The web/PWA uses separate gitignored `config/web.local.json` settings. Keep
canonical writes and both connected operation allowlists off unless the exact
account/environment and permitted Operator Control identity (`Mobile Web` or
the hosting desktop) have been verified. Enabling an allowlist permits only
those typed planning/operator
requests; it never changes `TRADING_ENABLED`, the shared Live Trading switch,
the execution lease, or broker configuration. The authenticated live-update
WebSocket and typed desktop pulses are notification paths, not additional
authority settings.

`Buffer %` is persisted Trade Card planning/compatibility metadata. It does not
raise the active passive-order trigger: live confirmation uses
`max(breakout_price, orb_high)`, and automatic execution uses ORB high. The
legacy `ENTRY_ATTEMPT_TTL_SECONDS` setting remains readable for compatibility,
but new passive entries have no 15-second auto-cancel/reprice deadline.

Multiple PROD accounts can use `KIS_PROD_ACCOUNTS` or numbered
`KIS_PROD_ACCOUNT_NO_1` through `_20`. Do not put real account numbers in
documentation, tests, or committed fixtures.

Stock symbols are never environment configuration. Controlled-live symbol
authority comes from exact active Trade Card rows in the shared operational
database; `data/trade_cards.json` is recovery-only and cannot authorize an
order.

`TRADING_ENABLED` is a per-machine one-way administrative lock, so it can be
false on the laptop and true on the PC without a synchronization fault. The
durable Live Trading on/off control is shared. Even with both enabled, only the
current execution owner can act, and every readiness and safety gate must pass.

Changing a feature flag never bypasses lease, ownership, reconciliation, risk,
market-data, mutation-budget, or broker-boundary checks. See
[Risk and Safety Controls](Risk-and-Safety-Controls) and
[Current Order Logic](https://github.com/cafe-auvers/quant_app/blob/master/docs/current_order_logic.md).
