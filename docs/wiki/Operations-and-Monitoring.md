# Operations and Monitoring

## Daily operator checks

- Confirm device role, Execution Owner, Operator Control, and Live Trading
  state.
- Review Health for MySQL, KIS snapshot age, mirror freshness, account
  reconciliation, request scheduler, event journal, alerting, and disk space.
- Confirm market-data dates match the expected completed NYSE session.
- Inspect unresolved/ambiguous/open orders before any retry or ownership
  transfer.
- Verify the Buy Board readiness label and action-specific blockers.
- Confirm connected browser/PWA actions reconcile from pending to canonical
  state and that peer screens update without a manual reload.
- For each Buy Today plan, inspect the 1m/5m/30m ORB details, confirmation
  source/time, selected generation, passive limit, broker identity, and memo.
- Treat `Entry Pending` as accepted or unresolved broker work, not as a fill;
  confirm fills and positions from broker reconciliation.
- Review higher-score replacements as one cancel-then-replace chain. Require an
  authoritative zero-fill cancellation before any new generation appears.
- Keep execution disabled during code/dependency/environment updates.

New passive entries do not have a 15-second auto-cancel/reprice timer. A working
order can remain pending until fill, explicit/EOD cancellation, broker
expiry/rejection, or a safe higher-score ORB replacement. A passive-limit touch
is not proof of fill.

At the end of the regular session, confirm that unsubmitted Buy Today plans
returned to Buylist, all-window invalid plans retained an `ORB Rejected`
diagnostic for Daily Summary, and Entry Pending orders completed the two-phase
cancel/reconcile flow. An uncertain cancellation remains pending and blocks
duplicate submission.

## Logs and evidence

- Application log: operational messages and redacted failures
- `data/event_journal.jsonl`: append-only lifecycle evidence
- `data/orders.json`: local durable legacy order ledger
- refresh status/log files under `data/`
- Gate 1 reports under `artifacts/` when generated
- unattended Gate 2 session checkpoints, logs, and reports under
  `%USERPROFILE%\quant_evidence\gate2_sessions`; inspect the newest session
  with `python scripts/manage_gate2_session.py status`

Do not paste raw account responses or tokens into issues/screenshots.

## Handoff and shutdown

Use guarded sleep/shutdown paths. They check refresh/runtime readiness, flush
local state, strictly publish required state, demote writer bindings, and only
then release ownership. If any required step fails, ownership is retained or
the next device must use stale-heartbeat fenced takeover and reconciliation.

For a normal explicit owner switch, the target must be visible in the shared
coordination store and show fresh `STANDBY_READY` with 7/7 readiness. The
steady readiness heartbeat is 240 seconds and the default freshness fence is
300 seconds. `PC`, `DB`, `Listener`, and `main.py` indicators describe
different paths; seeing them ON does not replace the shared readiness proof.

## Performance

The maintained synthetic audit covers sidebar projection and cache watermark
latency. Production chart/SQL latency needs sanitized local instrumentation;
never log sensitive broker identifiers while profiling.

## Cross-device update checks

Routine Watchlist, Buylist, breakout, Buy Today, Operator Control, runtime
state, and database-writability changes are push/change-pulse driven. The
initiating web/PWA responds optimistically, peer web clients refetch after an
authenticated WebSocket invalidation, and desktops refetch after a typed
coordination pulse. Initial reads and bounded revision checks recover missed
events.

Do not use a manual refresh as an execution control. If a screen remains stale,
check the web session/socket, PC listener, coordination database, desktop
process, and pulse files. The underlying runtime must still be `ACTIVE`, own
the lease and symbol, and pass every independent safety gate.

The complete state and failure semantics are in
[Current Order Logic](https://github.com/cafe-auvers/quant_app/blob/master/docs/current_order_logic.md)
and [Order Lifecycle](Order-Lifecycle).

Current release reference: [Opening liquidity and mobile workflow (2026-10-08)](../opening_liquidity_mobile_release_2026-10-08.md). Dated reports and archived plans retain their original scope.
