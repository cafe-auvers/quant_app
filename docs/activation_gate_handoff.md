# Activation Gate Handoff

Status: **ACTIVE — Gate 1 closed; Gate 2 live qualification is the next project step**

Date: 2026-08-29

Repository: `cafe-auvers/quant_app`

This is the operational handoff for continuing activation-gate work on the
PC or laptop. The
[Activation Gate Specification](activation_gate_specification.md) is the
single normative source for gate definitions, pass predicates, evidence,
invalidation, and promotion. If this handoff and that specification ever
disagree, the specification wins.

## Handoff outcome

Gate 1 is closed. Gate 2 is the next unresolved gate. Gates 2 through 5 require
live sessions and remain cumulative; none may be skipped. Live trading remains
unauthorized, and every broker-mutation capacity remains zero until the
applicable later gate and a separate operator promotion both pass.

| Gate | Handoff status | Pass logic in one sentence |
|---|---|---|
| 1. Deterministic simulation | **CLOSED / PASSED** | The exact clean commit, dependency identity, Python 3.11/3.12 CI matrix, every selected and required scenario, all group minimums, zero skips/unclassified/invariant violations, and closed activation defaults must all pass together. |
| 2. Live KIS read-only protocol | **NEXT / NOT PASSED** | Gate 1 must remain valid on the exact qualification commit, the deployed checkout and reviewed capability evidence must match it, and one complete regular session must pass every live protocol metric with zero broker mutations and approved independent review. |
| 3. Shadow execution | **NOT QUALIFIED** | Gate 2 must pass, real quotes must drive the frozen production decision runtime, every final-boundary mutation candidate must become an isolated `WOULD_*` record, all branches must be covered, and no real mutation, fake fill, production-ledger contamination, or unresolved oracle difference may occur. |
| 4. Controlled live | **NOT QUALIFIED** | Gate 3 and execution-specific KIS capability evidence must pass, then at least three supervised regular-session dates must satisfy the minimum-size controlled-live envelope with one owner, manual arming, exact risk/ownership/reconciliation, zero duplicate or unowned mutations, delivered alerts, and approved review. |
| 5. Unattended qualification | **NOT QUALIFIED** | Gate 4 must pass, then five consecutive full sessions—including restart, lease-handoff, and forced-reconnect drills—must complete with zero critical safety failures, working protective exits and external watchdog/alerts, matching broker truth, and approved review. |

Passing a gate does not arm or promote the application:

```text
QUALIFIED(Gate N) = compatible prior-gate chain AND every Gate-N predicate

PROMOTED(Gate N) = QUALIFIED(Gate N)
                 AND exact deployment/configuration match
                 AND explicit operator approval
```

## Gate 1 closure evidence

The immutable implementation baseline immediately before this handoff file was
published is:

| Evidence | Value |
|---|---|
| Protected `master` commit | `b286d1e1cfc2eda846d0226caf61a71b413b12be` |
| Git tree | `a72b723a525b8d56773438e3ba456cde5b792d4d` |
| Gate 1 result | `731 passed in 38.61s` |
| GitHub Actions run | `33259003818` |
| Gate 1 job | `99118419968` |
| Report artifact | `gate1-report-b286d1e1cfc2eda846d0226caf61a71b413b12be` |
| Artifact ID | `9716805163` |
| Artifact archive digest | `sha256:2601d8c88c5251b7aa8d3010cd6c3efb8cf36ddae2691f32d1b1c80250838cb4` |
| Local full suite | `2876 passed` |
| Production authorization | `false` |

Relevant pull requests:

