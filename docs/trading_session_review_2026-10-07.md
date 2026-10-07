# Trading review: October 6 US session

Prepared **October 7, 2026 KST**, after the session finished. Regular session: **October 6 13:30-20:00 UTC / 09:30-16:00 ET / October 6 22:30 to October 7 05:00 KST**. This is the post-close follow-up to the [earlier intraday review](trading_session_review_2026-10-06.md).

The refreshed ledger capture completed at **October 7 08:08:40 KST (October 6 23:08:40 UTC)**. The independent broker snapshot was observed at **08:08:31 KST (23:08:31 UTC)**. These are post-close observations, not an invented snapshot taken exactly at the closing bell. Application-log timestamps are KST; sequence/evidence tables below use UTC.

## Findings

- **TWLO, MRNA, SVIA, and BAND are broker-confirmed flat.** The hard-stop repair is deployed. The old trigger already compared `price <= stop`; stale exit references, bounded retries/collars, cancel reconciliation, and worker delays caused the failed liquidation sequence.
- **No additional orders appeared after the BE submission at 15:54 UTC.** The completed session contains 27 newly prepared orders: 13 FILLED, 12 CANCELLED, and 2 REJECTED. The eighth filled buy was BE. Eight buys and five sells filled; no canceled order in this session has a recorded fill.
- **Five positions remain open, with full stop quantity configured.** The post-close broker and cards agree, and there are no working orders, nonterminal execution orders, unresolved order-recovery states, or active capital reservations.
- **Buying-power validation, account scheduling, and occasional long execution cycles still need fixes.** AVAT/ZS were rejected for insufficient actual broker orderability; entry freshness at 15 seconds does not repair funding or stop-processing delays.
- **Newly confirmed from the application logs:** five unhandled Qt callback exceptions caused by `sys.stderr is None`; 12 optimistic card-version conflicts before the final deployment; continuing stale buying-power warnings; slow UI renders; and shadow interceptions mislabeled as live entry errors. The final-minute feed also rejected 185 timestamp-skew events.
- **Gate 2, Gate 3, and Gate 4 remain NOT_CERTIFIED.** All ten reviewed original journal files passed integrity validation. Integrity and clean collector shutdown do not establish the missing qualification scenarios.

No product code, trading control, position, or broker order was changed by this review. A read-only final-release post-session audit was generated today because the scheduled audit still targeted the earlier release.

## Post-close broker and card reconciliation

The deployed PC remains on approved clean release `32866eae800168b794811220b50ef84a457d1a26`. The collected PC row is ACTIVE, with executor, account reconciliation, command consumer, and market-data readiness true. The separate laptop FAILED row is dated October 4; it is not a new failure of the PC owner. Shared live control remains ON at revision 56; regular-session rules still govern new entries.

The broker read returned complete holdings, open-order, history, and reserved-order data, with no errors. No broker working orders were found.

| Symbol | Broker / card shares | Average entry USD | Post-close broker price USD | Active stop USD | Stop shares | Stop type |
|---|---:|---:|---:|---:|---:|---|
| ALAB | 8 / 8 | 385.73 | 389.8 | 374.635 | 8 | ORB_LOW |
| BE | 10 / 10 | 297.85 | 295.78 | 285.64 | 10 | ORB_LOW |
| CYPH | 897 / 897 | 3.31 | 3.26 | 3.09 | 897 | ORB_LOW |
| SIMO | 10 / 10 | 289.99 | 284.96 | 281.51 | 10 | ORB_LOW |
| VNCE | 184 / 184 | 11.86 | 12.24 | 11.88 | 184 | BREAKEVEN |

All five observed prices exceed their active stops. None of these cards has `exit_all_required` latched. These are app-managed stop settings; stop coverage in a card does not prove a native broker stop order exists.

