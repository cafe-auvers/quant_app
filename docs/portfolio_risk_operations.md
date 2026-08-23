# Portfolio risk operating profiles

Portfolio limits are engineering safety ceilings for exposure-increasing BUY
entries. They do not express expected profitability and never apply to a
protective SELL, partial SELL, SELL ALL, stop-loss, liquidation, cancellation,
reconciliation, or ambiguous-order recovery.

The tracked defaults are the Controlled Live profile:

| Limit | Current default |
|---|---:|
| Simultaneous filled/projected symbols | 30 |
| Total filled/projected open risk | 10% of account equity |
| Total filled/projected gross notional | 1,000% of account equity |

The gross-notional limit is an extreme final safety ceiling for corrupted
quantities, erroneous prices, excessive leverage, or unit-conversion defects.
It does not warn, restrict, or create UI noise below the ceiling. A 1,000%
gross ceiling permits extreme leverage; regulators warn that margin can amplify
losses and can lead to forced liquidation. See the
[SEC margin bulletin](https://www.sec.gov/investor/alerts/ib_marginaccounts.pdf)
and [FINRA brokerage-account guidance](https://www.finra.org/investors/investing/investment-accounts/brokerage-accounts).

## Operating profiles

These profiles use the existing environment settings and are **not** written to
`.env` or `.env.pc` by the application. Existing operator values always win;
promotion is an explicit configuration decision, never a silent migration.

| Setting | Controlled Live pilot | Full Live starting profile |
|---|---:|---:|
| `PORTFOLIO_MAX_SIMULTANEOUS_POSITIONS` | `30` | `30` |
| `PORTFOLIO_MAX_TOTAL_OPEN_RISK_FRACTION` | `0.10` | `0.20` |
| `PORTFOLIO_MAX_GROSS_NOTIONAL_FRACTION` | `10.0` | `10.0` |

All percentage settings are decimal fractions. `0.10` is 10%, `0.20` is 20%,
and `10.0` is 1,000% of fresh account equity. The only setting changed when
moving from Controlled Live to this Full Live starting profile is
`PORTFOLIO_MAX_TOTAL_OPEN_RISK_FRACTION=0.20`.

The Health tab displays these effective runtime values after local environment
overrides are loaded. This is configuration visibility only; it does not add a
soft gross-notional warning or another trading blocker below the hard ceiling.

Daily-loss, drawdown, sector, industry, correlation-group, strategy, and
incremental-buying-power fractions remain `0` (disabled) until fresh,
trustworthy canonical providers are connected. Missing optional analytical
data therefore cannot unexpectedly stop normal trading. The baseline position,
open-risk, and gross-notional limits remain active because they use canonical
cards, orders, reservations, and broker-discovered order state already needed
for execution safety.

The hard maximum is 30 unique open or projected symbols per account. Every
projected BUY is included across filled positions, remaining pending BUY
quantity (including partial fills), linked or unmatched active capital
reservations, concurrent proposals, and unresolved external BUY orders.
Multiple orders for one symbol count as one projected position, while their
genuine exposure is still included. A pending quantity already represented by
a matching reservation is not counted twice. The final gateway transaction
locks the account scope and re-evaluates concurrent reservations before any
broker call.

Stop-defined open risk is the primary portfolio exposure control:

```text
Position open risk
= remaining exposed quantity
× max(reference or mark price − effective stop price, 0)

Portfolio open-risk fraction
= total projected open risk
÷ fresh account equity
```

A `DISCOVERED_UNOWNED` external BUY always contributes its full remaining
notional as open risk. A same-symbol Kanban card does not prove that an
external or manual order is protected by the card's stop. Stop-defined risk
may replace that conservative treatment only after explicit adoption and an
authoritative protective relationship are verified. All three limits apply
only to exposure-increasing BUY entries. They never block SELL, partial SELL,
SELL ALL, stop-loss execution, liquidation, cancellation, reconciliation, or
recovery.

For a BUY cancel/replace, the replacement's live-envelope, controlled-live,
pre-trade, and portfolio approvals are validated before the original is
cancelled. The account transaction evaluates the replacement as the net target
by excluding the original pending reservation, retains the larger original or
replacement hold through cancellation, and transfers that same durable
reservation to the replacement. A definitive cancel rejection restores the
original hold; an ambiguous cancel keeps the larger hold until reconciliation.
Reconciliation groups orders that share the transferred reservation and lets
only the current replacement-chain leaf settle it, so a cancelled predecessor
cannot release capital beneath a live replacement. A live BUY whose referenced
reservation is missing or closed remains fully present in the final atomic risk
baseline, raises a critical reconciliation alert, and blocks only additional
BUY exposure until repaired; exits, cancellation, liquidation, reconciliation,
and recovery remain available.
