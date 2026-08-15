# Kanban Production Readiness — Requirements & Invariants

Status: **DRAFT — Workstream 1, pending sign-off**
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

## Workstream status ledger

| # | Workstream | Status |
|---|---|---|
| 1 | Freeze requirements and invariants (this document) | DRAFT — pending sign-off |
| 2 | Durable order ownership and command ledger | NOT STARTED |
| 3 | One guarded execution gateway | NOT STARTED |
| 4 | Account-level reconciliation engine | NOT STARTED |
| 5 | Production KIS real-time market data | NOT STARTED |
| 6 | Runtime readiness and device handoff | NOT STARTED |
| 7 | Complete test program | NOT STARTED (partially exists piecemeal; see below) |

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
| INV-10 | Existing positions require fresh trade data for stop evaluation. | The 1-minute-bar-close fallback can miss an intraminute breach entirely — a stock can trade through the stop and recover before the bar closes. |
| INV-11 | Feed disconnection immediately blocks new entries. | Today one successful symbol out of many marks the whole service "connected," so a mostly-broken feed still permits automatic entries on the few symbols still updating. |
| INV-12 | Startup cannot report healthy until reconciliation and market-data readiness both pass. | Startup health today only checks reconciliation; market-data readiness isn't part of `_buyboard_engine_healthy` at all. |
| INV-13 | Laptop/PC handoff cannot permit simultaneous destructive execution. | Partially enforced today via `ExecutionAuthority`/lease checks at scattered call sites (INV-3's problem in miniature); must be centralized. |
| INV-14 | Every workflow transition is restart-safe. | Directly falsified by the fix-8 "stale BUY_TODAY with existing local order" finding: a crash mid-transition left a card permanently outside tracking until this document's authors noticed by inspection. |
| INV-15 | Every automatic action is auditable and idempotent. | No command ledger exists today; a resubmitted trigger after a crash is only stopped by the accident of `DuplicateOpenOrderError`'s local-ledger check, not by a designed idempotency key. |

---

## A. Durable order ownership and command ledger (Workstream 2)

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| A1. Every submitted order gets an `ExecutionOrderRecord` written *before* the broker call, keyed by `client_order_id`. | `src/core/execution_order_record.py` (new) | `ExecutionOrderRecord` row, `status=PENDING_SUBMIT` | none yet (pre-call write) | Process dies before the broker call: record exists at `PENDING_SUBMIT` with no `broker_order_id` — startup must resolve this (see A4). | Startup discovery reconciles `PENDING_SUBMIT` records with no broker response by checking whether a matching broker order actually exists (ambiguous-submission recovery). | `test_execution_order_record_written_before_submission` | Workstream 2 |
| A2. `submitted_quantity`/`submitted_limit_price` persisted are the *actual* values sent to KIS after risk revalidation, never the card's original plan. | `execution_command_gateway.py` (Workstream 3) writes via this module | same record, `status=SUBMITTED`, `broker_order_id` set | `broker.submit_order(...)` | Broker accepts but the accept response is lost before being read: record stays `PENDING_SUBMIT` — see A4. | Same as A4. | `test_submitted_quantity_reflects_post_revalidation_size_not_the_original_plan` | Workstream 2 |
| A3. `OrderRecoveryState` (`NONE`, `DISCOVERING`, `OWNERSHIP_UNCERTAIN`, `CANCEL_REQUIRED`, `CANCEL_REQUESTED`, `AWAITING_CANCEL_CONFIRMATION`, `TERMINAL_RECONCILED`, `MANUAL_INTERVENTION_REQUIRED`) replaces `UNRECONCILED_BROKER_ORDER` as the sweep selector (INV-7). | `src/core/order_recovery_state.py` (new) | `ExecutionOrderRecord.recovery_state` | none | An invalid transition (e.g. `TERMINAL_RECONCILED` → `CANCEL_REQUIRED`) raises, it does not silently overwrite. | N/A — this is the state machine itself. | `test_order_recovery_state_transitions_are_validated`, `test_unreconciled_broker_order_warning_is_derived_not_authoritative` | Workstream 2 |
| A4. Ambiguous-submission recovery: a `PENDING_SUBMIT` record with no broker response after restart is resolved by exact `client_order_id`/idempotency-key lookup against broker history before falling back to weak matching. | `account_reconciliation.py` (Workstream 4) | `recovery_state=DISCOVERING` → resolved | `discover_orders` | Discovery incomplete: stays `DISCOVERING`, never assumed either way (INV-6). | Repeated on every reconciliation pass until resolved or flagged `MANUAL_INTERVENTION_REQUIRED` past a bounded retry count. | `test_ambiguous_submission_resolves_by_exact_identity_first`, `test_ambiguous_submission_falls_back_to_manual_intervention_after_bounded_retries` | Workstream 2 + 4 |
| A5. Command idempotency table with a unique constraint on `idempotency_key` prevents duplicate submit/cancel/replace after restart or handoff. | `src/services/execution_command_repository.py` (new) | `execution_commands` table | none directly | A duplicate command (same idempotency key) after restart raises/no-ops instead of re-submitting. | N/A | `test_duplicate_submit_command_after_restart_is_rejected_by_idempotency_key`, `test_duplicate_cancel_command_after_lease_handoff_is_rejected` | Workstream 2 |
| A6. `owner_device_id`/`lease_token`/`lease_epoch` are persisted per order and per command. | same as A1/A5 | fields on both records | none | A command whose `lease_epoch` doesn't match the current lease is rejected by the gateway (Workstream 3), not by the caller remembering to check. | N/A | `test_command_with_stale_lease_epoch_is_rejected_by_the_gateway` | Workstream 2 + 3 |

## B. One guarded execution gateway (Workstream 3)

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| B1. `submit_order`/`cancel_order`/`replace_order` are the *only* production entry points that call `broker.submit_order`/`broker.cancel_order`. | `src/services/execution_command_gateway.py` (new) | n/a (routes to A) | delegates | A direct call to `broker.*` from outside the gateway fails the architecture test (B-arch). | N/A | `test_architecture_no_direct_broker_mutation_outside_gateway` | Workstream 3 |
| B2. Before any KIS call, the gateway validates, in order: engine flag, admin/session kill switches, current lease + epoch match, account/environment match, exact broker-order ownership (cancel/replace only), idempotency key, current order status, quantity validity, rate-limit budget. | same | n/a | gate, then delegate | Any single gate failing rejects the whole command with a specific, logged reason — never a partial pass-through. | N/A | one test per gate: `test_gateway_rejects_when_engine_disabled`, `..._kill_switch_active`, `..._stale_lease`, `..._wrong_account`, `..._unowned_cancel_target`, `..._duplicate_idempotency_key`, `..._wrong_current_status`, `..._invalid_quantity`, `..._rate_limit_exceeded` | Workstream 3 |
| B3. `cancel_order` requires an exact `broker_order_id` sourced from an `ExecutionOrderRecord` this application owns (INV-1, INV-2) — never a discovery-matched snapshot alone. | same | reads A | `broker.cancel_order` | A caller that only has a discovery-matched snapshot (ownership uncertain) cannot call `cancel_order` directly — it must go through the `OWNERSHIP_UNCERTAIN` recovery path (A3), which is alert-only until ownership resolves. | N/A | `test_gateway_cancel_requires_an_owned_execution_order_record`, `test_discovery_only_match_cannot_reach_the_cancel_gateway_directly` | Workstream 3 |
| B4. Every gateway call writes an audit record (command, gate results, broker response) regardless of outcome. | `execution_command_repository.py` | `execution_commands.broker_response`, gate outcomes | n/a | A logging failure never blocks or duplicates the underlying broker action (write-after, not write-blocking-before, for the response half; the command record itself is written before, per A1/A5). | N/A | `test_gateway_call_is_audited_even_when_rejected`, `test_audit_log_failure_does_not_block_or_duplicate_the_broker_call` | Workstream 3 |

## C. Account-level reconciliation engine (Workstream 4)

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| C1. One `AccountBrokerSnapshot` (holdings, open orders, order history, reserved orders) is fetched once per account per reconciliation pass and reused by every card in that pass (INV-8). | `src/core/account_broker_snapshot.py`, `src/services/account_reconciliation.py` (new) | none (in-memory per pass) | `get_positions`, `discover_orders` (once each) | Any one source incomplete → `completeness=False` for the whole snapshot; the reducer treats the entire pass as not-yet-conclusive (INV-6), not per-card. | Retried next pass. | `test_one_snapshot_is_fetched_and_reused_across_every_card_in_an_account`, `test_incomplete_snapshot_blocks_terminal_conclusions_for_every_card_in_the_pass` | Workstream 4 |
| C2. The reducer is pure: given a snapshot + current local state, it returns a `ReconciliationPlan` (card/order/reservation updates, commands, alerts) with no network calls inside it. | `account_reconciliation.py` | n/a | none (reducer emits *commands*, doesn't execute them) | A reducer bug is testable without any broker/network double — pure function of recorded inputs. | N/A | `test_reducer_is_a_pure_function_of_snapshot_and_local_state` (property/fuzz-style over recorded fixtures) | Workstream 4 |
| C3. Terminal-resolution policy: prefer exact broker-order-ID match; else exact local-order reconciliation; else two consecutive complete-absence passes + a fresh holdings snapshot with no contradictory evidence. | `account_reconciliation.py` | `recovery_state` progression per A3 | none in the reducer itself | First complete absence: keep recovery state, retain holding, do not clear warning. Second complete absence: require fresh holding, reconcile quantity, mark terminal only if uncontradicted. | Contradictory evidence at any point resets the absence counter and stays `OWNERSHIP_UNCERTAIN`/flagged. | `test_first_complete_absence_does_not_resolve_terminal`, `test_second_consecutive_complete_absence_with_fresh_holding_resolves_terminal`, `test_contradictory_evidence_between_absences_resets_the_counter` | Workstream 4 |
| C4. The reducer covers every order/position category: entry BUY, entry-completion BUY, partial sell, sell all, stop-loss sell, reserved MOO sell, unknown submission, rejected/cancelled/expired, manual broker position (no card), manual broker order (no card), capital reservation without a live order, live order without a capital reservation. | `account_reconciliation.py` | varies by category | none in reducer | Each category has an explicit branch; an unrecognized combination produces an `alerts` entry, never a silent no-op. | N/A | one test per category listed (12 tests minimum) | Workstream 4 |
| C5. This replaces `reconcile_unresolved_orders_at_startup`, `reconcile_buy_today_orders`, `reconcile_untracked_position_remainders`, and the ordering dependency between them. | `account_reconciliation.py` supersedes `src/services/eod_trading_service.py`'s sweep functions | n/a | n/a | The old functions are deleted, not left dead in the tree, once the reducer's coverage (C4) is proven equivalent-or-better on every existing regression test for them. | N/A | Every existing test in `test_eod_trading_service.py` for the superseded functions is ported to exercise the reducer instead, and must still pass. | Workstream 4 |

## D. Production KIS real-time market data (Workstream 5)

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| D1. `kis_websocket.py`/`kis_ws_auth.py` handle approval-key issuance/refresh, connect, subscribe/unsubscribe, ACK/NACK parsing, ping/pong, `^`-delimited frame parsing, encrypted execution-notice decoding, reconnect with backoff+jitter, resubscription after reconnect. | `src/api/kis_websocket.py`, `src/api/kis_ws_auth.py` (new) | none (transport layer) | WS connect/subscribe (read-only market data; execution notices are supplementary, never authoritative — INV-4 keeps broker holdings/discovery as the source of truth for fills) | Malformed frame: logged, dropped, connection stays up. Auth failure: bounded retry then `MANUAL_INTERVENTION_REQUIRED`-equivalent alert. | Reconnect resubscribes every previously-desired symbol from `SymbolFeedState`, not just the ones that were acked before the drop. | See Workstream 7 protocol test list (fake WS server: ACK, NACK, ping/pong, malformed, reconnect, resubscribe, duplicate frame, out-of-order frame, queue overflow) | Workstream 5 |
| D2. `kis_realtime_market_data.py` implements the existing `RealtimeMarketDataService` interface over `HDFSCNT0` (trade) and `HDFSASP0` (quote); `H0GSCNI0`/`H0GSCNI9` may feed low-latency *notifications* only — broker reconciliation (Workstream 4) remains authoritative for fills. | `src/services/kis_realtime_market_data.py` (new) | none (in-memory `SymbolFeedState`) | n/a (consumes D1) | A parse failure for one symbol never blocks another symbol's feed. | N/A | `test_execution_notice_never_substitutes_for_broker_reconciled_fill` | Workstream 5 |
| D3. `QuoteSnapshot` carries `broker_event_at` (exchange timestamp) separately from `received_at` (local fetch time); staleness is judged on `broker_event_at`. | `src/services/realtime_market_data.py` (extend) | n/a | n/a | A repeated fetch of the same old REST bar must not appear fresh merely because `received_at` advances (INV-10's precedent bug). | N/A | `test_repeated_fetch_of_the_same_stale_bar_does_not_appear_fresh` | Workstream 5 |
| D4. `SymbolFeedState` tracks `desired`/`trade_acked`/`quote_acked`/timestamps/`last_error`/`reconnect_generation` per symbol; a symbol is execution-ready only when socket connected AND subscriptions acked AND latest event fresh AND no unresolved sequence/channel error. | `src/services/kis_realtime_market_data.py` | n/a (in-memory) | n/a | One symbol's failure state never marks any other symbol ready (INV-11's precedent bug: today one success marks the whole service connected). | Feed-level reconnect re-evaluates every symbol's state independently. | `test_one_healthy_symbol_does_not_mark_a_failing_symbol_ready`, `test_global_connected_flag_is_not_used_for_per_symbol_execution_readiness` | Workstream 5 |
| D5. WS I/O runs on its own thread/loop; the trading/reconciliation worker drains a thread-safe queue and never calls blocking REST/WS I/O for quotes inline. | `src/ui/buyboard/runtime_worker.py` (modify), `kis_realtime_market_data.py` | n/a | n/a | A slow account/reconciliation pass never delays quote reception; a slow/backed-up quote queue never blocks reconciliation (bounded queue with drop+count, not unbounded blocking). | N/A | `test_slow_reconciliation_pass_does_not_delay_queued_quote_delivery`, `test_full_quote_queue_drops_and_counts_rather_than_blocking` | Workstream 5 |
| D6. Degraded-mode policy table (WS healthy / one symbol stale / socket disconnected / REST fallback only / subscription NACK) is enforced exactly as specified — REST fallback is display/diagnostic only, never treated as equivalent stop-loss protection (INV-10). | `src/core/execution_config.py` (`MARKET_DATA_MODE`, `MARKET_DATA_FALLBACK_MODE`) + consuming call sites | n/a | n/a | Each row of the policy table is a distinct test. | N/A | `test_market_data_policy_websocket_healthy_allows_entries`, `..._one_symbol_stale_blocks_that_symbol_only`, `..._socket_disconnected_blocks_all_entries`, `..._rest_fallback_blocks_automatic_entries`, `..._subscription_nack_blocks_symbol_and_alerts` | Workstream 5 |
| D7. `.env.example` gets `KIS_PROD_WS_URL`, `KIS_SIM_WS_URL`, `KIS_WS_ENABLED`, `KIS_WS_HTS_ID`, `KIS_MARKET_DATA_MODE`, reconnect/ack/stale/queue tuning vars, `KIS_WS_RAW_CAPTURE_ENABLED`; `requirements.txt` gets a pinned WebSocket dependency (`websockets`, matching the official sample's protocol behavior). | `.env.example`, `requirements.txt` | n/a | n/a | n/a | n/a | config-presence test | Workstream 5 |
| D8. Health surface exposes: WS connected, approval-key age, ACK count vs expected, stale symbols, last trade/quote event, receive-lag p50/p95/p99, reconnect count, NACK count, malformed-frame count, queue depth, dropped-event count. | Health tab / `src/services/kis_realtime_market_data.py` metrics | n/a | n/a | n/a | n/a | `test_market_data_health_metrics_are_exposed_and_update` | Workstream 5 |

## E. Runtime readiness and device handoff (Workstream 6)

| Requirement | Owning module | Persisted state | Broker action | Failure behavior | Recovery behavior | Tests | Activation criterion |
|---|---|---|---|---|---|---|---|
| E1. `engine_healthy` requires lease current AND startup reconciliation complete AND account reconciliation fresh AND websocket connected AND critical-symbol subscriptions acked AND critical-symbol quotes fresh AND quote queue not lagging AND database writable (INV-12). | `src/ui/main_window.py` (`_buyboard_engine_healthy`, extend) | n/a | n/a | Any single condition false → unhealthy; the legacy monitor's fail-open-to-legacy-protection behavior (existing, keep) still applies. | N/A | one test per condition removed from the AND, `test_engine_healthy_requires_every_condition_including_market_data` | Workstream 6 |
| E2. Startup sequence is exactly: acquire lease → load durable cards/orders/commands → one broker snapshot per account → reconcile all accounts → start WebSocket → subscribe → receive ACKs → receive fresh events → mark healthy → allow automatic entries. | `src/ui/buyboard/runtime_worker.py` | n/a | per step | A failure at any step halts progression past it; later steps never run on an unconfirmed earlier one. | Retried per the normal per-step retry/backoff. | `test_startup_sequence_does_not_allow_entries_before_every_step_confirms` | Workstream 6 |
| E3. Handoff sequence: old device loses lease → gateway immediately rejects mutations (B2) → stop new entries → no destructive calls continue → new device acquires lease → full account reconciliation → WS ACK + fresh events → new device healthy. | `execution_command_gateway.py` + `runtime_worker.py` | lease state | none from the losing device | A command issued by the losing device after lease loss is rejected by the gateway (B2), not by a cooperative check in the losing device's own loop. | N/A | `test_losing_device_cannot_submit_after_lease_loss_even_if_its_own_loop_has_not_noticed_yet` | Workstream 6 |
| E4. Shutdown sequence: block new commands → flush command/reconciliation journal → final account reconciliation → unsubscribe + close WS → release lease. | `runtime_worker.py` | journal flush | unsubscribe | An interrupted shutdown (process killed) is recovered by the next startup's normal reconciliation — shutdown is best-effort, not relied upon for correctness. | Startup reconciliation (E2) is the actual safety net. | `test_interrupted_shutdown_is_recovered_by_next_startup_reconciliation` | Workstream 6 |

---

## Order-type coverage checklist (cross-reference for C4)

Every row must have an explicit reducer branch and at least one fault-injection scenario from Workstream 7:

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
| 1. Deterministic simulation | Replay, restart, fault-injection, multi-account tests all pass | All |
| 2. Live KIS WebSocket, read-only (`BUYBOARD_ENGINE_ENABLED=false`, `TRADING_ENABLED=false`, `KIS_WS_ENABLED=true`) | Real feed: symbol/exchange mapping, ACKs, bid/ask/last parsing, broker timestamps, real latency, disconnect/reconnect, resubscription, full-session stability | INV-9, INV-10, INV-11 |
| 3. Shadow execution | Real quotes, real decisions, broker mutations replaced with `WOULD_SUBMIT`/`WOULD_CANCEL`/`WOULD_SELL` audit entries, compared against live chart/account | All decision-path invariants, none of the mutation ones |
| 4. Controlled live | One account, one/two symbols, minimum size, supervised, external alerts on, legacy/Kanban ownership isolated | All |
| 5. Unattended activation | No duplicate commands; no unresolved local/broker discrepancy; no stale quantity; no command after lease loss; successful reconnect+resubscribe; every stop decision uses fresh event data; every auto-cancel has exact ownership; startup/handoff converge without manual repair; external critical alerts confirmed reaching the user outside the app | All |

`BUYBOARD_ENGINE_ENABLED` stays `false` until Gate 2 has passed, at minimum, and stays `false` in unattended/automatic form until Gate 5 passes.

---

## Change log

- 2026-08-15: Initial draft, branch created from `109c2c4` ("kanban fix 8").