TWLO, MRNA, SVIA, and BAND have CLOSED cards and zero broker shares. AVAT and ZS have zero shares following rejected buys. The 28-record audit also contains an **EFOR order prepared October 5** and reconciled October 6; exclude it from the 27 new-session order count. Including that older record produces 13 canceled records instead of 12.

There are 42 durable execution commands: **37 ACKNOWLEDGED, 3 FAILED, and 2 AMBIGUOUS**. Order exposure is reconciled, but the two ambiguous cancel commands require finalization. The one recorded mobile/web operator command, VNCE SELL_PARTIAL, is COMPLETED; this operator status records request handling, not the broker fill itself.

## Completed session sequence

Prices below are submitted limit prices. Times are local submission-start timestamps from the durable order ledger, not claimed exchange fill timestamps. A FILLED row includes the final average fill price; cancel/reject rows have zero recorded fill.

| UTC | Symbol | Side / intent | Shares | Limit USD | Final outcome |
|---|---|---|---:|---:|---|
| 13:31:59 | TWLO | BUY / ENTRY | 12 | 304.63 | FILLED; filled 12 at average $304.58 |
| 13:33:25 | BAND | BUY / ENTRY | 38 | 64.49 | FILLED; filled 38 at average $64.49 |
| 13:34:07 | VNCE | BUY / ENTRY | 276 | 11.86 | FILLED; filled 276 at average $11.86 |
| 13:36:27 | MRNA | BUY / ENTRY | 13 | 204.42 | CANCELLED; filled 0 |
| 13:37:50 | ALAB | BUY / ENTRY | 8 | 385.73 | FILLED; filled 8 at average $385.73 |
| 13:41:21 | TWLO | SELL / MANUAL_EXIT | 12 | 302.9974 | CANCELLED; filled 0 |
| 13:42:43 | TWLO | SELL / MANUAL_EXIT | 12 | 301.4748 | CANCELLED; filled 0 |
| 13:43:48 | TWLO | SELL / MANUAL_EXIT | 12 | 299.9522 | FILLED; filled 12 at average $300.73 |
| 13:44:51 | MRNA | BUY / ENTRY | 16 | 208.85 | FILLED; filled 16 at average $208.85 |
| 13:46:26 | SVIA | SELL / MANUAL_EXIT | 643 | 4.392925 | CANCELLED; filled 0 |
| 13:47:02 | SVIA | SELL / MANUAL_EXIT | 643 | 4.37085 | CANCELLED; filled 0 |
| 13:48:03 | SVIA | SELL / MANUAL_EXIT | 643 | 4.348775 | CANCELLED; filled 0 |
| 13:50:46 | MRNA | SELL / MANUAL_EXIT | 16 | 210.94 | CANCELLED; filled 0 |
| 13:51:27 | MRNA | SELL / MANUAL_EXIT | 16 | 209.88 | CANCELLED; filled 0 |
| 13:52:34 | MRNA | SELL / MANUAL_EXIT | 16 | 208.82 | CANCELLED; filled 0 |
| 14:03:02 | MRNA | SELL / STOP_LOSS | 16 | 193.11 | FILLED; filled 16 at average $197.165 |
| 14:03:06 | SVIA | SELL / STOP_LOSS | 643 | 4.15 | FILLED; filled 643 at average $4.2064 |
| 14:03:16 | BAND | SELL / MANUAL_EXIT | 38 | 64.1775 | CANCELLED; filled 0 |
| 14:03:46 | BAND | SELL / MANUAL_EXIT | 38 | 63.855 | CANCELLED; filled 0 |
| 14:04:44 | BAND | SELL / MANUAL_EXIT | 38 | 63.5325 | CANCELLED; filled 0 |
| 14:13:17 | BAND | SELL / STOP_LOSS | 38 | 61.47 | FILLED; filled 38 at average $62.75 |
| 14:13:47 | SIMO | BUY / ENTRY | 10 | 289.99 | FILLED; filled 10 at average $289.99 |
| 14:53:13 | CYPH | BUY / ENTRY | 897 | 3.31 | FILLED; filled 897 at average $3.31 |
| 15:02:31 | AVAT | BUY / ENTRY | 1444 | 2 | REJECTED; filled 0; APBK0952 (insufficient orderable funds) |
| 15:18:51 | ZS | BUY / ENTRY | 14 | 211.85 | REJECTED; filled 0; APBK0952 (insufficient orderable funds) |
| 15:28:23 | VNCE | SELL / PARTIAL_EXIT | 92 | 11.741 | FILLED; filled 92 at average $12.78 |
| 15:54:21 | BE | BUY / ENTRY | 10 | 297.85 | FILLED; filled 10 at average $297.85 |

