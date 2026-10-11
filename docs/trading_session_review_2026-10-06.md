# Trading session review — US October 6, 2026

> Post-close follow-up: [October 7 review of the completed October 6 US session](trading_session_review_2026-10-07.md). The intraday snapshot below is preserved.

Prepared October 7 KST. Broker/position checkpoint: **2026-10-07 01:12:06 KST / 2026-10-06 12:12:06 ET / 2026-10-06 16:12:06 UTC**. Readiness follow-up: 2026-10-07 01:13:48 KST. This is an intraday review; the US session has not finished.

Scope: today's US trading sequence, related premarket recovery, broker responses, canonical orders and commands, daily card history, passive runtime journals, deployed fixes, and remaining work. Evidence dated October 5 is treated as prior-session context even when its artifact folder ends in `20261006`. No new trading logic, broker order, or canonical trading state was changed by this audit.

## Most important findings

1. **Hard-stop execution fixes are deployed, and TWLO, MRNA, SVIA, and BAND are broker-confirmed flat.** The earlier failures were in exit pricing, retry limits, and processing delays; the old stop comparison already used `price <= stop`, rather than requiring equality.
2. **Buying-power validation is still wrong.** Both AVAT and ZS reached the broker and were rejected with `APBK0952`. The app treats an account-value cash estimate as spendable buying power. The broker's actual orderability query disagrees with that estimate.
3. **Routine quote latency improved substantially, but occasional long delays remain.** A BE entry coincided with an 8.562-second cycle and a 9.202-second queued quote. Later cumulative feed telemetry records a 12.255-second maximum queue delay. These are remaining tail delays, not evidence that every cycle is slow.
4. **Account-reconciliation readiness still intermittently expires.** It was false at 16:12:03 UTC, then true at 16:12:08 UTC. The live switch stayed on and the PC stayed ACTIVE. The earlier freshness/scheduling work mitigated this; it did not eliminate it.
5. **There are audit/status inconsistencies to repair.** Two completed sells retain ambiguous cancel-command records, explicit broker rejections are labeled ambiguous by the passive observer, and eight older critical incidents remain OPEN after being redelivered today.

## Current broker and application state

The PC owner is `DESKTOP-E42GSKJ`, running approved clean release `32866eae800168b794811220b50ef84a457d1a26`. Shared live control is ON at revision 56 for session October 6. At the follow-up, executor, reconciliation, and market-data readiness were all true. The checkpoint independently confirmed complete holdings, open-order, order-history, and reserved-order reads, with no broker errors.

| Symbol | Broker/app shares | Average entry USD | Checkpoint price USD | Active stop USD | Stop coverage | Stop type |
|---|---:|---:|---:|---:|---:|---|
| ALAB | 8 / 8 | 385.73 | 386.55 | 374.635 | 8 shares | ORB_LOW |
| BE | 10 / 10 | 297.85 | 297.0824 | 285.64 | 10 shares | ORB_LOW |
| CYPH | 897 / 897 | 3.31 | 3.375 | 3.09 | 897 shares | ORB_LOW |
| SIMO | 10 / 10 | 289.99 | 290.07 | 281.51 | 10 shares | ORB_LOW |
| VNCE | 184 / 184 | 11.86 | 13.09 | 11.88 | 184 shares | BREAKEVEN |

All five positions have full-quantity stop configuration and were above their stops in this checkpoint. None had a latched full-liquidation intent. These stops are app-managed protections, not proof that a native broker stop order exists; the remaining worker-delay/readiness issues still matter.

TWLO, MRNA, SVIA, and BAND each have zero broker/app shares and CLOSED cards. ZS and AVAT have zero shares and no accepted order from their rejected attempts. At the ledger checkpoint, there were **no nonterminal execution orders, no unresolved order-recovery states, no active capital reservations, no duplicate canonical broker-order identities, and no nonterminal operator commands**. The later broker checkpoint also showed no working orders.

## Session sequence

All times below are UTC; add 9 hours for KST and subtract 4 hours for ET on this session date. Order times are local submission-start times, not exchange fill timestamps.

