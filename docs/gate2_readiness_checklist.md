# Gate 2 — Live KIS Read-only Soak Readiness

Current disposition: **NOT READY / DO NOT START THE SOAK**

Gate-1-certified safety-backup baseline:
`master@b0422e7a92d8324e73469d286d2710ef32776ffc` (local tag
`safety-backup-20260823`; 676 Gate-1 tests passed on that exact commit).
Any later code change requires a new exact-head Gate-1 report before Gate 2.
Gate 2 validates live market data only; it does not authorize broker
mutations, shadow execution, or live trading. The guarded Kanban runtime may
remain available because its live-execution envelope stays disabled.

A separately approved supervised controlled-live pilot has its own stricter
risk envelope and does not make this checklist pass. See
[`controlled_live_pilot_runbook.md`](controlled_live_pilot_runbook.md). Normal
production WebSocket composition and this qualifier now share the same strict
manifest validator; production additionally pins the manifest digest and exact
runtime commit through `KIS_CAPABILITY_MANIFEST_*` / `KIS_RUNTIME_COMMIT_SHA`.

## WS0 blockers that must close first

- [ ] Capture real regular-session `HDFSCNT0` and `HDFSASP0` frames for the
  configured US exchanges and confirm every field mapping.
- [ ] Prove exchange event time versus local receive time, including timezone,
  date rollover, clock skew, and daylight-saving behavior.
- [ ] Determine whether either channel has a monotonic sequence field; prove
  reconnect/reset semantics before enabling sequence rejection.
- [ ] Configure a redacted HTS-ID test identity and verify
  `H0GSCNI0`/`H0GSCNI9` encryption and decrypted field mapping.
- [ ] Assemble those reviewed findings into the strict capability manifest
  described below. Each evidence digest and interpretation must validate.
- [ ] Demonstrate a forced disconnect, deterministic desired-set replay, and
  100% critical re-ACK in less than 10 seconds.
- [x] Enforce the credential-verified aggregate WebSocket limit of 41 total
  registrations per app-key session, including pending subscribe,
  pending-unsubscribe, active, and execution-notice slots. Explicit NACK and
  unsubscribe ACK release a slot; reconnect clears session ACK state while
  preserving desired replay intent.
- [x] Implement a standalone Gate-2 runner/report path. The application engine
  remains disabled and the runner composes only the market-data service.

## Later execution qualification (does not block Gate 2)

- [ ] Run controlled simulation submit/cancel/replace tests for
  `MGCO_APTM_ODNO`, immediate broker-order ID, recovery surfaces, history
  latency, and unambiguous pre-acceptance rate-limit errors.
- [ ] Complete history boundary tests: oldest range, a real continuation page,
  and known cancelled/rejected orders.
- [ ] Establish the broker-order-ID uniqueness domain across exchanges, dates,
  sessions, accounts, and app keys before treating
  `environment:account:broker_order_id` as globally sufficient.

## Exact preflight fence

- [ ] Qualification commit is reviewed and recorded.
- [ ] Full Gate-1 report is `PASSED` on that exact commit with zero skips,
  zero unclassified scenarios, and zero invariant violations.
- [ ] Worktree and deployed package match that exact commit.
- [ ] Every Gate-2 WebSocket/read-only row in
  `docs/kis_capability_matrix.md` is verified. Execution-only rows may remain
  open, but must be listed as later-gate blockers in the evidence bundle.
- [ ] Credential files and token caches are restricted to the deployment user.
- [ ] Raw capture output is outside the repository until redaction is complete.
- [ ] Application/database backup and log-retention locations have sufficient
  space for a full session.
- [ ] An operator is assigned for the entire session and has a documented stop
  procedure.

## Required activation snapshot for Gate 2 only

```text
TRADING_ENABLED=false
BUYBOARD_ENGINE_ENABLED=true
KIS_WS_ENABLED=true
KIS_WS_PROTOCOL_VERIFIED=true
KIS_MUTATION_BUDGET_VERIFIED=false
KIS_SUBMIT_MUTATION_CAPACITY=0
KIS_CANCEL_MUTATION_CAPACITY=0
KIS_REPLACE_MUTATION_CAPACITY=0
KIS_LIVE_EXECUTION_MODE=DISABLED
KIS_CONTROLLED_LIVE_SYMBOLS=
KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL=0
BROKER_EVENT_STALE_SECONDS=2
LOCAL_RECEIVE_STALE_SECONDS=2
```