### MRNA cancellation and resubmission

The initial 13-share buy at $204.42 was canceled with zero fill during a 1-minute to 5-minute ORB replacement. Cancellation was confirmed, but the replacement was aborted because its passive quote changed. **That replacement did not submit another buy.** Subsequent proposals were blocked by the 100% gross-notional limit. After the user re-added MRNA at 13:41:17, a separate 16-share 5-minute plan submitted at 13:44:51 and filled. The abandoned replacement command 90010 was explicitly finalized FAILED / REPLACEMENT_ABORTED_AFTER_CANCEL at 14:17:50.

MRNA, SVIA, and BAND then each exhausted three nonfilling manual sell attempts. Supervised guarded STOP_LOSS recovery filled MRNA/SVIA around 14:03 and BAND around 14:13. These recovery orders preceded the permanent hard-stop deployment at 14:27; the later deployment must not be credited with causing earlier fills. TWLO had already filled its third manual exit.

### VNCE partial sale

The 92-share sale was a **manual mobile/web partial-sale request at 15:28:03**, acknowledged as handled around 15:28:09. The broker-bound order submitted at 15:28:23 and filled at average $12.78. The remaining 184 shares have a $11.88 breakeven stop. This was not an automatic partial-profit trigger.

### End-of-session behavior

After the earlier 16:12 UTC checkpoint, the daily history contains six additional card snapshots, all for RNG/SDGR around 19:59-20:00. No additional canonical broker order was prepared. Both cards returned from BUY_TODAY to BUYLIST at 20:00:01-20:00:02 with ?Regular session is complete.? The configured final-minute entry cleanup begins 60 seconds before close; do not count that cleanup window as an unauthorized missed entry.

The final release's collector wrote `COLLECTOR_ENDED` and `SESSION_ENDED` at 20:00:10, with zero dropped batches and zero recorded collector errors. The daily ledger has **246 events: 223 CARD_SNAPSHOT and 23 BUY_TODAY_ADDED**. It does not itself contain a complete explicit fill/cancel/stop-event trail.

### Did the remaining holdings cross their stops later?

In the final release's captured, evaluated KIS quotes from 15:38 through the close, no recorded last price for these held symbols was at or below the configured stop. BE is checked only after its filled position was observed at 15:55:07. The minima below include the short post-close collection tail through 20:00:10.

| Symbol | Stop USD | Lowest captured last USD | Lowest captured positive bid USD | Captured last-price breaches |
|---|---:|---:|---:|---:|
| ALAB | 374.635 | 383.45 | 383.15 | 0 |
| BE | 285.64 | 295.4 | 288 | 0 |
| CYPH | 3.09 | 3.18 | 3.18 | 0 |
| SIMO | 281.51 | 283.07 | 283.05 | 0 |
| VNCE | 11.88 | 12.01 | 12 | 0 |

This finding covers retained accepted/evaluated observations. Rejected packets, coalesced events, and intervals without a usable event are not a complete market tick tape. It does not certify the stop behavior under a future gap or missing feed.

## Fixes already deployed or reconciled