| UTC | Event and outcome |
|---|---|
| 11:56 | Preopen review blocked new entries: SVIA had a queued exit, EFOR cancellation proof contradicted its old canonical recovery state, and reconciliation readiness had expired. |
| 12:28–12:35 | Release de226e59 corrected closed-hours outage exits, exact zero-fill cancellation handling, and refresh/cycle scheduling. EFOR became CANCELLED with zero fills; SVIA remained held and protected. |
| 12:59:35 | Operator armed live trading, revision 52. |
| 13:10–13:34 | Buy Today additions and ORB planning. The daily ledger records 23 additions, including repeated additions for MRNA and CYPH; additions are not broker orders. |
| 13:31:59–13:37:50 | TWLO, BAND, VNCE, MRNA, and ALAB buy submissions. MRNA's first 13-share order was later cancelled with zero fills. |
| 13:37:32–13:38 | MRNA higher-scoring ORB replacement cancelled the old order, then aborted before replacement submission because passive quote conditions changed. Subsequent planning was also blocked by the 100% portfolio gross-notional guard. |
| 13:41:17–13:44:51 | MRNA was re-added to Buy Today with a 5-minute, 16-share plan. TWLO's third sell filled; a separate MRNA 16-share entry was then submitted and filled. |
| 13:41–14:05 | TWLO, SVIA, MRNA, and BAND liquidation attempts repeatedly submitted limits and cancelled zero-fill orders. Cancel-confirmation alerts occurred and later resolved. |
| 14:03:02 / 14:03:06 | Supervised guarded recovery submitted MRNA and SVIA stop exits; both filled. This happened before the permanent stop-fix release was deployed. |
| 14:13:17 / 14:13:47 | BAND guarded stop exit submitted and filled; SIMO entry submitted and subsequently filled. |
| 14:17:50 | MRNA's abandoned replacement-command record was explicitly finalized as REPLACEMENT_ABORTED_AFTER_CANCEL with zero-fill old-order proof and no replacement submit. |
| 14:27:12–14:33 | Permanent hard-stop release 488c0694 deployed. Live trading restored at revision 54. Four exited symbols verified flat; ALAB, SIMO, and VNCE quantities matched KIS. |
| 14:36–14:49 | AVAT freshness investigation found 7.699–11.538 seconds of internal queue delay. Blocking account work reached 13.703 seconds; network receive delay was much smaller. |
| 14:53:13 | CYPH buy 897 at $3.31 submitted and filled. |
| 15:02:31 | AVAT buy 1,444 at $2.00 rejected: APBK0952, order exceeds available buying amount. |
| 15:18:51 | ZS buy 14 at $211.85 rejected with the same APBK0952 code. |
| 15:28:03–15:28:46 | Mobile Web requested a VNCE 92-share partial sale. The broker sell filled at average $12.78, leaving 184 shares; the remaining stop became breakeven at $11.88. A cancel raced the fill and received APBK0124. |
| 15:38:23–15:40:12 | Release 32866eae deployed with background account reads, reduced redundant persistence, and the approved 15-second entry freshness limit. Owner activation completed; live restored at revision 56. |
| 15:48:10 | Local Git exclusion added for atomic JSON state temporary files so they no longer intermittently invalidate release identity. |
| 15:54:21–15:55:07 | BE buy 10 at $297.85 submitted, accepted, and recorded filled. A long worker cycle and delayed quotes occurred around this submission. |
| 15:57:11 | Read-only ZS orderability check: KIS allowed four shares at the original limit, $915.31 orderable, while the app's current cash estimate was $1,409.85. |
| 16:12:03–16:13:48 | Independent broker checkpoint found five fully covered positions and no working orders. Runtime row briefly showed account_reconciliation_fresh=false, then recovered at 16:12:08; follow-up confirmed readiness true. |

### Broker order ledger

**27 new order records today:** 13 FILLED, 12 CANCELLED, 2 REJECTED. The full audit also includes EFOR's October 5 order repaired today, making 28 records touched/reviewed. FILLED here is the reconciled order outcome, not just API acceptance.

