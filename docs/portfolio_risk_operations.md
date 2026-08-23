# Portfolio risk operating profiles

Portfolio limits are engineering safety ceilings for exposure-increasing BUY
entries. They do not express expected profitability and never apply to a
protective SELL, partial SELL, SELL ALL, stop-loss, liquidation, cancellation,
reconciliation, or ambiguous-order recovery.

The tracked defaults remain unchanged:

| Limit | Current default |
|---|---:|
| Simultaneous filled/projected symbols | 20 |
| Total filled/projected open risk | 10% of account equity |
| Total filled/projected gross notional | 200% of account equity |

These are compatibility ceilings, not a claim that every account should use
them. A 200% gross ceiling permits leverage; regulators warn that margin can
amplify losses and can lead to forced liquidation. See the
[SEC margin bulletin](https://www.sec.gov/investor/alerts/ib_marginaccounts.pdf)
and [FINRA brokerage-account guidance](https://www.finra.org/investors/investing/investment-accounts/brokerage-accounts).

## Recommended rollout profiles

The following values are conservative starting profiles for qualifying the
software. They are documented separately and are **not** written to `.env` or
`.env.pc` by the application or this change.

| Setting | Controlled Live pilot | Full Live starting profile |
|---|---:|---:|
| `PORTFOLIO_MAX_SIMULTANEOUS_POSITIONS` | 3 | 10 |
| `PORTFOLIO_MAX_TOTAL_OPEN_RISK_FRACTION` | `0.02` | `0.05` |
| `PORTFOLIO_MAX_GROSS_NOTIONAL_FRACTION` | `0.30` | `1.00` |

The operator may retain the current 20/10%/200% values after reviewing account
objectives, margin terms, and supervised-session evidence. Promotion should be
an explicit configuration decision, never a silent migration.

Daily-loss, drawdown, sector, industry, correlation-group, strategy, and
incremental-buying-power fractions remain `0` (disabled) until fresh,
trustworthy canonical providers are connected. Missing optional analytical
data therefore cannot unexpectedly stop normal trading. The baseline position,
open-risk, and gross-notional limits remain active because they use canonical
cards, orders, reservations, and broker-discovered order state already needed
for execution safety.

Every projected BUY is counted once across filled positions, remaining pending
BUY quantity, linked or unmatched active capital reservations, and unresolved
external BUY orders. The final gateway transaction locks the account scope and
re-evaluates concurrent reservations before any broker call.