| Issue | Implemented change | Confirmed result / remaining limit |
|---|---|---|
| Closed-hours outage handling and stale reconciliation contradictions | Preopen `de226e59` repair respects session boundaries and terminal order/broker evidence. | Earlier EFOR/SVIA reconciliation spam stopped after the repair; their old incident records still remain to reconcile. |
| Stop exit stuck below a stale reference or after a small retry limit | `488c0694` uses current bid/trade/holdings fallbacks and a durable full-exit latch; liquidation can continue beyond the old cap. | MRNA/SVIA/BAND recovered and all four requested symbols are flat. Continuous low-latency stop execution is still unqualified. |
| Repeated cancels and slow cancel confirmation | Exact pending-cancel adoption and faster pending-exit reconciliation. | Four cancel-timeout incidents RESOLVED; two separate cancel/fill command outcomes still AMBIGUOUS. |
| Broker account reads blocking quote decisions | `32866eae` moves bounded read-only account work into a background reader; validates generation/age before owner-thread application. | Routine queue latency improved; funding snapshots still expired in some later cycles. |
| Repeated unchanged-card persistence and full cache reloads | Persist semantic changes and acknowledge known own writes safely. | Routine cycles shortened. Earlier version conflicts require focused concurrency validation; synchronous execution paths still have long tails. |
| Two-second entry freshness requirement rejecting processing waits | User-approved **15-second total age** for KIS trade and bid/ask, including processing wait and a submit-boundary recheck. | Deployed. It does not relax timestamp validity, current-session provenance, or protective-exit policies. |
| JSON temporary files invalidating the approved clean release | Narrow PC `.git/info/exclude` pattern `/data/.*.json.*.tmp`. | Verified local workaround; source-controlled portability work remains. |
| Abandoned MRNA replacement command | Finalized the exact existing command after zero-fill/no-replacement proof. | FAILED/aborted is now durable; no pending replacement exposure. |