| Start UTC | Start KST | Symbol | Side / intent | Shares | Submitted limit USD | Final status | Filled shares | Average fill USD |
|---|---|---|---|---:|---:|---|---:|---:|
| 13:31:59 | 2026-10-06 22:31:59 | TWLO | BUY / ENTRY | 12 | 304.63 | FILLED | 12 | 304.58 |
| 13:33:25 | 2026-10-06 22:33:25 | BAND | BUY / ENTRY | 38 | 64.49 | FILLED | 38 | 64.49 |
| 13:34:07 | 2026-10-06 22:34:07 | VNCE | BUY / ENTRY | 276 | 11.86 | FILLED | 276 | 11.86 |
| 13:36:27 | 2026-10-06 22:36:27 | MRNA | BUY / ENTRY | 13 | 204.42 | CANCELLED | 0 | — |
| 13:37:50 | 2026-10-06 22:37:50 | ALAB | BUY / ENTRY | 8 | 385.73 | FILLED | 8 | 385.73 |
| 13:41:21 | 2026-10-06 22:41:21 | TWLO | SELL / MANUAL_EXIT | 12 | 302.9974 | CANCELLED | 0 | — |
| 13:42:43 | 2026-10-06 22:42:43 | TWLO | SELL / MANUAL_EXIT | 12 | 301.4748 | CANCELLED | 0 | — |
| 13:43:48 | 2026-10-06 22:43:48 | TWLO | SELL / MANUAL_EXIT | 12 | 299.9522 | FILLED | 12 | 300.73 |
| 13:44:51 | 2026-10-06 22:44:51 | MRNA | BUY / ENTRY | 16 | 208.85 | FILLED | 16 | 208.85 |
| 13:46:26 | 2026-10-06 22:46:26 | SVIA | SELL / MANUAL_EXIT | 643 | 4.3929 | CANCELLED | 0 | — |
| 13:47:02 | 2026-10-06 22:47:02 | SVIA | SELL / MANUAL_EXIT | 643 | 4.3708 | CANCELLED | 0 | — |
| 13:48:03 | 2026-10-06 22:48:03 | SVIA | SELL / MANUAL_EXIT | 643 | 4.3488 | CANCELLED | 0 | — |
| 13:50:46 | 2026-10-06 22:50:46 | MRNA | SELL / MANUAL_EXIT | 16 | 210.94 | CANCELLED | 0 | — |
| 13:51:27 | 2026-10-06 22:51:27 | MRNA | SELL / MANUAL_EXIT | 16 | 209.88 | CANCELLED | 0 | — |
| 13:52:34 | 2026-10-06 22:52:34 | MRNA | SELL / MANUAL_EXIT | 16 | 208.82 | CANCELLED | 0 | — |
| 14:03:02 | 2026-10-06 23:03:02 | MRNA | SELL / STOP_LOSS | 16 | 193.11 | FILLED | 16 | 197.165 |
| 14:03:06 | 2026-10-06 23:03:06 | SVIA | SELL / STOP_LOSS | 643 | 4.15 | FILLED | 643 | 4.2064 |
| 14:03:16 | 2026-10-06 23:03:16 | BAND | SELL / MANUAL_EXIT | 38 | 64.1775 | CANCELLED | 0 | — |
| 14:03:46 | 2026-10-06 23:03:46 | BAND | SELL / MANUAL_EXIT | 38 | 63.855 | CANCELLED | 0 | — |
| 14:04:44 | 2026-10-06 23:04:44 | BAND | SELL / MANUAL_EXIT | 38 | 63.5325 | CANCELLED | 0 | — |
| 14:13:17 | 2026-10-06 23:13:17 | BAND | SELL / STOP_LOSS | 38 | 61.47 | FILLED | 38 | 62.75 |
| 14:13:47 | 2026-10-06 23:13:47 | SIMO | BUY / ENTRY | 10 | 289.99 | FILLED | 10 | 289.99 |
| 14:53:13 | 2026-10-06 23:53:13 | CYPH | BUY / ENTRY | 897 | 3.31 | FILLED | 897 | 3.31 |
| 15:02:31 | 2026-10-07 00:02:31 | AVAT | BUY / ENTRY | 1444 | 2 | REJECTED | 0 | — |
| 15:18:51 | 2026-10-07 00:18:51 | ZS | BUY / ENTRY | 14 | 211.85 | REJECTED | 0 | — |
| 15:28:23 | 2026-10-07 00:28:23 | VNCE | SELL / PARTIAL_EXIT | 92 | 11.741 | FILLED | 92 | 12.78 |
| 15:54:21 | 2026-10-07 00:54:21 | BE | BUY / ENTRY | 10 | 297.85 | FILLED | 10 | 297.85 |

