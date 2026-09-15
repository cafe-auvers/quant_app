# Completing gates without overnight monitoring

The [activation gate specification](activation_gate_specification.md) remains
the pass and promotion contract. This workflow changes how checks are prepared
and observed; it does not waive evidence or authorize trading.

| Gate | Required observation | Operator time | Tooling still needed |
|---|---|---|---|
| 2 | One complete regular session with every protocol metric passing | Prepare before open; inspect the saved result afterward. The detached read-only worker keeps Windows awake. | Use the local preflight below before launching. |
| 3 | One complete session through the final decision runtime, plus captured-live replay coverage | Runs headlessly for the complete session with every final mutation intercepted; independent review/finalization follows the run. | Implemented by `scripts/run_gate3_shadow.py`; genuine live evidence is still required. |
| 4 | At least three supervised regular-session dates, a genuine strategy entry outcome and a cancellation lifecycle | Supervise while live activity is active. The contract does not require three full nights. Ending a window still requires the reviewed reconciliation and position-protection procedure. | Implemented by the opt-in runtime journal and `scripts/manage_gate4_session.py`; reviewed execution-capability and live lifecycle evidence are still required. |
| 5 | Five consecutive full sessions, restart/handoff/reconnect drills, external watchdog and alert proof | Unattended sessions with planned drill and review work | Automate session evidence and drill records; verify the watchdog outside the trading process. A configured webhook is not delivery/watchdog proof. |

## Finish the tooling before freezing the qualification release

Freeze the release after testing the Gate-3 runner and Gate-4 collector, certify Gate 1,
collect Gate 2, collect Gate 3, supervise Gate 4 and run Gate 5. Allow roughly
ten trading-session dates for that sequential campaign, plus implementation,
review and any retries; strategy-triggered coverage can take longer.

Under the existing specification, every tracked change creates a new release
identity, including documentation or tests. Completing Gate 2 and only then
implementing Gate 3 requires another Gate-2 qualification on the new commit.
An older session remains useful diagnostic evidence, but does not qualify the
new release automatically. Any future policy for preserving equivalent evidence
needs its own reviewed specification and validator change.

## Gate 3: headless shadow session

Gate 3 must use the same clean exact commit as the passed Gate-2 report. It
also requires at least one canonical Trade Card for the reviewed symbol; the
runner refuses to invent a synthetic production card. Keep the output outside
the repository and launch before the NYSE regular-session open:

```powershell
python scripts/run_gate3_shadow.py `
  --gate2-report C:\quant_evidence\gate2\gate2_report.json `
  --capability-manifest C:\quant_evidence\gate2\gate2_capabilities.json `
  --session-date YYYY-MM-DD `
  --symbols AAPL `
  --output-dir C:\quant_evidence\gate3\YYYY-MM-DD
```

The runner uses genuine KIS quotes, the production `TradingEngine` composition,
an isolated SQLite command/order store, a final-boundary `WOULD_*` gateway and
before/after read-only hashes of canonical production ledgers. It records a
failed preliminary report until a different person reviews the completed
journals. Apply that review without rerunning the market session:

```powershell
python scripts/run_gate3_shadow.py `
  --finalize-only `
  --gate2-report C:\quant_evidence\gate2\gate2_report.json `
  --capability-manifest C:\quant_evidence\gate2\gate2_capabilities.json `
  --session-date YYYY-MM-DD `
  --output-dir C:\quant_evidence\gate3\YYYY-MM-DD `
  --review C:\quant_evidence\gate3\YYYY-MM-DD\independent_review.json
```

## Gate 4: three supervised controlled-live dates

First complete and independently review the exact-release execution-capability
manifest. The fail-closed template is
`config/gate4_execution_capabilities.example.json`; every row must be backed by
genuine credentialed evidence. The
[official KIS overseas-order example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/overseas_stock/overseas_stock_functions.py)
leaves
`MGCO_APTM_ODNO` blank, so do not assume external client-correlation support;
record its observed behavior and prove ambiguous-submission recovery. A
successful response without an immediate broker order ID is treated by the
runtime as ambiguous and cannot be retried automatically. Then prepare one
external append-only journal:

```powershell
python scripts/manage_gate4_session.py prepare `
  --gate3-report C:\quant_evidence\gate3\gate3_report.json `
  --capability-manifest C:\quant_evidence\gate4\capabilities.json `
  --journal C:\quant_evidence\gate4\gate4.evidence.jsonl
```

For each qualifying date, keep the shared switch off and explicitly start the
supervised session before launching the runtime:

```powershell
python scripts/manage_gate4_session.py start-session `
  --gate3-report C:\quant_evidence\gate3\gate3_report.json `
  --journal C:\quant_evidence\gate4\gate4.evidence.jsonl `
  --session-date YYYY-MM-DD `
  --supervisor <operator-id>
```

Set the gitignored runtime configuration to the same external journal with
`GATE4_QUALIFICATION_ENABLED=true`. The runtime must reach `ACTIVE` while
disarmed; arm only from the UI. The collector derives broker dispatches,
terminal reconciliation, protected positions, disarm proof and delivered
external alerts from runtime boundaries. It rejects events without an explicit
open supervised session and ignores events outside that session's start/end
window. Gate-4 qualification also requires a positive fixed
`KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL`; an equity-fraction-only envelope is
safe for ordinary controlled live but does not provide the single reviewed
numeric cap required by the Gate-4 report.

After at least three valid NYSE dates, independently review and finalize:

```powershell
python scripts/manage_gate4_session.py finalize `
  --gate3-report C:\quant_evidence\gate3\gate3_report.json `
  --capability-manifest C:\quant_evidence\gate4\capabilities.json `
  --journal C:\quant_evidence\gate4\gate4.evidence.jsonl `
  --controlled-live-config C:\quant_evidence\gate4\controlled-live-config.json `
  --risk-limits C:\quant_evidence\gate4\risk-limits.json `
  --review C:\quant_evidence\gate4\independent_review.json `
  --output C:\quant_evidence\gate4\gate4_report.json
```

Gate 4 cannot close after only one date under the normative contract: it needs
at least three supervised regular-session dates. No collector or scheduler may
compress or backfill that requirement.

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
