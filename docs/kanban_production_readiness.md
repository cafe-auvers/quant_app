# Kanban Production Readiness — Requirements & Invariants

Status: **DRAFT — Workstream 1, revision 2 (architecture-review amendments incorporated; pending sign-off)**
Branch: `feature/kanban-production-readiness`
Base: `109c2c4` ("kanban fix 8")
Supersedes: the "kanban fix 1" .. "kanban fix 8" review-and-patch cycle described in
`buydashboard_to_kanban.md`. That cycle repeatedly closed a reported defect
correctly while leaving the surrounding architecture unspecified, which is why
each fix reliably surfaced the next one (weak broker-order matching → an
actual auto-cancel of it; a lifecycle state inferred from a warning string →
a stale warning bug; card-level reconciliation → cross-account leakage; a
one-minute-bar quote used for stop protection → never actually addressed
because it was declared "out of scope" every round).

Revision 2 responds to an architecture review of revision 1 that found the
draft ~80-85% complete: sound direction, but resting on an unverified KIS
capability assumption (A4) and a real-time-data queue policy that could drop
the one event proving a stop breach (D5). Both are fixed below, along with
several missing operational workstreams the review identified (migration,
legacy/Kanban ownership isolation, rate limiting, database-outage behavior,
external alerting) and a fully-specified Workstream 7 test matrix, so the
document is an actually-complete frozen contract rather than one that defers
its own test plan to "later."

## How to use this document

This is the frozen contract every workstream in this branch implements
against. It is written **before** the code it governs, not derived from it
after the fact — that inversion is the specific process failure this branch
exists to correct.

Rules while this branch is open:

