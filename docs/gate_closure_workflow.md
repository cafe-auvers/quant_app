# Completing gates without overnight monitoring

The [activation gate specification](activation_gate_specification.md) remains
the pass and promotion contract. This workflow changes how checks are prepared
and observed; it does not waive evidence or authorize trading.

| Gate | Required observation | Operator time | Tooling still needed |
|---|---|---|---|
| 2 + 3 | One combined complete regular session: every Gate-2 protocol metric plus Gate-3 shadow/replay coverage | Prepare before open; inspect Gate 2 first and independently finalize Gate 3 afterward. One process owns the KIS WebSocket. | Use `manage_gate2_session.py start --combine-gate3`. |
| 4 | Initial pass: at least three supervised regular-session dates, a genuine strategy entry outcome and a cancellation lifecycle. Later evidence-only requalification: one supervised delta date. | Supervise while live activity is active. Ending a window still requires the reviewed reconciliation and position-protection procedure. | Implemented by the opt-in runtime journal and `scripts/manage_gate4_session.py`; delta mode also requires an exact reviewed change-impact manifest and passed baseline. |
| 5 | Five consecutive full sessions, restart/handoff/reconnect drills, external watchdog and alert proof | Unattended sessions with planned drill and review work | Automate session evidence and drill records; verify the watchdog outside the trading process. A configured webhook is not delivery/watchdog proof. |

## Finish the tooling before freezing the qualification release

Freeze the release after testing the combined runner and Gate-4 collector,
certify Gate 1, collect Gate 2 and Gate 3 together, supervise Gate 4 and run
Gate 5. The initial campaign needs one combined Gate-2/Gate-3 date, at least
three Gate-4 dates and five Gate-5 dates, plus review and any retries;
strategy-triggered coverage can take longer.

Every commit still receives a new release identity and Gate-1 report. Gate 2
and Gate 3 remain exact-commit but share one collection date. After Gate 4 has
passed once, the reviewed requalification policy below prevents an
evidence-tool-only change from discarding its historical three-date coverage.

## Gate 2 + Gate 3: one headless session

Use the detached Gate-2 manager with `--combine-gate3`. It starts one KIS
client, performs all Gate-2 continuity/reconnect/stale/notice checks, and sends
the same accepted quote batches to the isolated Gate-3 shadow runtime:

```powershell
python scripts/manage_gate2_session.py start `
  --confirm-read-only `
  --combine-gate3 `
  --environment PROD `
  --symbols AAPL `
  --session-date YYYY-MM-DD `
  --gate1-report C:\quant_evidence\gate1\gate1_report.json `
  --capability-manifest C:\quant_evidence\gate2\gate2_capabilities.json `
  --redacted-evidence C:\quant_evidence\gate2\regular-session-frames.json `
  --reconnect-after-seconds 3600 `
  --silent-stale-probe-after-seconds 5400
```

The session writes `gate2_report.json` and a `gate3` subdirectory. Gate 3 is
reported as `BLOCKED_BY_GATE2`, `FAILED`, or
`EVIDENCE_COMPLETE_PENDING_REVIEW`. When Gate 2 passes and Gate 3 has no other
violation, apply the independent Gate-3 review with the existing
`--finalize-only` command. No market-session rerun is needed for review.

### Standalone Gate 3 fallback

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

The first Gate-4 pass always requires at least three supervised dates. Once it
passes, build a draft manifest for a later commit:

```powershell
python scripts/build_gate4_requalification_manifest.py `
  --baseline-gate4-report C:\quant_evidence\gate4\baseline\gate4_report.json `
  --output C:\quant_evidence\gate4\delta\change_impact.json
```

The generated manifest is `PENDING` until independently reviewed. If its exact
Git diff is `EVIDENCE_ONLY`, collect one new supervised date and add these
arguments to `manage_gate4_session.py finalize`:

```text
--baseline-gate4-report <passed-baseline-report>
--change-impact-manifest <approved-exact-diff-manifest>
```

Any production-affecting or unknown changed path mechanically keeps the
three-new-session requirement. Evidence cannot be backfilled or manually
reclassified downward.

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
