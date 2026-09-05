# Activation Gate Specification

Status: **CANONICAL — Gate 1 closed on protected master; Gate 2 live qualification is next**

This document is the authoritative definition of the five production
activation gates. It separates three questions that must not be conflated:

1. Is the guarded engine available for Kanban, reconciliation, monitoring,
   and decision evaluation?
2. Has a particular qualification gate passed for an exact release candidate?
3. Has an operator explicitly authorized the next operating envelope?

`BUYBOARD_ENGINE_ENABLED=true` answers only the first question. It never
authorizes a KIS broker mutation. Gate evidence answers the second question,
and an explicit operator promotion answers the third.

## Current qualification status

Gate 1 was closed on protected `master` on 2026-08-29 with 731 deterministic
scenarios and exact-commit Python 3.11/3.12 CI evidence. Because editing a
tracked status file creates a new commit, this document does not hard-code its
own final SHA. The live source of truth is the successful protected
`Gate 1 deterministic simulation` check and its `gate1-report-<full SHA>`
artifact for the current `master` commit. Any later commit invalidates that
commit's qualification until the same exact-SHA check passes again.

| Gate | Current disposition | What remains |
|---|---|---|
| 1. Deterministic simulation | **CLOSED / PASSED for the current protected `master` exact-SHA report** | Re-run the protected Python 3.11/3.12 matrix and Gate-1 report after every later commit. |
| 2. Live KIS read-only protocol qualification | **BLOCKED / NOT PASSED** | Close the live capability evidence and complete one full-session evidence bundle. |
| 3. Shadow execution | **OFFLINE BOUNDARY, STORE, AND VALIDATOR IMPLEMENTED / NOT QUALIFIED** | Complete Gate 2, compose the final shadow runner against isolated state, then collect one real-quote session plus captured-live branch coverage and review. |
| 4. Controlled live | **GUARDRAILS AND FAIL-CLOSED REPORT VALIDATOR IMPLEMENTED / NOT QUALIFIED** | Complete execution-capability evidence and at least three supervised regular-session dates. |
| 5. Unattended qualification | **FAIL-CLOSED REPORT/PROMOTION VALIDATORS IMPLEMENTED / NOT QUALIFIED** | After Gate 4, complete five consecutive NYSE sessions, all required drills, alert/watchdog proof, independent review, and a separate operator promotion decision. |

The currently qualified level is Gate 1. Four higher gates remain. No statement
in this document authorizes live trading.

### Offline remediation completed and certified

The following items can be implemented and verified without contacting KIS,
and are now present in code:

- Gate-1 schema v2 fails on a dirty tree, incomplete commit identity, missing
  tracked-tree/dependency digests, or missing exact-commit Python 3.11/3.12 CI
  evidence.
- `src/core/orb_entry_logic.py` is the single canonical executable ORB entry
  contract. The legacy `PASSIVE_PULLBACK_V1` compatibility facade delegates to
  it so strategy/UI callers cannot retain a second interpretation. The policy
  uses a frozen floor, confirmation trigger, and exact passive execution price;
  the retired percentage buffer and probe flags cannot authorize an entry.
- A higher-timeframe replacement is allowed only for a strictly higher score,
  a strictly higher timeframe, and a zero-fill working order. Cancellation is
  exact-owned and authoritative before a replacement is revalidated.
- Gate 3 has an append-only, redacted, physically isolated `.shadow.jsonl`
  mutation boundary for `WOULD_SUBMIT`, `WOULD_CANCEL`, `WOULD_REPLACE`, and
  `WOULD_SELL`, plus a cumulative report validator. It returns no fake broker
  acknowledgement or fill.
- Gates 4 and 5 have cumulative fail-closed validators. They enforce exact
  upstream report digests, full commit identities, independent review,
  calendar-valid observation dates, zero-tolerance counters, required drills,
  configuration/risk digests, and pilot-evidence exclusion.
