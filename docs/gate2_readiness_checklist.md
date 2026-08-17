# Gate 2 — Live KIS Read-only Soak Readiness

Current disposition: **NOT READY / DO NOT START THE SOAK**

Gate-1-certified baseline: `master@b9895a9ac387331eaf3782a7ca439d8b4838a08e`.
Any later code change requires a new exact-head Gate-1 report before Gate 2.
Gate 2 validates live market data only; it does not authorize broker
mutations, the Kanban runtime, shadow execution, or live trading.

## WS0 blockers that must close first

- [ ] Capture real regular-session `HDFSCNT0` and `HDFSASP0` frames for the
  configured US exchanges and confirm every field mapping.
- [ ] Prove exchange event time versus local receive time, including timezone,
  date rollover, clock skew, and daylight-saving behavior.
- [ ] Determine whether either channel has a monotonic sequence field; prove
  reconnect/reset semantics before enabling sequence rejection.
- [ ] Configure a redacted HTS-ID test identity and verify
  `H0GSCNI0`/`H0GSCNI9` encryption and decrypted field mapping.
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
BUYBOARD_ENGINE_ENABLED=false
KIS_WS_ENABLED=true
KIS_WS_PROTOCOL_VERIFIED=true
KIS_MUTATION_BUDGET_VERIFIED=false
KIS_SUBMIT_MUTATION_CAPACITY=0
KIS_CANCEL_MUTATION_CAPACITY=0
KIS_REPLACE_MUTATION_CAPACITY=0
```

- [ ] `KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY=41` comes from WS0-E04.
- [ ] Requested trade + quote + execution-notice registrations never exceed
  the aggregate total. For the read-only soak, leave `KIS_WS_HTS_ID` empty
  unless execution-notice verification is the explicit test objective.
- [ ] `KIS_WS_SYMBOL_KEYS_JSON` contains only WS0-verified keys.
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
  desired or acknowledged state.
- [ ] Verify a stale critical symbol is declared stale within three seconds and
  cannot produce an entry decision.
- [ ] Replay synthetic stop thresholds through the live accumulator without
  permitting any broker mutation.
- [ ] Stop immediately if an activation flag changes, a mutation is attempted,
  a secret appears in output, or the aggregate subscription budget is crossed.

## Acceptance metrics

| Metric | Pass requirement | Evidence field |
|---|---|---|
| Continuous read-only soak | One full regular session | start/end/session calendar |
| Critical subscription ACK | 100% | requested/acked keys |
| Silent parser failures | 0 | parser drop/error counts |
| Unhandled disconnects | 0 | disconnect classifications |
| Injected reconnects | 100% recover | injection/recovery pairs |
| Critical ACK recovery | < 10 seconds | p50/p95/max recovery |
| Critical stale detection | ≤ 3 seconds | per-symbol detection latency |
| Entry attempts while stale | 0 | decision audit |
| Duplicate-subscription corruption | 0 | desired/ACK set consistency |
| Missed synthetic stop breaches | 0 | injected/latched/consumed IDs |
| Queue/accumulator deadlocks | 0 | watchdog/cycle progress |
| Receive lag p95 | < 1 second | broker-event to receive |
| Receive lag p99 | < 2 seconds | broker-event to receive |
| Secret/approval-key leakage | 0 | redaction scan |
| Broker mutations | 0 | gateway/broker boundary audit |

## Machine-readable evidence bundle

The generated Gate-2 report must contain:

- exact commit SHA and Gate-1 report digest;
- environment, symbols, TR IDs, verified subscription keys, and requested
  aggregate slots;
- KIS capability-matrix revision/digest;
- session calendar, UTC/KST/US-Eastern start and end times;
- every metric above with numerator, denominator, threshold, and result;
- reconnect injection timestamps and ACK-recovery durations;
- parser/frame counts by TR ID and schema fingerprint;
- p50/p95/p99/max event, receive, and queue lag;
- activation-default snapshot and `broker_mutations=0`;
- redaction scan result and hashes of redacted raw evidence;
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
  --timestamp-evidence C:\redacted\timestamps.json `
  --execution-notice-evidence C:\redacted\notice.json `
  --trade-sequence-finding NO_USABLE_SEQUENCE `
  --quote-sequence-finding NO_USABLE_SEQUENCE `
  --reconnect-after-seconds 3600 `
  --output C:\redacted\gate2_report.json
```

Use `MONOTONIC` instead only when credentialed evidence proves a usable
monotonic sequence for that channel. Raw/unredacted captures remain outside
the repository.