Limits above show the durable request values. The broker adapter normalizes valid price increments; a submitted limit is not the achieved execution price. Local `row_updated_at` records reconciliation/persistence, not an authoritative exchange fill time.

### MRNA submit → cancel → later submit

The first order was 13 shares at $204.42. A higher-scoring 5-minute ORB triggered a cancel-then-replace request at 13:37:32. The original order cancelled with **zero fills**. The post-cancel passive quote check aborted replacement, so no replacement broker submission was journaled. Card history then shows portfolio risk rejections around **118.06–118.11% projected gross notional versus a 100% maximum**. MRNA was re-added at 13:41:17 with a 16-share, 5-minute plan. A later separate entry at $208.85 filled after TWLO had closed. The records show no two accepted MRNA buys simultaneously working and no duplicate fill from the initial cancelled order.

### Failed and ambiguous commands

The session has 42 durable execution commands: 37 ACKNOWLEDGED, three FAILED, and two AMBIGUOUS. ACKNOWLEDGED describes a command response and does not by itself mean the associated order filled.

| Command | Symbol | Persisted status | Actual known outcome / remaining work |
|---:|---|---|---|
| 90010 replace | MRNA | FAILED | Aborted after zero-fill cancellation; explicitly reconciled at 14:17:50. Resolved operationally. |
| 90018 cancel | TWLO | AMBIGUOUS | APBK0124, no cancellable quantity. The exact sell is FILLED and the position is flat; command-level finalization remains open. |
| 90043 submit | AVAT | FAILED | Explicit APBK0952 rejection, zero fills; buying-power validation remains open. |
| 90044 submit | ZS | FAILED | Explicit APBK0952 rejection, zero fills; same buying-power issue. |
| 90046 cancel | VNCE | AMBIGUOUS | APBK0124 after the 92-share sell filled; holding correctly reconciled to 184. Command-level finalization remains open. |

For TWLO/VNCE, a filled sell does not mean the cancel succeeded. Finalize the command with exact terminal-order evidence and a fill-race outcome, preserving the original broker error. Do not resend a cancel or create a replacement solely to remove the ambiguous label.

## What was fixed and verified

| Issue | Completed change | Evidence / status |
|---|---|---|
| Closed-hours outage creating an unwanted SVIA Sell All | New automatic outage exits restricted appropriately to market-session handling; genuine existing stop intent retained. | de226e59 preopen verification: SVIA OPEN_POSITION, 643 shares, stop $4.24. |
| EFOR cancellation contradiction | Recognize exact unique zero-fill KIS cancellation evidence and retire contradictory original history. | EFOR CANCELLED, zero fills, TERMINAL_RECONCILED; no current uncertain orders. |
| Stop breach not completing liquidation | Persist stop-loss latch until broker-flat; refresh exit reference from fresh bid/trade or KIS holdings; continue liquidation beyond former three-attempt/five-percent constraints; fence cancellation/replacement against exact terminal proof. | 488c0694 deployed 14:27 UTC, inherited by 32866eae. TWLO/MRNA/SVIA/BAND flat. Recovery sells preceded deployment. |
| Slow pending liquidation reconciliation | Faster pending-exit reconciliation and adoption of existing cancellations without issuing duplicate cancels. | Deployed; four cancel-confirmation timeout incidents resolved. Residual long worker cycles still need work. |
| Routine account reads blocking quote evaluation | Bounded background read; apply results on owner thread; reject superseded results; retain actual response freshness. | 32866eae deployed. Routine latency improvement measured; account freshness still briefly expires. |
| Unchanged cards causing repeated persistence/cache reloads | Persist semantic changes, skip unchanged payloads, acknowledge known own writes safely. | Deployed; materially shorter routine cycles. Blocking order-path work remains. |
| Two-second entry freshness limit too restrictive for processing waits | Approved maximum total entry age raised to 15 seconds for trade and bid/ask, including queue wait; recheck at submission boundary. | Deployed. Channel ACK, valid/current-session KIS data, and exit/ingress policies still apply. |
| Atomic JSON state writes intermittently failing clean-release checks | Local `.git/info/exclude` ignores `/data/.*.json.*.tmp`. | 80 clean samples; later identity checks passed. Local workaround, not yet a portable source-controlled fix. |
| MRNA abandoned replacement command | Exact zero-fill/no-replacement proof finalized the existing command. | Explicitly reconciled as failed/aborted at 14:17:50; no unresolved order exposure. |