- Gate qualification and operator promotion are separate Python decisions.
  Neither validator edits activation state.

Code presence alone does not pass a gate. Gate 1 is closed only by its
exact-commit protected report; these offline changes do not replace live
evidence and do not authorize a KIS mutation.

### Next project step

```text
resolve Gate-2 live KIS protocol blockers
  -> review the exact-commit capability manifest and evidence digests
  -> run the full read-only Gate-2 session
  -> independently review and validate the Gate-2 evidence bundle
```

Gate 2 is now the first unavoidable live-environment step. Gates 3, 4, and 5
remain strictly cumulative after it.

## Global pass and promotion contract

A gate pass is tied to an immutable evidence identity:

```text
EvidenceIdentity =
    commit SHA
  + clean tracked-tree digest
  + dependency-lock digest
  + environment and account/app-key scope
  + qualification configuration digest
  + evidence-bundle digest
  + independent review record
```

The cumulative pass rule is:

```text
PASS(Gate N) =
    PASS(every earlier gate on the compatible evidence chain)
AND every Gate-N predicate is true
AND every required observation is present
AND unresolved critical findings = 0
AND independent review = APPROVED
```

Promotion is separate:

```text
PROMOTE_TO(Gate N operating envelope) =
    PASS(Gate N)
AND explicit operator approval
AND deployed identity matches the approved evidence identity
```

Passing a report must never edit activation configuration, arm a trading
session, or promote the runtime automatically.

### Invalidation

- Any tracked source, test, dependency, schema, strategy, or documentation
  commit creates a new SHA and invalidates the prior exact-commit chain for a
  new release candidate.
- A WebSocket adapter, capability interpretation, symbol-key, or qualification
  configuration change invalidates Gate 2 and every later gate.
- A decision-rule or shadow-runtime change invalidates Gate 1, Gate 3, and
  every later gate. Under the repository's strict exact-commit policy, a new
  commit also requires a fresh Gate-2 chain unless a future reviewed policy
  explicitly defines an evidence-preserving equivalence procedure.
- An execution-gateway, reconciliation, ownership, lease, mutation-budget,
  capital, or risk change invalidates Gate 1, Gate 4, and Gate 5, plus any
  earlier exact-commit evidence required by their chain.
- Failed, partial, aborted, or manually repaired qualification sessions do not
  count as passes.

### Exposure-increasing BUY versus protective actions

Entry notional limits, position-count limits, gross-exposure limits, and
portfolio open-risk limits apply only to exposure-increasing BUY actions.
They must not block a protective SELL, Partial Sell, Sell All, stop-loss,
liquidation, exact-owned cancellation, reconciliation, or ambiguous-order
recovery. Universal safety boundaries—such as current lease ownership, exact
order ownership for cancellation, idempotent command identity, and
reconciliation after ambiguity—still apply to the actions they protect.

## Gate 1 — Deterministic simulation

### Purpose

Prove deterministic safety, state-machine completeness, restart behavior,
fault handling, WebSocket protocol behavior, multi-device handoff, model-based
exploration, and legacy/Kanban parity without production activity.

### Pass predicate

```text
G1_PASS =
    clean exact source identity
AND dependency identity recorded
AND supported Python CI matrix passes
AND pytest_exit_code = 0
AND every selected scenario = PASSED
AND every required scenario was executed
AND every required group meets its minimum
AND skipped scenarios = 0
AND unclassified scenarios = 0
AND invariant_violations = 0
AND repository defaults equal the closed activation contract
```

The required post-failure properties are:

```text
no duplicate order
no cancellation without exact order ownership
local projected quantity never understates broker holdings
no open broker order is silently forgotten
no new entry uses stale market data
no destructive mutation occurs after lease loss
```

### Evidence requirements