The final release passed all five hosted CI jobs: Python 3.12 **3,464 passed / 29 skipped**, Python 3.11 passed, dependency audit passed, repository hygiene passed, and Gate 1 passed. [Final-release CI](https://github.com/cafe-auvers/quant_app/actions/runs/37486745111). The stop release also passed its [CI run](https://github.com/cafe-auvers/quant_app/actions/runs/37477233968). These are existing release checks, not a substitute for full-session Gate 2/3/4 evidence.

## Remaining work, ordered by impact

| Priority | Issue and evidence | Required resolution | Status |
|---|---|---|---|
| P1 | **Actual broker buying power:** AVAT $2,888 and ZS $2,965.90 were rejected APBK0952. Later KIS ZS orderability was $915.31 / 4 shares while the app estimated $1,409.85. | Use fresh account/symbol/exchange/price-specific KIS orderability, reservations and concurrent-order/fee handling; keep equity separate from spendable funds. Log the values at the attempted order. | OPEN; no blind resubmission. |
| P1 | **Funding/reconciliation scheduling:** 802 stale buying-power warnings in the reviewed log window; 48 after final live re-enable, reaching 51.8 seconds of snapshot age. A separate full-reconciliation readiness lapse was recorded at 16:12:03, recovering by 16:12:08. | Prioritize full reconciliation and timely funding reads; instrument request start/completion/discard/application. Preserve real freshness deadlines. | OPEN; background work reduced blocking but did not eliminate deadline misses. |
| P1 | **Residual worker delays:** final segment maximum total cycle 8.562 seconds; 8.531 seconds in decisions/heartbeat/persistence; engine queue maximum 12.255 seconds. | Instrument guarded submit/cancel, database and persistence durations; move blocking work away from protective evaluation while preserving ownership/idempotency and atomic risk checks. Verify a slow unrelated order cannot delay a stop. | OPEN. |
| P1 | **Qt callback exception:** five CRITICAL unhandled `AttributeError` records, including market close, because `_qt_message_handler` writes to absent `sys.stderr`. | Use a safe logging sink/fallback under `pythonw`; verify the Qt callback when stderr is absent and when writing fails. | NEW / OPEN in deployed `main.py`. Later records show continuation, not proof of five application terminations. |
| P1 investigation | **Last-minute timestamp skew:** 184 TRADE and 1 QUOTE rejections accumulated at 19:59-20:00; all seven subscribed symbols were stale in sampled final-minute health checks despite ACKed channels. | Capture rejected broker/receive/parse timestamps per channel and host clock-offset evidence; distinguish delayed source packets, receiver delay and clock health. Keep invalid events rejected and test protective fallback. | NEW / cause not yet established. Most rejections are absent from accepted-event latency statistics. |
| P2 | **Card-version conflicts:** 12 `TradeCardVersionConflictError` stacks; latest at 15:18:16 before final deployment. | Reload/merge authoritative state and retry only safe state updates after a conflict, without repeating a broker mutation or dropping operator changes. Validate overlapping ORB/operator/runtime writes. | Historical fault confirmed; no recurrence found after `32866eae` in retained logs. Resolution not formally proven. |
| P2 | **Cancel/fill command races:** TWLO command 90018 and VNCE 90046 still AMBIGUOUS with APBK0124 after their exact orders FILLED. | Reconcile command outcomes from exact filled-order history; preserve ?cancel lost to fill? rather than asserting a successful cancellation. | OPEN; no nonterminal order exposure at checkpoint. |
| P2 | **Incorrect diagnostic outcomes:** observer calls definitive AVAT/ZS APBK0952 rejections AMBIGUOUS; three expected `ShadowMutationIntercepted` exceptions are logged as entry errors (TWLO/SDGR/BE). | Separate retry eligibility from definitive rejection classification; give intercepted shadow outcomes a distinct diagnostic level and mode. | OPEN. BE's real buy is FILLED, not failed. |
| P2 | **Incomplete lifecycle/protection audit:** final collector has only startup comparison/protection events plus BE dispatch/ACK; no complete normal lifecycle closure. Daily snapshots omit active stop/coverage/latch fields; legacy event journal has not changed since August 20. | Emit durable reconciliation/fill/cancel/close/protection outcomes and authoritative funding/quote/timestamp provenance with exact command correlation. | OPEN; canonical ledger plus broker read currently supplies the stronger outcome evidence. |
| P2 | **Historical alert redelivery:** 8 older incidents remain OPEN, 5 RESOLVED; 35 DELIVERED attempts in the reviewed October 6-onward capture versus 19 deliveries recorded before passive collectors stopped. | Verify current truth for each historical incident and record supported resolution/acknowledgment. Keep old reminders from obscuring new stop alerts. | OPEN; deliveries are not 35 new incidents. |
| P2 | **UI render stalls:** 39 slow-render warnings; 9.985-second worst render before final fix, 2.142-second worst warning after final live re-enable. | Profile/reduce main-thread projection/chart/row work; verify operator responsiveness separately from execution-worker latency. | OPEN; UI durations do not by themselves prove the worker blocked. |
| P2 | **Symbol and waiting messages:** obsolete BRR waits on ORB; RNG/SDGR used `EXECUTION_LEVEL_ALREADY_REACHED` while prices were below entry levels; end-minute data-unavailable reasons conflate several feed conditions. | Validate/map renamed symbols explicitly; report missing timestamp, channel/clock state, age and breakout condition separately. | OPEN; retain breakout/session rules and avoid implicit re-entry on a rename. |
| P2 | **Ignore-policy portability:** temporary JSON fix exists only in PC Git metadata. | Add the narrow generated-file rule to source control; verify actual source edits still invalidate release approval. | OPEN / local workaround only. |
| Qualification | **Scheduled audit targets superseded `de226e59` evidence.** Task returned exit code 0 but output says INCOMPLETE_PASSIVE_COLLECTION / NOT_CERTIFIED. | Bind each audit task to the actual release/session; finalize each segment honestly and surface semantic audit outcome, not just task exit success. | NEW; manually audited `32866eae` today. Scheduler remains unchanged. |

The later $915.31 broker buying-power result is not backdated to the original ZS rejection. The exact orderable amount at the original AVAT/ZS calls was not retained. The relevant broker query is the [official KIS overseas orderability inquiry](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/inquire_psamount/inquire_psamount.py).

BRR changed to SVIA effective September 22 according to the [issuer announcement](https://www.nasdaq.com/press-release/silvia-inc-begins-trading-nasdaq-under-new-ticker-svia-2026-09-22). Preserve historical order identity and the existing SVIA CLOSED state when implementing symbol validation.

The funding-cache 15-second age is **separate from the 15-second entry-event rule** and the full-account reconciliation deadline. Raising event freshness does not make funding fresh. Twenty-seven application exceptions were valid gross-notional risk rejections; those limits should remain enforced. Expected shadow interception is also not a real broker API failure.

## Gate 2, 3 and 4 evidence results

**Result for the final deployed release: Gate 2 NOT_CERTIFIED; Gate 3 NOT_CERTIFIED; Gate 4 NOT_CERTIFIED.** The exact-release review basis already said this before deployment, and the completed evidence still does not satisfy the formal validators. No certificate, approval, upstream review, or missing probe was fabricated by this audit.

### Collection and integrity

Original PC files were audited with `AppendOnlyEvidenceJournal.audit()` before sanitizing the summaries. Every reviewed journal has zero malformed rows, duplicate event IDs, hash-chain errors, commit/gate identity mismatches, and unknown event types. Five folders / ten journals passed this integrity check. The `odd_orb_fix` files include earlier-session history and two start/end pairs; they are not an additional full October 6 session.

| Release | Collector UTC window | Last recorded state | Live / shadow journal rows | Accepted evaluated quotes | Full regular session flag |
|---|---|---|---:|---:|---|
| `04bffd4f` | 05:57:53 to 12:28:30 | RUNNING | 1,958 / 67 | 65 | False |
| `de226e59` | 12:29:26 to 14:27:08 | RUNNING | 629 / 12,002 | 11,998 | False |
| `488c0694` | 14:27:48 to 15:38:17 | RUNNING | 355 / 12,810 | 12,790 | False |
| `32866eae` | 15:38:52 to 20:00:10 | ENDED | 1,478 / 88,403 | 88,399 | False |

The earlier RUNNING states are stale report files left by deployment restarts, not proof that three old collectors are still running. Their journals have no corresponding final end markers; do not insert synthetic ones. The final release started **2 hours 8 minutes after the open**, so its clean finish cannot establish a full regular session. Joining different releases does not qualify one exact release.

The current segment contains 1,447 feed samples, 88,399 REAL_QUOTE_EVALUATED records, one observed ENTRY_ALLOWED branch, and a clean end. Its counters report zero observer drops/errors. This review generated a [post-session audit of 32866eae](../artifacts/trading_session_review_20261007/post_session_audit_32866eae.json): **INCOMPLETE_PASSIVE_COLLECTION / NOT_CERTIFIED**, because full-session coverage is false, despite both journals passing integrity and the collector ending.

### Gate 2: feed quality and continuity

Final-release accepted-event **regular-session** measurements are below. Runtime-wide values are separate; these are not full-day metrics across releases. Receive-lag statistics exclude rejected packets; queue statistics cover unique coalesced engine-drain observations, excluding historical stop replay.

| Measurement | Samples | p50 ms | p95 ms | p99 ms | Maximum ms |
|---|---:|---:|---:|---:|---:|
| Receive lag | 81,906 | 793 | 1,650 | 2,401 | 4,979.266 |
| Parsing | 81,906 | 0 | 0 | 2 | 16.181 |
| Engine queue | 65,876 | 604 | 1,609 | 2,122 | 12,255.049 |

The observed receive p95/p99 fall within the reporter's 2,000/3,500 ms limits for this segment. At final sampling there were zero reconnects, NACKs, parser failures, malformed frames, duplicates and dropped feed events. Subscription capacity peaked at 19 of 41 occupied slots; it was 11 occupied / 30 available after RNG/SDGR cleanup. Capacity exhaustion does not explain the long delays.

**Continuity remains unqualified.** Stale symbols were common under the stricter health age policy: for example VNCE appeared stale in 1,421 of 1,447 samples and CYPH in 1,360. That metric combines trade/quote ages and channel/clock state, uses the general execution-health policy, and is not the 15-second entry predicate. It cannot by itself identify a missed entry or prove a broken connection. The all-symbol final-minute staleness and 185 skew rejections merit timestamp/fallback investigation.

Missing: a standalone read-only full-session run, independently recorded host clock discipline, controlled reconnect/replay and silent-channel probes, formal continuity validation, and the reviewed exact-release result. An actively trading passive observer is not the mutation-free Gate-2 runner.

### Gate 3: shadow behavior

The isolated shadow used real KIS quotes and the production decision composition. In the final release it emitted **one WOULD_SUBMIT for BE**. It emitted **zero WOULD_CANCEL, WOULD_REPLACE and WOULD_SELL**; the prior de226 segment emitted two WOULD_SUBMIT events, and the 488 segment emitted none.

Only **ENTRY_ALLOWED** of the eight required branches was observed in the final segment. There are **zero SAFETY_FENCE_PROBE, ORACLE_COMPARISON, CAPTURED_LIVE_REPLAY_STARTED/ENDED and PRODUCTION_LEDGER_SNAPSHOT records** in that segment. Shadow isolation and a clean end are useful evidence, but they leave missing cancellation/protective-exit branches, stale-data/lease/ownership/reconciliation/kill-switch fences, captured replay, oracle agreement, unchanged-production-ledger proof, full-session coverage and a reviewed Gate-2 chain.

The BE shadow exception logged at 15:54:27 is the expected final-boundary interception. It does not invalidate the real BE broker fill, nor does it qualify the missing branches.

### Gate 4: real controlled-live sequence

The final passive segment records **1 RUNTIME_ACTIVE, 1 ENTRY_CANDIDATE, 1 MUTATION_DISPATCHED, 1 MUTATION_TERMINAL, 5 LIFECYCLE_COMPARISON and 20 POSITION_PROTECTED** events. BE's candidate was $2,978.50, below its recorded $15,801.86 effective equity-based cap, with an active card and atomic risk-recheck fields. Its dispatch records owned=true, duplicate=false, automatic_retry=false. Its terminal observation is **BROKER_ACCEPTED**, with broker_confirmed_terminal=false; final FILLED status comes from the canonical ledger and broker truth, not from treating that ACK as a fill.

The final segment has no MANUAL_ARM, DISARM_PROBE, FINAL_RECONCILIATION or EXTERNAL_ALERT_DELIVERED event. The live-control audit independently records the 15:40:12 ON transition after ACTIVE readiness, and earlier passive segments contain manual-arm/alert observations. Those records are not a complete, formally reviewed Gate-4 journal for the final release. Startup comparison/protection records also do not supply all later fill/close outcomes.

Missing: reviewed Gate-3 chain, three qualifying supervised session dates, verified start-disarmed/manual-arm sequence, complete lifecycle/capability/alert/final-reconciliation evidence, and a real disarm probe blocking the next mutation. Today's separate post-close matching broker read is useful operational evidence, but it was not a FINAL_RECONCILIATION event inside a qualifying Gate-4 session before its end marker.

### Overnight audit task

`QuantApp_PassiveGateAudit_de226e59_20261006` ran at the planned October 7 05:15 KST time and Task Scheduler recorded result **0**. Its [actual output](../artifacts/trading_session_review_20261007/scheduled_old_release_audit.json) is **INCOMPLETE_PASSIVE_COLLECTION / NOT_CERTIFIED**, generated at 20:15:03 UTC, bound to `de226e59` and its pre-restart segment. Task execution succeeded; qualification did not. There was no latest-release post-session audit artifact in the current collector folder before this review.

The new read-only audit today uses the actual `32866eae` segment and reaches the same qualification status for the correct reason: late start and missing qualification evidence. It changes no release, arm state, broker state, or evidence journal.

## Evidence, validation and limits

| Evidence | What it supports |
|---|---|
| [Final session capture](../artifacts/trading_session_review_20261007/trading_session_audit.json) | Canonical cards, 28 reviewed order records, 42 commands, 246 daily events, operator/control/alert records, post-close broker truth, and original journal integrity results. |
| [Final-release post-session audit](../artifacts/trading_session_review_20261007/post_session_audit_32866eae.json) | Original journal hashes/counts, clean end, false full-session coverage, and explicit NOT_CERTIFIED result. |
| [Earlier release scheduled audit](../artifacts/trading_session_review_20261007/scheduled_old_release_audit.json) | Successful scheduled execution with an incomplete older collection. |
| [Late-session analysis](../artifacts/trading_session_review_20261007/late_session_analysis.json) | Accepted/evaluated quote minima, observed branch count, health/staleness samples and close-time channel cleanup. |
| [Application log audit](../artifacts/trading_session_review_20261007/application_log_audit.json) | Rotating-log coverage and grouped warnings/errors. |
| [Sanitized application error details](../artifacts/trading_session_review_20261007/application_error_details.json) | Exact exception stacks, callback fault, version conflicts, risk/shadow classification, slow-render records and buying-power age warnings. |
| [Gate follow-up](../artifacts/trading_session_review_20261007/gate_followup.json) | Exact-release review basis, earlier gate diagnostic, skew-rejection counter progression and final-minute metrics. |
| [ZS rejection diagnosis](../artifacts/nonblocking_quotes_20261006/zs_broker_rejection_diagnosis.json) | APBK0952 and the later broker orderability comparison. |
| [Intraday review](trading_session_review_2026-10-06.md) | Earlier detailed recovery/release chronology and before/after performance evidence. |

The application-log scan covered 3,002 timestamped records from October 6 18:00 KST through October 7 05:00:09 KST: **1,622 INFO, 1,092 WARNING, 283 ERROR and 5 CRITICAL**. Error-level lines include valid risk rejections and isolated shadow events; they are not 288 distinct live failures. Repeated historical EFOR/SVIA preopen faults explain a substantial part of the earlier volume. Continuation traceback lines are retained in the sanitized error artifact.

Validation for this review checked original journal integrity; reconciled every post-close broker holding quantity to its card; verified no working/nonterminal/recovery/reservation exposure; separated the older EFOR record; and checked report counts against the captured ledger. No new runtime test suite was run for this documentation-only task. Exchange fill timestamps, rejected-event payload timings, and per-operation blocking durations were not fully retained; those limits remain explicit. Financial prices in the holdings table are the broker's post-close checkpoint values, not independently certified closing prices.

Recommended next implementation sequence: fix the Qt callback; correct broker funds and refresh scheduling; investigate final-minute skew and stop fallback; remove residual blocking work; reconcile command/status/alert telemetry; verify card-conflict handling and UI responsiveness; then collect and review complete exact-release Gate 2/3/4 evidence.


## Root cause remediation deployed — 7 October 2026

Release `58c797c04ca3aa5d8423ee81174fb8f366d81737` is deployed and ACTIVE on the PC. The identified processing, ingress-age, protective scheduling, exact orderability, replacement cash, persistence, UI/logging and daily-evidence causes were corrected. All five hosted checks passed; Gate 1 passed. Five broker positions and their full stops were preserved, and no maintenance broker orders were submitted. The US 7 October collector is running; Gates 2/3/4 remain NOT_CERTIFIED. See the [root-cause fixes and verified deployment report](trading_root_cause_fixes_2026-10-07.md) for the issue-by-issue corrections, tests, operational proof and remaining qualification/ticker requirements.
