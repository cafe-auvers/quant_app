# Kanban Production Readiness — Requirements & Invariants

Status: **SIGNED OFF — Workstream 1 complete; revision 3.4 amendment recorded**
Branch: PR1 merged to `master` (commit `5b50e1d`, PR #4). PR2 onward branch
directly off `master` (revision 3.3 — see rule 3 / [PR structure](#pr-structure-revision-3)).
The formerly-planned `feature/kanban-production-readiness` integration
branch is deprecated; see the revision 3.3 change-log entry.
Base: `109c2c4` ("kanban fix 8")
Supersedes: the "kanban fix 1" .. "kanban fix 8" review-and-patch cycle described in
`buydashboard_to_kanban.md`. That cycle repeatedly closed a reported defect
correctly while leaving the surrounding architecture unspecified, which is why
each fix reliably surfaced the next one.

Revision 3 responds to a second architecture review of revision 2, which put
it at ~92-95% complete. The most consequential findings: A4 conflated two
different situations (an ambiguous status for an order the app definitely
submitted, vs. a broker order with no local record at all — a real ownership
question, not a status question); the emergency-journal design in J2
contradicted the gateway's own mandatory-journal rule and never resolved how
a lease is proven valid while the database is down; E1's "keep legacy
fail-open protection" directly contradicted Workstream 9's single-owner rule;
and the document, having grown into a full execution-platform hardening
program, never added a workstream for the thing that started all of this —
proving Kanban actually replaced the legacy Buy Dashboard's own actions. All
are fixed below, along with the smaller precision fixes (state-transition
tables, stop-version concurrency, channel-specific subscription priority,
migration-rollback safety after broker activity, mutation-retry rules, an
external watchdog for the "app crashed" alert case) and the PR-structure
change from one mega-PR to a staged release train.

Revision 3.1 is a narrow errata pass responding to a third review that put
revision 3 at ~98% complete — six precise corrections, not new architecture:
`OrderOwnership` conflated application *origin* with exact broker *identity*
(a `PREPARED` record was called "verified" before any broker call had even
happened); A4b's `DiscoveredExternalOrder` was wrongly routed through
`OrderRecoveryState`, a state machine that belongs to `ExecutionOrderRecord`
only; nothing prevented a single discovered broker order from being
classified as both an A4a candidate and a new A4b external order at once;
`REJECTED` was being used for both an explicit broker rejection and an
*inferred* non-acceptance, which need different evidentiary bars; the
stop-version "synchronous drain" in D5 needed an actual lock/atomic-swap
primitive to be a real guarantee rather than a hand-wave; and the
Workstream 0 gate was broader than necessary, blocking capability-
independent schema/state-machine work in PR1 that doesn't actually depend
on any KIS-specific behavior.

Revision 3.3 is a process correction, not an architecture change: PR1 was
opened and merged as PR #4, discovering along the way that the repository's
actual `.github/workflows/ci.yml` only triggers on `branches: [master]` —
a PR opened against an intermediate long-lived integration branch, as
revision 3's PR structure originally specified, would show zero CI checks,
silently defeating rule 2's "needs a passing test" requirement. Rather than
maintaining a branch whose only purpose (deferring one merge event) CI
never actually validated, this revision replaces the integration-branch
requirement: every PR (1-8) targets `master` directly, reviewed and CI-gated
like any other PR. `BUYBOARD_ENGINE_ENABLED=false` and each workstream's own
feature flags remain the sole activation gate — landing a PR's code on
`master` is explicitly not equivalent to activating it. See the revision
3.3 change-log entry for the full detail.

Revision 3.4 explicitly narrows the Workstream 0 WebSocket gate after PR4's
blocking review. Capability-specific adapters may be implemented provisionally
before live evidence only when they are inactive, clearly labelled
non-authoritative, and impossible to activate with default configuration.
Workstream 0 evidence still gates every production interpretation: enabling
the transport, accepting a timestamp/sequence mapping as execution-grade, and
setting non-zero capacity. The provisional adapter and its fixtures must be
validated and adapted to the recorded evidence before WS0 sign-off. This is an
explicit contract revision; it does not treat unverified behavior as fact.

## How to use this document

This is the frozen contract every workstream in this branch implements
against. It is written **before** the code it governs, not derived from it
after the fact.

Rules while this program is open:

1. No workstream's implementation may weaken an invariant below to make a
   test pass. If an invariant turns out to be wrong or unbuildable, that is
   a reason to stop and revise *this document* explicitly (logged in
   [Change log](#change-log)), not to quietly code around it.
2. Every row in the requirement matrices below needs a passing test before
   its workstream is considered done. "Passing test" means the specific
   scenario in the row, not just "the suite is green."
3. **Staged release train, not one mega-PR** (revision 2's "one PR" rule is
   replaced — see [PR structure](#pr-structure-revision-3)). Each PR (1-8)
   is its own pull request, targeting `master` directly *(revision 3.3 —
   the originally-planned long-lived integration branch is deprecated; see
   the PR structure section)*, fully tested and reviewable on its own. None
   of them individually changes runtime behavior in production — every new
   code path stays behind `BUYBOARD_ENGINE_ENABLED=false` and the relevant
   feature flags until PR8's own Gate 1 run passes. No partial *activation*,
   even though the PRs land on `master` incrementally.
4. `BUYBOARD_ENGINE_ENABLED` stays `false` in production for the entire
   duration of this program, regardless of how much of it is complete.
5. **Workstream 0 gates production use and verified semantics of specific
   capability-dependent code, not all implementation work in Workstreams 2
   and 5** (narrowed in revisions 3.1 and 3.4 — see the "Workstream 0"
   section below for the precise split). Everything capability-independent in
   Workstream 2 (schemas, both transition tables, `DiscoveredExternalOrder`,
   `ExternalOrderDisposition`, the command repository, A4b's conservative
   default behavior) may proceed immediately, in parallel with Workstream 0.
   A4a's KIS-specific correlation-key adapter remains gated on evidence.
   Workstream 5 may carry a provisional inactive WebSocket adapter, but it
   cannot be enabled or treated as execution-grade until Workstream 0 records
   and signs off the corresponding behavior.

## PR structure (revision 3)

Replaces revision 2's "one branch, one PR" rule — 13 workstreams in a single
PR would be unreviewable and unrevertable. **Revision 3.3 correction:**
each PR (1-8) targets `master` directly, not a long-lived integration
branch as originally specified here — the repository's actual CI
(`.github/workflows/ci.yml`) only triggers on `branches: [master]`, so a PR
against an intermediate integration branch would carry no CI signal at all,
contradicting rule 2's "needs a passing test" requirement and this
document's own repeated expectation of visible CI per PR. There is no
longer a separate "the branch merges to master once" event — each PR lands
on `master` as soon as it is reviewed and its own CI is green, inert until
its feature flag is explicitly flipped. `feature/kanban-production-readiness`
is deprecated (never received PR1 and is not used going forward).

| PR | Workstreams | Contents |
|---|---|---|
| PR1 | 0 (skeleton), 2 | Schemas, `ExecutionOrderStatus`/`OrderRecoveryState`/`OrderOrigin`/`BrokerIdentityStatus`/`ExternalOrderDisposition` state machines, `ExecutionOrderRecord`, `DiscoveredExternalOrder`, command repository, A4b's conservative default behavior — **excludes** A4a's KIS-specific correlation-key adapter, which stays gated on Workstream 0 |
| PR2 | 3, 9 | Execution gateway + legacy/Kanban ownership enforcement (the gateway is where H1's rejection actually lives) |
| PR3 | 4 | Account-level reconciliation engine |
| PR4 | 5 | Market-data transport (WebSocket + accumulator) |
| PR5 | 6 | Runtime readiness and device handoff |
| PR6 | 8, 10, 11, 12 | Migration, rate-limit scheduling, database-outage behavior, external alerting |
| PR7 | 13 | Kanban feature parity and UI projection |
| PR8 | 7 (capstone) | Final integration, full Gate 1 run, activation readiness |

Each PR (1-7) includes and must pass its **own** relevant slice of Workstream
7's test matrix (fault-injection/protocol/multi-device scenarios that
exercise that PR's code) — Workstream 7 is not one deferred block of testing
saved for PR8. PR8 is the final end-to-end confirmation that every prior
PR's behavior composes correctly, plus the Gate 1 run in full.

## Workstream status ledger

| # | Workstream | Status |
|---|---|---|
| 0 | KIS protocol capability verification | NOT STARTED — capability-specific adapters remain gated; skeleton complete |
| 1 | Freeze requirements and invariants (this document) | DONE — revision 3.1 signed off |
| 2 | Durable order ownership and command ledger | PR1 IMPLEMENTED, not activated — merged to `master` (`5b50e1d`): schemas, all three state machines, durable repositories, command ledger. Excludes A4a's KIS-specific correlation-key adapter (stays gated on Workstream 0). |
| 3 | One guarded execution gateway | PR2 IMPLEMENTED, not activated — `ExecutionCommandGateway` (`src/services/execution_command_gateway.py`): dual-mode, with genuinely separate call shapes per mode (`submit_order`/`cancel_order` for `LEGACY_COMPATIBILITY`; `submit_guarded`/`cancel_guarded`/`replace_guarded` taking explicit request models with caller-generated stable command identities for `GUARDED_ENGINE`). Full A1-A11/B1-B4 sequence, one authoritative atomic capital reservation with an in-transaction availability check, a real lease-epoch gate, H1 ownership enforcement, a mutation-budget seam for Workstream 10, and fail-closed guarded runtime composition. Runtime-level tests cover restart-restored caller identity, normalized results, full-context tracked cancellation, Partial Sell/Sell All, one-reservation entry, and unresolved post-broker persistence without retry. |
| 4 | Account-level reconciliation engine | PR3 IMPLEMENTED, not activated — one immutable `AccountBrokerSnapshot` per account/pass, per-source `SnapshotCompleteness`, a pure `ReconciliationPlan` reducer, durable two-generation order/reservation absence evidence, lifecycle-linked behavioral C4 projection with terminal attempt-group retirement, execution-boundary and last-broker-boundary fencing for active unowned orders with definitive pre-broker aborts, guarded execution of reducer commands, safe external SELL exposure handling, atomic account-plan persistence plus strict allocator reservation CAS, failure-invalidated action readiness, and one startup/periodic runtime pass replacing the three ordered EOD sweeps. KIS submission-time mapping and production threshold calibration remain gated on Workstream 0; without verified broker submission time, A4a stays manual and unmatched broker orders remain separate `DiscoveredExternalOrder`s. |
| 5 | Production KIS real-time market data | PR4 IMPLEMENTED and merged to `master` (`952179e`), not activated — approval-key/transport lifecycle, ACK/NACK and encrypted-notice framing, exact-event freshness/dedup validation, per-symbol channel readiness, lossless stop-version accumulator, channel-specific capacity, health metrics, market-session semantics, bounded emergency pricing, and tiered persisted outage state are implemented behind `BUYBOARD_ENGINE_ENABLED=false`, `KIS_WS_ENABLED=false`, and `KIS_WS_PROTOCOL_VERIFIED=false`. Live parsing/subscription activation remains blocked until Workstream 0 fills `docs/kis_capability_matrix.md` with credentialed evidence; no vendor-sample assumption is recorded as verified. |
| 6 | Runtime readiness and device handoff | PR5 DRAFT IMPLEMENTED, not activated — durable epoch fencing across clean and stale handoff, generation-fenced `STANDBY_READY` takeover after final reconciliation, persist-before-open `ACTIVE`, strict E1 aggregate health, read-only successor standby, KANBAN/unknown-scoped legacy suppression, exposure-aware ordered shutdown with abort recovery, and stop-latch-preserving promotion are implemented. All execution and WebSocket activation flags remain false. |
| 7 | Complete test program | NOT STARTED — matrix fully specified; distributed across PR1-7, capstone in PR8 |
| 8 | Migration and cutover | PR6 DRAFT IMPLEMENTED, not activated — exact-live-lease-fenced backup/cutover/direct rollback, crash-resumable migration, post-mutation rollback refusal, and forward-only broker reconciliation plus compatibility transform. |
| 9 | Legacy/Kanban ownership isolation | PR2 IMPLEMENTED, not activated — `ExecutionWorkflowService` is the one workflow service both the legacy Buy Dashboard's submission/cancellation entry points and the Kanban runtime (`buyboard_runtime.py`) now default to; an architecture test enforces no direct KIS-mutation call site outside the gateway/adapter. H1's persisted, multi-strategy `execution_owner` table (`src/core/execution_ownership.py` + `execution_ownership_repository.py`) is built and enforced at the gateway (B2) in `GUARDED_ENGINE` mode — `MANUAL` rejects every application source, `KANBAN` accepts only `KANBAN_BOARD`, unassigned defaults `LEGACY` (H2) and rejects `KANBAN_BOARD`. In-process mutual exclusion per `(environment, account_no, symbol)` is enforced additionally, regardless of mode, as a same-process race guard distinct from H1's durable assignment. |
| 10 | Rate-limit and command-priority scheduling | PR6 DRAFT IMPLEMENTED, not activated — strict priorities, per-account/endpoint budgets initialized only from explicit WS0 evidence, and typed pre-acceptance retry classification. |
| 11 | Database-outage behavior | PR6 DRAFT IMPLEMENTED, not activated — bounded last-verified emergency lease, versioned ownership proof, fsynced local journal, protective completion-BUY cancellation, card-correlation recovery, and mandatory post-recovery broker reconciliation. |
| 12 | External-alert delivery | PR6 DRAFT IMPLEMENTED, not activated — durable incident retry/dedupe/ack/escalation, HTTPS provider wiring, DB-independent local alert spool/direct delivery, and external-watchdog heartbeat publication. |
| 13 | Kanban feature parity and UI projection | PR7 IMPLEMENTED on `agent/pr7-kanban-feature-parity`, not activated â€” typed domain-owned board requests now enter only through `ExecutionWorkflowService`; card/ownership/readiness revisions are fenced, UI state is rebuilt from reconciled card/order/external-order projections, legacy submit/cancel UI workers use the same workflow boundary, and unowned broker orders remain distinct until explicit audited adoption. All production activation flags and verified capacities remain closed. |

---

## Non-negotiable invariants

INV-1 through INV-20 are unchanged from revision 2 (see prior change log
entries for their rationale). Revision 3 adds:

| ID | Invariant | Why (concrete precedent) |
|---|---|---|
| INV-21 | The Kanban board is a projection of domain state; a drag/gesture requests an action and never itself declares that the action succeeded. Legacy Buy Dashboard and Kanban invoke the same underlying workflow service — no UI module calls the broker, reconciliation engine, or command repository directly. | Workstream 1 review round 2, item 1 — without this the document could certify the whole execution engine while never proving Kanban actually replaced what the legacy dashboard did. |
| INV-22 | An order discovered at the broker with no matching local application record is never attached to a card, cancelled, replaced, or capital-reserved-against automatically. It is alert-and-display-only until a human explicitly adopts it, and that adoption is recorded as adoption, not fabricated as original application ownership. | Workstream 1 review round 2, item 2 — A4 originally treated "ambiguous status for our own order" and "an order we have no record of" as the same recovery problem; they are different questions (status vs. ownership) with different safe defaults. |
| INV-23 | A destructive broker mutation is retried automatically only when the broker has explicitly confirmed pre-acceptance rejection. A timeout or ambiguous response is never retried — it becomes `UNKNOWN_SUBMISSION_STATE` and goes through reconciliation instead. | Workstream 1 review round 2, item 15 — a naive rate-limit-retry policy (I1) could otherwise resubmit a mutation whose actual broker outcome is unknown. |
| INV-24 | The execution lease's validity for permitting a destructive call is provable independently of canonical-database availability, within a bounded, monotonically-expiring emergency allowance — never assumed still valid indefinitely merely because the database is unreachable. | Workstream 1 review round 2, item 6 — J2's emergency-exit path otherwise has no way to prove the lease is still current while the DB (where lease state normally lives) is down. |
| INV-25 | An execution lease is released while positions remain open only when a successor is `STANDBY_READY` and the handoff is confirmed, or the user explicitly accepts unprotected shutdown after a high-severity warning, or nothing remains open. | Workstream 1 review round 2, item 13 — E4's shutdown sequence otherwise releases the lease unconditionally, which can leave open positions with no lease holder protecting them at all. |

---

## 0. KIS protocol capability verification (Workstream 0)

Revision 3.4 amends the gate below: see the capability matrix and required
outputs. It blocks A4a and capability-dependent C1/C3 implementation, and it
blocks production activation/verified semantics for D1, D3, and D11. A
provisional, inactive D1/D3/D11 adapter may be developed before evidence so
the lifecycle can be reviewed and tested against fakes, but it is not a
verified KIS contract. A4b's conservative default behavior is not blocked.
Live sign-off still requires KIS credentials this environment doesn't have.

| Capability | Required proof | Verification method | If unavailable, fallback |
|---|---|---|---|
| External correlation key | Whether `MGCO_APTM_ODNO` (or any other field) accepts a unique application-supplied ID on submission | Submit test orders in simulation with a unique token in `MGCO_APTM_ODNO`, inspect the response | A4a uses the `UNKNOWN_SUBMISSION_STATE` path unconditionally — no heuristic-matching fallback is ever treated as ownership |
| Correlation recovery | Whether that value is echoed back in submission responses, open-order queries, history queries, and execution notices | Cross-check one test order's token across all four query surfaces | Same fallback as above |
| Broker order ID | Exact response field name; whether it's present immediately on submission ack or only appears later | Inspect real submission/query responses | `ACKNOWLEDGED` status waits for it; a submission ack with no ID yet keeps the record at `SUBMITTING` |
| Broker-order identity uniqueness scope | Whether a broker order ID is unique only within (environment, account, exchange, session/trading-date) or has a wider or narrower actual scope — i.e. whether IDs are ever reused across sessions/trading dates, and whether the same numeric ID can independently exist on two different exchanges for the same account | Inspect real order IDs across multiple sessions/trading dates and, where the account trades more than one exchange, across exchanges; ask whether KIS documents an explicit reuse/rollover policy | `broker_identity_key` (PR1, revision 3.2) is provisionally scoped to `environment:account_no:broker_order_id` only — narrower than a true `(environment, account, exchange, session_date, broker_order_id)` key. If IDs are confirmed to repeat across sessions/trading dates or independently across exchanges, the key must be widened before Workstream 0's gate lifts on any capability that depends on long-lived exact-identity uniqueness; until then this is a known, provisional gap, not a silently-assumed-safe one |
| History latency | Time from submit/cancel to appearance in `inquire-ccnl`/`inquire-nccs` | Timed test submissions, repeated across a session | Reconciliation retry interval (Workstream 4) must exceed the measured worst-case latency with margin |
| History completeness | Max date range, pagination behavior, exchange coverage, whether cancelled/rejected orders appear at all | Boundary-condition queries | `SnapshotCompleteness` (C1) requires every configured exchange/source to individually succeed |
| WebSocket symbol key format | Exact subscription key format per exchange | Inspect the official sample's subscribe payloads + a live test subscribe | Provisional adapter stays disabled; blocks D1 production activation until confirmed |
| WebSocket connection/subscription limits | Max symbols per connection; max connections per approval key/account | Attempt subscribing an increasing symbol count against sim/prod | Drives the subscription-capacity tiers in D11 |
| Simulation environment differences | Which TR IDs, order types, and WS feeds are unsupported or behave differently in simulation | Attempt each capability in sim, record failures | Determines which Workstream 7 scenarios can run in Gate 1 (sim) vs. must wait for Gate 2/4 (prod) |
| Quote timestamp fields | Exact field(s) carrying exchange event time vs. local receive time | Inspect real WS frames | Provisional mapping is non-authoritative; blocks execution-grade D3 activation until confirmed |
| Sequence numbering | Whether any WS channel actually provides a monotonic sequence field, and its exact semantics | Inspect real WS frames across a session, including a forced reconnect | Determines whether D3's sequence-regression check is enabled at all for that channel (see revised D3 below) |
| Execution notice encryption | Whether/how `H0GSCNI0`/`H0GSCNI9` payloads are encrypted; which fields survive decryption | Inspect the official sample's decrypt routine + a live test notice | Determines whether D2's "notification-only" use is even parseable; if not, D2 drops that input entirely |

Required output: `docs/kis_capability_matrix.md` + `tests/fixtures/kis_protocol/`
(redacted captured request/response and WS frame samples).

**Gate (narrowed in revisions 3.1 and 3.4):**

```
Gated on this matrix (do not write until evidenced):
  - A4a's exact-correlation-key broker adapter
  - exact broker-order recovery logic that depends on KIS's real behavior
  - latency/completeness-dependent reconciliation thresholds (C1/C3)

May be written provisionally before evidence, but MUST remain inactive and
explicitly non-authoritative until every corresponding matrix row is signed:
  - D1's WebSocket parsers and subscription client
  - D3's broker-timestamp/sequence field mapping
  - D11's capacity-tier machinery

For those provisional D1/D3/D11 adapters, Workstream 0 evidence is required
before any of the following is permitted:
  - KIS_WS_PROTOCOL_VERIFIED=true or a live connection/subscription
  - treating the provisional field mapping as execution-grade broker time
  - configuring a non-zero production/simulation channel capacity
  - claiming the adapter conforms to actual KIS behavior rather than fakes

NOT gated -- may proceed immediately, in parallel with Workstream 0:
  - ExecutionOrderRecord / DiscoveredExternalOrder schemas
  - ExecutionOrderStatus / OrderRecoveryState / OrderOrigin /
    BrokerIdentityStatus / ExternalOrderDisposition state machines
  - the command repository and its idempotency guarantee
  - A4b's conservative "never auto-own, never auto-cancel" default
    behavior (this rule does not depend on what KIS supports -- it is
    conservative regardless)
  - protocol interfaces and test harnesses (fake broker/fake WS server)
```

---

## A. Durable order ownership and command ledger (Workstream 2)

### Enums across two record types (revised in 3.1)

`ExecutionOrderRecord` (an order the application itself is tracking,
created either via A1's own submission flow or via explicit adoption of a
`DiscoveredExternalOrder`) carries three independent dimensions:

- **`ExecutionOrderStatus`** — the order's own broker-facing lifecycle (full
  transition table below).
- **`OrderRecoveryState`** — whether the application currently trusts its
  own view of that status (full transition table below).
- **`OrderOrigin`** / **`BrokerIdentityStatus`** *(revision 3.1, replacing
  revision 3's single `OrderOwnership`)* — application origin and exact
  broker identity are genuinely different questions and collapsing them
  into one field was imprecise: revision 3 said "every `ExecutionOrderRecord`
  created via A1 is `APPLICATION_VERIFIED` from the moment it's created,"
  which is true of *origin* but not of *broker identity* — at `PREPARED` no
  broker call has even happened yet, and at `SUBMITTING` the application
  definitely attempted a submission but the exact resulting broker order may
  still be unknown. That distinction is exactly what A4a exists to protect,
  so it needs its own field rather than being implied by "ownership."

  ```python
  class OrderOrigin(str, Enum):
      APPLICATION = "APPLICATION"        # created via A1's own submission flow
      USER_ADOPTED = "USER_ADOPTED"      # created by adopting a DiscoveredExternalOrder

  class BrokerIdentityStatus(str, Enum):
      NOT_ASSIGNED = "NOT_ASSIGNED"      # no broker call made yet (PREPARED)
      AMBIGUOUS = "AMBIGUOUS"            # broker boundary may have been entered; outcome unknown (SUBMITTING/UNKNOWN_SUBMISSION_STATE)
      EXACT = "EXACT"                    # broker_order_id confirmed (ACKNOWLEDGED and beyond, or a completed adoption)
      NO_BROKER_ORDER_CONFIRMED = "NO_BROKER_ORDER_CONFIRMED"  # (revision 3.2) confirmed no broker order exists -- REJECTED/NOT_ACCEPTED_CONFIRMED reached from AMBIGUOUS, not EXACT
  ```

  *(Revision 3.2 addition: `NO_BROKER_ORDER_CONFIRMED`.)* `AMBIGUOUS` means
  "the broker boundary may have been entered, outcome unknown" -- once that outcome is
  confirmed negative (an explicit `REJECTED` response, or A4a's inferred
  `NOT_ACCEPTED_CONFIRMED`) with no `broker_order_id` ever having been
  assigned, `AMBIGUOUS` is stale and wrong: there is no longer anything
  unknown about it. `NO_BROKER_ORDER_CONFIRMED` is the confirmed-negative
  counterpart to `EXACT`'s confirmed-positive. This transition is
  automatic, driven by the status transition itself (`REJECTED`/
  `NOT_ACCEPTED_CONFIRMED` reached while identity was `AMBIGUOUS`, not
  `EXACT`) -- it never applies to a *late* rejection reached from
  `ACKNOWLEDGED`/`WORKING`, where a real `broker_order_id` was already
  confirmed and stays `EXACT` (the order existed and is now merely
  terminal, which is different from having never existed).

  Expected combinations:

  | `ExecutionOrderStatus` | `OrderOrigin` | `BrokerIdentityStatus` |
  |---|---|---|
  | `PREPARED` | `APPLICATION` | `NOT_ASSIGNED` |
  | `CANCELLED_LOCALLY` (including a final-gate abort after the `SUBMITTING` commit) | `APPLICATION` | `NOT_ASSIGNED` |
  | `SUBMITTING` / `UNKNOWN_SUBMISSION_STATE` | `APPLICATION` | `AMBIGUOUS` |
  | `ACKNOWLEDGED` and beyond (except as below) | `APPLICATION` | `EXACT` -- **required**, not merely typical: a transition into `ACKNOWLEDGED` without a confirmed `broker_order_id` (either already `EXACT`, or supplied in that same transition) must raise, not persist an inconsistent record |
  | `REJECTED` / `NOT_ACCEPTED_CONFIRMED` reached while identity was still `AMBIGUOUS` | `APPLICATION` | `NO_BROKER_ORDER_CONFIRMED` *(revision 3.2)* |
  | `REJECTED` reached from `ACKNOWLEDGED`/`WORKING` (a late rejection) | `APPLICATION` | `EXACT` -- the broker order genuinely existed |
  | adopted external order | `USER_ADOPTED` | `EXACT` (adoption requires the discovered order's own exact `broker_order_id`; there is no adopted-but-ambiguous state) |

  The cancellation gate (B2/B3) requires **all** of: `origin in
  (APPLICATION, USER_ADOPTED)`, `broker_identity_status == EXACT`,
  `broker_order_id is not null`, `recovery_state` in an explicit allow-list
  (`NONE` or `CANCEL_REQUIRED` -- *revision 3.2*: not merely "anything
  other than `BROKER_IDENTITY_UNCERTAIN`", which would have also wrongly
  permitted `DISCOVERING`, `MANUAL_INTERVENTION_REQUIRED`, or a cancel
  already in flight via `CANCEL_REQUESTED`/`AWAITING_CANCEL_CONFIRMATION`
  to accept a second, duplicate cancel command), and a current status that
  can actually reach `CANCEL_PENDING` -- never `origin` alone, which is
  precisely the gap a `PREPARED`-is-"verified" reading of revision 3 could
  have opened. Once `broker_identity_status` reaches `EXACT`, it is
  immutable except for idempotent re-confirmation of the *same*
  `broker_order_id` -- an attempt to set it to a *different* `broker_order_id`
  is a contradiction and must raise, never silently overwrite (same "any
  contradiction escalates" posture as `OrderRecoveryState`/
  `ExternalOrderDisposition`).

A broker order discovered with no local submission history at all is a
fundamentally different object: a **`DiscoveredExternalOrder`**, not an
`ExecutionOrderRecord` — keeping it out of the same table keeps INV-1
("every order *submitted by the application*") clean, and (revision 3.1) it
does not share `OrderRecoveryState` either, since that state machine
belongs to `ExecutionOrderRecord` specifically. See
`ExternalOrderDisposition` (next subsection) for its own lifecycle.

### `ExecutionOrderStatus` transition table

| From | To | Trigger |
|---|---|---|
| `PREPARED` | `SUBMITTING` | Gateway gates (B2) all pass; about to call the broker |
| `PREPARED` | `CANCELLED_LOCALLY` | Gateway gates reject before any broker call was ever made — distinct from broker-confirmed `CANCELLED` (INV-2: nothing to disown, nothing was ever sent) |
| `SUBMITTING` | `ACKNOWLEDGED` | Broker responded, accepted, `broker_order_id` known |
| `SUBMITTING` | `REJECTED` | Broker responded, explicitly rejected the submission |
| `SUBMITTING` | `UNKNOWN_SUBMISSION_STATE` | Timeout, crash, or network loss before a response was durably persisted (see the SUBMITTING-commit rule below) |
| `SUBMITTING` | `CANCELLED_LOCALLY` | A mutable ownership/lease/external-order fence fails at the final check after the journal commit but before the broker boundary; command becomes `PRE_BROKER_ABORTED` and the reservation is released atomically, so this is definitive local non-acceptance, never ambiguity |
| `UNKNOWN_SUBMISSION_STATE` | `ACKNOWLEDGED` | A4a resolves: broker confirms the order exists, exact identity established |
| `UNKNOWN_SUBMISSION_STATE` | `REJECTED` | The broker/exchange returns **explicit rejection evidence** for this exact submission — a real rejection response, never an inference |
| `UNKNOWN_SUBMISSION_STATE` | `NOT_ACCEPTED_CONFIRMED` | *(revision 3.1)* A4a resolves **by inference**: exact correlation-key lookup plus complete broker-history evidence confirms no broker order was ever created for this submission — distinct from `REJECTED`'s explicit-evidence bar; see the corrected A4a below |
| `ACKNOWLEDGED` | `WORKING` | Broker confirms the order is live/queued |
| `ACKNOWLEDGED` | `PARTIALLY_FILLED` / `FILLED` | Immediate fill observed |
| `ACKNOWLEDGED` | `REJECTED` | Late exchange-side rejection after broker-level acceptance |
| `ACKNOWLEDGED` | `CANCEL_PENDING` | *(revision 3.2)* A cancel command was submitted immediately after acknowledgement, before a separate `WORKING` observation arrived — the acknowledged order is already live at the broker (`broker_order_id` known), so there is no safety reason to force a wait for `WORKING` before an urgent cancel can be requested |
| `WORKING` | `PARTIALLY_FILLED` / `FILLED` / `REJECTED` / `EXPIRED` | Fill, late rejection, or time-in-force expiry |
| `WORKING` | `CANCEL_PENDING` | A cancel command (B1/B3) was submitted |
| `PARTIALLY_FILLED` | `FILLED` / `EXPIRED` | Remainder fills or expires |
| `PARTIALLY_FILLED` | `CANCEL_PENDING` | A cancel command was submitted for the remainder |
| `CANCEL_PENDING` | `CANCELLED` | Broker confirms the cancel |
| `CANCEL_PENDING` | `FILLED` / `PARTIALLY_FILLED` | A fill raced the cancel — must be handled explicitly, never assumed impossible |
| `CANCEL_PENDING` | prior `ACKNOWLEDGED` / `WORKING` / `PARTIALLY_FILLED` state | A mutable ownership/lease/external-order fence fails at the final check before the cancel reaches the broker; restore the exact pre-cancel state and retire the caller-owned cancel ID as `PRE_BROKER_ABORTED` |
| `CANCEL_PENDING` | `WORKING` | *(revision 3.2)* The broker explicitly rejects the cancel request itself (e.g. the order had already progressed past the point a cancel could apply) — the order is simply still working, unchanged |
| `CANCEL_PENDING` | `EXPIRED` | *(revision 3.2)* Time-in-force expiry races the cancel request — same "must be handled explicitly" rule as a racing fill |

Terminal (no outbound transitions): `FILLED`, `CANCELLED`, `REJECTED`,
`EXPIRED`, `CANCELLED_LOCALLY`, `NOT_ACCEPTED_CONFIRMED`. Any transition
attempted from a terminal state raises (A3's existing rule).

`replace_order` is **not** a first-class status. It is a composed logical
operation: cancel the existing `ExecutionOrderRecord` through the normal
`CANCEL_PENDING → CANCELLED` path, then create a brand-new
`ExecutionOrderRecord` through the normal `PREPARED → ...` path, linked via
`replaces_execution_order_id`. This is easier to audit than adding
replace-specific states, and every existing recovery/gateway rule already
applies to both halves without modification.

### `OrderRecoveryState` transition table

| From | To | Trigger |
|---|---|---|
| `NONE` | `DISCOVERING` | An ambiguity is detected: a record stuck at `SUBMITTING` after restart (A4a). *(Revision 3.1: A4b no longer routes through this table at all — a `DiscoveredExternalOrder` isn't an `ExecutionOrderRecord` and never was `NONE` to begin with; see `ExternalOrderDisposition` below.)* |
| `DISCOVERING` | `NONE` | A4a resolves cleanly via exact identity (correlation key, if Workstream 0 confirms one exists) |
| `DISCOVERING` | `BROKER_IDENTITY_UNCERTAIN` | A4a cannot resolve via exact identity within a bounded attempt count |
| `BROKER_IDENTITY_UNCERTAIN` | `CANCEL_REQUIRED` | Broker identity becomes exact (A4a's correlation key appears late) and the resolved order needs cancelling |
| `BROKER_IDENTITY_UNCERTAIN` | `MANUAL_INTERVENTION_REQUIRED` | Bounded retries exhausted with no resolution |
| `CANCEL_REQUIRED` | `CANCEL_REQUESTED` | Gateway (B1/B3) accepts the cancel command |
| `CANCEL_REQUESTED` | `AWAITING_CANCEL_CONFIRMATION` | Cancel submitted, awaiting broker confirmation |
| `AWAITING_CANCEL_CONFIRMATION` | `TERMINAL_RECONCILED` | Broker confirms terminal (cancelled, or a late fill reconciled) |
| any | `MANUAL_INTERVENTION_REQUIRED` | An unrecoverable contradiction at any stage (never silently overwritten) |
| `TERMINAL_RECONCILED` / `MANUAL_INTERVENTION_REQUIRED` | — | Terminal — no further automatic transitions |

`UNRECONCILED_BROKER_ORDER` (presentation warning) is derived from
`recovery_state not in (NONE, TERMINAL_RECONCILED)`, unchanged from
revision 2 (INV-7).

### `ExternalOrderDisposition` — `DiscoveredExternalOrder`'s own lifecycle (revision 3.1)

`DiscoveredExternalOrder` (A4b) is a distinct record type with its own,
much simpler lifecycle — it never shares `OrderRecoveryState` with an
`ExecutionOrderRecord`, since it isn't one:

```python
class ExternalOrderDisposition(str, Enum):
    DISCOVERED_UNOWNED = "DISCOVERED_UNOWNED"   # default, on creation
    USER_ADOPTED = "USER_ADOPTED"               # explicit user action taken
    DISMISSED_TERMINAL = "DISMISSED_TERMINAL"   # broker confirms it's gone, nothing was ever adopted
```

| From | To | Trigger |
|---|---|---|
| — | `DISCOVERED_UNOWNED` | A4b creates the record |
| `DISCOVERED_UNOWNED` | `USER_ADOPTED` | An explicit, audited user "Adopt" action (L4) |
| `DISCOVERED_UNOWNED` | `DISMISSED_TERMINAL` | Reconciliation (Workstream 4) later confirms this exact broker order reached a terminal broker-side status with no adoption ever having happened |

`UNRECONCILED_BROKER_ORDER`-equivalent alerting for a `DiscoveredExternalOrder`
is derived from `disposition == DISCOVERED_UNOWNED` — the same "derived,
never authoritative" rule INV-7 already applies to `OrderRecoveryState`;
revision 3.1 extends its scope to this record type too.

**Adoption creates a brand-new, separate `ExecutionOrderRecord`** — the
original `DiscoveredExternalOrder` is never mutated into one. It stays
immutable (aside from its own `disposition` field) as the audit trail's
permanent record of what was actually discovered and when:

```
DiscoveredExternalOrder(disposition=DISCOVERED_UNOWNED)
  → explicit, audited user "Adopt" action (L4)
  → disposition -> USER_ADOPTED (on the DiscoveredExternalOrder, preserved)
  → creates a NEW ExecutionOrderRecord(
        origin=USER_ADOPTED,
        broker_identity_status=EXACT,
        broker_order_id=<the discovered order's own exact ID>,
        adopted_from_external_order_id=<the DiscoveredExternalOrder's ID>,
        recovery_state=NONE,
        adoption_permissions=<the specific AdoptedOrderPermission set the
                               adoption UI actually granted>,
    )
  → only this new ExecutionOrderRecord may ever be linked to a card or
    reach the cancel gateway (B2/B3), and only for the specific actions
    the adoption UI explicitly authorized (see AdoptedOrderPermission below)
```

**Adoption permission scoping** *(revision 3.2)* — "only for the specific
actions the adoption UI explicitly authorized" needs a concrete mechanism,
not just prose. `USER_ADOPTED` origin alone must not imply blanket
authority to cancel or replace an order the application never submitted:

```python
class AdoptedOrderPermission(str, Enum):
    LINK_TO_CARD = "LINK_TO_CARD"  # may be associated with a TradeCardState at all
    CANCEL = "CANCEL"              # may reach the cancel gateway (B2/B3)
    REPLACE = "REPLACE"            # may reach the replace gateway (B2/B3)
```

`ExecutionOrderRecord.adoption_permissions` (a set of `AdoptedOrderPermission`,
empty for `origin=APPLICATION` records -- the application's own submissions
never need this, they already have full authority over what they
themselves created) is populated once, at adoption time, from exactly what
the adoption UI presented and the user granted -- never defaulted to "all
permissions" merely because `origin == USER_ADOPTED`. B2/B3's identity gate
(revised above) additionally requires, for `USER_ADOPTED` records
specifically, that the action being attempted is present in
`adoption_permissions`.

Persistence for both `ExecutionOrderRecord` and `DiscoveredExternalOrder`
*(revision 3.2 clarification, not new scope -- A1's "Persisted state"
column already said "`ExecutionOrderRecord` row," this makes the durable-
table requirement explicit rather than leaving it implied)*: both need
real, restart-surviving tables (`execution_orders`,
`discovered_external_orders`), not merely in-memory dataclasses -- INV-1
and INV-22 are only actually guaranteed once these survive a crash, which
an in-memory-only representation cannot do. Adoption itself
(`DiscoveredExternalOrder` → new `ExecutionOrderRecord`) must be one atomic
transaction: insert the new `execution_orders` row and update the
`discovered_external_orders` row's `disposition` together, or neither --
never one committed without the other. Every repository function A1/A5
need must additionally support being handed an already-open, caller-owned
transaction/connection (not only opening and committing its own), since
A1's own atomicity requirement (command + reservation + order record, one
transaction) needs to compose three separate repositories' writes into a
single commit once the execution gateway (Workstream 3) orchestrates them.

### Atomic pre-submission transaction (INV-16), with the commit-ordering fix

```
calculate final quantity, price, exchange, order type (post risk-revalidation)
  → atomically persist, in one local transaction:
        execution_command (A5)
        capital_reservation
        ExecutionOrderRecord(status=PREPARED, origin=APPLICATION,
                              broker_identity_status=NOT_ASSIGNED)
  → durably commit status=SUBMITTING  ***before*** any broker call is made
      (revision 3 fix: PREPARED-committed-but-SUBMITTING-write-failed must
      never fall through to calling the broker anyway -- that would reopen
      the exact ambiguous gap INV-1 exists to close)
  → re-read mutable ownership, lease, and active-external-order fences
      immediately before the broker boundary
        gate failure → atomically persist command=PRE_BROKER_ABORTED,
                       order=CANCELLED_LOCALLY, release reservation
  → execute the KIS request
  → on a response:
        success  → persist status=ACKNOWLEDGED + broker_order_id
        rejected → persist status=REJECTED, release the reservation
  → on no response (timeout/crash/network loss before the above persist):
        record stays at SUBMITTING (this state was already durably
        committed before the call, so it survives a crash faithfully)
```

Recovery semantics after a restart, made explicit:

- **`PREPARED` after restart** (the `SUBMITTING` commit never happened): the
  broker call was never authorized — safe to move directly to
  `CANCELLED_LOCALLY` or resume using the same command identity (A5's
  idempotency key covers a resumed attempt).
- **`SUBMITTING` after restart**: broker acceptance is genuinely unknown
  unless the final-gate abort transaction already converted it to
  `CANCELLED_LOCALLY`. Never blindly resubmit — this is A4a's exact trigger.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| A1. Every submission's command, capital reservation, and `ExecutionOrderRecord(PREPARED, origin=APPLICATION, broker_identity_status=NOT_ASSIGNED)` are written atomically, before the broker call; `SUBMITTING` is durably committed before the call. | `src/core/execution_order_record.py` (new) | see above | none yet | The transaction fails to commit: nothing was submitted, nothing to recover. If `SUBMITTING` fails to commit, the broker is never called. | N/A | `test_prepare_writes_command_reservation_and_order_record_atomically`, `test_prepare_failure_leaves_no_partial_state`, `test_broker_is_never_called_if_the_submitting_commit_fails` | Workstream 2 |
| A2. `submitted_quantity`/`submitted_limit_price` recorded are the *actual* post-risk-revalidation values, written at `PREPARED`, before the KIS call. | `execution_command_gateway.py` (Workstream 3) writes via this module | `ExecutionOrderRecord.submitted_quantity/submitted_limit_price` at `PREPARED` | none yet | n/a (covered by A1) | n/a | `test_submitted_quantity_reflects_post_revalidation_size_not_the_original_plan` | Workstream 2 |
| A3. `ExecutionOrderStatus`, `OrderRecoveryState`, and `ExternalOrderDisposition` transitions are all validated against their respective tables above; `UNRECONCILED_BROKER_ORDER` (and its `DiscoveredExternalOrder` equivalent) is derived, never authoritative. `broker_identity_status` only ever reaches `EXACT` alongside a confirmed `broker_order_id` — never inferred from `origin` alone. | `src/core/order_recovery_state.py` (new) | `ExecutionOrderRecord.status`/`.recovery_state`/`.origin`/`.broker_identity_status`, `DiscoveredExternalOrder.disposition` | none | An invalid transition raises; it does not silently overwrite. | N/A | one test per row in each transition table above, plus `test_unreconciled_broker_order_warning_is_derived_not_authoritative`, `test_broker_identity_status_never_reaches_exact_without_a_confirmed_broker_order_id` | Workstream 2 |
| A4a. Ambiguous **status** recovery (our own record stuck at `SUBMITTING` after restart, `broker_identity_status=AMBIGUOUS`) is resolved only by exact identity, per what Workstream 0 proves KIS supports. Origin is never in question here — we know we submitted it; only whether the broker accepted it is unknown. | `account_reconciliation.py` (Workstream 4) | `recovery_state=DISCOVERING` → resolved or `MANUAL_INTERVENTION_REQUIRED`; `broker_identity_status` → `EXACT` on success | `discover_orders` | See A4a branches below. | See below. | `test_a4a_resolves_by_exact_correlation_key_when_supported`, `test_a4a_never_resubmits_a_submitting_record`, `test_a4a_inferred_non_acceptance_uses_not_accepted_confirmed_not_rejected` | Workstream 2 + 4, gated on Workstream 0 |
| A4b. Ambiguous **ownership** recovery (a broker order that reaches step 4 of the classification precedence below — nothing local claims it at all, not even heuristically) creates a `DiscoveredExternalOrder(disposition=DISCOVERED_UNOWNED)` — never an `ExecutionOrderRecord`, never attached to a card, never cancelled/replaced, never capital-reserved-against, automatically. | same | `DiscoveredExternalOrder` row | none automatic | Always alert-and-display; never a card mutation. | Resolved only by an explicit, audited user "adopt" action, which creates a *new*, separate `ExecutionOrderRecord(origin=USER_ADOPTED, broker_identity_status=EXACT)` — see `ExternalOrderDisposition`. | `test_a4b_creates_a_discovered_external_order_not_an_execution_order_record`, `test_a4b_never_auto_cancels_or_attaches_to_a_card`, `test_user_adoption_is_explicit_and_recorded_as_adoption`, `test_a_broker_order_used_as_an_a4a_candidate_is_not_also_created_as_an_external_order` | Workstream 2 + 4 — capability-independent conservative behavior may proceed immediately |
| A5. Command idempotency table with a unique constraint on `idempotency_key` prevents duplicate submit/cancel/replace after restart or handoff. | `src/services/execution_command_repository.py` (new) | `execution_commands` table | none directly | A duplicate command is rejected/no-ops instead of re-submitting. | N/A | `test_duplicate_submit_command_after_restart_is_rejected_by_idempotency_key`, `test_duplicate_cancel_command_after_lease_handoff_is_rejected` | Workstream 2 |
| A6. `owner_device_id`/`lease_token`/`lease_epoch` are persisted per order and per command. | same as A1/A5 | fields on both records | none | A command whose `lease_epoch` doesn't match the current lease is rejected by the gateway. | N/A | `test_command_with_stale_lease_epoch_is_rejected_by_the_gateway` | Workstream 2 + 3 |

**A4a, written out in full:**

```
Internal client_order_id is always durable locally, from A1, regardless of
what KIS supports.

IF Workstream 0 confirms a verified, externally-echoed correlation key:
    resolve exact broker identity using that key. (Do not build this branch
    speculatively before Workstream 0 has evidence.)

ELSE (default, conservative assumption until Workstream 0 says otherwise):
    mark UNKNOWN_SUBMISSION_STATE / recovery_state=BROKER_IDENTITY_UNCERTAIN
    block further entry commands for this exact account+symbol
    discover *candidates* using the persisted actual fingerprint
        (submitted_quantity, submitted_limit_price, side, symbol, account,
         submission time window -- never a bare account+symbol+side scan)
    NEVER automatically cancel or replace a candidate found this way
        (candidates never reach broker_identity_status=EXACT)

    IF Workstream 0 proves KIS supports a strong-enough absence confirmation
       (exact correlation-key lookup + complete broker-history evidence that
        no order was ever created for this exact submission):
        resolve to NOT_ACCEPTED_CONFIRMED (revision 3.1) -- distinct from
        REJECTED, which is reserved for an explicit broker rejection
        response, never an inference
    ELSE:
        require MANUAL_INTERVENTION_REQUIRED unless/until exact identity
        appears -- do not use NOT_ACCEPTED_CONFIRMED speculatively
```

**A4b, written out in full** (the review's central correction):

```
A broker order reaches step 4 of the reconciliation classification
precedence below (revision 3.1) -- i.e. nothing local claims it, not even
heuristically as an A4a candidate. This is an ownership question, not a
status question -- the application has no record of ever submitting it. It
may be a manual order, a legacy-engine order, another application, or a
prior database generation.

    create/update a DiscoveredExternalOrder(disposition=DISCOVERED_UNOWNED)
    surface it as a distinct alert/UI element, separate from any card
    NEVER attach it to a TradeCardState automatically
    NEVER cancel or replace it automatically
    NEVER reserve or release capital based on assumed ownership of it
    remains DISCOVERED_UNOWNED until a human explicitly adopts it, or
        reconciliation confirms it reached a terminal broker status with
        no adoption ever having happened (-> DISMISSED_TERMINAL)

    IF a user explicitly adopts it (L4's "Adopt" action):
        creates a NEW, separate
            ExecutionOrderRecord(origin=USER_ADOPTED, broker_identity_status=EXACT)
        the original DiscoveredExternalOrder's own disposition becomes
            USER_ADOPTED and is preserved as the audit trail of what was
            actually discovered, never rewritten to look application-
            originated -- see ExternalOrderDisposition above
```

### Reconciliation classification precedence (revision 3.1)

A single discovered broker order must never simultaneously become an A4a
candidate for one local record *and* a new `DiscoveredExternalOrder` — that
would both alert on it as unowned and treat it as a match candidate for an
existing ambiguous submission, which is confusing and could let it be
"resolved" twice, inconsistently. Classification is deterministic, applied
in this order, and each broker order is consumed by the first step that
claims it:

```
1. Exact match: broker_order_id equals a known ExecutionOrderRecord's own
   broker_order_id -- this order is that record's own; no further
   classification needed.
2. Verified correlation-key match (only if Workstream 0 confirms one
   exists): the order carries a correlation key matching an
   UNKNOWN_SUBMISSION_STATE ExecutionOrderRecord -- A4a resolves it
   directly, broker_identity_status -> EXACT.
3. Heuristic candidate: the order plausibly corresponds to an
   BROKER_IDENTITY_UNCERTAIN ExecutionOrderRecord's fingerprint (A4a's candidate
   list) -- it stays a non-owning candidate attached to that record's own
   recovery state. It is NOT independently cancellable (B3 still requires
   broker_identity_status == EXACT), and it does NOT also spawn a
   DiscoveredExternalOrder.
4. Everything not consumed by steps 1-3 becomes a DiscoveredExternalOrder
   (A4b) -- genuinely nothing local claims it, even heuristically.
```

Test: `test_a_broker_order_used_as_an_a4a_candidate_is_not_also_created_as_an_external_order`.

## B. One guarded execution gateway (Workstream 3)

Unchanged core design from revision 2 (mandatory command journal /
broker-response persistence / supplementary audit log, three different
failure domains). B2/B3 updated for the A4a/A4b split and Workstream 9:

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| B1. `submit_order`/`cancel_order`/`replace_order` (the last, composed per A's transition table) are the *only* production entry points that call `broker.submit_order`/`broker.cancel_order`. | `src/services/execution_command_gateway.py` (new) | n/a (routes to A) | delegates | A direct call to `broker.*` from outside the gateway fails the architecture test. | N/A | `test_architecture_no_direct_broker_mutation_outside_gateway` | Workstream 3 |
| B2. Before any KIS call, the gateway validates, in order: engine flag, admin/session kill switches, current lease + epoch match, account/environment match, **execution ownership** (H1: this account+symbol belongs to this caller's engine), exact broker-order identity (cancel/replace only — `origin in (APPLICATION, USER_ADOPTED)` **and** `broker_identity_status == EXACT` **and** `broker_order_id` is set; a `USER_ADOPTED` record's adoption must have explicitly authorized this specific action), idempotency key, current order status, quantity validity, rate-limit budget. | same | n/a | gate, then delegate | Any single gate failing rejects the whole command with a specific, logged reason. | N/A | one test per gate, including `test_gateway_rejects_a_symbol_not_owned_by_the_calling_engine`, `test_gateway_rejects_cancel_without_exact_broker_identity` | Workstream 3 |
| B3. `cancel_order`/`replace_order` require `origin in (APPLICATION, USER_ADOPTED)` **and** `broker_identity_status == EXACT` (revision 3.1 — identity, not origin alone). A `DiscoveredExternalOrder` (never an `ExecutionOrderRecord`, per A4b) can never reach these calls, and neither can an `ExecutionOrderRecord` still at `broker_identity_status == AMBIGUOUS` even though its `origin` is already `APPLICATION`. | same | reads A | `broker.cancel_order` | An `BROKER_IDENTITY_UNCERTAIN` record, or any `DiscoveredExternalOrder`, cannot reach the cancel gateway — stays alert-only. | N/A | `test_gateway_cancel_requires_exact_broker_identity_not_origin_alone`, `test_a_submitting_records_ambiguous_broker_identity_cannot_reach_the_cancel_gateway`, `test_a_discovered_external_order_can_never_reach_the_cancel_gateway` | Workstream 3 |
| B4a. The mandatory command journal (A1/A5) is written *before* every broker call; if it fails, the broker is never called. | `execution_command_repository.py` | `execution_commands` row, pre-call | none (blocks the call) | Journal write failure = command never sent. | N/A | `test_command_journal_write_failure_prevents_the_broker_call` | Workstream 3 |
| B4b. The broker-response persist happens after the call; if it fails, the action is never retried — local state goes `UNKNOWN_SUBMISSION_STATE` and A4a is triggered immediately. This rule is authoritative over any retry policy elsewhere (INV-23) — Workstream 10's scheduler must defer to it, never override it. | same | `ExecutionOrderRecord`/`execution_commands.broker_response`, post-call | none (does not retry) | Response-persist failure ≠ resubmission. | A4a. | `test_broker_response_persist_failure_never_triggers_a_retry`, `test_rate_limit_scheduler_never_retries_an_ambiguous_mutation_response` | Workstream 3 |
| B4c. The supplementary audit/event log may fail non-blockingly, only because B4a already guarantees a durable command record exists regardless. | `execution_command_repository.py` / event journal | audit trail | n/a | A logging failure here never blocks or duplicates the broker action. | N/A | `test_supplementary_audit_log_failure_does_not_block_or_duplicate_the_broker_call` | Workstream 3 |

## C. Account-level reconciliation engine (Workstream 4)

`SnapshotCompleteness`, the action-completeness table, and the
absence-generation rule (C3) are unchanged from revision 2. Revision 3
corrects the emergency Sell All quantity math (C1's last row):

### Emergency Sell All quantity (correction)

"Fresh holdings only" is not itself a safe submitted quantity — it ignores
any already-working sell order for the same symbol, which could turn an
emergency exit into an oversell/duplicate-exit attempt. The actual submitted
quantity must account for known-owned outstanding sell exposure:

```
emergency_sell_quantity = fresh_broker_holdings_quantity
                           - exact_known_owned_outstanding_sell_quantity
```

Policy when the outstanding sell quantity itself can't be established:

```
IF a known-owned (origin in APPLICATION/USER_ADOPTED, broker_identity_status
   EXACT) working exit order exists for this symbol:
    prefer cancelling/replacing that exact order over submitting a second,
    separate emergency order, when the gateway can do so within the
    outage's time budget.

IF outstanding exit quantity is uncertain (e.g. open-order discovery is
   itself unavailable):
    IF the broker exposes a "sellable"/"orderable" quantity field distinct
       from raw holdings:
        submit only that broker-reported sellable quantity.
    ELSE:
        do not guess -- critical alert + MANUAL_INTERVENTION_REQUIRED,
        or a separately-approved, broker-specific emergency policy (not
        assumed by this document).
```

"Holdings quantity" and "sellable quantity" are treated as genuinely
different numbers, not interchangeable.

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| C1. One `AccountBrokerSnapshot` fetched once per account per pass, tagged with `SnapshotCompleteness`, reused by every card (INV-8). Emergency Sell All quantity follows the corrected math above — never raw holdings alone. | `src/core/account_broker_snapshot.py`, `src/services/account_reconciliation.py` (new) | none (in-memory per pass) | `get_positions`, `discover_orders` (once each) | Per-source completeness independent; action-completeness table determines what each action may conclude. | Incomplete sources retried next pass; only actions that need them are blocked. | `test_one_snapshot_is_fetched_and_reused_across_every_card_in_an_account`, `test_incomplete_reserved_orders_does_not_block_an_emergency_sell_all`, `test_emergency_sell_all_subtracts_known_owned_outstanding_sell_quantity`, `test_emergency_sell_all_uses_broker_sellable_quantity_when_outstanding_is_uncertain`, `test_emergency_sell_all_alerts_rather_than_guesses_when_neither_is_available` | Workstream 4 |
| C2. The reducer is pure: given a snapshot + current local state, returns a `ReconciliationPlan` with no network calls inside it. | `account_reconciliation.py` | n/a | none | Testable without any broker/network double. | N/A | `test_reducer_is_a_pure_function_of_snapshot_and_local_state` | Workstream 4 |
| C3. Terminal-resolution policy: exact broker-order-ID match, else exact local-order reconciliation, else the absence-generation rule (unchanged from revision 2). | `account_reconciliation.py` | `absence_count`, `last_absence_snapshot_id`, `last_absence_observed_at`, `last_absence_session_date`, `recovery_state` progression | none | First absence: keep state, retain holding, don't clear warning. | Contradictory evidence resets `absence_count`. | `test_first_complete_absence_does_not_resolve_terminal`, `test_second_qualifying_absence_with_fresh_holdings_and_no_contradiction_resolves_terminal` | Workstream 4 |
| C4. The reducer covers every category: entry BUY, entry-completion BUY, partial sell, sell all, stop-loss sell, reserved MOO sell, unknown submission, rejected/cancelled/expired, manual broker position (no card), `DiscoveredExternalOrder` (A4b), capital reservation without a live order, live order without a capital reservation. | `account_reconciliation.py` | varies | none | Each category has an explicit branch; unrecognized combinations alert, never silently no-op. | N/A | one test per category (12 tests minimum) | Workstream 4 |
| C5. This replaces `reconcile_unresolved_orders_at_startup`, `reconcile_buy_today_orders`, `reconcile_untracked_position_remainders`, and their ordering dependency. | `account_reconciliation.py` supersedes `src/services/eod_trading_service.py`'s sweep functions | n/a | n/a | Old functions deleted once C4's coverage is proven equivalent-or-better on every existing regression test. | N/A | Every existing `test_eod_trading_service.py` test for the superseded functions ported to the reducer. | Workstream 4 |

## D. Production KIS real-time market data (Workstream 5)

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| D1. `kis_websocket.py`/`kis_ws_auth.py` handle approval-key issuance/refresh, connect, subscribe/unsubscribe, ACK/NACK parsing, ping/pong, frame parsing, encrypted execution-notice decoding, reconnect with backoff+jitter, resubscription after reconnect. | `src/api/kis_websocket.py`, `src/api/kis_ws_auth.py` (new) | none | WS connect/subscribe (read-only; execution notices supplementary only — INV-4) | Malformed frame: logged, dropped, connection stays up. Auth failure: bounded retry then a critical alert. | Reconnect resubscribes every desired symbol from `SymbolFeedState`. | Workstream 7's WS protocol list. | Provisional inactive adapter allowed by revision 3.4; production activation gated on Workstream 0 |
| D2. `kis_realtime_market_data.py` implements `RealtimeMarketDataService` over `HDFSCNT0`/`HDFSASP0`; `H0GSCNI0`/`H0GSCNI9` feed low-latency *notifications* only — broker reconciliation (Workstream 4) remains authoritative for fills. | `src/services/kis_realtime_market_data.py` (new) | none | n/a | A parse failure for one symbol never blocks another. | N/A | `test_execution_notice_never_substitutes_for_broker_reconciled_fill` | Workstream 5 |
| D3. `QuoteSnapshot` carries `broker_event_at` separately from `received_at`; execution readiness requires broker age + receive age + queue delay all within budget. Deduplication is by **exact duplicate event identity** (same channel, same sequence/trade identifier, same broker timestamp, same payload) — a repeated *price* at a new, distinct trade is a normal, valid event and must never be rejected. Sequence-regression checks apply only to channels Workstream 0 confirms actually provide a real, documented sequence field. | `src/services/realtime_market_data.py` (extend) | n/a | n/a | Also rejects future broker timestamps, excessive clock skew, non-monotonic timestamps — `clock_health` feeds E1. | N/A | `test_repeated_price_at_a_distinct_trade_is_not_rejected_as_a_duplicate`, `test_exact_duplicate_event_identity_is_coalesced`, `test_sequence_check_is_only_enforced_for_channels_workstream_0_confirms_have_one`, `test_future_broker_timestamp_is_rejected` | Workstream 5 |
| D4. `SymbolFeedState` tracks per-symbol subscription/freshness/error state; a symbol is execution-ready only when socket connected AND subscriptions acked AND latest event fresh AND no unresolved sequence/channel error. | `src/services/kis_realtime_market_data.py` | n/a | n/a | One symbol's failure never marks another ready. | Reconnect re-evaluates every symbol independently. | `test_one_healthy_symbol_does_not_mark_a_failing_symbol_ready` | Workstream 5 |
| D5. Market events reach the trading engine through the per-symbol `PendingMarketState` accumulator, with explicit stop-version concurrency handling (below) — never a lossy FIFO (INV-10, INV-17). | `src/ui/buyboard/runtime_worker.py` (modify), `kis_realtime_market_data.py` | n/a | n/a | A slow reconciliation pass never delays quote reception; a backed-up drain never *loses* a price extreme or a latched breach. | N/A | see D5 detail | Workstream 5 |
| D6. Degraded-mode policy table enforced exactly as specified — REST fallback is display/diagnostic only, never equivalent stop-loss protection. | `src/core/execution_config.py` | n/a | n/a | Each row is a distinct test. | N/A | `test_market_data_policy_websocket_healthy_allows_entries`, etc. (unchanged from revision 2) | Workstream 5 |
| D7. `.env.example`/`requirements.txt` config as specified in revision 2, unchanged. | `.env.example`, `requirements.txt` | n/a | n/a | n/a | n/a | config-presence test | Workstream 5 |
| D8. Health metrics as specified in revision 2, with `dropped_event_count` clarified to only ever mean coalesced *duplicate* events (D3's exact-identity dedup), never a lost extreme/breach. Add per-channel: `trade_channels_desired/acked`, `quote_channels_desired/acked`, `critical_trade_channels_missing`, `critical_quote_channels_missing` (D11). | Health tab / metrics | n/a | n/a | n/a | n/a | `test_market_data_health_metrics_are_exposed_and_update` | Workstream 5 |
| D9. Decision semantics (entry trigger, stop trigger, ORB formation, market sessions unchanged from revision 2) plus explicit **emergency pricing** branches (below, closing the "D10 may need to liquidate with no fresh bid" gap). | `src/services/trading_engine.py` (modify) | n/a | n/a | See below. | n/a | see below | Workstream 5 |
| D10. Tiered feed-outage policy for existing positions, with the reclassification rule corrected (below) and account-level risk factors added. | `src/core/execution_config.py`, `src/services/trading_engine.py` | outage state per position | emergency Sell All via the gateway, HIGH-tier only, after grace | See below. | See below. | see below | Workstream 5 |
| D11. Subscription-capacity management is **channel-specific** (trade vs. quote priority, not one combined priority) — see below. | `src/services/kis_realtime_market_data.py` | `desired`/`subscribed`/`rejected_due_to_capacity`/`critical_..._missing`, split by channel | subscribe/unsubscribe | A `BUY_TODAY` card without an acked subscription stays inactive and visibly blocked. | Re-prioritized on every capacity change. | `test_trade_channel_for_open_positions_outranks_quote_channel_for_buy_today` | Workstream 5 |

#### D5. `PendingMarketState` with stop-version concurrency

The `PendingMarketState` dataclass is unchanged from revision 2 (retains
min/max trade since drain, latest bid/ask, event count, latch state).
Revision 3 resolves the concurrency gap the review found: if the active stop
changes *while* events are accumulating in one drain window, a scalar
min/max can't tell whether a given trade breached the stop version that was
actually active at that trade's own timestamp.

**Resolution: a stop-price change is a synchronization boundary.** Rather
than teaching the accumulator about multiple stop versions (which needs
per-tick version bookkeeping) or pushing full tick-path storage into the
accumulator (which reopens the original lossy-queue problem in a different
shape), the engine forces an immediate drain-and-evaluation of the current
`PendingMarketState` against the **old** stop version *before* the new stop
takes effect.

**Revision 3.1 correction:** "synchronously drain" is not itself sufficient
without a real synchronization primitive shared between the market-data
feed thread (which publishes ticks into the accumulator) and the engine
thread (which changes stops) — without one, a trade arriving at the exact
moment of a stop change could be attributed to the wrong version by a race.
This must be an actual per-symbol lock (or an equivalent atomic
compare-and-swap on the accumulator reference), acquired by *both* sides:

```
Engine side (changing the stop, v_old -> v_new):
  acquire per-symbol state lock
  → atomically detach the PendingMarketState governed by v_old
  → install v_new and a fresh PendingMarketState for it
  → release the lock
  → evaluate the detached accumulator against v_old (safe to do outside
    the lock now that it's detached and no longer being written to)

Feed side (publishing a trade/quote for this symbol):
  acquire the same per-symbol state lock (or use the equivalent atomic
    swap primitive) before writing into the currently-installed
    PendingMarketState
  → release after the write

A trade arriving exactly during the engine's atomic detach-and-install
step is therefore deterministically resolved to exactly one version --
whichever side actually holds the lock at that instant -- never split
across both, and never lost to the race.
```

This keeps the accumulator itself simple (still just "coalesce, but never
drop an extreme or a breach," from revision 2) and keeps stop-version
awareness where it already belongs — the trading engine, which is the only
component that changes stops in the first place.

PR4 blocking-review correction: the detached `PendingMarketState` generation
does not own acknowledgement lifetime. Each symbol bucket retains the exact
`(card_key, stop_version)` breach identities (including simultaneous old/new
versions) and their representative event until the engine acknowledges that
exact pair successfully. Rotating or draining a generation never clears the
bucket-level latch. The min and max are representative `QuoteSnapshot`s, not
only scalar prices, so event time and exact identity survive coalescing and
both downward stops and upward entry breakouts remain actionable.

Tests: `test_a_breach_between_two_higher_prices_in_one_drain_window_is_never_lost`, `test_a_stop_price_change_forces_a_drain_against_the_old_version_first`, `test_a_trade_after_a_stop_change_is_evaluated_against_the_new_version_only`, `test_trade_arriving_exactly_during_stop_version_change_is_assigned_to_one_and_only_one_stop_version`, `test_latch_clears_only_on_explicit_engine_acknowledgement`.

#### D9. Decision semantics (additions: emergency pricing)

Entry trigger, stop trigger, ORB formation, and market-session handling are
unchanged from revision 2. Added — explicit **emergency exit pricing**,
since D10 can require liquidation exactly when no fresh bid is available:

```
Regular session, fresh bid available:
    marketable limit using bid * (1 - configured collar); the raw bid alone
    is not the configured-collar branch.

Regular session, no fresh bid:
    use a verified KIS-supported emergency order type if one exists
    (Workstream 0 to confirm), else a bounded price collar derived from
    the last trusted quote with controlled, limited reprice attempts --
    never an unbounded market order assumed safe by default.

Outside regular session:
    persist a next-session Sell All / MOO / LOO instruction, per whatever
    Workstream 0 confirms KIS actually supports for the account.

Trading halt:
    retain the exit intent, alert, and retry as soon as execution is
    possible again -- "force-liquidate" never implies execution is always
    technically possible.
```

#### D10. Feed-outage policy for existing positions (corrected reclassification)

Tier classification (`HIGH`/`LOW`), the short grace period for `HIGH`, and
the long hard ceiling for `LOW` are unchanged from revision 2. Corrected:
**during a total outage for a specific symbol, its price-based tier freezes
at the last trusted observation** — the application has no new trusted price
data for that symbol to justify claiming the situation "worsened." Escalation
during an outage is driven only by sources that don't require the missing
feed:

```
During a total symbol-data outage, price-based tier classification remains
FROZEN at the last trusted observation for that symbol.

The position may still escalate LOW -> HIGH during the outage based on:
  - elapsed outage duration (bounded by MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS,
    unchanged from revision 2),
  - a separately-available, still-healthy broader market signal (e.g. an
    index/ETF move, if that feed is unaffected),
  - risk classification already known before the outage began (this was
    always allowed; it just isn't "new" information).

A recovered trusted price for the symbol itself may immediately reclassify
LOW -> HIGH (or confirm it should stay LOW) -- that is genuinely new data.
```

Tier classification also now considers account-level risk, not only
position-notional percentages: position risk as % of account equity,
position concentration, distance to stop in R/ATR multiples, and the
symbol's liquidity/spread tier.

Every emergency Sell All origin uses the same conflict-resolution sequence:
a working entry-completion BUY is assigned a tracked cancel identity and must
reach broker-confirmed terminal reconciliation before actual orderable
quantity is refreshed and any liquidation SELL is submitted. The retry stage
independently rechecks this fence so a user, stop, or outage origin cannot
bypass it.

Tests (in addition to revision 2's): `test_frozen_tier_does_not_escalate_purely_from_elapsed_time_alone_without_a_configured_duration_ceiling`, `test_a_broader_market_signal_can_escalate_a_frozen_tier`, `test_a_recovered_price_for_the_symbol_itself_reclassifies_immediately`, `test_tier_classification_considers_account_level_risk_factors`.

#### D11. Channel-specific subscription priority

Revision 2's single combined priority list is replaced with separate
trade-channel and quote-channel priority, since a stop-loss decision only
needs the trade channel while exit pricing needs the quote channel — under
capacity pressure they shouldn't compete on the same list:

```
Trade channel priority:
  1. STOP_BREACHED / EXIT_PENDING symbols
  2. all other OPEN_POSITION symbols
  3. ENTRY_PENDING symbols
  4. BUY_TODAY symbols
  5. display-only symbols

Quote channel priority:
  1. EXIT_PENDING / OPEN_POSITION symbols (pricing an exit)
  2. ENTRY_PENDING symbols (pricing a completion)
  3. BUY_TODAY symbols (pricing an entry)
  4. display-only symbols
```

Metrics split accordingly: `trade_channels_desired/acked`,
`quote_channels_desired/acked`, `critical_trade_channels_missing`,
`critical_quote_channels_missing` (folded into D8).

## E. Runtime readiness and device handoff (Workstream 6)

The standby-readiness startup/handoff ordering from revision 2 is unchanged.
Two corrections:

### E1 corrected: legacy fail-open is scoped to legacy-owned symbols

Revision 2's E1 said to keep "the legacy monitor's fail-open-to-legacy-
protection behavior" unconditionally, which directly contradicts Workstream
9's single-owner rule (INV-18) — a Kanban health failure on a Kanban-owned
symbol must not silently reactivate legacy execution for that exact symbol,
or both engines could act on it at once.

```
The legacy monitor's fail-open protection applies ONLY to symbols whose
execution_owner is LEGACY (Workstream 9).

For a KANBAN-owned symbol:
    the legacy monitor may still observe and alert on it,
    but the gateway (B2) rejects every legacy-sourced mutation for that
        symbol regardless of Kanban's own health.
    a Kanban outage on a Kanban-owned symbol surfaces as a critical alert
        requiring an EXPLICIT ownership transfer back to LEGACY (a
        Workstream 9 action with its own audit trail) -- never an implicit
        fallback.
```

### E4 corrected: lease release requires either a ready successor or nothing open

```
A device may release the execution lease while positions remain open ONLY IF:
  - a successor device has reached STANDBY_READY and the handoff is
    confirmed (E2/E3's sequence), OR
  - the user explicitly accepts unprotected/supervised shutdown after a
    high-severity warning naming every open position, OR
  - every position and working order is already closed/cancelled.

For unattended sessions specifically: no ready successor AND an open
position means shutdown is REFUSED, or the configured emergency-liquidation
policy (D10-equivalent) runs before the lease is released -- "release the
lease anyway" is not an available outcome while unattended.
```

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| E1. `engine_healthy` requires lease current AND startup reconciliation complete AND account reconciliation fresh AND websocket connected AND critical-symbol subscriptions acked (both channels, D11) AND critical-symbol quotes fresh AND market-data accumulator draining within budget AND database writable (INV-12, INV-19). Legacy fail-open scoped per the correction above. | `src/ui/main_window.py` (extend) | n/a | n/a | Any single condition false → unhealthy. | N/A | one test per condition, plus `test_legacy_fail_open_never_activates_for_a_kanban_owned_symbol` | Workstream 6 |
| E2. Standby-readiness startup/handoff ordering (unchanged from revision 2). | `src/ui/buyboard/runtime_worker.py` | `STANDBY_READY`/`ACTIVE` device state | per step | Failure halts progression past that step. | Retried per normal backoff. | `test_startup_sequence_does_not_allow_entries_before_every_step_confirms` | Workstream 6 |
| E3. Handoff: old device's gateway immediately rejects mutations once it stops holding the lease; new device only takes over after `STANDBY_READY` + final reconciliation. | `execution_command_gateway.py` + `runtime_worker.py` | lease state, device state | none from losing device | Rejected by the gateway, not a cooperative check. | N/A | `test_losing_device_cannot_submit_after_lease_loss` | Workstream 6 |
| E4. Shutdown: block new commands → flush journal → final reconciliation → unsubscribe/close WS → release lease, gated by the corrected rule above. | `runtime_worker.py` | journal flush, shutdown-gate state | unsubscribe | An interrupted shutdown is recovered by next startup's reconciliation. | Startup reconciliation is the real safety net. | `test_shutdown_with_open_positions_and_no_successor_is_refused_in_unattended_mode`, `test_shutdown_proceeds_once_a_successor_is_standby_ready` | Workstream 6 |

---

## F. Complete test program (Workstream 7)

Unchanged scenario lists from revision 2 (F1 crash boundaries, F2 WS
protocol, F3 multi-device, F4 model-based). Distribution across the PR
structure: each PR (1-7) implements and passes the subset of F1-F4 that
exercises its own new code; PR8 is the full end-to-end Gate 1 run plus
anything that only makes sense once every PR is integrated (e.g. F3's
handoff-during-open-position scenario, which spans Workstreams 2, 3, 6, and
13).

Required property after every F1/F3 injected failure, unchanged: **no
duplicate order, no unowned cancellation, no position quantity below broker
holdings, no open broker order silently forgotten, no new entry while data
is stale, no destructive action after lease loss** — every restart converges
to broker truth.

CI gates for the final integration PR (PR8), all required and visible:
`compileall`, `pytest` on 3.11 and 3.12, architecture-boundary tests,
state-machine tests (both transition tables in full), WebSocket protocol
tests, fault-injection tests, Kanban/legacy parity tests (Workstream 13).
Each earlier PR (1-7) requires its own subset of these gates for that PR's
own code before it can land on the integration branch — none of them skip
CI, only the *scope* of what's required narrows to that PR's surface area.

---

## G. Migration and cutover (Workstream 8)

Unchanged except G3 (rollback), corrected for safety after live broker
activity:

### G3 corrected: rollback is unsafe once broker mutations have occurred post-migration

```
persisted marker: post_migration_broker_mutation_occurred: bool

BEFORE any post-migration broker mutation:
    full backup restoration is allowed -- nothing broker-side has diverged
    from what the backup represents yet.

AFTER any post-migration broker mutation (the marker is set the moment the
gateway makes its first live call under the new schema):
    NEVER restore the stale backup directly -- it would discard broker
    activity the backup knows nothing about.
    a downgrade instead requires: a fresh broker snapshot, full
    reconciliation against it, an explicit compatibility transformation
    back to the old schema shape, and explicit handling of any order that
    has no old-schema equivalent (e.g. a DiscoveredExternalOrder).
```

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| G1. Schema version tracked; migration converts existing order-ledger, trade-card, and capital-reservation records. | `src/services/schema_migration.py` (new) | schema version marker | none | Migration failure aborts startup. | Idempotent re-run on next startup. | `test_migration_is_idempotent` | Workstream 8 |
| G2. First launch after migration: back up → migrate → mark unresolved legacy orders `BROKER_IDENTITY_UNCERTAIN` → full reconciliation → block entries until complete. | same | backup + migrated records | discovery only | n/a | n/a | `test_first_launch_after_migration_blocks_entries_until_reconciliation_completes` | Workstream 8 |
| G3. Rollback safety gated by the exact live device/token/epoch and `post_migration_broker_mutation_occurred`, per the correction above. | same | backup artifact, the marker | none pre-marker; full downgrade procedure post-marker | A stale lease cannot cut over or restore; direct restore attempted post-marker is refused. | Fresh broker snapshot → full reconciliation → explicit compatibility transform. | `test_cutover_rechecks_live_lease_before_migration_mutation`, `test_stale_lease_cannot_directly_restore_cutover_backup`, `test_post_mutation_recovery_reconciles_fresh_snapshot_then_transforms` | Workstream 8 |
| G4. Mixed-version prevention: two devices never run different schema versions simultaneously against the same account. | `runtime_worker.py` startup check | schema version check | none | Startup refuses on a version mismatch with an active lease holder on the other version. | Resolved by upgrading/downgrading the mismatched device. | `test_startup_refuses_to_proceed_on_schema_version_mismatch_with_an_active_lease` | Workstream 8 |

## H. Legacy/Kanban ownership isolation (Workstream 9)

Unchanged from revision 2, now explicitly consumed by B2 (gateway) and E1
(legacy fail-open scoping) rather than existing in isolation:

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| H1. Every account+symbol has exactly one `execution_owner` (`LEGACY`/`KANBAN`/`MANUAL`) and, for `KANBAN`, a `strategy_instance_id`. Enforced at the gateway (B2), not by convention. | `src/core/execution_ownership.py` (new) | `execution_owner`, `strategy_instance_id` per account+symbol | none | An action from the non-owning engine is rejected at the gateway. | N/A | `test_legacy_monitor_cannot_act_on_a_kanban_owned_symbol`, `test_kanban_cannot_act_on_a_legacy_owned_symbol` | Workstream 9 |
| H2. During controlled rollout, Kanban owns only explicitly assigned symbols; everything else defaults `LEGACY`. | same | ownership table | none | Unassigned defaults closed to Kanban, not open. | N/A | `test_unassigned_symbol_defaults_to_legacy_ownership` | Workstream 9 |

## I. Rate-limit and command-priority scheduling (Workstream 10)

Priority ordering unchanged from revision 2. I1 corrected for mutation
safety (INV-23):

### I1 corrected: reads and mutations are not retried the same way

```
GET/read request:
    safe to retry per the configured backoff policy.

submit/cancel/replace (any gateway mutation):
    retry automatically ONLY when KIS explicitly confirms the request was
        rejected before acceptance (a clean, unambiguous pre-acceptance
        rejection response).
    a timeout, connection reset, or ambiguous 5xx is NEVER retried by the
        scheduler -- it is routed to UNKNOWN_SUBMISSION_STATE and
        reconciliation (A4a/B4b), exactly as if no scheduler existed.

The scheduler's own retry/backoff logic never overrides B4b's ambiguity
rule -- it can only apply to the read side and the narrow "confirmed
pre-acceptance rejection" mutation case.
```

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| I1. Separate read/write budgets, per-account/endpoint throttling, backoff on rate-limit responses, with the mutation-safety correction above. Mutation budgets become KNOWN only from explicitly WS0-verified configuration. | `src/services/kis_request_scheduler.py`, `runtime_worker.py`, `broker.py` | in-memory budget state | n/a (wraps calls) | Reads retry per policy; only typed KIS pre-acceptance rate-limit refusal can retry; ambiguous mutations never auto-retry. | Deterministic restart reinitializes from the same verified policy; unverified deployments remain closed. | `test_verified_configuration_initializes_once_without_heartbeat_refill`, `test_worker_activates_only_explicitly_verified_account_endpoint_budgets`, `test_real_kis_classifier_only_retries_typed_rate_limit_rejections` | Workstream 10 + WS0 evidence |
| I2. Priority order: (1) emergency Sell All/stop-loss exit, (2) exit reconciliation/cancellation, (3) lease/handoff validation, (4) position/order reconciliation, (5) entry cancellation, (6) new entry, (7) display/non-critical refresh. | same | n/a | n/a | Lower priority never starves higher. | N/A | `test_exit_requests_are_never_starved_by_display_refresh_backlog` | Workstream 10 |
| I3. New entries fail closed when the request budget is uncertain. | same | n/a | n/a | Uncertain budget blocks new entries only. | N/A | `test_new_entries_fail_closed_when_budget_state_is_uncertain` | Workstream 10 |
| I4. Request metrics visible in Health. | Health tab | n/a | n/a | n/a | n/a | `test_request_scheduler_metrics_are_exposed` | Workstream 10 |

## J. Database-outage behavior (Workstream 11)

Corrected in full — revision 2's J2 contradicted B4a (mandatory DB-backed
command journal before every broker call) and never resolved how a lease is
proven valid while the database is down (INV-24). The fix is an explicit
persistence-mode switch inside the *same* gateway, not a bypass of it:

```python
class ExecutionPersistenceMode(str, Enum):
    CANONICAL_DATABASE = "CANONICAL_DATABASE"
    LOCAL_EMERGENCY_JOURNAL = "LOCAL_EMERGENCY_JOURNAL"
```

When the canonical database is unreachable, the gateway's mandatory
pre-call journal (B4a) writes to the local emergency journal *instead of*
refusing to write at all — B4a's actual rule ("no durable command record,
no broker call") is preserved exactly; only *where* that durable record
lives changes. The local journal must provide: atomic append, `fsync`
before the write is considered durable, a unique idempotency key, a local
monotonic sequence number, full account/environment/order identity, the
cached lease token and epoch, replay protection, a reconciliation marker
(so the canonical DB knows this entry hasn't been folded in yet), file
locking, and an integrity checksum per entry.

### The lease problem (INV-24)

Lease state normally lives in the database — while it's down, the gateway
cannot re-check the lease the normal way. It cannot simply assume the lease
is still valid indefinitely, and it cannot refuse every emergency action
either (that would defeat the point of an emergency path). Bounded design:

```
A device may continue taking EMERGENCY actions only (never ordinary
commands -- those stay blocked per J1) through LOCAL_EMERGENCY_JOURNAL
persistence, ONLY WHILE ALL of:
  - it held a verified-current lease at the moment the database became
    unreachable (not merely "at some point in the past"),
  - the locally cached lease has not passed its own monotonic expiry
    (a short, bounded allowance -- independent of the DB, computed from a
    local clock and the last-known lease epoch/expiry),
  - no handoff request was already pending when the database went down,
  - the outage has not exceeded MARKET_DATA_OUTAGE_MAX_HOLD_SECONDS-scale
    bound for emergency lease extension (a distinct, explicitly configured
    ceiling -- not reused from the market-data constant by coincidence).

Once the locally-provable lease allowance expires with the database still
unreachable:
    no further destructive broker call of any kind, emergency or not.
    trigger the highest-severity external alert.
```

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| J1. On database unavailability: block all *ordinary* commands (they require `CANONICAL_DATABASE` persistence); market data keeps flowing. | `execution_command_gateway.py` | n/a | blocked | Ordinary commands don't reach the broker while the DB is down. | Resumes automatically once writable again. | `test_ordinary_commands_are_blocked_while_the_database_is_unwritable` | Workstream 11 |
| J2. Emergency actions switch the gateway's own mandatory journal (B4a) to `LOCAL_EMERGENCY_JOURNAL` mode, gated by the bounded last-verified lease and cached versioned KANBAN ownership/strategy proof — never a bypass of B4a, H1, or INV-3. | `src/services/emergency_journal.py`, `execution_command_gateway.py` | append-only local file (atomic, fsynced, checksummed), including ownership proof and card correlation | exact protective entry-completion BUY cancel, protective SELL cancel, Sell All | DB loss never extends the prior lease expiry; missing ownership proof or a failed local write prevents the broker call. | Fold command/order/card correlation, then require full fresh broker reconciliation before reopening execution. | `test_outage_detection_never_extends_last_verified_lease_allowance`, `test_emergency_mutation_without_cached_ownership_proof_fails_closed`, `test_runtime_outage_cancels_completion_buy_then_submits_one_sell`, `test_recovery_folds_emergency_sell_correlation_into_sticky_card`, `test_database_recovery_forces_full_projection_before_reopening_commands` | Workstream 11 |

## K. External-alert delivery (Workstream 12)

K1/K2 unchanged from revision 2. Added K3 — the review correctly points out
that a crashed process cannot alert about its own crash:

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| K1. Alerts delivered through a production-configurable HTTPS channel external to the local machine, with delivery-attempt tracking, acknowledgement, retry, deduplication key, escalation, and a DB-independent fsynced local spool. | `src/services/external_alerting.py`, `main_window.py` | canonical delivery/ack log plus local outage spool | n/a | Delivery failure retries per policy and escalates; a canonical DB outage still spools and attempts direct delivery. | Spool entries fold into the canonical incident stream after recovery. | `test_alert_delivery_failure_retries_and_escalates`, `test_database_outage_spools_and_directly_delivers_critical_alert` | Workstream 12 |
| K2. Critical alert types enumerated: market-data outage, stale critical symbol, execution lease lost, account reconciliation failed, unknown submission state, `DiscoveredExternalOrder` found, cancel-confirmation timeout, emergency liquidation attempted, database unavailable, application heartbeat missing. | same | n/a | n/a | Each type independently testable. | N/A | one test per alert type | Workstream 12 |
| K3. "Application heartbeat missing" requires a component *external to this process* — the app publishes a heartbeat; a separate watchdog (not this process, since a crashed process can't alert about itself) checks for expiry and raises the alert. The specific provider (small hosted endpoint, existing monitoring service, second always-on observer) is not chosen by this document, but the watchdog is identified as a distinct runtime dependency this program depends on, not something Workstream 12 can satisfy alone. | external watchdog (separate from this codebase) + `src/services/external_alerting.py` (heartbeat publisher only) | last-published-heartbeat timestamp, externally | n/a | A missed heartbeat is detected by the *external* watchdog, not by this process. | N/A | `test_heartbeat_is_published_on_the_expected_cadence` (this codebase can only test that it publishes correctly, not that the external watchdog reacts) | Workstream 12, external dependency required before Gate 5 |

---

## L. Kanban feature parity and UI projection (Workstream 13)

Missing entirely from revisions 1 and 2, and the review is right that this
matters: the document could otherwise certify a hardened execution engine
underneath a Kanban board that still doesn't actually do everything the
legacy Buy Dashboard did. This workstream is what actually finishes the
original task the "kanban fix 1".."kanban fix 8" cycle was for.

Governed by INV-21 (Kanban is a projection, not a second source of truth)
and INV-22 (discovered-external-order handling, from A4b, applies here too
— the UI must render `DiscoveredExternalOrder`s distinctly from application-
owned cards).

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| L1. No Kanban UI module calls the broker, the execution gateway, the reconciliation engine, or the command repository directly — every action goes through the same workflow service the legacy Buy Dashboard uses. | `src/ui/buyboard/` (audit + enforce) | n/a | n/a | A direct call from a UI module fails an architecture test, same pattern as B1. | N/A | `test_architecture_no_kanban_ui_module_calls_broker_or_gateway_directly` | Workstream 13 |
| L2. A drag/gesture on the board issues a *command request*; the card only reflects the new state once the workflow service (and, downstream, the gateway) confirms it — a drag never itself declares success. | `src/ui/buyboard/board.py` (modify) | n/a (UI state only) | n/a (delegates) | A rejected command reverts the card's visual position/state, with the rejection reason shown. | N/A | `test_a_rejected_drag_command_reverts_the_cards_visual_state` | Workstream 13 |
| L3. Full parity matrix (below) — every legacy Buy Dashboard action has a Kanban equivalent that produces the *same underlying command* (tested by asserting on the command, not merely the resulting screen). | `src/ui/buyboard/` | varies per action | varies per action | n/a | n/a | one test per parity row below, asserting command equality, not just visual equivalence | Workstream 13 |
| L4. `DiscoveredExternalOrder`s (A4b) render as a visually distinct element, never merged into a card's own state, with an explicit "Adopt" action that is the *only* path from `DiscoveredExternalOrder` to a `USER_ADOPTED` `ExecutionOrderRecord` (INV-22; see `ExternalOrderDisposition`). | `src/ui/buyboard/board.py` | n/a | n/a | n/a | n/a | `test_discovered_external_order_renders_distinctly_from_owned_cards`, `test_adopt_action_is_the_only_path_to_a_user_adopted_execution_order_record` | Workstream 13 |

### L3 parity matrix

| Legacy Buy Dashboard action | Kanban equivalent |
|---|---|
| Add to Buy Today | Drag `Buylist` → `Buy Today` |
| Cancel a pending entry | Drag `Entry Pending` → `Buylist`, or an explicit Cancel control |
| Partial sell | Drag `Open Position` → `Partial Sell` + quantity dialog |
| Sell All | Drag `Open Position` → `Sell All` |
| Change stop type | ORB-low / breakeven / manual-price control on the card |
| Pre-market Sell All | Durable next-session exit instruction (ties into D9's outside-regular-session pricing) |
| EOD unfilled entry | Automatic return to `Buylist` |
| Partial fill | Card remains tracked as a position plus the remaining working-order state (not silently treated as fully complete) |

PR7 traceability is intentionally split between the new frontend-boundary
suite and the already-merged execution/reconciliation suites. The board
tests assert the command/intention boundary; the downstream tests assert
that broker truth, not the gesture, completes the lifecycle:

| WS13 scenario | Regression coverage |
|---|---|
| Buylist â†’ Buy Today, revision-aware activation | `test_buylist_to_buy_today_is_a_revision_aware_workflow_request` |
| Entry pending/fill/cancel/EOD return | `test_entry_pending_card_moves_to_open_position_on_full_fill_at_deadline`, `test_entry_pending_zero_fill_cancels_releases_capital_and_returns_to_buylist` |
| Ambiguous entry and duplicate UI actions | `test_ambiguous_entry_blocks_user_cancel_until_reconciliation`, `test_two_sell_all_gestures_record_one_intent_and_never_declare_flat` |
| Partial Sell request/fill/reconciliation | `test_partial_sell_uses_broker_orderable_quantity_and_stays_pending`, `test_partial_sell_fill_moves_stop_to_breakeven_and_returns_to_open_position` |
| Sell All BUY-conflict cancellation/retry/flat confirmation | `test_outage_sell_all_cancels_completion_buy_before_one_sell`, `test_sell_all_closes_once_broker_confirms_zero` |
| ORB/breakeven/manual stop projection | `test_stop_changes_use_frozen_orb_then_breakeven_then_manual` |
| Stale card/readiness/ownership protection | `test_stale_card_revision_cannot_overwrite_reconciled_truth`, `test_stale_readiness_generation_and_reconciliation_both_fail_closed`, `test_ownership_revision_change_after_render_is_rejected` |
| External order visibility and explicit adoption | `test_external_order_is_distinct_fenced_and_only_explicitly_adopted`, `test_external_order_without_a_trade_card_still_projects_and_can_be_explicitly_adopted` |
| Legacy/Kanban workflow parity | `test_legacy_and_kanban_destructive_paths_both_reference_shared_workflow`, guarded/legacy workflow integration suites |

---

## Order-type coverage checklist (cross-reference for C4)

Every row must have an explicit reducer branch and at least one fault-injection scenario from Workstream 7 (F1/F4):

- [x] Entry BUY
- [x] Entry completion BUY (remaining target after a partial fill)
- [x] Partial Sell
- [x] Sell All
- [x] Stop-loss Sell
- [x] Reserved market-on-open Sell
- [x] Unknown submission state
- [x] Rejected / cancelled / expired order
- [x] Manual broker position (no local card)
- [x] `DiscoveredExternalOrder` (no local card, A4b)
- [x] Capital reservation with no live order
- [x] Live order with no capital reservation

## Activation gates (recap, owning invariants noted)

| Gate | Proves | Invariants exercised |
|---|---|---|
| 1. Deterministic simulation | Replay, restart, fault-injection (F1/F3), protocol (F2), model-based (F4), and Kanban-parity (L3) tests all pass | All |
| 2. Live KIS WebSocket, read-only (`BUYBOARD_ENGINE_ENABLED=false`, `TRADING_ENABLED=false`, `KIS_WS_ENABLED=true`) | Real feed, against the measurable criteria below | INV-9, INV-10, INV-11, INV-17, INV-20 |
| 3. Shadow execution | Real quotes, real decisions, broker mutations replaced with `WOULD_SUBMIT`/`WOULD_CANCEL`/`WOULD_SELL` audit entries, compared against live chart/account | All decision-path invariants |
| 4. Controlled live | One account, one/two symbols, minimum size, supervised, external alerts on, legacy/Kanban ownership isolated | All |
| 5. Unattended activation | No duplicate commands; no unresolved local/broker discrepancy; no stale quantity; no command after lease loss; successful reconnect+resubscribe; every stop decision uses fresh event data; every auto-cancel has exact ownership; startup/handoff converge without manual repair; external critical alerts confirmed reaching the user outside the app; the external heartbeat watchdog (K3) is actually running | All |

### Gate 2 — measurable acceptance criteria

Unchanged from revision 2:

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
| Missed synthetic stop breaches | 0 |
| Queue/accumulator deadlocks | 0 |
| Receive-lag p95 | under 1 second |
| Receive-lag p99 | under 2 seconds |
| Secret/approval-key leakage in logs | 0 |

`BUYBOARD_ENGINE_ENABLED` stays `false` until Gate 2 has passed at minimum,
and stays `false` in unattended/automatic form until Gate 5 passes.

---

## Change log

- 2026-08-16 (PR5): Implemented Workstream 6 runtime readiness and device
  handoff from post-PR4 `master@952179e`. Main-device leases now persist a
  monotonically advancing epoch across takeover and clean-release tombstones;
  the real lease protocol verifies device, token, and epoch at the gateway.
  Runtime health is the fail-closed conjunction of lease currency, startup and
  fresh account reconciliation, WebSocket connection, both critical channel
  ACK sets, fresh critical quotes, bounded accumulator drain, database
  writability, and durable ACTIVE state. Pull-only devices run through a
  physically read-only broker facade. A readiness generation is published only
  after final reconciliation, is demoted on any dependency loss, and is
  atomically rechecked during lease acquisition. The outgoing owner's separate
  confirmation neither refreshes the successor heartbeat nor survives a new
  generation, and is tied to the outgoing lease epoch. Clean release is fenced
  by the exact device, token, and positive epoch, so a stale same-device process
  cannot tombstone a successor lease and an unknown epoch cannot authorize an
  exposed-position handoff. `ACTIVE` is durably persisted before the local
  command gate opens. Startup admits no mutations before standby prerequisites
  and activation reconciliation.
  Shutdown blocks commands, flushes the durable journal boundary, performs a
  final projection-only reconciliation, unsubscribes/closes market data, then
  permits lease release only for no exposure, a confirmed ready successor, or
  explicit supervised acceptance. Unattended shutdown with exposure and no
  successor is refused; unknown exposure and a failed final release abort close
  and restore protection unless separately accepted as a supervised emergency.
  The market-data accumulator is retained across standby promotion so an
  unacknowledged stop breach cannot disappear. KANBAN-owned and ownership-
  unknown symbols never fail open to legacy.
  `BUYBOARD_ENGINE_ENABLED=false`, `KIS_WS_ENABLED=false`, and
  `KIS_WS_PROTOCOL_VERIFIED=false` remain unchanged. Local validation:
  `python -m compileall -q src tests` and `1721 passed`.
- 2026-08-16 (revision 3.4, PR4 blocking review): Explicitly revised the
  Workstream 0 WebSocket gate. D1/D3/D11 adapters may exist provisionally
  before credentialed evidence only while disabled, labelled
  non-authoritative, zero-capacity, and fail-closed. Workstream 0 sign-off
  remains mandatory before live connection/subscription, execution-grade
  timestamp/sequence interpretation, non-zero capacity, or any production
  activation claim. A4a and capability-dependent C1/C3 remain implementation-
  gated. This records the contract change instead of silently coding around
  the signed revision 3.1 wording.
- 2026-08-16 (PR3): Implemented Workstream 4's account-level reconciliation
  engine and runtime integration. Added deterministic account snapshots and
  plans, action-specific completeness, durable absence generations, all C4
  classifications, conservative unowned-order handling, emergency Sell All
  exposure math, and removed the three order-dependent EOD reconciliation
  sweeps. Production A4a correlation and timing calibration remain gated on
  the unverified Workstream 0 capability matrix.
- 2026-08-16 (PR3 blocking-review hardening): Added a durable gateway fence
  for active `DISCOVERED_UNOWNED` orders; connected reducer cancel/emergency
  commands to the shared guarded workflow; refused emergency sizing in the
  presence of unowned broker exposure; implemented behavioral card and
  reservation projection for entry, completion, partial exit, Sell All,
  stop-loss, and reserved-MOO categories; required complete type-specific
  evidence for every absence generation using the US market-session date;
  replaced the worker's global completeness exclusion with action-specific
  readiness; escalated exact terminal contradictions; made orphan-reservation
  repair two-generation/evidence-based; committed each account plan in one
  database transaction; and made critical alert incidents account-scoped and
  re-armable after resolution. Runtime/fake-broker tests now assert real submit
  and cancel calls, plus ambiguity suppressing all subsequent broker calls.
- 2026-08-16 (PR3 residual hardening): First-sighting terminal external
  history is persisted directly as audit-only instead of becoming a transient
  execution fence; exact order history projects onto a card only through its
  durable client-ID or current attempt-group link; a failed due reconciliation
  invalidates cached action readiness; active external fences are re-read at
  the final submit/cancel boundary; and capital reservations now carry a
  migrated optimistic version used by gateway and account-plan CAS writes.
- 2026-08-16 (PR3 final residual hardening): A final mutable-gate failure
  now performs an explicit pre-broker abort: submit retires the command and
  order locally while releasing capital, and cancel restores its exact
  pre-cancel state and retires the failed caller-owned cancel ID. Entry and
  exit attempt groups are retired when their objective/trade cycle ends, so
  historical exact orders cannot correlate to a later cycle. The allocator
  now uses a strict database CAS before updating JSON; on conflict it reloads
  and mirrors the winning authoritative reservation before failing closed.
  Local validation: `python -m compileall -q src tests` and `1608 passed`.
- 2026-08-16 (PR3 final runtime follow-up): A pre-broker-aborted entry now
  advances the consumed logical attempt number before entering cooldown, so
  deterministic retry identity moves from attempt 1 to attempt 2 instead of
  replaying the retired command ID. EOD now retires an entry-completion group
  when no live completion order remains, while preserving that correlation
  when a real order still needs cancellation and terminal reconciliation.
  Local validation: `python -m compileall -q src tests` and `1610 passed`.
- 2026-08-15: Initial draft, branch created from `109c2c4` ("kanban fix 8").
- 2026-08-15 (revision 2): Incorporated first architecture review. Added
  Workstream 0, INV-20, rewrote A4 (single version), atomic pre-submission
  transaction, split B4, `SnapshotCompleteness`, absence-generation rule,
  triple market-data staleness, `PendingMarketState` accumulator (INV-17),
  D9/D10/D11, standby-readiness handoff, full Workstream 7 matrix,
  Workstreams 8-12, measurable Gate 2 criteria.
- 2026-08-15 (revision 3): Incorporated second architecture review. Split
  A4 into A4a (status ambiguity for our own order) and A4b (ownership
  ambiguity for a discovered order with no local record), added
  `OrderOwnership`/`DiscoveredExternalOrder` and INV-22. Added full
  `ExecutionOrderStatus`/`OrderRecoveryState` transition tables and the
  SUBMITTING-must-be-committed-before-the-broker-call rule. Corrected
  emergency Sell All quantity math (subtract known-owned outstanding sell
  exposure, never raw holdings alone). Rewrote Workstream 11 (J) around an
  `ExecutionPersistenceMode` switch inside the gateway instead of a bypass
  of it, and resolved the emergency-lease-validity problem (INV-24).
  Rescoped E1's legacy fail-open behavior to legacy-owned symbols only,
  resolving the contradiction with Workstream 9 (INV-18). Corrected D3's
  duplicate-detection to exact event identity rather than repeated price,
  and made sequence-regression checks conditional on Workstream 0 evidence.
  Added D5's stop-version concurrency resolution (forced drain on stop
  change). Corrected D10's reclassification rule (frozen at last-trusted
  during a total outage; escalation only from duration or an independently
  healthy signal) and added account-level risk factors. Added D9's
  explicit emergency-pricing branches. Made D11's subscription priority
  channel-specific (trade vs. quote). Added E4's lease-release gating
  (INV-25). Corrected G3's rollback safety for post-migration broker
  activity. Corrected I1's mutation-retry rule (INV-23) so the rate-limit
  scheduler can never override B4b's ambiguity handling. Added K3 (external
  heartbeat watchdog, since a crashed process can't alert about itself).
  Added Workstream 13 (Kanban feature parity and UI projection) and
  INV-21, closing the gap where the document could certify the execution
  engine without ever proving Kanban replaced the legacy dashboard's own
  actions. Replaced the single-PR rule with an 8-PR staged release train,
  each carrying its own required CI/test scope.
- 2026-08-15 (revision 3.1): Narrow errata pass responding to a third
  review. Replaced `OrderOwnership` with separate `OrderOrigin` (did the
  application create this record) and `BrokerIdentityStatus` (is the exact
  broker order known) fields — a `PREPARED` record is `origin=APPLICATION`
  but `broker_identity_status=NOT_ASSIGNED`, not "verified," since no
  broker call has happened yet; the cancel gate now requires exact identity,
  not origin alone. Gave `DiscoveredExternalOrder` its own
  `ExternalOrderDisposition` lifecycle instead of routing it through
  `OrderRecoveryState` (which belongs to `ExecutionOrderRecord` only), and
  specified that adoption creates a new, separate `ExecutionOrderRecord`
  rather than mutating the original discovered record. Added the
  reconciliation classification precedence (exact match → verified
  correlation key → heuristic A4a candidate → only then
  `DiscoveredExternalOrder`) so one broker order can never become both an
  A4a candidate and a duplicate external-order alert. Split `REJECTED`
  (explicit broker rejection) from the new `NOT_ACCEPTED_CONFIRMED`
  (inferred non-acceptance via exact correlation + complete history), since
  they need different evidentiary bars. Added the per-symbol lock/atomic-
  swap primitive D5's stop-version synchronization boundary needs to be a
  real guarantee rather than a hand-wave. Narrowed the Workstream 0 gate to
  the specific KIS-capability-dependent code paths (A4a's adapter, D1/D3/D11)
  rather than blocking all of Workstreams 2 and 5, unblocking PR1's
  capability-independent schemas and state machines to start immediately
  alongside Workstream 0 rather than after it.
- 2026-08-15: Revision 3.1 approved by the project owner. Workstream 1
  signed off; subsequent changes require an explicit logged contract
  revision.
- 2026-08-15 (revision 3.2): Narrow errata discovered during PR1
  implementation, per rule 1 ("a reason to stop and revise this document
  explicitly, not to quietly code around it") -- code was not written
  around these gaps; the contract was corrected first. Added
  `ACKNOWLEDGED -> CANCEL_PENDING` (an urgent cancel must not be forced to
  wait for a `WORKING` observation that may not have arrived yet) and
  `CANCEL_PENDING -> WORKING`/`CANCEL_PENDING -> EXPIRED` (an explicitly
  rejected cancel, or a time-in-force expiry racing the cancel, both
  previously had no valid outcome in the table) to the
  `ExecutionOrderStatus` transition table. Added
  `BrokerIdentityStatus.NO_BROKER_ORDER_CONFIRMED` -- `AMBIGUOUS` was
  staying stale once non-acceptance was actually confirmed. Renamed
  `OrderRecoveryState.OWNERSHIP_UNCERTAIN` to `BROKER_IDENTITY_UNCERTAIN`
  throughout -- for A4a specifically, the application's own origin/
  ownership was never in question, only the broker identity resolution;
  the old name no longer matched revision 3.1's origin/identity split.
  Made the cancellation gate's `recovery_state` check an explicit
  allow-list (`NONE`/`CANCEL_REQUIRED`) instead of a `!=
  BROKER_IDENTITY_UNCERTAIN` deny-list, which had wrongly permitted
  `DISCOVERING`, `MANUAL_INTERVENTION_REQUIRED`, and an already-in-flight
  cancel (`CANCEL_REQUESTED`/`AWAITING_CANCEL_CONFIRMATION`) to accept a
  second, duplicate cancel command. Made `broker_identity_status == EXACT`
  immutable except for idempotent same-ID reconfirmation -- reassigning a
  different `broker_order_id` is now an explicit contradiction, not a
  silent overwrite. Added `AdoptedOrderPermission` so a `USER_ADOPTED`
  record's authority is exactly what the adoption UI granted, never
  blanket authority implied by origin alone. Clarified (not expanded
  scope) that `ExecutionOrderRecord`/`DiscoveredExternalOrder` need real
  durable tables, not in-memory dataclasses, for INV-1/INV-22 to actually
  hold across a restart, and that adoption must be one atomic transaction
  across both tables. Clarified that A1/A5's repositories must support a
  caller-owned transaction/connection, not only their own, since A1's
  atomicity requirement needs three repositories' writes composed into a
  single commit once Workstream 3 orchestrates them.
- 2026-08-15 (revision 3.2, continued): Second PR1 hardening pass. Added a
  narrow Workstream 0 capability-matrix row, "Broker-order identity
  uniqueness scope" -- PR1's `broker_identity_key` is provisionally scoped
  to `environment:account_no:broker_order_id`; whether that scope actually
  matches KIS's real ID-reuse/rollover behavior (across sessions/trading
  dates, and across exchanges for a multi-exchange account) is unverified
  without live credentials, so it is logged as a known, provisional gap
  rather than a silently-assumed-safe one. Corrected PR1's implementation
  to match: `execution_orders.broker_identity_key` and
  `discovered_external_orders.broker_identity_key` are now real database
  `UNIQUE` constraints (the prior application-level "SELECT then INSERT"
  check alone was a race between two concurrent transactions that could
  both pass the check and commit duplicate claims). Added
  `ExecutionOrderRecord.validate_consistency` -- an aggregate invariant
  check independent of how the record was built, called at construction,
  after every status transition, and before every repository write, since
  a corrupted persisted payload or a record built by direct construction
  otherwise bypassed the per-field checks entirely. Fixed both
  repositories' optimistic-concurrency updates, which were mutating the
  caller's in-memory `version` before the write was confirmed to have
  applied -- a rejected write left the caller believing a version the
  database never actually stored. Added immutable-identity-field
  protection (`environment`/`account_no`/`symbol`, plus `broker_order_id`
  for discovered orders) so an update can no longer silently change what a
  record's own indexed columns say it is. Hardened
  `execution_command_repository.update_command_response`: it now looks up
  the command's own stored `account_no` for redaction rather than trusting
  an optional caller-supplied parameter, and it is now a compare-and-set
  write (`status='REQUESTED' AND version=expected_version`) so a second
  write to an already-recorded response fails loudly instead of silently
  overwriting the real outcome. Made `_parse_dt` raise on a blank or
  malformed `requested_at` instead of silently substituting the current
  time. Finished moving broker-response/raw-payload redaction to the
  shared `src/utils/redaction.py` utility across all three PR1 modules.
- 2026-08-15 (revision 3.3): Process correction, not an architecture
  change. PR1 (`feature/kanban-pr1-order-schemas`, commit `0864b45`) was
  opened as PR #4 and merged directly to `master` (merge commit `5b50e1d`)
  rather than against the `feature/kanban-production-readiness` integration
  branch rule 3 and the original PR-structure section specified. The
  content merged is verified identical to what was reviewed -- same
  commit, same 1474-passed/compileall-clean result, no divergence -- only
  the target branch differed from the plan. Investigating why surfaced a
  real gap in the plan itself: the repository's actual
  `.github/workflows/ci.yml` triggers only on `branches: [master]`, for
  both `push` and `pull_request`. A PR opened against an intermediate
  long-lived integration branch, as originally specified, would therefore
  carry **zero** CI checks -- silently defeating rule 2's "needs a passing
  test" requirement and this document's own repeated expectation of
  visible CI per PR. Rather than reverting PR1 and maintaining a branch
  whose only purpose (deferring one eventual merge to `master`) CI never
  actually validated, this revision formally replaces the integration-
  branch requirement: rule 3 and the PR-structure section now specify that
  every PR (1-8) targets `master` directly, each reviewed and CI-gated on
  its own, same as PR1 turned out to actually be.
  `BUYBOARD_ENGINE_ENABLED=false` and each workstream's own feature flags
  remain the sole activation gate -- landing a PR's code on `master` is
  explicitly not
  equivalent to activating it, and nothing merged in PR1 is wired into any
  existing entry point yet (the execution gateway that would do that
  wiring is Workstream 3 / PR2, not yet started). The
  `feature/kanban-production-readiness` branch is deprecated: it never
  received PR1 and is not used for PR2 onward. Workstream 1's sign-off
  (revision 3.1) is unaffected -- this is a release-process correction,
  not a reopening of the requirements content itself.
- 2026-08-15: PR2 (Workstreams 3 + 9) implemented on
  `feature/kanban-pr2-execution-gateway`, targeting `master` per revision
  3.3. Not a contract revision -- an implementation-status note, same as
  PR1's landing wasn't. `ExecutionCommandGateway`
  (`src/services/execution_command_gateway.py`) implements the full A1-A11
  submit sequence and the B1-B4 cancel/replace sequence described above,
  dual-mode per `src.core.execution_mode.ExecutionMode`:
  `LEGACY_COMPATIBILITY` (a transparent pass-through to the real broker --
  today's mode, always, since `BUYBOARD_ENGINE_ENABLED` stays `false`) and
  `GUARDED_ENGINE` (the new sequence, implemented and tested, never
  selected in production by this PR). `ExecutionWorkflowService`
  (`src/services/execution_workflow_service.py`) is the one workflow
  service both the legacy Buy Dashboard and Kanban now default to;
  `order_execution_service.submit_guarded_overseas_order` and
  `order_reconciliation.cancel_and_reconcile_order` -- the two real,
  already-shared choke points both surfaces' submission and cancellation
  ultimately went through even before this PR -- now default their
  `broker=` parameter to the gateway instead of a raw `KisBroker`, with no
  other change to either function's own gate sequence, call signature, or
  behavior (proven by dedicated characterization tests, not merely
  asserted). An architecture test statically scans the codebase and fails
  if any module outside an explicit, narrow allowlist constructs a
  `KisBroker` or calls a KIS order-mutation function directly, plus a
  runtime test proving the sanctioned path actually reaches the real
  broker adapter through the gateway.

  Two scope boundaries logged explicitly rather than silently assumed
  complete: (1) `_cancel_discovered_order`'s auto-cancel of an unowned,
  no-local-record broker order (`buyboard_runtime.py`) was left untouched
  -- it is a Workstream 4 (account reconciliation / A4b) concern, and
  fixing its underlying auto-cancel policy (not merely rerouting the same
  call through a new pipe) is that workstream's job, not PR2's; it still
  only runs when `BUYBOARD_ENGINE_ENABLED=true`, which stays false. (2) H1's
  full requirement -- a *persisted*, multi-strategy `execution_owner` table
  per account+symbol (`src/core/execution_ownership.py`) -- is not built;
  PR2 instead enforces a lighter, in-process mutual-exclusion claim per
  `(environment, account_no, symbol)` inside the gateway itself, which
  satisfies "legacy background workers cannot continue issuing orders in
  parallel with the gateway" but not H1's full durable-ownership-table
  scope. Full test suite: 1516 passed (was 1474 after PR1's second
  hardening pass). `python -m compileall`: clean.
- 2026-08-15 (PR2, second pass): A second code-review round on PR2's
  implementation found the guarded path was not yet safe or usable
  through the real production workflow, despite passing tests -- several
  tests proved repository primitives or direct gateway calls rather than
  the actual restart and workflow scenarios their names implied. Not a
  contract revision (per rule 1: corrected in code and tests, not by
  narrowing the requirement) except where explicitly noted below.

  **Caller-stable command identity (was: a fresh UUID+timestamp minted
  inside the gateway on every call, making restart-safe idempotency
  impossible in principle).** `src/core/execution_request.py` adds
  `SubmitExecutionRequest`/`CancelExecutionRequest`/`ReplaceExecutionRequest`,
  each carrying an identity the *caller* (`ExecutionWorkflowService`)
  generates once, before the first gateway call, and must reuse to replay
  the *same* logical decision. `ExecutionCommandGateway.submit_order`/
  `cancel_order` (the `Broker`-protocol methods) are now
  `LEGACY_COMPATIBILITY`-only and raise `WrongGatewayModeError` if reached
  in `GUARDED_ENGINE` mode; `submit_guarded`/`cancel_guarded`/
  `replace_guarded` (taking the new request models) are `GUARDED_ENGINE`-
  only. This is the "do not force the execution gateway to masquerade as
  the old minimal Broker protocol" correction -- the two modes now have
  genuinely different call shapes rather than one shape straining to
  serve both. `ExecutionWorkflowService.request_submit`/`request_cancel`/
  (new) `request_replace` are the real, mode-aware entry points a caller
  actually uses; both are exercised end-to-end in
  `tests/test_execution_workflow_service_guarded_integration.py`,
  including a restart-idempotency test that constructs a *second* gateway
  instance and replays the same stable identity.

  **Post-broker persistence failure (was: a raw database exception could
  escape after the broker had already accepted a submission or answered a
  cancel, with nothing telling the caller not to treat it as a rejection
  or safe to retry).** New `AmbiguousPostBrokerPersistenceError`, raised
  whenever the write that records a broker outcome fails after the broker
  call itself already completed -- the durable record is left at whatever
  status it held before that write (never silently advanced), and the
  exception is explicit that a caller must never resubmit/re-cancel and
  must never reclassify it as `REJECTED`/`FAILED`.

  **Guarded lease gate (was: `lease=None` silently meant "unfenced," and
  `lease_epoch` was accepted without being verified against anything).**
  `GUARDED_ENGINE` mode now requires an explicit lease and a lease
  protocol that reports `epoch_verified=True`. At PR2 time,
  `DefaultExecutionLeaseProtocol` reported `epoch_verified=False` because no
  durable epoch authority existed; PR5 supersedes that limitation with the
  epoch persisted in main-device ownership state. `LeaseNotVerifiedError` covers a
  missing lease, an unverifiable epoch, and a stale epoch uniformly.

  **H1 implemented, not deferred.** `src/core/execution_ownership.py` +
  `src/services/execution_ownership_repository.py`: a real, persisted
  `execution_owner` (`LEGACY`/`KANBAN`/`MANUAL`) per
  `(environment, account_no, symbol)`, with `strategy_instance_id` required
  for `KANBAN`, defaulting to `LEGACY` when unassigned (H2) --
  `ExecutionCommandGateway._require_ownership` enforces it as part of the
  B2 sequence in `GUARDED_ENGINE` mode. The lighter in-process mutual-
  exclusion registry from the first PR2 pass is kept, but now explicitly
  described as a same-process race guard distinct from H1's own durable
  assignment, not a substitute for it.

  **Ownership-registry concurrency bug fixed.** The registry's earlier
  same-source reentrancy allowance tracked only the source *value*, not
  which caller/thread held it -- two different threads sharing a source
  could each believe they held the claim, and one finishing would drop
  the other's protection too. Fixed by removing the reentrancy allowance
  entirely (nothing in the gateway actually needed it -- `replace_guarded`
  already called its internal methods directly, never through the public
  claiming methods) in favor of strict, unconditional per-key exclusion.
  A real multithreaded contention test now exercises this directly.

  **`REPLACE` permission enforced independently of `CANCEL`.**
  `replace_guarded`'s internal cancel step is now authorized by
  `AdoptedOrderPermission.REPLACE` (via a new `_cancellable_for_replace`
  predicate, parallel to but distinct from `is_cancellable`, which stays
  untouched as part of PR1's frozen contract), not by `CANCEL` -- a
  `USER_ADOPTED` record with `CANCEL` but not `REPLACE` can no longer have
  a replacement submitted on its behalf.

  **Cancel idempotency key corrected.** Was `f"{client_order_id}:CANCEL:{attempt_number}"`
  -- `attempt_number` belongs to the *submission* attempt, so an explicitly
  rejected cancel (order returns to `WORKING`) permanently blocked every
  later, genuinely new cancel decision for that order, since the key never
  changed. Now `f"CANCEL:{cancel_command_id}"`, where `cancel_command_id`
  is the caller's own stable identity for *that specific cancel decision*
  -- a replay of an unresolved cancel reuses it; a new decision mints a
  new one.

  **Cancel account/environment mismatch now checked.** `_do_cancel`
  compares the caller-supplied `environment`/`account_no` against the
  order record's own persisted values and raises `CancelNotPermittedError`
  on a mismatch, instead of silently using the record's real values and
  ignoring what the caller asked for.

  **`MutationBudgetProtocol` seam added** (`src/services/mutation_budget_protocol.py`)
  for Workstream 10's future rate-limit-aware implementation --
  `GUARDED_ENGINE` mode requires one to be explicitly injected
  (`GuardedEngineRequiresMutationBudgetError` otherwise); `AllowAllMutationBudget`
  exists for tests and for the guarded composition root to use *visibly*,
  never as a silent gateway default.

  **Guarded composition root added.** `build_guarded_execution_gateway`
  requires every `GUARDED_ENGINE` dependency (engine, lease protocol,
  mutation budget) as an explicit keyword argument, so a missing one fails
  immediately and loudly rather than only at the first real submission.
  `buyboard_runtime.build_buyboard_runtime` now selects it when
  `BUYBOARD_ENGINE_ENABLED=true` (failing fast if `capital_reservation_engine`
  wasn't supplied), and the process-wide `get_default_execution_gateway()`
  singleton stays `LEGACY_COMPATIBILITY`-only, exactly as before.

  **Exit-order capital reservation corrected.** A `SELL` submission now
  reserves zero notional (was: `quantity * limit_price`, the same as a
  `BUY` entry) -- a regular partial sell or Sell All was incorrectly
  reducing capital available for new entries until a later reconciliation
  pass released it; only a `BUY` actually needs to reserve buying power.

  Full test suite: 1541 passed (was 1516). `python -m compileall`: clean.
- 2026-08-16 (PR2, third pass): A third review found the second pass's own
  fixes had left the *real* Kanban runtime composition broken -- correct
  in isolation, not actually wired together. Seven findings, six resolved
  in that pass; the two deeper integration findings recorded below were
  subsequently closed by the fourth pass in this same PR2 branch.

  **Finding 1 (buyboard_runtime called the wrong API) -- fixed.**
  `build_buyboard_runtime`'s `submit_order`/`submit_sell_order`/
  `_cancel_order` now call `execution_workflow_service.request_submit`/
  `request_cancel` instead of `submit_guarded_overseas_order`/
  `cancel_and_reconcile_order` directly -- the latter call the gateway's
  `Broker`-protocol methods, which the gateway (correctly, per the second
  pass) now rejects outright in `GUARDED_ENGINE` mode
  (`WrongGatewayModeError`). Safe for the only reachable mode
  (`LEGACY_COMPATIBILITY`): `request_submit`/`request_cancel` forward
  every keyword argument to the exact same legacy functions unchanged,
  proven by the full existing `test_buyboard_runtime.py` suite passing
  with no behavioral difference (only the wrapped `broker` object's
  identity changes, from the raw broker to a thin source-attribution
  adapter around it). `_resolved_mode` (new,
  `execution_workflow_service.py`) treats a plain `Broker` (no `.mode`
  attribute -- e.g. a test double injected via `build_buyboard_runtime`'s
  own `broker=` override) as implicitly `LEGACY_COMPATIBILITY`, preserving
  that existing flexibility rather than requiring every caller to wrap a
  fake in a full gateway.

  Fixing this surfaced two further, smaller gaps, both closed: (a)
  `build_buyboard_runtime` had no `strategy_instance_id` concept at all,
  so it could never satisfy H1's "KANBAN plus strategy_instance_id"
  requirement (finding 3, below) even after routing was fixed -- added as
  a new `build_buyboard_runtime(..., strategy_instance_id="")` parameter,
  threaded through every submission/cancellation this runtime makes; (b)
  a second, previously-missed direct call to `submit_guarded_overseas_order`
  in `submit_sell_order` (the SELL adapter, review finding P0-3) needed
  the identical fix.

  Historical note, resolved in the fourth pass below:
  `PositionActionCallbacks.cancel_order` originally supplied only
  `client_order_id`; it now carries a durable `CancelIntent` containing
  environment, account, cancel command ID, lease, source, and strategy
  identity.

  **Finding 2 (guarded composition root uses a lease verifier that can
  never pass) -- fixed.** `build_buyboard_runtime` now refuses
  `GUARDED_ENGINE` activation outright (`RuntimeError` at composition
  time) whenever `BUYBOARD_ENGINE_ENABLED=true` and no explicit `broker=`
  override is supplied. At PR2 time the default lease protocol could not
  verify an epoch; PR5 now supplies that verification, while
  `AllowAllMutationBudget` remains a testing placeholder until Workstream 10, so the previous
  composition would have "succeeded" at startup and failed only on the
  first real command. This has no production effect (the flag stays
  `false` for the whole program) but closes a real internal
  inconsistency between the gateway's own fail-closed lease gate and its
  composition root.

  **Finding 3 (H1 stores but doesn't enforce `strategy_instance_id`) --
  fixed.** All three request models
  (`SubmitExecutionRequest`/`CancelExecutionRequest`/`ReplaceExecutionRequest`)
  now carry `strategy_instance_id`; `_require_ownership` rejects a blank
  or mismatched one for a `KANBAN`-owned symbol -- one Kanban strategy
  instance can no longer act on a symbol assigned to a different one.

  **Finding 6 (replace could cancel a valid order before validating the
  replacement) -- fixed.** `replace_guarded` now validates the *entire*
  replacement request (account/environment match, quantity, price, a
  fresh `new_client_order_id`, ownership/permission, lease, both
  mutation budgets) before ever cancelling the original, and persists a
  durable parent `replace` command row (`REPLACE:{replace_command_id}`)
  before either broker call -- restart recovery reconstructs "how far did
  this get" from the parent row plus its linked cancel/submit sub-commands'
  own already-durable statuses, rather than needing new intermediate-state
  writes (the command ledger's compare-and-set persistence is a single
  `REQUESTED -> one terminal state` transition by design, not a
  multi-step state machine, and this reuses that as-is). Tests prove an
  invalid quantity/price, a duplicate `new_client_order_id`, and an
  account/environment mismatch all make zero cancel calls.

  **Finding 7 (no re-fencing immediately before the broker call) --
  fixed, "at minimum."** `_do_submit`/`_do_cancel` now re-verify ownership
  and lease a second time, immediately before the actual broker call, not
  only once earlier before the journal/status-transition commit -- closes
  the concrete race where another device transfers ownership or advances
  the lease epoch in that window. This is a fresh re-check against current
  state, not yet a persisted fencing-token/version proof threaded through
  the command row itself (the review's "at minimum" framing already
  anticipated a lighter floor here; a full fencing-token design remains a
  further enhancement, not built in this pass).

  **Findings 4 and 5 -- not resolved in the third pass, then closed by the
  fourth pass below.** Both required modifying pre-existing,
  already-tested legacy modules (`entry_attempt_manager.py`,
  `position_manager.py`, `TradeCardState`) outside every file this PR has
  otherwise touched, with real regression risk to code that has nothing
  to do with the guarded engine:
  - Finding 4: restart-safe idempotency currently depends on whatever
    calls `request_submit` durably remembering and resupplying the same
    `client_order_id` after a real process crash. Nothing in
    `EntryAttemptManager`'s own persisted state
    (`attempt_group_id`/attempt count) currently derives or persists a
    gateway `client_order_id` before invoking submission, so there is
    nothing yet to restore. This PR's own restart test (both pass 2's
    direct-gateway version and this pass's workflow-layer version)
    proves the *gateway's* replay behavior is correct given a stable
    identity; it does not prove the application can reconstruct that
    identity after a crash, because nothing durably tracks it yet.
  - Finding 5: `EntryAttemptManager` reserves capital via the existing
    best-effort `capital_allocator`/`save_reservation` path *before*
    calling its `submit_order` callback; the gateway's own A1 transaction
    reserves *again*, independently, inside `_do_submit`. If
    `GUARDED_ENGINE` mode were ever actually reachable through
    `EntryAttemptManager` today, a successful entry would double-reserve
    buying power (a conservative-direction bug -- it would under-utilize
    capital, not risk overspending it, but it is still wrong). The
    guarded result (`ExecutionOrderRecord`) also isn't projected back
    into whatever shape `EntryAttemptManager`/`TradingEngine` expects
    from a submission (they were built against `BrokerOrder`); this
    surfaced concretely while fixing finding 1, once `submit_order`'s
    real callback could reach the gateway at all.

  That was the third-pass state only; the fourth pass below supersedes the
  temporary unconditional refusal and closes both integration gaps.

  Full test suite: 1552 passed (was 1541). `python -m compileall`: clean.
  No visible GitHub CI yet for this pass -- still local results only,
  per the review's own note; a PR/CI run remains to be opened.

- 2026-08-16 (PR2, fourth hardening pass): Closed every remaining
  third-pass integration finding through the actual Buy Board runtime
  callback graph.

  **Durable logical execution identity.** `TradeCardState` now persists
  entry/exit client-order IDs, attempt-group and pending-attempt numbers,
  stable cancel-command IDs, and unresolved-submission flags. The runtime
  derives `client_order_id` deterministically from durable attempt state,
  commits it with optimistic concurrency before the gateway call, reloads
  it after restart, and never mints another ID while that command is
  unresolved.

  **One capital owner.** `EntryAttemptManager` no longer creates a separate
  reservation in guarded mode. The gateway alone locks current active
  reservations, validates live available buying power, and inserts exactly
  one reservation beside the command and PREPARED order in the same A1
  transaction. Insufficient capital rolls the transaction back before any
  broker call.

  **Common result projection.** Both compatibility and guarded submission
  paths return `ExecutionSubmissionResult` with `UnifiedExecutionStatus`,
  broker/client identities, quantities, reservation identity, and
  ambiguity. `EntryAttemptManager` and `TradingEngine` consume this single
  contract rather than branching on `BrokerOrder` versus
  `ExecutionOrderRecord`.

  **Full cancellation context.** Tracked entry/exit cancellation now uses
  a durable `CancelIntent` containing client and cancel command IDs,
  environment, account, epoch-bearing lease, source, and strategy instance.
  The intent is persisted before `request_cancel_intent` reaches
  `cancel_guarded`.

  **No mode downgrade.** When `BUYBOARD_ENGINE_ENABLED=true`,
  `build_buyboard_runtime` accepts only a validated
  `ExecutionCommandGateway(mode=GUARDED_ENGINE)` plus an epoch-bearing
  lease, mutation budget, buying-power provider, strategy identity, and
  durable card-persistence callback. A plain broker or legacy gateway is
  rejected at composition time. With the flag false, compatibility
  composition remains available.

  **Real composition scenarios.** Tests now drive the assembled runtime and
  prove one entry creates one reservation, insufficient capital makes no
  broker call, caller identity survives a repository reload, tracked cancel
  reaches `cancel_guarded`, Partial Sell and Sell All reach
  `submit_guarded`, blank/mismatched strategy identity is rejected, a plain
  broker cannot downgrade enabled mode, and a post-broker persistence
  ambiguity remains unresolved without automatic retry.