- Machine-readable Gate-1 report for the exact clean commit.
- Tracked-tree and dependency-lock digests.
- Python 3.11 and 3.12 CI results linked to the same commit.
- Scenario IDs, results, groups, minimums, deterministic seeds, and complete
  closed-default snapshot.
- Zero skips, zero unclassified scenarios, and zero invariant violations.

### Interpretation

Gate 1 certifies structural and execution safety in deterministic tests. It
does not prove live KIS semantics, live broker behavior, trading-strategy
profitability, or production authorization. The report's
`production_activation_authorized=false` field is a non-authorization marker,
not a runtime permission decision.

## Gate 2 — Live KIS read-only protocol qualification

### Purpose

Prove that real KIS WebSocket trade, quote, reconnect, freshness, capacity,
and read-only execution-notice behavior are correctly interpreted while every
broker mutation remains impossible.

The name deliberately says **protocol qualification**, rather than
market-data-only qualification, because the current full Gate-2 contract also
requires encrypted execution-notice interpretation. If that capability is
later moved to Gate 4, the checklist and machine-readable schema must be
changed together through review.

### Required activation snapshot

```text
BUYBOARD_ENGINE_ENABLED=true
TRADING_ENABLED=false
KIS_LIVE_EXECUTION_MODE=DISABLED
KIS_WS_ENABLED=true
KIS_WS_PROTOCOL_VERIFIED=true
KIS_MUTATION_BUDGET_VERIFIED=false
KIS_SUBMIT_MUTATION_CAPACITY=0
KIS_CANCEL_MUTATION_CAPACITY=0
KIS_REPLACE_MUTATION_CAPACITY=0
KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL=0
KIS_CONTROLLED_LIVE_MAX_ENTRY_EQUITY_FRACTION=0
KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY=41
```

### Pass predicate

```text
G2_PASS =
    G1_PASS on the exact qualification commit
AND clean deployed checkout matches that commit
AND reviewed capability manifest matches commit and evidence digests
AND trade/quote timestamp semantics are verified
AND trade/quote sequence and reconnect/reset semantics are verified
AND execution-notice encryption and field mapping are verified
AND read-only activation snapshot is exact
AND one complete regular-session soak finishes
AND every Gate-2 metric passes in one evidence bundle
AND broker mutation attempts = 0
AND independent review = APPROVED
```

### Mandatory metrics

| Metric | Pass requirement |
|---|---:|
| Full regular session | Start no later than open and end no earlier than close |
| Scheduled continuity samples | At least 95%, with zero unexplained critical-unready samples |
| Critical subscription ACK | 100% |
| Aggregate registration usage | At most 41 |
| Required frame coverage | At least one `HDFSCNT0` and `HDFSASP0` frame |
| Parser or malformed-frame failures | 0 |
| Unexpected or unresolved disconnects | 0 |
| Injected disconnect recovery | 100% |
| Reconnect to critical re-ACK | Less than 10 seconds for every injection |
| Critical stale detection | At most 3 seconds |
| Controlled stale-entry probe | At least one rejection and zero allows |
| Duplicate-subscription corruption | 0 |
| Missed synthetic stop breaches | 0 |
| Queue/accumulator deadlocks | 0 |
| Receive-lag p95 | Less than 1 second |
| Receive-lag p99 | Less than 2 seconds |
| Secret or approval-key leaks | 0 |
| Broker mutation attempts | 0 from an initialized final-boundary audit |

### Current blockers

- Real regular-session trade/quote frame interpretation.
- Exchange event-time, local-receive-time, timezone, rollover, clock-skew,
  and daylight-saving evidence.
- Sequence availability and reset/reconnect behavior.
- Execution-notice encryption and decrypted field mapping.
- Reviewed exact-commit capability manifest and evidence digests.
- Forced-disconnect replay with 100% critical re-ACK in under 10 seconds.

## Gate 3 — Shadow execution

### Purpose

Run the final decision runtime against real quotes while replacing every
broker-bound mutation with a durable, isolated `WOULD_*` observation.