Latest deployed-release validation: **all five hosted CI jobs passed**; Python 3.12 reported 3,464 passed and 29 skipped, Python 3.11 also passed, plus dependency audit, repository hygiene, and Gate 1. These tests do not certify full-session live operation or guarantee execution at the stop price. [Release CI](https://github.com/cafe-auvers/quant_app/actions/runs/37486745111). [Hard-stop CI](https://github.com/cafe-auvers/quant_app/actions/runs/37477233968).

## Remaining issues and recommended resolution

Priority P1 means an execution/funding/reliability issue to address next. P2 means an audit, operator-status, data-validation, or portability issue. These items were identified by this audit and remain open unless explicitly marked otherwise.

### P1-1 — Use real broker buying power

AVAT's $2,888 attempt and ZS's $2,965.90 attempt both failed with `APBK0952 — 주문가능금액을 초과 했습니다` (order exceeds the available buying amount). At the later ZS check, KIS returned `$915.31` orderable and `ord_psbl_qty=4` at $211.85, while the app estimated `$1,409.85`. The exact broker buying-power value at either original rejection was not recorded; the later figures must not be backdated to those orders.

Cause: `BuyboardRuntimeWorker._extract_account_balance` takes `ovrs_cash_usd` from the dashboard account-value breakdown. That breakdown can derive a cash residual from foreign asset valuation; it is not the broker's exact orderable amount. Use a bounded, fresh, account/symbol/exchange/price-aware result from [KIS overseas buying-power inquiry](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/inquire_psamount/inquire_psamount.py), with reservation, fee/currency, and concurrent-order handling. Separate equity for risk sizing from orderable funds. Log the values used. Keep the requested strategy quantity or reject clearly; any automatic quantity reduction needs a defined sizing policy. Do not blindly resubmit a rejected symbol.

### P1-2 — Remove residual long work from quote/stop evaluation

Comparable three-minute windows improved queue p95 from **10.529 to 1.932 seconds**. The before account phase reached 13.703 seconds; the early post-deployment maximum was 0.407 seconds. A later cumulative account-application phase reached 1.906 seconds.

However, the BE submit at 15:54:21–15:54:24 coincided with a **8.562-second cycle**, including 8.531 seconds in decisions/heartbeat/persistence. At 15:54:28 a CYPH quote had waited **9.202 seconds**, with 10.111 seconds total age. Later cumulative feed telemetry recorded queue p95 1.863 seconds, p99 2.489 seconds, and maximum **12.255 seconds**. Different windows/counters are labeled here deliberately.

The deployed change removes routine broker reads, but guarded order submissions and database/persistence work still run synchronously on the owner worker. Their exact per-operation contributions to the long cycle are not yet instrumented. Record each boundary's duration and scheduling delay, then separate blocking execution work while preserving single ownership, lease checks, idempotency, atomic reservations, and owner-thread state application. Give protective stop decisions priority. Validate with a slow broker/database plus an unrelated symbol crossing its stop. Raising entry freshness to 15 seconds does not fix delayed stop processing.

### P1-3 — Prevent intermittent account-reconciliation deadline misses

The runtime row at 16:12:03 had `executor_ready=false`, `order_reconciliation_ready=false`, and reason `account_reconciliation_fresh`; at 16:12:08 those fields recovered. An independent complete KIS read succeeded. This shows a freshness/scheduling lapse, not proof of a current broker outage or missing holdings.

The worker starts normal full reconciliation around age 50 seconds against a 60-second deadline and shares one reader with balance-only work. An in-flight balance read can delay the full read, and ten seconds of lead time may be insufficient. This is a plausible code-level contributor; the exact request-start/completion/discard sequence was not retained for this occurrence. Instrument that sequence, prioritize full reconciliation before its deadline, and include real read/application time in scheduling. Verify readiness stays stable during active entries and slow reads. Do not label an old snapshot fresh or simply extend its age limit to hide the issue.

### P2-1 — Finish ambiguous cancel-command records

TWLO command 90018 and VNCE command 90046 remain AMBIGUOUS although their exact orders are FILLED and holdings reconcile. Add command-level reconciliation for a cancel/fill race and persist a final outcome backed by exact broker history. The absence of nonterminal orders currently prevents this from being an active position mismatch, but the audit stream remains misleading.

### P2-2 — Correct rejection and lifecycle telemetry

AVAT/ZS durable commands correctly say FAILED and orders REJECTED, but the passive `MUTATION_TERMINAL` observer says AMBIGUOUS/unresolved. Its exception path uses the scheduler's narrow rate-limit retry classifier to decide ambiguity. Retry eligibility and definitive order rejection are separate questions. Use the broker submission classifier for diagnostic outcome; keep automatic mutation retry policy separate.

Also emit final fill, close, cancellation, and protection events during regular reconciliation, not only startup/full-pass observations. The legacy `data/event_journal.jsonl` has not changed since August 20 and cannot reconstruct today's Kanban trades. The current daily ledger records card snapshots and Buy Today additions, but its snapshot fields omit active stop/stop coverage/latch details. Preserve these, authoritative broker timestamps when available, price source, rejected funding values, and final command correlation. A broker ACK must never be presented as a fill.

### P2-3 — Reconcile older OPEN critical alerts

Thirteen incidents were updated today: the four liquidation cancel-timeout incidents are RESOLVED, one older SVIA outage is RESOLVED, and **eight historical incidents remain OPEN**. Those include EFOR contradiction, old lease loss, an older `OPSQ1002 SESSION FULL`, old TNET/PARR reconciliation messages, and database-unavailable incidents. Several originated in August/September. The delivery ledger records 27 DELIVERED attempts today; passive observers saw 19 deliveries across collected segments. These counts measure deliveries, not 27 new failures.

Current reads do not reproduce those historical faults, and the eight OPEN statuses are not sufficient evidence that all eight conditions are happening now. Recheck each incident against current authoritative state, link resolution evidence, and close or acknowledge only after that verification. Stop historical redelivery from obscuring new liquidation alerts. Current resolved cancel-timeout incidents demonstrate that resolution works for some paths but is incomplete overall.

### P2-4 — Validate obsolete symbols and improve ORB messages

BRR was added at 13:12:37 and waited for a current-session ORB; it is now back in Buylist. BRR changed to **SVIA effective September 22**, verified in the [issuer announcement hosted by Nasdaq](https://www.nasdaq.com/press-release/silvia-inc-begins-trading-nasdaq-under-new-ticker-svia-2026-09-22). The old symbol must be rejected or mapped explicitly before planning/subscription. Retain historic symbol/order identity, deduplicate against existing SVIA state, and preserve its CLOSED state; a rename must not implicitly create another entry.

RNG/SDGR are armed but show `EXECUTION_LEVEL_ALREADY_REACHED`. Their recorded trusted prices were below their execution levels (RNG $77.83 versus $80; SDGR $29.265 versus $32.95). The code uses this message for several failed price/ask conditions, including price below the level. Waiting for breakout is valid behavior here; the message is inaccurate. Show the actual condition: waiting for breakout, missing ask, or invalid passive-entry quote. Do not change the trading rule merely to clear the label.

Current-session KIS bars and fresh KIS trade/quote data remain entry requirements. Yahoo display data is not authorization for a KIS order. Distinguish ticker invalidity, missing bars, provider/session mismatch, channel silence, receive age, and queue wait in the operator message rather than saying only 'waiting for ORB'.

### P2-5 — Make the temporary-file release-identity fix portable

The PC's `.git/info/exclude` workaround is active and verified, but will not automatically exist in another clone. Add the narrow generated-JSON temporary-file pattern to the source-controlled ignore policy, retaining tracked-source and release/manifest checks. Verify a real source edit still invalidates approval while an atomic local-state write does not.

### Qualification and end-of-session work — still open

Gates 2/3/4 remain **NOT_CERTIFIED**. Missing evidence includes a full read-only session with reconnect/silent-channel probes, reviewed branch/fence replay, and three supervised session dates with complete lifecycle/disarm evidence. There were multiple runtime releases today, so a collection segment must not be represented as one full-session observation of the current release. The session close is October 7 05:00 KST / October 6 16:00 ET. The earlier planned post-session audit time was 05:15 KST; its successful completion is not yet evidenced in this intraday report. Finalize and audit each journal segment after close, then append the final broker and ledger reconciliation.

## Behavior that should remain intact

- Stops trigger at or below the stop and remain latched until broker-confirmed flat. The hard rule concerns dispatching and completing liquidation; a falling/gapping market cannot guarantee the fill equals the configured stop price.
- Cancel-then-replace waits for exact terminal cancellation proof; any fill on the old order must be accounted for before another buy.
- Portfolio exposure/risk limits rejecting a proposed entry are valid controls. Today's 100% gross-notional rejections are not evidence of a broker API failure.
- Rejected, ambiguous, accepted, and filled outcomes remain distinct. A network-ambiguous mutation must not be blindly retried.
- Feed capacity is not exhausted: the follow-up shows seven desired/acknowledged trade channels and seven quote channels, 15 occupied subscription slots including execution notices, and 26 available out of 41. No reconnects, NACKs, malformed frames, or dropped events were reported in that current-release sample. The observed long queue delays are not explained by subscription capacity.

## Evidence and review limits

Primary data is a read-only capture. Orders/commands/card history were collected at 16:02:45 UTC; alert/control/latency supplements were collected later; holdings/working orders were checked again at 16:12:06 UTC; readiness recovery was read at 16:13:48 UTC. Prices and state can change after those checkpoints. Source checks used the exact deployed release, not the unrelated dirty root workspace.

The reviewed passive JSONL segments contained no malformed JSON, reported no collector errors, and no dropped batches. This audit did not independently recertify their cryptographic chains; earlier journal-integrity verification exists, and a final post-close integrity review remains required. Normalized broker submission/fill timestamps are not populated for every order, so the table preserves local request times and final broker outcomes without inventing exact exchange fill times. Earlier displayed/derived cash is not backdated from a current query.

| Evidence | Contents |
|---|---|
| [trading_session_audit.json](../artifacts/nonblocking_quotes_20261006/trading_session_audit.json) | Canonical cards, 28 reviewed order records, 42 commands, broker history, controls, current-release and earlier collector segments. |
| [trading_session_supplement.json](../artifacts/nonblocking_quotes_20261006/trading_session_supplement.json) | Daily event history, operator partial-sale request, alerts/deliveries, live-control transitions, exact-identity duplicate check, and latency samples. |
| [trading_session_final_snapshot.json](../artifacts/nonblocking_quotes_20261006/trading_session_final_snapshot.json) | Latest complete holdings/order checkpoint, all five stops, closed positions, and brief readiness failure. |
| [trading_session_readiness_followup.json](../artifacts/nonblocking_quotes_20261006/trading_session_readiness_followup.json) | Readiness recovery and later cumulative feed/cycle telemetry. |
| [zs_broker_rejection_diagnosis.json](../artifacts/nonblocking_quotes_20261006/zs_broker_rejection_diagnosis.json) | ZS rejection and independent KIS orderability comparison. |
| [verification.json](../artifacts/nonblocking_quotes_20261006/verification.json) | Latest release validation and deployment evidence. |
| [performance_before_final.json](../artifacts/nonblocking_quotes_20261006/performance_before_final.json) | Comparable three-minute pre-fix latency sample. |
| [performance_after.json](../artifacts/nonblocking_quotes_20261006/performance_after.json) | Comparable three-minute early post-fix sample. |
| [Hard-stop review](../artifacts/hard_stop_recovery_20261006/review.md) | Recovery, permanent fix, checks, and broker confirmation. |
| [Preopen reconciliation verification](../artifacts/fix_live_reconciliation_20261006/final_verification.json) | EFOR/SVIA fixes, preopen state, and formal qualification limits. |

No credentials or full account numbers are included in this report. Broker identifiers in supporting captures are redacted; the supplemental hashed order references permit correlation without exposing the originals.

Recommended implementation order: actual broker funding validation; stable reconciliation scheduling; removal of remaining blocking work from protective evaluation; command/telemetry/alert finalization; symbol/message/ignore-policy cleanup; final session evidence and qualification.

Current release reference: [Opening liquidity and mobile workflow (2026-10-08)](opening_liquidity_mobile_release_2026-10-08.md). Dated reports and archived plans retain their original scope.
