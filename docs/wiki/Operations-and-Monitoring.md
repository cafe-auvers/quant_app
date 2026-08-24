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
- Keep execution disabled during code/dependency/environment updates.

## Logs and evidence

- Application log: operational messages and redacted failures
- `data/event_journal.jsonl`: append-only lifecycle evidence
- `data/orders.json`: local durable legacy order ledger
- refresh status/log files under `data/`
- Gate 1/Gate 2 evidence under `artifacts/` when generated

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
