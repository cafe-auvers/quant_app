# Automatic stops for manual broker purchases

The user's 2026-11-11 policy replaces the previous requirement to set an initial
stop explicitly for a newly discovered manual holding.

When a positive KIS holding is discovered without an existing or pending stop,
the dashboard freezes an initial manual-price stop using a **2% account NAV
risk budget per stock**, replacing the previous 7%-of-purchase-price default:

```text
risk dollars = fresh USD account NAV * 0.02
stop price = average purchase cost - risk dollars / full held share count
```

For example, $10,000 NAV and 100 shares bought at $10 gives a $200 initial
risk budget and an $8 stop. Prices round up to the supported tick so rounding
cannot increase that budget. If the position's full purchase value is below
the budget, the stop uses the minimum tradable $0.0001 price; the initial gross
loss is then smaller than 2% NAV. An unrepresentable below-cost stop is never
installed. The stop covers the full broker holding, including shares currently
reserved by a working partial sale.

Only a complete, positive, finite account-equity value from the reconciled
account's broker snapshot is eligible. Its real balance timestamp must be
no later than the reconciliation timestamp and no more than
`FULL_RECONCILIATION_SECONDS` old (60 seconds by default). Buying power, another
account's NAV and cached startup holdings cannot substitute for it. Missing or
stale NAV leaves `STOP_REQUIRED` and `AUTO_MANUAL_STOP_NAV_REQUIRED` visible;
the next fresh reconciliation retries automatically. The user can still set
an explicit manual stop while NAV is unavailable.

The runtime setting is `MANUAL_POSITION_DEFAULT_NAV_RISK_PERCENT=2` in
`config/runtime.json`. The old `MANUAL_POSITION_DEFAULT_STOP_PERCENT` key is
no longer read, so an old 7% override cannot turn into a 7%-of-NAV budget.

The stored default survives polling, average-cost changes and restarts. Adding
shares updates coverage while keeping the selected price. An explicit manual
stop request wins, including a concurrent request during automatic enrollment.
Initial manual-price stops below cost remain editable; existing ORB and
breakeven/profit-protection rules keep their tightening-only bounds. Broker
confirmation of a flat position retires the stop; a later manual repurchase
receives a new default from its own cost, quantity and fresh NAV. This is an
initial risk budget, not continuous portfolio rebalancing: later NAV movements
do not move the stop, and adding shares at the fixed stop can increase dollar
risk. Existing active and pending prices are preserved regardless of origin;
the release does not guess whether an old 7% price was chosen by the user. The
read-only pre-release broker check on 2026-11-11 found no holdings or working
orders, so no existing automatic stops needed migration.

The active Main runtime automatically enrolls the stop through the existing
revision-checked board workflow and market-data stop coordinator. This also
claims default LEGACY symbol ownership for Kanban, so a displayed stop can
reach the guarded protective-sell executor. No additional user gesture is
required. Explicit MANUAL ownership and another strategy's ownership remain
fenced. Incomplete reconciliation or unresolved external/ambiguous/unlinked
orders leave a visible `AUTO_MANUAL_STOP_PENDING` warning and are retried after
the normal reconciliation path resolves them. Missing or invalid purchase cost
leaves `STOP_REQUIRED` rather than creating an invalid price.

This is the existing application-managed stop mechanism, not a resting native
KIS stop order. Actual broker mutations retain the Live Trading switch, current
Main lease, readiness, ownership, broker-identity and execution-budget checks.
The normal stop-loss liquidation takes priority over a partial-sell objective.
It cannot guarantee a fill at the stop price or protection while the executor
is unavailable. The existing regular-session execution policy still applies.

The policy also applies to existing unprotected manual holdings on the next
complete holdings reconciliation. If a holding is already below the new stop,
a qualifying regular-session quote can start a full protective exit immediately
after enrollment. Reconciliation itself sends no broker order.

The PC release needs its normal exact-commit capability review and readiness
checks before activation. Web clients consume the existing stop/pending-stop
fields; no new wire format or stop enum is introduced.
