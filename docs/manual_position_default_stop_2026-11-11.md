# Automatic stops for manual broker purchases

The user's 2026-11-11 policy replaces the previous requirement to set an initial
stop explicitly for a newly discovered manual holding.

When a positive KIS holding is discovered without an existing or pending stop,
the dashboard freezes a manual-price stop at 7% below the reported average
purchase cost. Prices round up to the supported tick so rounding cannot increase
the initial percentage risk. For example, a $100 average cost produces a $93
stop; $3.50 produces $3.26. The stop covers the full broker holding.

The stored default survives polling, average-cost changes and restarts. Adding
shares updates coverage while keeping the selected price. An explicit manual
stop request wins, including a concurrent request during automatic enrollment.
Initial manual-price stops below cost remain editable; existing ORB and
breakeven/profit-protection rules keep their tightening-only bounds. Broker
confirmation of a flat position retires the stop; a later manual repurchase
receives a new default from its own cost.

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
