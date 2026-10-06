# Live feed and engine latency diagnostics

The passive Gate 2/3/4 collector keeps `formal_gate_result=NOT_CERTIFIED`.
Diagnostics alongside trading do not replace a complete read-only Gate 2 run,
Gate 3 branch/fence replay, independent review, or the required supervised
Gate 4 dates.

## What the October 5 evidence showed

The final PC segment reported receive-lag p99 of 3,902 ms, above Gate 2's
unchanged strict 3,500 ms limit. The captured, coalesced observations put almost
all arrivals above that limit in trade messages during the final two minutes.
Quote messages in that segment remained below 2,309 ms. This points to delayed
trade delivery before parsing; it does not establish a defect in KIS's internal
infrastructure or guarantee that another session will meet the limit.

The same journal exposed a separate local measurement bug: the old
`protocol.queue_lag_*` values were recorded at parsing time. They missed the
later wait until the engine drained the accumulator. Unique captured
observations in the final segment had an engine-drain p99 of 8,266 ms. Those
observations include older extrema retained by coalescing, so their count and
distribution differ from the complete ingress histogram.

## Timing fields

`gate2.feed_samples.protocol` retains cumulative receive-lag statistics. Its
`queue_lag_*` fields now measure receive-to-engine-drain delay.
`parser_lag_*` retains the earlier receive-to-parser measurement explicitly.
Historical parser-based queue numbers are not comparable to the corrected
engine-drain numbers.

`gate2.latency.runtime` includes premarket observations.
`gate2.latency.regular_session` contains only accepted events whose broker
timestamp belongs to a regular session and identifies that New York session
date. Each contains `receive`, `parsing`, and `engine_queue` statistics plus
separate `HDFSCNT0` trade and `HDFSASP0` quote channel statistics. A regular
event that arrives shortly after the close remains in its broker session's
statistics. The live collector does not reset a session's slow tail.

Each series provides `sample_count`, `p50_ms`, `p95_ms`, `p99_ms`, and `max_ms`.
Receive/parser counts cover accepted ingress events; queue counts cover unique
coalesced observations at drain time. Duplicate combined deliveries and
historical protective-stop replays do not create additional queue samples.
Timestamp, sequence, deduplication, skew rejection, and execution freshness
checks remain enforced.

`runtime_cycles` records the latest and maximum duration of canonical loading,
operator commands, account refresh/reconciliation, ORB planning, subscription
capture, stop handoff/draining, decisions/heartbeat/persistence, and the whole
cycle. It helps distinguish blocking local work from slow arrivals.

## Runtime improvement and limits

The engine uses the configured heartbeat as a cycle budget. An 800 ms cycle
with a one-second heartbeat waits approximately 200 ms, instead of adding a
new one-second wait. A cycle already over budget resumes after the minimum
one-millisecond wait. Reconciliation, persistence, lease checks, risk checks,
and protective-stop handling still run in their original order.

This removes avoidable scheduling delay. Synchronous broker/database work can
still delay a cycle, and upstream trade delivery cannot be made faster by
changing a local percentile calculation. Inspect the new phase timings and
per-channel tails after genuine collection before claiming a performance
improvement or a formal gate pass. No timestamp adjustment, stale-feed entry,
test order, or live fault injection is authorized by these diagnostics.
