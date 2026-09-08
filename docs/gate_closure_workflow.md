# Completing gates without overnight monitoring

The [activation gate specification](activation_gate_specification.md) remains
the pass and promotion contract. This workflow changes how checks are prepared
and observed; it does not waive evidence or authorize trading.

| Gate | Required observation | Operator time | Tooling still needed |
|---|---|---|---|
| 2 | One complete regular session with every protocol metric passing | Prepare before open; inspect the saved result afterward. The detached read-only worker keeps Windows awake. | Use the local preflight below before launching. |
| 3 | One complete session through the final decision runtime, plus captured-live replay coverage | Intended to run without continuous watching, with all mutations intercepted | Compose the production decision runtime with the existing isolated shadow boundary/store and automate replay and evidence collection. The validator alone cannot collect this evidence. |
| 4 | At least three supervised regular-session dates, a genuine strategy entry outcome and a cancellation lifecycle | Supervise while live activity is active. The contract does not require three full nights. Ending a window still requires the reviewed reconciliation and position-protection procedure. | Collect broker/runtime lifecycle observations and review records automatically. |
| 5 | Five consecutive full sessions, restart/handoff/reconnect drills, external watchdog and alert proof | Unattended sessions with planned drill and review work | Automate session evidence and drill records; verify the watchdog outside the trading process. A configured webhook is not delivery/watchdog proof. |

## Finish the tooling before freezing the qualification release

Implement and test the missing Gate-3 runner and later-gate collectors before
the final qualification campaign. Then freeze the release, certify Gate 1,
collect Gate 2, collect Gate 3, supervise Gate 4 and run Gate 5. Allow roughly
ten trading-session dates for that sequential campaign, plus implementation,
review and any retries; strategy-triggered coverage can take longer.

Under the existing specification, every tracked change creates a new release
identity, including documentation or tests. Completing Gate 2 and only then
implementing Gate 3 requires another Gate-2 qualification on the new commit.
An older session remains useful diagnostic evidence, but does not qualify the
new release automatically. Any future policy for preserving equivalent evidence
needs its own reviewed specification and validator change.

## Check Gate 2 locally before reserving a session

Run from the intended execution host using its qualification Python environment:

```powershell
python scripts/check_gate2_readiness.py `
  --environment PROD `
  --symbols AAPL `
  --session-date YYYY-MM-DD `
  --gate1-report C:\redacted\gate1_report.json `
  --capability-manifest C:\redacted\gate2_capabilities.json `
  --redacted-evidence C:\redacted\regular-session-frames.json
```

The command reports local blockers together and exits nonzero when any remain.
It reads configuration and evidence without opening KIS, constructing a broker,
changing activation or writing an evidence report. Credential checks expose
presence only. `--json` makes the diagnostic usable by an operator's scheduler.
For daytime planning, `--start-at` accepts a timezone-aware future launch time;
it does not schedule a task. Repeat checks at actual launch because source,
configuration and timing can change.

`LOCAL_CHECKS_COMPLETE` is not a gate pass or proof of live readiness. Verify
the intended host, installed scheduled task, sole app-key session, adequate
uptime/storage and independent review. A read-only collector cannot protect
existing trading positions or replace their execution owner.

Once prerequisites pass, use the detached command in the
[Gate-2 checklist](gate2_readiness_checklist.md#unattended-durable-session).
Launch shortly before open; the reconnect and stale-probe offsets are measured
from collector startup. Launching many hours early can exercise those probes
outside the qualifying session. An after-open start cannot satisfy a complete
session and is rejected by the runner.

After the session, on the same execution host:

```powershell
python scripts/manage_gate2_session.py status
```

The default evidence directory belongs to that machine's Windows user. A
laptop reporting `NO_SESSION` does not establish that the PC has no session.
Use `--session-dir` for an explicit recorded session. A scheduled task's
successful exit may only mean startup was checked: the formal
`gate2_report.json`, compatible evidence and required independent review decide
qualification. Closing the terminal, dashboard or Codex does not stop the
detached worker; Windows must remain powered and signed in for an interactive
scheduled task. The worker prevents idle sleep, not power loss or reboot.

## Starting supervised trading before full unattended qualification

The [controlled-live pilot](controlled_live_pilot_runbook.md) is an existing,
separately approved path for supervised trading. It requires the matching
approved release, reviewed capability/risk envelope and current live readiness,
and starts disarmed. It does not close Gates 2–5. Do not make full unattended
qualification a prerequisite to evaluating that already-defined supervised path.
