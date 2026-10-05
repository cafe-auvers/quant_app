# Kanban recovery runbook

The deployed canonical operational store is Supabase PostgreSQL, private schema
`quant_coordination`. Restore that configured connection and verify current canonical/broker
state; do not switch to the stale former TiDB source or a private SQLite file. Daily
verified PostgreSQL exports and the stopped-writer migration export have different recovery
roles. See [Supabase migration and recovery](supabase_coordination_migration.md) and
[Deployed setup](deployed_setup.md).

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
3. Leave the app running while its recovery probe retries. Database writability
   and runtime-state transitions now refresh the board automatically; a manual
   board refresh is not required. Restart only if the process or worker is no
   longer running, is on an older build, or automatic recovery reports a
   persistent fault.
4. Wait until the intended Execution Owner reports `ACTIVE` and broker
   reconciliation completes. A non-owner may correctly remain
   `STANDBY_READY`.
5. Review every external or unmatched broker order. Explicitly adopt or dismiss
   it before resuming trading.
6. Confirm the exact account, open orders, holdings, orderable quantities, and
   live-mode authorization before making another change.

If a web/PWA or peer desktop changed state during the outage, startup reads and
revision fallbacks reload canonical truth after recovery. Typed pulses provide
the normal immediate path. Neither automatic screen refresh nor restored
writability is permission to execute; lease, ownership, reconciliation,
market-data, risk, capital, and live-mode gates still apply.

## Entry replacement recovery

If a card shows `REPLACE_PENDING`, `CANCEL_PENDING`, or
`REPLACEMENT_SUBMISSION_UNRESOLVED`, do not create a manual replacement in the
app or KIS. Reconcile the exact old and new client/broker order identities.

- A working or uncertain old order blocks a replacement submission.
- Any old-order fill aborts replacement and must use the old generation's ORB
  low for protection.
- A confirmed-cancelled zero-fill old order may resume only the already
  journaled replacement submit leg with its persisted stable identity.
- A post-cancel market/risk failure leaves the old order cancelled and submits
  nothing; the runtime does not silently recreate it.
- An ambiguous replacement POST remains fenced and must be reconciled, never
  retried with a new identity.

See [Current Order Logic](current_order_logic.md) for the full state sequence.

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
