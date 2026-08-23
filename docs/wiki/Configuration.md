# Configuration

Configuration is environment-driven. `.env.example` is the authoritative key
catalog and safe-default reference.

| Family | Purpose | Default posture |
|---|---|---|
| `MYSQL_*` | Canonical market-data database | Optional |
| `COORD_DB_*` | Cross-device control/coordination SQL | Optional, TLS required |
| `KIS_PROD_*` | Production account/API configuration | No secret defaults |
| `TRADING_ENABLED` | Administrative hard-lock | Fail-closed |
| `BUYBOARD_ENGINE_ENABLED` | Guarded Kanban runtime availability | `true`; not broker authorization |
| `KANBAN_STRATEGY_INSTANCE_ID` | Stable Kanban ownership identity | Must be deliberate |
| `KIS_LIVE_EXECUTION_MODE` | Disabled/controlled/full-live envelope | Disabled |
| `KIS_CONTROLLED_LIVE_*` | Symbol/notional pilot fences | Empty/zero |
| `KIS_WS_*`, `KIS_MARKET_DATA_*` | Verified real-time capability | Fail-closed |
| `KIS_MUTATION_*` | Shared request budgets and spacing | Unknown/zero blocks entry |
| `EXTERNAL_ALERT_*` | Critical out-of-process alerting | Optional |
| `QUANT_BACKUP_DIR` | Offsite local-state backup target | Optional |
| `AUTO_CLAIM_MAIN_ON_HANDOFF` | PC ownership auto-claim | `0` |

Multiple PROD accounts can use `KIS_PROD_ACCOUNTS` or numbered
`KIS_PROD_ACCOUNT_NO_1` through `_20`. Do not put real account numbers in
documentation, tests, or committed fixtures.

Changing a feature flag never bypasses lease, ownership, reconciliation, risk,
market-data, mutation-budget, or broker-boundary checks. See
[Risk and Safety Controls](Risk-and-Safety-Controls).