1. No workstream's implementation may weaken an invariant below to make a
   test pass. If an invariant turns out to be wrong or unbuildable, that is
   a reason to stop and revise *this document* explicitly (with the change
   logged in [Change log](#change-log)), not to quietly code around it.
2. Every row in the requirement matrices below needs a passing test before
   its workstream is considered done. "Passing test" means the specific
   scenario in the row, not just "the suite is green."
3. Nothing in this branch merges to `master` piecemeal. Intermediate commits
   organize the work; the PR merges once, when every workstream's status
   below is `DONE` and Gate 1 (deterministic simulation) passes in full.
4. `BUYBOARD_ENGINE_ENABLED` stays `false` in production for the entire
   duration of this branch's development, regardless of how much of it is
   complete.
5. **Workstream 0 gates Workstreams 2 and 5.** No ownership-recovery code
   (Workstream 2's A4) and no WebSocket client (Workstream 5's D1) may be
   written until the relevant capability-matrix rows in Workstream 0 are
   filled in with evidence from the real KIS API, not assumptions. Workstream
   0 requires live KIS credentials (production read-only and/or simulation)
   that this development environment does not have — it has to be run from
   an environment that does, before those two workstreams start.

## Workstream status ledger

| # | Workstream | Status |
|---|---|---|
| 0 | KIS protocol capability verification | NOT STARTED — blocks Workstreams 2 (A4) and 5 (D1) |
| 1 | Freeze requirements and invariants (this document) | DRAFT rev. 2 — pending sign-off |
| 2 | Durable order ownership and command ledger | NOT STARTED |
| 3 | One guarded execution gateway | NOT STARTED |
| 4 | Account-level reconciliation engine | NOT STARTED |
| 5 | Production KIS real-time market data | NOT STARTED |
| 6 | Runtime readiness and device handoff | NOT STARTED |
| 7 | Complete test program | NOT STARTED — matrix now fully specified below (was previously deferred) |
| 8 | Migration and cutover | NOT STARTED |
| 9 | Legacy/Kanban ownership isolation | NOT STARTED |
| 10 | Rate-limit and command-priority scheduling | NOT STARTED |
| 11 | Database-outage behavior | NOT STARTED |
| 12 | External-alert delivery | NOT STARTED |

Update this table as each workstream lands. A workstream is `DONE` only when
every matrix row it owns has a passing test named in that row's `Tests`
column and `python -m compileall` + the full suite are green on this branch.

---

## Non-negotiable invariants

Every requirement row below traces back to one or more of these. An
implementation choice that isn't justified by a row tracing to an invariant
should be treated as scope creep, not added.

| ID | Invariant | Why (concrete precedent) |
|---|---|---|
| INV-1 | Every broker order submitted by the application has a durable local identity. | Recovery has repeatedly had to *infer* ownership from account+symbol+side+quantity+price because no durable record of "we submitted this exact order" survives a crash between broker acceptance and local persistence. |
| INV-2 | Every automatic cancellation identifies an exact application-owned broker order. | "kanban fix 8"'s direct-cancel-by-discovery could still, in principle, cancel a manually-placed order that happens to coincidentally match quantity and price — corroboration is not identity. |
| INV-3 | No destructive broker call occurs without a current execution lease. | Lease checks were added ad hoc per call site; a new call site (the fix-8 discovered-order cancel) shipped without one. |
| INV-4 | Broker holdings are authoritative for current quantity and average price. | Fixed three separate times (fix 6, two spots in fix 7/8) because it was implemented per-code-path instead of once. |
| INV-5 | No order leaves reconciliation until terminal broker status is confirmed. | The fix-8 "retry gap" finding: a partial-fill remainder moved to `OPEN_POSITION` and then fell out of every sweep permanently after one failed cancel attempt. |
| INV-6 | Missing order history alone is never treated as terminal confirmation. | P1-15 (`discover_all_orders.complete`) already encodes this for the current design; it must survive the rewrite. |
| INV-7 | No warning string is used as lifecycle state. | `UNRECONCILED_BROKER_ORDER` is simultaneously a user-facing warning and the periodic sweep's selector for "needs more work" — conflating them produced the stale-warning bug fixed in "kanban fix 8" and will keep producing bugs like it. |
| INV-8 | One account snapshot is used consistently throughout one reconciliation pass. | Card-level reconciliation functions each independently call broker discovery/positions, so two cards for the same account in the same pass can reconcile against two different broker snapshots. |
| INV-9 | New entries require a fresh, acknowledged quote for that exact symbol. | Not enforced today; `_evaluate_buy_today` checks staleness against a receive timestamp that can lag the real event by up to a minute. |
| INV-10 | Existing positions require fresh trade data for stop evaluation, and no stop-relevant price event may be silently discarded on the way to the trading engine. | The 1-minute-bar-close fallback can miss an intraminute breach entirely, and a naive lossy quote queue could drop the one tick that proved a breach happened (Workstream 1 review, item 8) — both are the same underlying failure: summarizing away the exact data a stop decision depends on. |
| INV-11 | Feed disconnection immediately blocks new entries. | Today one successful symbol out of many marks the whole service "connected," so a mostly-broken feed still permits automatic entries on the few symbols still updating. |
| INV-12 | Startup cannot report healthy until reconciliation and market-data readiness both pass. | Startup health today only checks reconciliation; market-data readiness isn't part of `_buyboard_engine_healthy` at all. |
| INV-13 | Laptop/PC handoff cannot permit simultaneous destructive execution. | Partially enforced today via `ExecutionAuthority`/lease checks at scattered call sites (INV-3's problem in miniature); must be centralized. |
| INV-14 | Every workflow transition is restart-safe. | Directly falsified by the fix-8 "stale BUY_TODAY with existing local order" finding: a crash mid-transition left a card permanently outside tracking until this document's authors noticed by inspection. |
| INV-15 | Every automatic action is auditable and idempotent. | No command ledger exists today; a resubmitted trigger after a crash is only stopped by the accident of `DuplicateOpenOrderError`'s local-ledger check, not by a designed idempotency key. |
| INV-16 | The execution command, execution order record, and capital reservation for one submission are committed atomically, before the broker call. | Workstream 1 review, item 3: writing these separately (or after the KIS call) reopens exactly the ambiguous-state gap INV-1 exists to close. |
| INV-17 | A market-data event relevant to an existing position's stop is never silently discarded by the local pipeline; coalescing may summarize, but must retain price extremes and any breach. | Workstream 1 review, item 8 — see INV-10. |
| INV-18 | Only one automatic execution owner (`LEGACY` or `KANBAN`) may act on a given account+symbol at a time. | Workstream 1 review, item H — without this, both systems can independently submit or cancel for the same symbol during rollout. |
| INV-19 | A persistence/database outage blocks new automatic commands but must not leave an existing position without an emergency-exit path. | Workstream 1 review, item J — "the database must be writable" (original E1) doesn't say what happens to an already-open position while it isn't. |
| INV-20 | A KIS API/protocol behavior is relied upon for an ownership or safety decision only after being verified against the real API, never assumed from vendor documentation or the official sample alone. | Workstream 1 review, item 1 — A4 originally assumed `client_order_id` round-trips through KIS order history without that ever having been confirmed against the real endpoint. |

---

## 0. KIS protocol capability verification (Workstream 0)

This workstream has no code deliverable other than the matrix and fixtures
below — it is research against the *real* KIS API (production read-only
and/or simulation) that Workstreams 2 (A4) and 5 (D1) depend on. **It cannot
be executed from this development environment**, which has no live KIS
credentials; it must be run by someone with production/simulation API access,
and its findings pasted into `docs/kis_capability_matrix.md` before A4 or D1
implementation starts.

| Capability | Required proof | Verification method | If unavailable, fallback |
|---|---|---|---|
| External correlation key | Whether `MGCO_APTM_ODNO` (or any other field) accepts a unique application-supplied ID on submission | Submit test orders in simulation with a unique token in `MGCO_APTM_ODNO`, inspect the response | A4 uses the `UNKNOWN_SUBMISSION_STATE` path unconditionally (see A4) — no heuristic-matching fallback is ever treated as ownership |
| Correlation recovery | Whether that value is echoed back in submission responses, open-order queries, history queries, and execution notices | Cross-check one test order's token across all four query surfaces | Same fallback as above |
| Broker order ID | Exact response field name; whether it's present immediately on submission ack or only appears later | Inspect real submission/query responses | `ACKNOWLEDGED` status waits for it; a submission ack with no ID yet keeps the record at `SUBMITTING` (see A2) |
| History latency | Time from submit/cancel to appearance in `inquire-ccnl`/`inquire-nccs` | Timed test submissions, repeated across a session | Reconciliation retry interval (Workstream 4) must exceed the measured worst-case latency with margin |
| History completeness | Max date range, pagination behavior, exchange coverage, whether cancelled/rejected orders appear at all | Boundary-condition queries (very old orders, every configured exchange, a known-cancelled order) | `discovery.complete` (C1) requires every configured exchange/source to individually succeed, per revised C1 below |
| WebSocket symbol key format | Exact subscription key format per exchange (prefix, case, delimiter) | Inspect the official sample's subscribe payloads + a live test subscribe | Blocks D1 implementation until confirmed |
| WebSocket connection/subscription limits | Max symbols per connection; max connections per approval key/account | Attempt subscribing an increasing symbol count against sim/prod | Drives the subscription-capacity tiers in D11 |
| Simulation environment differences | Which TR IDs, order types, and WS feeds are unsupported or behave differently in simulation | Attempt each capability in sim, record failures | Determines which Workstream 7 scenarios can run in Gate 1 (sim) vs. must wait for Gate 2/4 (prod) |
| Quote timestamp fields | Exact field(s) carrying exchange event time vs. local receive time, on both `HDFSCNT0` and `HDFSASP0` | Inspect real WS frames | Blocks D3's `broker_event_at` implementation until confirmed |
| Execution notice encryption | Whether/how `H0GSCNI0`/`H0GSCNI9` payloads are encrypted; which fields survive decryption | Inspect the official sample's decrypt routine + a live test notice | Determines whether D2's "notification-only, never authoritative" use is even parseable; if not, D2 drops the H0GSCNI* input entirely rather than guess at a format |

Required output:

- `docs/kis_capability_matrix.md` — the filled-in table above, with raw
  (redacted) evidence for each row.
- `tests/fixtures/kis_protocol/` — the actual captured (redacted: no real
  account numbers, tokens, or PII) request/response and WS frame samples
  gathered during verification, reused by Workstream 7's protocol tests so
  those tests run against real recorded shapes, not invented ones.

**Gate**: no line of Workstream 2's A4 or Workstream 5's D1 is written until
every row above has a filled-in, evidenced answer — "assume it behaves like
the official sample" is not an acceptable entry.

---

## A. Durable order ownership and command ledger (Workstream 2)

### Order status vs. recovery state

Two separate enums, intentionally not merged:

- **`ExecutionOrderStatus`** — the order's own lifecycle, mirroring what the
  broker actually reports:

  ```python
  class ExecutionOrderStatus(str, Enum):
      PREPARED = "PREPARED"                            # persisted, not yet sent
      SUBMITTING = "SUBMITTING"                         # KIS call in flight
      ACKNOWLEDGED = "ACKNOWLEDGED"                     # broker_order_id known
      UNKNOWN_SUBMISSION_STATE = "UNKNOWN_SUBMISSION_STATE"  # ambiguous window
      REJECTED = "REJECTED"
      WORKING = "WORKING"
      PARTIALLY_FILLED = "PARTIALLY_FILLED"
      FILLED = "FILLED"
      CANCEL_PENDING = "CANCEL_PENDING"
      CANCELLED = "CANCELLED"
      EXPIRED = "EXPIRED"
  ```

- **`OrderRecoveryState`** — whether the application currently *trusts* its
  own view of that status, independent of what the status value is:

  ```python
  class OrderRecoveryState(str, Enum):
      NONE = "NONE"                       # normal tracking, no recovery needed
      DISCOVERING = "DISCOVERING"         # actively resolving an ambiguous window
      OWNERSHIP_UNCERTAIN = "OWNERSHIP_UNCERTAIN"
      CANCEL_REQUIRED = "CANCEL_REQUIRED"
      CANCEL_REQUESTED = "CANCEL_REQUESTED"
      AWAITING_CANCEL_CONFIRMATION = "AWAITING_CANCEL_CONFIRMATION"
      TERMINAL_RECONCILED = "TERMINAL_RECONCILED"
      MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"
  ```

  `UNRECONCILED_BROKER_ORDER` (the presentation-layer warning) is *derived*
  from `recovery_state not in (NONE, TERMINAL_RECONCILED)` — never the other
  way around (INV-7).

### Atomic pre-submission transaction (INV-16)

```
calculate final quantity, price, exchange, order type (post risk-revalidation)
  → atomically persist, in one local transaction:
        execution_command (Workstream 2, A5)
        capital_reservation
        ExecutionOrderRecord(status=PREPARED)
  → set status=SUBMITTING
  → execute the KIS request
  → on a response:
        success  → persist status=ACKNOWLEDGED + broker_order_id
        rejected → persist status=REJECTED, release the reservation
  → on no response (timeout/crash/network loss before the above persist):
        record stays at SUBMITTING
        → next process to look at it (this process's own retry logic, or a
          restart) treats SUBMITTING exactly like a crash-during-send and
          routes it to A4's UNKNOWN_SUBMISSION_STATE handling
```

A crash after the KIS call but before the `ACKNOWLEDGED` persist is
observationally identical to a genuine send failure — both leave the record
at `SUBMITTING`, and both are handled by A4.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| A1. Every submission's command, capital reservation, and `ExecutionOrderRecord(PREPARED)` are written atomically, before the broker call. | `src/core/execution_order_record.py` (new) | `ExecutionOrderRecord` row (`PREPARED`), `execution_commands` row, capital reservation — one local transaction | none yet | The transaction itself fails to commit: nothing was submitted, nothing to recover — safe by construction. | N/A | `test_prepare_writes_command_reservation_and_order_record_atomically`, `test_prepare_failure_leaves_no_partial_state` | Workstream 2 |
| A2. `submitted_quantity`/`submitted_limit_price` recorded are the *actual* post-risk-revalidation values, written at `PREPARED`, before the KIS call — never the card's original plan, and never written only after the fact. | `execution_command_gateway.py` (Workstream 3) writes via this module | `ExecutionOrderRecord.submitted_quantity/submitted_limit_price` at `PREPARED` | none yet | n/a (already covered by A1) | n/a | `test_submitted_quantity_reflects_post_revalidation_size_not_the_original_plan`, `test_submitted_fields_are_persisted_before_the_broker_call_not_after` | Workstream 2 |
| A3. `OrderRecoveryState` transitions are validated; `UNRECONCILED_BROKER_ORDER` is derived, never authoritative (INV-7). | `src/core/order_recovery_state.py` (new) | `ExecutionOrderRecord.recovery_state` | none | An invalid transition (e.g. `TERMINAL_RECONCILED` → `CANCEL_REQUIRED`) raises; it does not silently overwrite. | N/A — this is the state machine itself. | `test_order_recovery_state_transitions_are_validated`, `test_unreconciled_broker_order_warning_is_derived_not_authoritative` | Workstream 2 |
| A4. Ambiguous-submission recovery (a record stuck at `SUBMITTING` after restart, or discovered with no matching local record at all) is resolved **only** by exact identity, per what Workstream 0 proves KIS supports. | `account_reconciliation.py` (Workstream 4) | `recovery_state=DISCOVERING` → resolved or `MANUAL_INTERVENTION_REQUIRED` | `discover_orders` | See branches below. | See branches below. | `test_ambiguous_submission_resolves_by_exact_correlation_key_when_supported`, `test_ambiguous_submission_never_auto_cancels_a_heuristic_match_when_no_correlation_key_exists`, `test_ambiguous_submission_blocks_further_entries_for_the_symbol_until_resolved` | Workstream 2 + 4, gated on Workstream 0 |
| A5. Command idempotency table with a unique constraint on `idempotency_key` prevents duplicate submit/cancel/replace after restart or handoff. | `src/services/execution_command_repository.py` (new) | `execution_commands` table | none directly | A duplicate command (same idempotency key) after restart is rejected/no-ops instead of re-submitting. | N/A | `test_duplicate_submit_command_after_restart_is_rejected_by_idempotency_key`, `test_duplicate_cancel_command_after_lease_handoff_is_rejected` | Workstream 2 |
| A6. `owner_device_id`/`lease_token`/`lease_epoch` are persisted per order and per command. | same as A1/A5 | fields on both records | none | A command whose `lease_epoch` doesn't match the current lease is rejected by the gateway (Workstream 3), not by the caller remembering to check. | N/A | `test_command_with_stale_lease_epoch_is_rejected_by_the_gateway` | Workstream 2 + 3 |

**A4, written out in full** (this is the actual correction the review
required — INV-20 in practice):

```
Internal client_order_id is always durable locally, from A1, regardless of
what KIS supports.

IF Workstream 0 confirms a verified, externally-echoed correlation key:
    resolve exact broker identity using that key.
    (This branch may not exist. Do not build it speculatively before
    Workstream 0 has evidence -- see the Workstream 0 gate.)

ELSE (no verified correlation key -- the default, conservative assumption
      until Workstream 0 says otherwise):
    mark the record UNKNOWN_SUBMISSION_STATE / recovery_state=OWNERSHIP_UNCERTAIN
    block further entry commands for this exact account+symbol
    discover *candidates* using the persisted actual fingerprint
        (submitted_quantity, submitted_limit_price, side, symbol, account,
         and the submission time window from A1/A2 -- never a bare
         "matches account+symbol+side" scan)
    NEVER automatically cancel or replace a candidate found this way --
        candidates are corroboration, not identity (INV-2)
    require manual confirmation (MANUAL_INTERVENTION_REQUIRED) unless/until
        exact broker identity becomes available through some other signal
        (e.g. it later appears with the correlation key after all, or the
         user manually confirms it in the UI)
```

## B. One guarded execution gateway (Workstream 3)

Command persistence is split into two failure domains, deliberately not
treated the same way:

- **Mandatory command journal** (A1/A5): written *before* any broker call.
  If this write fails, **the broker is never called** — there is nothing to
  recover because nothing happened.
- **Broker-response persistence**: written *after* the broker responds. If
  this write fails, the command already reached the broker and may have
  succeeded — **the gateway must not retry the action** (that could
  duplicate a live order). It marks local state `UNKNOWN_SUBMISSION_STATE`
  and triggers immediate reconciliation (A4) instead.
- **Supplementary audit/event log** (human-readable trail, metrics): may
  fail non-blockingly, *provided the mandatory command record from the first
  bullet already exists*. This is the only one of the three "logging can
  fail without blocking the broker action" applies to.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| B1. `submit_order`/`cancel_order`/`replace_order` are the *only* production entry points that call `broker.submit_order`/`broker.cancel_order`. | `src/services/execution_command_gateway.py` (new) | n/a (routes to A) | delegates | A direct call to `broker.*` from outside the gateway fails the architecture test (B-arch). | N/A | `test_architecture_no_direct_broker_mutation_outside_gateway` | Workstream 3 |
| B2. Before any KIS call, the gateway validates, in order: engine flag, admin/session kill switches, current lease + epoch match, account/environment match, exact broker-order ownership (cancel/replace only — per A4/INV-2, never a bare discovery match), idempotency key, current order status, quantity validity, rate-limit budget (Workstream 10). | same | n/a | gate, then delegate | Any single gate failing rejects the whole command with a specific, logged reason — never a partial pass-through. | N/A | one test per gate: `test_gateway_rejects_when_engine_disabled`, `..._kill_switch_active`, `..._stale_lease`, `..._wrong_account`, `..._unowned_cancel_target`, `..._duplicate_idempotency_key`, `..._wrong_current_status`, `..._invalid_quantity`, `..._rate_limit_exceeded` | Workstream 3 |
| B3. `cancel_order` requires an exact `broker_order_id` sourced from an `ExecutionOrderRecord` this application owns with `recovery_state` not `OWNERSHIP_UNCERTAIN` (INV-1, INV-2) — never a discovery-matched snapshot alone. | same | reads A | `broker.cancel_order` | A caller that only has a discovery-matched candidate (A4's `OWNERSHIP_UNCERTAIN` branch) cannot call `cancel_order` directly — it stays alert-only until ownership resolves. | N/A | `test_gateway_cancel_requires_an_owned_execution_order_record`, `test_ownership_uncertain_candidate_cannot_reach_the_cancel_gateway` | Workstream 3 |
| B4a. The mandatory command journal (A1/A5) is written *before* every broker call; if it fails, the broker is never called. | `execution_command_repository.py` | `execution_commands` row, pre-call | none (blocks the call) | Journal write failure = command never sent. | N/A | `test_command_journal_write_failure_prevents_the_broker_call` | Workstream 3 |
| B4b. The broker-response persist happens after the call; if it fails, the action is never retried — local state goes `UNKNOWN_SUBMISSION_STATE` and reconciliation (A4) is triggered immediately. | same | `ExecutionOrderRecord`/`execution_commands.broker_response`, post-call | none (does not retry) | Response-persist failure ≠ resubmission. | A4's ambiguous-submission path. | `test_broker_response_persist_failure_never_triggers_a_retry`, `test_broker_response_persist_failure_marks_unknown_submission_state` | Workstream 3 |
| B4c. The supplementary audit/event log may fail non-blockingly, only because B4a already guarantees a durable command record exists regardless. | `execution_command_repository.py` / event journal | audit trail | n/a | A logging failure here never blocks or duplicates the broker action. | N/A | `test_supplementary_audit_log_failure_does_not_block_or_duplicate_the_broker_call` | Workstream 3 |

## C. Account-level reconciliation engine (Workstream 4)

### Per-source completeness, not one binary flag

```python
@dataclass(frozen=True)
class SnapshotCompleteness:
    holdings_complete: bool
    open_orders_complete: bool
    history_complete: bool
    reserved_orders_complete: bool
    account_balance_complete: bool
```

Different actions need different subsets to be complete — collapsing this
into one `complete` flag (as the current `BrokerOrderDiscoveryResult.complete`
does) means one failing, unrelated source (e.g. reserved-order history) can
block protecting an existing position, which is backwards:

| Action | Required completeness |
|---|---|
| New entry | holdings, open orders, balance |
| Cancel a known-owned live order | open orders (or an exact single-order query) |
| Position quantity update | holdings |
| Terminal order conclusion | open orders **and** history |
| Reserved MOO reconciliation | reserved orders |
| Emergency Sell All | fresh holdings only — order discovery may follow, never block, an emergency exit |

### Absence-generation rule (C3, precise version)

"Two consecutive complete-absence passes" from revision 1 was underspecified
— two calls made milliseconds apart against a delayed backend must not count
as two independent confirmations. Persist, per tracked order:

```python
absence_count: int
last_absence_snapshot_id: str   # identifies which AccountBrokerSnapshot observed the absence
last_absence_observed_at: datetime
last_absence_session_date: date
```

A second absence only counts toward terminal resolution when **all** of:

- it comes from a **different** `AccountBrokerSnapshot` generation than the
  first (not the same fetch re-read twice),
- at least `MIN_ABSENCE_CONFIRMATION_INTERVAL_SECONDS` elapsed since the
  first,
- both absences were checked against the **same** broker order identity
  (not "still nothing for this symbol" broadened past the specific order),
- the resolving pass has a **fresh** holdings snapshot,
- there is **no contradictory evidence** anywhere in that pass (a holding
  change, an execution notice, or a reappeared order record for the same
  identity) — any contradiction resets `absence_count` to 0,
- both observations are in the **same market session**, unless carrying an
  absence over a session boundary is explicitly allowed for that order type.

The exact-broker-order-ID path (C3's primary path, unchanged from revision 1)
is preferred and should make this fallback rare in practice — it exists for
the case where even the exact ID can no longer be queried at all.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| C1. One `AccountBrokerSnapshot` (holdings, open orders, order history, reserved orders) is fetched once per account per reconciliation pass, tagged with a `SnapshotCompleteness`, and reused by every card in that pass (INV-8). | `src/core/account_broker_snapshot.py`, `src/services/account_reconciliation.py` (new) | none (in-memory per pass) | `get_positions`, `discover_orders` (once each) | Each source's own completeness flag is independent; the action-completeness table above (not a single OR/AND over all sources) determines what each action may conclude this pass. | Any incomplete source is retried next pass; only the actions that specifically need it are blocked. | `test_one_snapshot_is_fetched_and_reused_across_every_card_in_an_account`, `test_incomplete_reserved_orders_does_not_block_an_emergency_sell_all`, `test_incomplete_holdings_blocks_new_entries_but_not_a_known_cancel` | Workstream 4 |
| C2. The reducer is pure: given a snapshot + current local state, it returns a `ReconciliationPlan` (card/order/reservation updates, commands, alerts) with no network calls inside it. | `account_reconciliation.py` | n/a | none (reducer emits *commands*, doesn't execute them) | A reducer bug is testable without any broker/network double — pure function of recorded inputs. | N/A | `test_reducer_is_a_pure_function_of_snapshot_and_local_state` (property/fuzz-style over recorded fixtures) | Workstream 4 |
| C3. Terminal-resolution policy: prefer exact broker-order-ID match; else exact local-order reconciliation; else the absence-generation rule above. | `account_reconciliation.py` | `absence_count`, `last_absence_snapshot_id`, `last_absence_observed_at`, `last_absence_session_date`, `recovery_state` progression per A3 | none in the reducer itself | First complete absence: keep recovery state, retain holding, do not clear warning, record the absence fields. | Contradictory evidence at any point resets `absence_count` to 0 and stays flagged. | `test_first_complete_absence_does_not_resolve_terminal`, `test_second_absence_from_the_same_snapshot_generation_does_not_count`, `test_second_absence_within_the_minimum_interval_does_not_count`, `test_second_qualifying_absence_with_fresh_holdings_and_no_contradiction_resolves_terminal`, `test_contradictory_evidence_between_absences_resets_the_counter` | Workstream 4 |
| C4. The reducer covers every order/position category: entry BUY, entry-completion BUY, partial sell, sell all, stop-loss sell, reserved MOO sell, unknown submission, rejected/cancelled/expired, manual broker position (no card), manual broker order (no card), capital reservation without a live order, live order without a capital reservation. | `account_reconciliation.py` | varies by category | none in reducer | Each category has an explicit branch; an unrecognized combination produces an `alerts` entry, never a silent no-op. | N/A | one test per category listed (12 tests minimum) | Workstream 4 |
| C5. This replaces `reconcile_unresolved_orders_at_startup`, `reconcile_buy_today_orders`, `reconcile_untracked_position_remainders`, and the ordering dependency between them. | `account_reconciliation.py` supersedes `src/services/eod_trading_service.py`'s sweep functions | n/a | n/a | The old functions are deleted, not left dead in the tree, once the reducer's coverage (C4) is proven equivalent-or-better on every existing regression test for them. | N/A | Every existing test in `test_eod_trading_service.py` for the superseded functions is ported to exercise the reducer instead, and must still pass. | Workstream 4 |

## D. Production KIS real-time market data (Workstream 5)

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| D1. `kis_websocket.py`/`kis_ws_auth.py` handle approval-key issuance/refresh, connect, subscribe/unsubscribe, ACK/NACK parsing, ping/pong, `^`-delimited frame parsing, encrypted execution-notice decoding, reconnect with backoff+jitter, resubscription after reconnect. | `src/api/kis_websocket.py`, `src/api/kis_ws_auth.py` (new) | none (transport layer) | WS connect/subscribe (read-only market data; execution notices are supplementary, never authoritative — INV-4 keeps broker holdings/discovery as the source of truth for fills) | Malformed frame: logged, dropped, connection stays up. Auth failure: bounded retry then a critical alert (Workstream 12). | Reconnect resubscribes every previously-desired symbol from `SymbolFeedState`, not just the ones that were acked before the drop. | See Workstream 7's WS protocol test list. | Workstream 5, gated on Workstream 0 |
| D2. `kis_realtime_market_data.py` implements the existing `RealtimeMarketDataService` interface over `HDFSCNT0` (trade) and `HDFSASP0` (quote); `H0GSCNI0`/`H0GSCNI9` may feed low-latency *notifications* only — broker reconciliation (Workstream 4) remains authoritative for fills. | `src/services/kis_realtime_market_data.py` (new) | none (in-memory `SymbolFeedState`) | n/a (consumes D1) | A parse failure for one symbol never blocks another symbol's feed. | N/A | `test_execution_notice_never_substitutes_for_broker_reconciled_fill` | Workstream 5 |
| D3. `QuoteSnapshot` carries `broker_event_at` (exchange timestamp) separately from `received_at` (local fetch time). Execution readiness requires **all three**: `broker_age <= BROKER_EVENT_STALE_SECONDS`, `receive_age <= LOCAL_RECEIVE_STALE_SECONDS`, `queue_delay <= MAX_MARKET_DATA_QUEUE_DELAY_SECONDS` — a recent broker timestamp sitting unprocessed in a backed-up local queue is not fresh. | `src/services/realtime_market_data.py` (extend) | n/a | n/a | Also detects and rejects: future broker timestamps, excessive local/exchange clock skew, non-monotonic timestamps, repeated identical events, sequence-number regressions — surfaced as a `clock_health` status feeding engine readiness (E1). | N/A | `test_repeated_fetch_of_the_same_stale_bar_does_not_appear_fresh`, `test_recent_broker_timestamp_with_a_backed_up_queue_is_not_execution_ready`, `test_future_broker_timestamp_is_rejected`, `test_non_monotonic_timestamp_is_rejected`, `test_sequence_regression_is_rejected` | Workstream 5 |
| D4. `SymbolFeedState` tracks `desired`/`trade_acked`/`quote_acked`/timestamps/`last_error`/`reconnect_generation` per symbol; a symbol is execution-ready only when socket connected AND subscriptions acked AND latest event fresh (D3) AND no unresolved sequence/channel error. | `src/services/kis_realtime_market_data.py` | n/a (in-memory) | n/a | One symbol's failure state never marks any other symbol ready (INV-11's precedent bug: today one success marks the whole service connected). | Feed-level reconnect re-evaluates every symbol's state independently. | `test_one_healthy_symbol_does_not_mark_a_failing_symbol_ready`, `test_global_connected_flag_is_not_used_for_per_symbol_execution_readiness` | Workstream 5 |
| D5. Market events reach the trading engine through a **per-symbol accumulator**, not a lossy FIFO — see [PendingMarketState](#d5-pendingmarketstate-not-a-lossy-queue) below (INV-10, INV-17). | `src/ui/buyboard/runtime_worker.py` (modify), `kis_realtime_market_data.py` | n/a | n/a | A slow account/reconciliation pass never delays quote reception; a backed-up drain never *loses* a price extreme or a latched stop breach — it may only delay when the engine sees it (bounded by `queue_delay`, which D3 already treats as staleness). | N/A | see D5 detail below | Workstream 5 |
| D6. Degraded-mode policy table (WS healthy / one symbol stale / socket disconnected / REST fallback only / subscription NACK) is enforced exactly as specified — REST fallback is display/diagnostic only, never treated as equivalent stop-loss protection. | `src/core/execution_config.py` (`MARKET_DATA_MODE`, `MARKET_DATA_FALLBACK_MODE`) + consuming call sites | n/a | n/a | Each row of the policy table is a distinct test. | N/A | `test_market_data_policy_websocket_healthy_allows_entries`, `..._one_symbol_stale_blocks_that_symbol_only`, `..._socket_disconnected_blocks_all_entries`, `..._rest_fallback_blocks_automatic_entries`, `..._subscription_nack_blocks_symbol_and_alerts` | Workstream 5 |
| D7. `.env.example` gets `KIS_PROD_WS_URL`, `KIS_SIM_WS_URL`, `KIS_WS_ENABLED`, `KIS_WS_HTS_ID`, `KIS_MARKET_DATA_MODE`, reconnect/ack/stale/queue tuning vars, `KIS_WS_RAW_CAPTURE_ENABLED`; `requirements.txt` gets a pinned WebSocket dependency (`websockets`, matching the official sample's protocol behavior). | `.env.example`, `requirements.txt` | n/a | n/a | n/a | n/a | config-presence test | Workstream 5 |
| D8. Health surface exposes: WS connected, approval-key age, ACK count vs expected, stale symbols, last trade/quote event, receive-lag p50/p95/p99, reconnect count, NACK count, malformed-frame count, queue depth, dropped-event count (which, per D5, should only ever count *coalesced-duplicate* drops, never a lost extreme/breach). | Health tab / `src/services/kis_realtime_market_data.py` metrics | n/a | n/a | n/a | n/a | `test_market_data_health_metrics_are_exposed_and_update` | Workstream 5 |
| D9. Decision semantics are explicit, not left to be inferred from the data-source choice. | `src/services/trading_engine.py` (modify) | n/a | n/a | See sub-bullets. | n/a | see below | Workstream 5 |
| D10. Feed-outage policy for **existing** positions is explicit, tiered by risk, and bounded — see [outage policy](#d10-feed-outage-policy-for-existing-positions-tiered) below. | `src/core/execution_config.py`, `src/services/trading_engine.py` | outage state per position | emergency `Sell All` via the guarded gateway (Workstream 3), for HIGH-tier only, after grace | See detail below. | See detail below. | see below | Workstream 5 |
| D11. Subscription-capacity management: when desired symbols exceed KIS's actual per-connection/per-account limit (Workstream 0), subscribe in priority order and leave the rest visibly blocked. | `src/services/kis_realtime_market_data.py` | `desired_symbols`, `subscribed_symbols`, `rejected_due_to_capacity`, `critical_symbols_without_subscription` metrics | subscribe/unsubscribe | A `BUY_TODAY` card without an acked subscription stays inactive and visibly blocked — never silently treated as tradeable. | Re-prioritized on every capacity change (a position closing frees a slot for the next-priority symbol). | `test_subscription_capacity_prioritizes_open_positions_over_buy_today`, `test_a_symbol_without_acked_subscription_cannot_become_execute_ready` | Workstream 5 |

#### D9. Decision semantics

- **Entry trigger**: fresh last trade ≥ breakout trigger AND a fresh ask
  exists AND the symbol's feed is execution-ready (D4). The trade proves the
  breakout happened; the ask prices the limit order.
- **Stop trigger**: **any** valid regular-session trade ≤ the active stop
  price latches `STOP_BREACHED` immediately. The one-minute-bar-close check
  is removed entirely — INV-10 exists specifically to kill it.
- **Sell pricing**: use the fresh bid for a marketable limit; if the bid is
  unavailable or stale, fall through to the explicit emergency-exit policy
  (D10), never a guessed price.
- **ORB formation**: state explicitly, per deployment, whether the 5-minute
  opening-range is built from locally-aggregated WebSocket trades (using
  exchange timestamps and regular-session boundaries) or from
  execution-grade KIS minute bars — this is a real design choice with
  different failure modes, not left implicit.
- **Market sessions**: premarket, regular session, after-hours, market
  holidays, early closes, and DST transitions are all explicitly handled; a
  price event outside the strategy's configured session must never
  accidentally arm an entry.

#### D5. `PendingMarketState`, not a lossy queue

A bounded FIFO with drop+count (revision 1's design) can drop exactly the
event that proves a stop was breached — e.g. `101 → 99 (breach) → 101`,
where dropping the middle tick under load means the engine only ever sees
101 and never fires. This is the single most serious correctness bug this
revision fixes.

```python
@dataclass
class PendingMarketState:
    latest_trade: Optional[QuoteSnapshot]
    latest_quote: Optional[QuoteSnapshot]

    minimum_trade_price_since_drain: Optional[float]
    maximum_trade_price_since_drain: Optional[float]

    first_event_at: Optional[datetime]
    last_event_at: Optional[datetime]
    event_count: int

    stop_breach_latched: bool
    breached_stop_version: Optional[int]   # ties the latch to the exact stop it breached
```

The market-data thread may coalesce ordinary quote updates under load, but
it must always retain: the lowest and highest trade price since the engine
last drained this symbol, any stop-breach event, the latest bid/ask, an
event count, and any sequence/channel error. Once `stop_breach_latched=True`
it stays latched until the trading engine explicitly acknowledges it —
never cleared by a later "back above stop" tick arriving in the same drain
window.

`D8`'s `dropped_event_count` metric, under this design, only ever counts
coalesced *duplicate* quote updates (e.g. five best-bid ticks collapsed to
the latest) — a price extreme or a breach is never a "drop."

Tests: `test_a_breach_between_two_higher_prices_in_one_drain_window_is_never_lost`, `test_coalescing_ordinary_quote_updates_preserves_the_min_and_max_trade`, `test_stop_breach_latch_survives_a_later_recovery_tick_in_the_same_window`, `test_latch_clears_only_on_explicit_engine_acknowledgement`.

#### D10. Feed-outage policy for existing positions (tiered)

**Decision (confirmed by the project owner):** tiered by risk, not a single
global policy — a position already near danger is forced closed quickly; a
position with comfortable room is held and alerted, but not forever.

```
MARKET_DATA_OUTAGE_GRACE_SECONDS=           # short: applies to HIGH-tier positions
MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS=        # long: hard ceiling for LOW-tier positions too
MARKET_DATA_OUTAGE_RISK_BUFFER_PCT=         # proximity-to-stop threshold for HIGH tier
MARKET_DATA_OUTAGE_LOSS_THRESHOLD_PCT=      # unrealized-loss threshold for HIGH tier
```

Tier classification, evaluated using the **last trusted** price/quote before
the outage began (never a stale or guessed price):

```python
class OutageRiskTier(str, Enum):
    HIGH = "HIGH"   # forced liquidation after the short grace period
    LOW = "LOW"     # hold and alert, bounded by the long max-hold ceiling
```

A position is `HIGH` tier if, at the last trusted observation, **any** of:
- price is within `MARKET_DATA_OUTAGE_RISK_BUFFER_PCT` of `active_stop_price`,
- unrealized loss already exceeds `MARKET_DATA_OUTAGE_LOSS_THRESHOLD_PCT` of
  position notional,
- the position was already mid-exit (`PARTIAL_SELL`/`SELL_ALL`, or a stop
  breach latched *before* the outage started).

Otherwise `LOW` tier.

```
Socket or critical symbol goes stale/disconnected for a held symbol:
  → immediately block new entries for that symbol (INV-11, unconditional)
  → classify the position's outage tier using the last trusted price
  → begin reconnect attempts immediately, regardless of tier

  HIGH tier:
    → if not recovered within MARKET_DATA_OUTAGE_GRACE_SECONDS:
          issue emergency Sell All via the guarded gateway (Workstream 3)
    → critical alert (Workstream 12) fires immediately, not only at expiry

  LOW tier:
    → hold, critical alert fires immediately
    → re-classify on every reconnect attempt / any recovered price (a LOW
      position can escalate to HIGH mid-outage if the situation worsens
      using whatever last-trusted data is available)
    → if the outage is still unresolved at MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS
      (a longer ceiling than the HIGH-tier grace period), force-liquidate
      regardless of tier -- "hold and alert" is never an indefinite policy
      for an unattended session
```

`HOLD_AND_ALERT`-only (no forced liquidation, ever) remains selectable via
config for *supervised* sessions, where a human is expected to react to the
alert directly — it is never the effective policy while genuinely
unattended.

Tests: `test_high_tier_position_liquidates_after_short_grace_period`, `test_low_tier_position_holds_through_short_grace_period`, `test_low_tier_position_escalates_to_high_on_reclassification`, `test_low_tier_position_force_liquidates_at_the_max_hold_ceiling`, `test_tier_classification_never_uses_a_stale_or_guessed_price`.

## E. Runtime readiness and device handoff (Workstream 6)

### Revised startup/handoff ordering (standby-readiness)

Revision 1's sequence acquired the execution lease *before* reconciliation
and WebSocket startup, which creates avoidable protection downtime: a new
device could acquire the lease and then fail partway through becoming ready,
while the previously-healthy device is already fenced out by the lease loss.
Revised to reach readiness *before* taking over:

```
STARTUP (no existing lease holder -- cold start):
  load durable state
  → connect database
  → read-only broker reconciliation (Workstream 4, does not require the lease)
  → establish WebSocket, subscribe critical symbols
  → receive ACKs and fresh events (D1/D4)
  → acquire execution lease
  → revalidate the lease-sensitive broker/account snapshot (state may have
    moved during the above steps)
  → mark healthy
  → permit broker commands (Workstream 3 gateway now accepts them)

HANDOFF (an existing device already holds the lease):
  new device performs every step above through "receive ACKs and fresh
    events" WITHOUT acquiring the lease -- it reaches STANDBY_READY
  → new device persists a handoff request
  → old device's gateway stops accepting new mutating commands (INV-13)
  → old device releases the lease
  → new device acquires the lease
  → new device runs one immediate final reconciliation pass (state may
    have moved between STANDBY_READY and lease acquisition)
  → new device becomes ACTIVE, marks healthy, permits broker commands
```

A cold startup with no existing lease holder may acquire the lease earlier
in its own sequence (there's no old device to fence), but it still cannot
report healthy until every readiness condition below passes regardless.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| E1. `engine_healthy` requires lease current AND startup reconciliation complete AND account reconciliation fresh AND websocket connected AND critical-symbol subscriptions acked AND critical-symbol quotes fresh (D3/D4) AND market-data accumulator (D5) draining within budget AND database writable (INV-12, INV-19). | `src/ui/main_window.py` (`_buyboard_engine_healthy`, extend) | n/a | n/a | Any single condition false → unhealthy; the legacy monitor's fail-open-to-legacy-protection behavior (existing, keep) still applies. | N/A | one test per condition removed from the AND, `test_engine_healthy_requires_every_condition_including_market_data` | Workstream 6 |
| E2. Startup sequence follows the standby-readiness ordering above — reconciliation and market-data readiness are confirmed *before* the lease is acquired on a cold start, and before a handoff's old device is fenced out. | `src/ui/buyboard/runtime_worker.py` | `STANDBY_READY`/`ACTIVE` device state | per step | A failure at any step halts progression past it; later steps never run on an unconfirmed earlier one. | Retried per the normal per-step retry/backoff. | `test_startup_sequence_does_not_allow_entries_before_every_step_confirms`, `test_new_device_reaches_standby_ready_before_the_old_device_is_fenced` | Workstream 6 |
| E3. Handoff: old device's gateway immediately rejects mutations once it stops holding the lease (INV-13); new device only takes over after reaching `STANDBY_READY` and running a final reconciliation pass. | `execution_command_gateway.py` + `runtime_worker.py` | lease state, device state | none from the losing device | A command issued by the losing device after lease loss is rejected by the gateway (B2), not by a cooperative check in the losing device's own loop. | N/A | `test_losing_device_cannot_submit_after_lease_loss_even_if_its_own_loop_has_not_noticed_yet`, `test_new_device_does_not_take_over_until_standby_ready_and_reconciled` | Workstream 6 |
| E4. Shutdown sequence: block new commands → flush command/reconciliation journal → final account reconciliation → unsubscribe + close WS → release lease. | `runtime_worker.py` | journal flush | unsubscribe | An interrupted shutdown (process killed) is recovered by the next startup's normal reconciliation — shutdown is best-effort, not relied upon for correctness. | Startup reconciliation (E2) is the actual safety net. | `test_interrupted_shutdown_is_recovered_by_next_startup_reconciliation` | Workstream 6 |

---

## F. Complete test program (Workstream 7)

This was previously "deferred to later," which the review correctly pointed
out means the frozen contract wasn't actually complete. It's specified in
full here; no scenario listed below may be dropped without an explicit,
logged revision to this document.

| Requirement | Tests |
|---|---|
| F1. Every crash boundary below has a dedicated fault-injection test. Required property after each: **no duplicate order, no unowned cancellation, no position quantity below broker holdings, no open broker order silently forgotten, no new entry while data is stale, no destructive action after lease loss** — every restart converges to broker truth. | one test per boundary (list below) |
| F2. Every WebSocket protocol scenario below has a dedicated fake-integration-server test, built on the redacted fixtures from Workstream 0. | one test per scenario (list below) |
| F3. Every multi-device scenario below has a dedicated test. | one test per scenario (list below) |
| F4. Model-based end-to-end scenarios below each run as a full lifecycle test. | one test per scenario (list below) |

**F1 — crash boundaries:**
- before command persistence
- after command persistence, before the broker call
- during a broker call timeout (no response either way)
- after broker acceptance, before response persistence
- after a partial fill
- during cancel submission
- during cancel confirmation
- during a replace/reprice
- during lease loss
- during a database failure
- during WebSocket disconnect
- during shutdown
- during optimistic-lock version conflict
- during capital-reservation release

**F2 — WebSocket protocol (fake server, using Workstream 0's captured frame shapes):**
- approval-key failure
- initial connection
- multiple subscriptions
- partial ACK failure
- subscription NACK
- ping/pong
- malformed frame
- duplicate frame
- out-of-order frame
- exchange-timestamp regression
- disconnect
- exponential reconnect
- automatic resubscription after reconnect
- no data for one symbol (others unaffected)
- event-queue/accumulator overload
- stop-breach latching under load (the D5 scenario)
- encrypted execution notice (decode + supplementary-only use)
- clean shutdown

**F3 — multi-device:**
- old device issues a command after lease transfer
- both devices attempt lease acquisition simultaneously
- new device fails readiness partway through a handoff
- database connection loss during handoff
- old device resumes from sleep with a stale lease

**F4 — model-based end-to-end scenarios:**
- `BUY_TODAY` → no trigger → EOD → `BUYLIST`
- `BUY_TODAY` → submitted → partial fill → cancel → `OPEN_POSITION`
- `BUY_TODAY` → crash after submission → restart → recover the exact same order (not a heuristic match)
- `OPEN_POSITION` → partial sell → late fill → reconcile
- `OPEN_POSITION` → stop hit (intraminute tick, not bar close) → Sell All
- Sell All before market open → reserved MOO → market open
- lease loss during a pending cancel
- manual broker order for the same symbol (must never be auto-cancelled — INV-2)
- manual broker position with no card
- WebSocket disconnect during an open position (D10 tiered-outage scenario)
- WebSocket reconnect and resubscribe
- PC/laptop handoff during an open position

CI gates for this PR (all required, all visible):

- `python -m compileall`
- `pytest` on Python 3.11
- `pytest` on Python 3.12
- architecture-boundary tests (B1)
- state-machine tests (A3)
- WebSocket protocol tests (F2)
- fault-injection tests (F1)

No merge without every one of the above passing and visible.

---

## G. Migration and cutover (Workstream 8)

The new architecture changes persisted state materially — this needs an
explicit migration plan, not an assumption that the new code just reads old
rows correctly.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| G1. Schema version is tracked; a migration converts existing order-ledger, trade-card, and capital-reservation records to the new schema. | `src/services/schema_migration.py` (new) | schema version marker | none | Migration failure aborts startup rather than run on partially-migrated state. | Migration is re-run (idempotent) on next startup. | `test_migration_is_idempotent`, `test_migration_aborts_startup_on_failure_rather_than_running_partially_migrated` | Workstream 8 |
| G2. On first launch after migration: back up all existing state → migrate → mark every unresolved legacy order `OWNERSHIP_UNCERTAIN` → run full account reconciliation → block automatic entries until it completes. | same | backup artifact + migrated records | discovery only | n/a | n/a | `test_first_launch_after_migration_marks_unresolved_orders_ownership_uncertain_and_blocks_entries` | Workstream 8 |
| G3. Rollback: the pre-migration backup can restore the prior state if the new schema/code needs to be reverted. | same | backup artifact | none | n/a | n/a | `test_rollback_restores_pre_migration_state` | Workstream 8 |
| G4. Mixed-version prevention: two devices must never run against the same account on different schema versions simultaneously — one running old code, one new. | `runtime_worker.py` startup check | schema version check vs. persisted marker | none | Startup refuses to proceed (not merely warns) on a version mismatch with an active lease holder on the other version. | Resolved by upgrading/downgrading the mismatched device. | `test_startup_refuses_to_proceed_on_schema_version_mismatch_with_an_active_lease` | Workstream 8 |

## H. Legacy/Kanban ownership isolation (Workstream 9)

Gate 4 requires this; nothing in revision 1 implemented it.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| H1. Every account+symbol has exactly one `execution_owner` (`LEGACY`/`KANBAN`/`MANUAL`) and, for `KANBAN`, a `strategy_instance_id`. | `src/core/execution_ownership.py` (new) | `execution_owner`, `strategy_instance_id` per account+symbol | none | An action from the non-owning engine for that symbol is rejected at the gateway (B2), not merely discouraged by convention. | N/A | `test_legacy_monitor_cannot_act_on_a_kanban_owned_symbol`, `test_kanban_cannot_act_on_a_legacy_owned_symbol` | Workstream 9 |
| H2. During controlled rollout, Kanban owns only explicitly assigned symbols; every other symbol defaults to `LEGACY`. | same | ownership table, explicit assignment | none | An unassigned symbol defaults closed to Kanban automation, not open. | N/A | `test_unassigned_symbol_defaults_to_legacy_ownership` | Workstream 9 |

## I. Rate-limit and command-priority scheduling (Workstream 10)

B2 references a rate-limit budget; this workstream is what actually owns it.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| I1. `kis_request_scheduler.py` enforces separate read/write budgets and per-endpoint throttling, with exponential backoff on rate-limit responses. | `src/services/kis_request_scheduler.py` (new) | in-memory budget state | n/a (wraps calls) | A rate-limited response backs off and retries per endpoint, not globally. | N/A | `test_rate_limited_response_backs_off_per_endpoint` | Workstream 10 |
| I2. Requests are prioritized: (1) emergency Sell All/stop-loss exit, (2) exit reconciliation/cancellation, (3) lease/handoff validation, (4) position/order reconciliation, (5) entry cancellation, (6) new entry, (7) display/non-critical refresh. | same | n/a | n/a | A lower-priority backlog never starves a higher-priority request — the scheduler preempts/reorders, not merely FIFOs per priority. | N/A | `test_exit_requests_are_never_starved_by_display_refresh_backlog`, `test_priority_ordering_is_enforced_under_a_full_budget` | Workstream 10 |
| I3. New entries fail closed (never attempted) when the request budget is uncertain (e.g. scheduler state itself is unavailable after a restart). | same | n/a | n/a | Uncertain budget state blocks new entries only — exits still get priority per I2. | N/A | `test_new_entries_fail_closed_when_budget_state_is_uncertain` | Workstream 10 |
| I4. Request metrics are visible in Health. | Health tab | n/a | n/a | n/a | n/a | `test_request_scheduler_metrics_are_exposed` | Workstream 10 |

## J. Database-outage behavior (Workstream 11)

E1 said "the database must be writable" without defining behavior while it
isn't — this workstream closes INV-19.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| J1. On database unavailability: block new entries, keep receiving market data, do not issue any ordinary command that can't be durably journaled (B4a's rule extended to "no DB, no journal, no call"). | `execution_command_gateway.py` | n/a | blocked | Ordinary commands simply don't reach the broker while the DB is down. | Resumes automatically once the DB is writable again — no manual restart required. | `test_ordinary_commands_are_blocked_while_the_database_is_unwritable` | Workstream 11 |
| J2. Emergency exits are the one exception: write to a local append-only emergency journal (file-based, no DB dependency) → submit through the gateway → reconcile against the canonical DB once it returns. | `src/services/emergency_journal.py` (new) | append-only local file | `broker.cancel_order`/Sell All, via the gateway | If even the local emergency journal write fails, trigger the highest-severity external alert (Workstream 12) — this is the one case where the app cannot self-protect and a human must intervene. | The canonical DB reconciles the emergency-journal entries once it's back. | `test_emergency_exit_writes_to_local_journal_when_database_is_unavailable`, `test_emergency_journal_reconciles_into_the_canonical_db_on_recovery`, `test_emergency_journal_write_failure_triggers_the_highest_severity_alert` | Workstream 11 |

## K. External-alert delivery (Workstream 12)

Gate 5 requires this; revision 1 only referenced it via the OS tray
notification, which the review correctly notes is not sufficient for a
sleeping/unattended PC.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| K1. Alerts are delivered through a channel external to the local machine (not only an OS tray notification), with delivery-attempt tracking, acknowledgement/confirmation, retry policy, a deduplication key, and escalation. | `src/services/external_alerting.py` (new) | delivery attempt/ack log | n/a | A delivery failure retries per policy and escalates rather than silently dropping the alert. | N/A | `test_alert_delivery_failure_retries_and_escalates`, `test_duplicate_alerts_are_deduplicated_by_key` | Workstream 12 |
| K2. Critical alert types are enumerated and each has a defined trigger: market-data outage, stale critical symbol, execution lease lost, account reconciliation failed, unknown submission state, unowned broker order discovered, cancel-confirmation timeout, emergency liquidation attempted, database unavailable, application heartbeat missing. | same | n/a | n/a | Each type is independently testable. | N/A | one test per alert type listed | Workstream 12 |

---

## Order-type coverage checklist (cross-reference for C4)

Every row must have an explicit reducer branch and at least one fault-injection scenario from Workstream 7 (F1/F4):

- [ ] Entry BUY
- [ ] Entry completion BUY (remaining target after a partial fill)
- [ ] Partial Sell
- [ ] Sell All
- [ ] Stop-loss Sell
- [ ] Reserved market-on-open Sell
- [ ] Unknown submission state
- [ ] Rejected / cancelled / expired order
- [ ] Manual broker position (no local card)
- [ ] Manual broker order (no local card)
- [ ] Capital reservation with no live order
- [ ] Live order with no capital reservation

## Activation gates (recap, owning invariants noted)

| Gate | Proves | Invariants exercised |
|---|---|---|
| 1. Deterministic simulation | Replay, restart, fault-injection (F1/F3), protocol (F2), and model-based (F4) tests all pass | All |
| 2. Live KIS WebSocket, read-only (`BUYBOARD_ENGINE_ENABLED=false`, `TRADING_ENABLED=false`, `KIS_WS_ENABLED=true`) | Real feed, against the measurable criteria below | INV-9, INV-10, INV-11, INV-17, INV-20 |
| 3. Shadow execution | Real quotes, real decisions, broker mutations replaced with `WOULD_SUBMIT`/`WOULD_CANCEL`/`WOULD_SELL` audit entries, compared against live chart/account | All decision-path invariants, none of the mutation ones |
| 4. Controlled live | One account, one/two symbols, minimum size, supervised, external alerts on (Workstream 12), legacy/Kanban ownership isolated (Workstream 9) | All |
| 5. Unattended activation | No duplicate commands; no unresolved local/broker discrepancy; no stale quantity; no command after lease loss; successful reconnect+resubscribe; every stop decision uses fresh event data; every auto-cancel has exact ownership; startup/handoff converge without manual repair; external critical alerts confirmed reaching the user outside the app (not merely emitted) | All |

### Gate 2 — measurable acceptance criteria

Revision 1 described Gate 2 directionally; these are the actual pass/fail
thresholds, proposed engineering targets rather than claims about KIS's own
service guarantees — adjust only after the live read-only soak produces real
latency measurements, and log any adjustment in the [Change log](#change-log).

| Metric | Requirement |
|---|---|
| Continuous read-only soak | one full regular trading session, no crash/restart needed |
| Subscription ACK | 100% of critical symbols |
| Silent parser failures | 0 |
| Unhandled disconnects | 0 |
| Automatic reconnect | succeeds on every injected disconnect |
| Reconnect + critical-symbol ACK recovery | under 10 seconds |
| Critical-symbol stale detection | within 3 seconds |
| Entries attempted while a symbol is stale | 0 |
| Duplicate-subscription corruption | 0 |
| Missed synthetic stop breaches (D5 test injections) | 0 |
| Queue/accumulator deadlocks | 0 |
| Receive-lag p95 | under 1 second |
| Receive-lag p99 | under 2 seconds |
| Secret/approval-key leakage in logs | 0 |

`BUYBOARD_ENGINE_ENABLED` stays `false` until Gate 2 has passed at minimum,
and stays `false` in unattended/automatic form until Gate 5 passes.

---

## Change log

- 2026-08-15: Initial draft, branch created from `109c2c4` ("kanban fix 8").
- 2026-08-15 (revision 2): Incorporated architecture review of revision 1.
  Added Workstream 0 (KIS capability verification, gating Workstreams 2 and
  5) and INV-20. Rewrote A4 to never treat a heuristic quantity/price match
  as ownership when no verified correlation key exists (was previously
  assumed available). Added the atomic pre-submission transaction and
  `ExecutionOrderStatus` enum to A1/A2, and INV-16. Split B4 into mandatory
  command journal / broker-response persistence / supplementary audit log
  with different failure semantics. Replaced C1's single `complete` flag
  with `SnapshotCompleteness` and an action-specific completeness table.
  Replaced C3's "two consecutive absences" with a precise absence-generation
  rule (snapshot identity, minimum interval, contradiction reset). Replaced
  D3's broker-event-only staleness with a three-part check plus clock-health
  detection. Replaced D5's lossy bounded queue with the `PendingMarketState`
  per-symbol accumulator that never drops a price extreme or a latched stop
  breach, and added INV-17. Added D9 (explicit entry/stop/sell/ORB/session
  decision semantics) and D10 (tiered feed-outage policy for existing
  positions — tier confirmed with the project owner as risk-tiered, not a
  single global policy) and D11 (subscription-capacity management). Revised
  E2's startup/handoff ordering to standby-readiness (reconciliation and
  market data confirmed before lease acquisition/fencing). Fully specified
  Workstream 7's test matrix (was previously deferred). Added Workstream 8
  (migration/cutover), Workstream 9 (legacy/Kanban ownership isolation,
  INV-18), Workstream 10 (rate-limit/command-priority scheduling), Workstream
  11 (database-outage behavior, INV-19), and Workstream 12 (external-alert
  delivery). Added Gate 2's measurable acceptance-criteria table.