- [ ] `KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY=41` comes from WS0-E04.
- [ ] The Gate-2-only freshness budgets plus reporter poll interval remain at
  or below three seconds; the values above leave deterministic scheduling
  margin without changing the application's fail-closed production defaults.
- [ ] Legacy `KIS_WS_TRADE_CHANNEL_CAPACITY` and
  `KIS_WS_QUOTE_CHANNEL_CAPACITY` values are not treated as broker limits.
  Live composition uses the aggregate pool as its sole capacity authority.
- [ ] Requested trade + quote + execution-notice registrations never exceed
  the aggregate total. `KIS_WS_HTS_ID` is required because the current Gate-2
  contract includes execution-notice encryption/mapping verification.
- [ ] Gitignored `data/kis_ws_symbol_keys.json` contains only WS0-verified
  keys and passes `python scripts/manage_kis_ws_symbol_keys.py validate`.
- [ ] The runtime startup log records `production_activation_authorized=false`.
- [ ] No process with the same app key holds a second WebSocket session.

## Soak execution

- [ ] Begin before the U.S. regular session and run through the full close.
- [ ] Record subscription request, ACK/NACK, data, reconnect, parser, queue,
  freshness, and secret-redaction counters continuously.
- [ ] Inject at least one controlled network disconnect after healthy ACK/data
  flow is established.
- [ ] Confirm every desired critical subscription is re-ACKed after reconnect.
- [ ] Exercise duplicate subscribe/unsubscribe requests without corrupting
  desired or acknowledged state; record every actual operation by connection
  generation and reject duplicate protocol transitions.
- [ ] Verify a stale critical symbol is declared stale within three seconds and
  cannot produce an entry decision. This must use the connected silent-channel
  suppression probe, independently of disconnect ACK invalidation.
- [ ] Replay synthetic stop thresholds through the live accumulator without
  permitting any broker mutation.
- [ ] Stop immediately if an activation flag changes, a mutation is attempted,
  a secret appears in output, or the aggregate subscription budget is crossed.

## Acceptance metrics

| Metric | Pass requirement | Evidence field |
|---|---|---|
| Continuous read-only soak | One full regular session with at least 95% of scheduled samples | start/end/session calendar and sample count |
| Critical subscription ACK | 100% | requested/acked keys |
| Silent parser failures | 0 | parser drop/error counts |
| Unhandled disconnects | 0 | disconnect classifications |
| Injected reconnects | 100% recover | injection/recovery pairs |
| Critical ACK recovery | < 10 seconds | p50/p95/max recovery |
| Critical stale detection | ≤ 3 seconds | per-symbol detection latency |
| Stale entry-readiness fence | At least one rejection and zero allows during the controlled stale window | instrumented `entry_quote_ready()` boundary audit |
| Duplicate-subscription corruption | 0 | actual generation/TR/key operations and duplicate-request probe |
| Missed synthetic stop breaches | 0 | injected/latched/consumed IDs |
| Queue/accumulator deadlocks | 0 | independent watchdog/cycle progress |
| Receive lag p95 | < 1 second | broker-event to receive |
| Receive lag p99 | < 2 seconds | broker-event to receive |
| Secret/approval-key leakage | 0 | full captured-log scan including issued approval key |
| Broker mutations | Initialized audit source and 0 attempts | sole real `KisBroker` mutation-boundary audit |

## Machine-readable evidence bundle

The generated Gate-2 report must contain:

- exact commit SHA and Gate-1 report digest;
- environment, symbols, TR IDs, verified subscription keys, and requested
  aggregate slots;
