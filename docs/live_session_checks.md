# Checks alongside live trading

The passive collector can monitor the current live executor without taking its
WebSocket, draining its accumulator, submitting orders, or writing shared state.
Set these non-secret values in `config/runtime.local.json` before restarting the
reviewed release:

```json
{
  "LIVE_SESSION_CHECKS_ENABLED": "true",
  "LIVE_SESSION_DATE": "2026-10-05",
  "LIVE_SESSION_EVIDENCE_DIR": "C:/quant_evidence/live_20261005"
}
```

The output directory must be outside the repository. The dated collection stops
after that regular session closes; it does not stop or disarm the executor.
Activation and every existing execution guard still apply after a release change.

`checks_report.json` updates every ten seconds. Check its timestamp, collector
state, dropped batches, and errors to verify collection. Evidence stays on the
PC; this feature performs no extra shared-database or broker queries.

- Gate 2 diagnostics include connection/channel health, capacity, parser and
  duplicate counters, accepted quote counts, and broker/receive/queue latency.
- Gate 3 uses detached copies of pre-decision cards and the accepted production
  quote batches. It runs the production decision engine with the existing
  final-boundary shadow gateway and its physically isolated SQLite store.
  `WOULD_*` events are intentions, not broker acknowledgements or fills. Equity
  is the minimum observed account equity in the copied cards; this diagnostic
  run does not certify per-account sizing or the complete ORB workflow.
- Gate 4 diagnostics capture the existing actual runtime events, including
  mutation dispatch/terminal events, reconciliation, protection, and real arm
  or disarm events when they occur.

The bounded background collector cannot block broker calls on disk I/O. It
records queue loss or observation errors; an incomplete collection cannot be
described as complete evidence. Collector initialization failure is logged and
does not change trading state. A missing or stale report is a collection failure.

These files explicitly state `PASSIVE_LIVE_DIAGNOSTICS` and `NOT_CERTIFIED`.
They are not inputs that grant an activation-gate pass. Full Gate 2 still needs
its read-only session and controlled reconnect/silent-channel probes; Gate 3
needs complete branch/fence replay and its reviewed Gate-2 chain; Gate 4 needs
its reviewed Gate-3 chain, three supervised dates, and required lifecycle/disarm
coverage. Never inject disconnects into an active live session to manufacture
that missing evidence. Use the separate qualification workflow for closure.