### Pass predicate

```text
G3_PASS =
    G2_PASS
AND final production decision runtime is used
AND documented strategy rules are frozen for the qualification commit
AND real quotes drive the live decision pass
AND every broker-bound mutation candidate is intercepted at the final boundary
AND mutation candidates have 100% corresponding WOULD_* audit coverage
AND actual broker mutation attempts = 0
AND fake broker acknowledgements or fills = 0
AND production command/order/position ledgers are not contaminated
AND shadow state is physically isolated and visibly labelled
AND every decision branch is observed live or exercised from captured-live replay
AND unresolved decision-oracle differences = 0
AND stale-data, lease-loss, ownership, reconciliation, and kill-switch fences behave correctly
AND Gate-3 report review = APPROVED
```

Required audit event types are:

```text
WOULD_SUBMIT
WOULD_CANCEL
WOULD_REPLACE
WOULD_SELL
```

Gate 3 requires at least one complete regular session plus complete decision-
branch coverage using captured-live replay for branches that do not naturally
occur during the session. Replay output must remain shadow-only and must never
be represented as a broker acknowledgement or fill.

Before Gate 3, the intended ORH pullback execution behavior and the pending
1-minute-to-5-minute better-plan cancellation/replacement behavior must be
implemented, tested, and frozen. Gate-3 decision comparison is meaningless if
the intended strategy contract is still changing.

## Gate 4 — Controlled live

### Purpose

Validate the final broker path with genuine minimum-size activity, a narrow
symbol/notional envelope, continuous supervision, and external alerts.

### Prerequisites

- Gate 3 passed for the compatible evidence chain.
- Immediate order identity, broker-order-ID scope, accepted mutation behavior,
  order-history latency/completeness, and unambiguous pre-acceptance rate-limit
  semantics are verified for the pilot account and endpoints.
- The PC is the sole authoritative WebSocket/execution owner; the laptop is
  pull-only.
- Startup reconciliation, buying power, lease, database, WebSocket, mutation
  budget, and external-alert delivery are current.

### Pass predicate

```text
G4_PASS =
    G3_PASS
AND execution-specific KIS capabilities are verified
AND KIS_LIVE_EXECUTION_MODE=CONTROLLED_LIVE
AND session starts disarmed and is armed manually only after ACTIVE readiness
AND exactly one current execution owner and lease exist
AND every entry is backed by an authoritative active Trade Card
AND every exposure-increasing BUY is below the reviewed notional cap
AND portfolio risk is atomically rechecked before every exposure-increasing BUY
AND automatic mutation retry attempts = 0
AND supervised broker/local/card lifecycle comparisons agree
AND duplicate or unowned mutations = 0
AND unresolved ambiguous identities = 0
AND disarming proves the next broker mutation is blocked
AND external critical alerts are delivered
AND final reconciliation matches KIS truth
AND Gate-4 report review = APPROVED
```

Qualification requires at least three supervised regular-session dates. At
least one genuine strategy-triggered entry must reach a broker-confirmed
terminal outcome, and its resulting position must be safely exited or remain
correctly reconciled and protected under the reviewed swing-trading plan. A
controlled cancellation lifecycle must also be observed. Natural partial
fills, rejections, or ambiguities must be handled correctly whenever they
occur; they must never be forced merely to satisfy coverage.

The existing optional pre-Gate-2 controlled-live pilot is an exception path,
not a gate pass. It requires separately recorded risk acceptance and cannot be
counted toward Gate 4 until Gates 2 and 3 pass and the evidence is revalidated
against the compatible chain.

## Gate 5 — Unattended qualification

### Purpose

Prove that the system can operate without continuous supervision, remain
protected across realistic infrastructure failures, and alert the operator
from outside the application process.

### Pass predicate