- KIS capability-matrix revision/digest;
- reviewed capability-manifest digest and normalized verified capabilities;
- session calendar, UTC/KST/US-Eastern start and end times;
- every metric above with numerator, denominator, threshold, and result;
- reconnect injection timestamps and ACK-recovery durations;
- parser/frame counts by TR ID and schema fingerprint;
- session-wide sample counts plus p50/p95/p99/max receive and queue lag;
- activation-default snapshot plus initialized runtime-safety audit sources,
  observed stale-readiness rejection, and `broker_mutations=0`;
- captured-log digest/byte count, issued-approval-key scan result, and hashes
  of redacted raw evidence;
- overall `PASSED` or `FAILED` with explicit blockers.

Gate 2 passes only when every metric passes in one evidence bundle. A failed or
partial session does not authorize Gate 3. After the run, restore
`KIS_WS_ENABLED=false` and `KIS_WS_PROTOCOL_VERIFIED=false` until the evidence
has received an explicit review decision.

The runner never changes configuration or activation state. Once all
WebSocket-specific WS0 rows are independently verified and a new exact-head
Gate-1 report is available, invoke it from an operator-controlled environment:

```powershell
python scripts/run_gate2_soak.py `
  --confirm-read-only `
  --environment PROD `
  --symbols AAPL,MSFT `
  --session-date YYYY-MM-DD `
  --gate1-report artifacts/gate1_report.json `
  --capability-manifest C:\redacted\gate2_capabilities.json `
  --redacted-evidence C:\redacted\regular-session-frames.json `
  --reconnect-after-seconds 3600 `
  --silent-stale-probe-after-seconds 5400 `
  --log-output C:\redacted\gate2_runtime.log `
  --output C:\redacted\gate2_report.json
```

The capability manifest has schema version 1, the exact soak commit and
environment, an approved reviewer identity/time, and exactly one entry for
each required capability:

```json
{
  "schema_version": 1,
  "commit_sha": "<exact 40-character soak commit>",
  "environment": "PROD",
  "review": {
    "status": "APPROVED",
    "author": "<bundle author identity>",
    "reviewer": "<different reviewer identity>",
    "reviewed_at": "<timezone-aware ISO-8601>",
    "method": "PROCEDURAL_DUAL_CONTROL",
    "reference": "<immutable approval record URL or attestation digest>"
  },
  "capabilities": [
    {
      "capability_id": "HDFSCNT0_TIMESTAMP_SEMANTICS",
      "status": "VERIFIED",
      "environment": "PROD",
      "tr_id": "HDFSCNT0",
      "interpretation": "EXCHANGE_EVENT_TIME_AMERICA_NEW_YORK",
      "evidence_file": "trade-timestamp.json",
      "evidence_sha256": "<sha256>"
    }
  ]
}
```

The bundle must also contain `HDFSASP0_TIMESTAMP_SEMANTICS`, both channel
`*_SEQUENCE_SEMANTICS` entries, and `EXECUTION_NOTICE_ENCRYPTION`. A sequence
interpretation is exactly `MONOTONIC` or `NO_USABLE_SEQUENCE`; `MONOTONIC`
also requires the reviewed `sequence_field` and `reset_semantics` of either
`RESET_ON_RECONNECT` or `CONTINUES_ACROSS_RECONNECT`; the live parser and
reconnect lifecycle then use those exact values.
`EXECUTION_NOTICE_ENCRYPTION` remains mandatory for the full Gate-2 report.
The separately controlled supervised-pilot composition may omit it; in that
case the notice channel is not subscribed and REST reconciliation remains the
only execution authority.
Each referenced evidence file is a nonempty JSON observation with matching
capability/environment/TR/interpretation fields. Empty files, digest
mismatches, unreviewed manifests, CLI-only assertions, and commit mismatches
fail before any live connection. Raw/unredacted captures remain outside the
repository.

The runner requires at least one separately named, nonempty
`--redacted-evidence` file outside the repository and records its digest in
the final report. The manifest also rejects equal author/reviewer strings and
requires an approval method/reference. These fields are completeness checks,
not cryptographic identity authentication: the operator must verify the
reviewer's real identity and independence through the referenced GitHub
review, signed attestation, or documented dual-control record. Until that
external control exists, the bundle is not independently reviewed even if a
locally supplied string passes schema validation.
