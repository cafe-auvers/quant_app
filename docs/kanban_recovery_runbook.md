# Kanban recovery runbook

The recovery snapshot is an explicit safe state, not a silent execution
fallback. If the Kanban operational store cannot be opened, the app keeps the
last known cards and fresh KIS holdings, orderable quantities, and prices
visible where available, but locks card mutations and broker execution.

Keep `BUYBOARD_ENGINE_ENABLED=true`. Live authorization remains independent:
`KIS_LIVE_EXECUTION_MODE=DISABLED` blocks every app submit, sell, cancel, and
replace at the central execution gateway, whether the engine flag is true or
false.

## Normal restoration

1. Do not edit the recovery snapshot, execution journal, or runtime database.
2. Restore access to the Kanban operational store.
3. Restart the app with the Buy Board engine enabled.
4. Wait until the board reports ACTIVE and broker reconciliation completes.
5. Review every external or unmatched broker order. Explicitly adopt or dismiss
   it before resuming trading.
6. Confirm the exact account, open orders, holdings, orderable quantities, and
   live-mode authorization before making another change.

## Protective action while the store is unavailable

An already-active runtime may use the guarded emergency path only for an exact
protective SELL or exact cancel when it still holds a valid cached device lease
and order ownership and can durably write the emergency journal. The central
live-mode policy is still enforced. This path cannot create a new BUY.

A process that failed at startup cannot prove and persist current ownership or
its lease, so it must not create an unsafe parallel app execution path. If a
protective exit cannot wait for restoration:

1. Open the official KIS HTS or mobile interface.
2. Verify the exact environment/account, holding, orderable quantity, and every
   open order.
3. Cancel any conflicting order and wait for broker confirmation.
4. Submit at most one necessary protective SELL. Do not place a recovery BUY.
5. Never submit or retry the same order in both KIS and this app.
6. After the store returns, wait for reconciliation to import or identify the
   manual action before taking another action.

Recovery does not qualify the system for unattended live trading. Supervised
real-session evidence, restart/lease/reconnect exercises, and external-alert
delivery evidence remain required.