```text
G5_PASS =
    G4_PASS
AND FULL_LIVE scope and risk limits are explicitly approved
AND five consecutive full regular sessions complete
AND duplicate commands = 0
AND unresolved broker/local discrepancies = 0
AND stale projected quantities = 0
AND commands after lease loss = 0
AND every reconnect restores the complete desired subscription set
AND restart and device handoff converge without manual data repair
AND every stop decision uses fresh event data
AND every automatic cancellation has exact order ownership
AND protective exits remain operational
AND external critical-alert delivery is confirmed
AND an independent external heartbeat watchdog is running and tested
AND startup and final reconciliation match broker truth
AND unresolved critical incidents = 0
AND Gate-5 report review = APPROVED
```

The five-session window must include at least one planned process restart, one
execution-lease handoff, and one forced WebSocket reconnect. A drill fails the
qualification window if it requires manual database repair, creates an
unresolved broker identity, loses protective state, or permits a mutation from
the wrong owner.

Gate-5 qualification still does not promote the runtime. Promotion is a
separate exact-report, exact-deployment, exact-configuration decision requiring
explicit operator approval. The promotion validator also records
`activation_state_changed=false`; an operator-controlled deployment procedure
must apply an approved decision separately.

## Workstream 14 — Activation Gate Closure

This is the active project milestone. Its offline implementation and Gate-1
exact-commit certification are complete; live qualification remains. It does
not itself pass Gate 2 or authorize trading.

### Delivery order

1. **Implemented offline:** Normalize gate terminology, ordering, status, and cross-references across
   the repository. Keep remediation **phases** distinct from activation
   **gates**.
2. **Implemented and certified:** Harden Gate-1 evidence with clean-tree,
   tracked-tree, dependency-lock, and CI-matrix identity; regenerate it on
   every later release-candidate commit.
3. **Implemented offline:** Freeze and implement the final ORH pullback plus higher-timeframe
   replacement strategy behavior, with deterministic characterization tests.
4. **Core implemented offline; final runner/live coverage pending:** Implement a final-boundary shadow adapter, isolated shadow store,
   `WOULD_*` schema, decision oracle, coverage matrix, and Gate-3 reporter.
5. **Implemented offline:** Implement machine-readable Gate-4 and Gate-5 report schemas, validators,
   observation counters, drill records, evidence digests, and independent
   review fields.
6. **Implemented offline:** Enforce cumulative compatible-evidence dependencies and explicit promotion;
   no script may alter activation state merely because a report passes.
7. **Implemented offline:** Add tests proving that missing evidence, skipped coverage, dirty source,
   digest mismatch, out-of-sequence promotion, and pilot-exception evidence all
   fail closed.
8. **Complete for current master:** Run compile checks, targeted tests, the
   full test suite, and a clean exact-head Gate-1 certification. Repeat this
   step after every later tracked change.

### Definition of done

Workstream 14 is complete only when:

- this file is the single normative gate specification;
- every other gate document links here instead of defining a conflicting pass
  rule;
- all five gates have versioned machine-readable evidence schemas and
  validators, even when a later gate remains operationally unexecuted;
- Gate 3 has a real isolated runtime path and report generator;
- Gate 4 and Gate 5 have minimum/exact session counts, coverage requirements, drill
  requirements, and zero-tolerance critical metrics;
- the early controlled-live pilot is mechanically prevented from satisfying
  Gate 4;
- a clean exact-head Gate-1 report is regenerated after all changes; and
- repository defaults remain mutation-blocked while the guarded engine remains
  available.

## Related documents

- [Activation gate handoff](activation_gate_handoff.md)
- [Kanban production readiness](kanban_production_readiness.md)
- [Gate-2 readiness checklist](gate2_readiness_checklist.md)
- [KIS capability matrix](kis_capability_matrix.md)
- [Controlled-live pilot runbook](controlled_live_pilot_runbook.md)
- [Portfolio risk operating profiles](portfolio_risk_operations.md)
- [Kanban architecture](kanban_architecture.md)