- [PR #95 — activation-gate closure implementation](https://github.com/cafe-auvers/quant_app/pull/95)
- [PR #96 — exact-commit certification anchor](https://github.com/cafe-auvers/quant_app/pull/96)
- [PR #97 — protected-master Gate 1 status](https://github.com/cafe-auvers/quant_app/pull/97)

The repository uses an exact-commit policy. Publishing this tracked handoff is
therefore itself a new release-candidate commit and must receive a fresh
successful Python 3.11/3.12 and Gate 1 Actions run before `master` is again
described as closed. Do not copy the baseline SHA above into a claim about a
later commit. For the current truth, resolve `master` and require the successful
`Gate 1 deterministic simulation` check whose report artifact ends in that
exact full SHA.

## What was completed offline

- Gate 1 report schema v2 now fails closed on a dirty tree, incomplete commit
  identity, digest mismatch, missing required scenarios, or incomplete exact-
  commit Python CI evidence.
- `src/core/orb_entry_logic.py` is the canonical ORB entry contract; legacy
  compatibility code delegates to it.
- Higher-timeframe replacement is limited to a strictly higher score, strictly
  higher timeframe, and a zero-fill working order, with authoritative exact-
  owned cancellation before revalidation.
- Gate 3 has an append-only, redacted, physically isolated shadow mutation
  boundary and cumulative validator.
- Gates 4 and 5 have cumulative, fail-closed report and promotion validators.
- Gate qualification remains separate from operator promotion; validation
  never changes activation state.

## Next project step: Gate 2

The next work item is to resolve the live KIS protocol blockers and collect one
complete, read-only regular-session evidence bundle:

```text
confirm the exact current Gate 1 commit
  -> synchronize PC and laptop to that exact commit
  -> review capability manifest and evidence digests
  -> verify mutation-blocked Gate 2 activation snapshot
  -> capture real trade, quote, timestamp, sequence, and encrypted notice data
  -> force reconnect and prove 100% critical re-ACK in under 10 seconds
  -> complete the full regular-session soak
  -> independently review and validate the Gate 2 evidence bundle
```

The required mutation-blocked snapshot is:

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
KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY=41
```

Use [Gate 2 Readiness Checklist](gate2_readiness_checklist.md) for the live
procedure and evidence fields. Synthetic or reconstructed observations must
not be represented as live evidence.

## Workstation pickup procedure

Run this separately on both the PC and laptop before any live qualification:

```bash
git fetch origin
git switch master
git pull --ff-only origin master
git status --short
git rev-parse HEAD
```

Continue only when both checkouts are clean and `git rev-parse HEAD` returns
the same commit that owns the successful exact-SHA Gate 1 report.

## Codex prompt for pickup in VS Code

```text
Open cafe-auvers/quant_app and treat docs/activation_gate_specification.md as
the normative contract and docs/activation_gate_handoff.md as the operational
handoff.

First verify—not merely assume—that Gate 1 is CLOSED for the exact current
origin/master commit. Fetch and fast-forward master, require a clean checkout,
identify the successful Python 3.11/3.12 and Gate 1 deterministic-simulation
GitHub Actions checks for that exact full SHA, inspect the matching
gate1-report-<full SHA> artifact, and confirm zero skipped scenarios, zero
unclassified scenarios, zero invariant violations, the complete required
scenario/group matrix, exact source/dependency identity, and
production_activation_authorized=false. If any tracked correction is needed,
make it through a focused branch and PR, then recertify the new exact commit;
never weaken or bypass the Gate 1 predicate to make it pass.

Once Gate 1 is confirmed closed, make Gate 2 live KIS read-only protocol
qualification the next project step. Keep TRADING_ENABLED=false,
KIS_LIVE_EXECUTION_MODE=DISABLED, all mutation capacities at zero, and the
controlled-live notional cap at zero. Use docs/gate2_readiness_checklist.md,
collect only genuine live-session evidence, and do not claim Gate 2 passed
until every predicate in the normative specification and independent review
passes on one compatible exact-commit evidence chain.
```

## Stop conditions

- Do not proceed with Gate 2 if either machine is dirty or on a different SHA.
- Do not treat engine availability as permission to mutate KIS.
- Do not loosen tests, required metrics, defaults, or evidence schemas to
  manufacture a pass.
- Do not count a partial, interrupted, synthetic, manually repaired, or
  different-commit session as qualifying evidence.
- Any later tracked change requires exact-commit Gate 1 recertification and may
  invalidate the compatible evidence chain described in the specification.
