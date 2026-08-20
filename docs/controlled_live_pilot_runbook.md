# Supervised Controlled-Live Pilot

This runbook defines an optional, explicitly approved supervised pilot of the
final Kanban runtime. It is not Gate 2, does not mark Gate 2 passed, and does
not authorize unattended operation. Repository defaults remain disabled.

The pilot and later full-live mode use the same WebSocket, runtime, gateway,
broker, database, reconciliation, stop, and handoff code. Promotion changes
only the risk-envelope configuration.

## Evidence required before configuration

- A Gate-1 report must pass on the exact deployed commit after every code
  change.
- A reviewed external capability bundle must verify the `HDFSCNT0` and
  `HDFSASP0` timestamp interpretations plus each channel's sequence/reset
  semantics. Normal production composition pins the manifest path, its SHA-256
  digest, environment, and exact runtime commit.
- Execution-notice verification is optional for this supervised pilot because
  REST broker reconciliation remains authoritative. An unverified notice
  channel is not subscribed. It remains mandatory for the full Gate-2 report.
- Trade and quote subscription keys, aggregate capacity, buying power, startup
  reconciliation, lease ownership, and external alert delivery must be
  verified for the pilot account and symbols.
- The PC is the sole KIS WebSocket owner. The laptop remains pull-only and does
  not open a second session with the app key.

## Fail-closed controlled-live configuration

The values below are placeholders, not activation instructions or evidence:

```text
KIS_CAPABILITY_MANIFEST_PATH=<external reviewed bundle>
KIS_CAPABILITY_MANIFEST_SHA256=<reviewed manifest digest>
KIS_RUNTIME_COMMIT_SHA=<exact deployed 40-character commit>

KIS_LIVE_EXECUTION_MODE=CONTROLLED_LIVE
KIS_CONTROLLED_LIVE_SYMBOLS=<one or two approved symbols>
KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL=<minimum practical per-entry cap>

KIS_MUTATION_BUDGET_VERIFIED=true
KIS_SUBMIT_MUTATION_CAPACITY=<reviewed conservative policy>
KIS_CANCEL_MUTATION_CAPACITY=<reviewed conservative policy>
KIS_REPLACE_MUTATION_CAPACITY=<reviewed conservative policy>
KIS_MUTATION_BUDGET_WINDOW_SECONDS=1
KIS_MUTATION_MIN_SPACING_SECONDS=0.2
KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS=1

KIS_MARKET_DATA_MODE=WEBSOCKET
KIS_WS_ENABLED=true
KIS_WS_PROTOCOL_VERIFIED=true
BUYBOARD_ENGINE_ENABLED=true
TRADING_ENABLED=true
```

`TRADING_ENABLED=true` only permits the in-app session toggle. Trading still
starts disarmed after every process launch. `CONTROLLED_LIVE` additionally
blocks production BUYs outside the allowlist or above the per-entry cap.
Protective SELLs are not blocked by the entry cap. The shared scheduler
enforces process-wide spacing across endpoints and performs one mutation
attempt only. The low-level KIS token-expiry branches also do not repeat a
mutation in controlled-live mode; a later action must come from the durable
workflow after reconciliation, never an inline retry.

Missing, malformed, unreviewed, or mismatched values prevent the active
runtime or WebSocket service from composing.

## Supervised sequence

1. Start with the in-process trading switch off.
2. Require `ACTIVE` runtime, current lease, writable database, completed and
   fresh reconciliation, connected WebSocket, ACKed trade/quote channels,
   fresh quotes, healthy accumulator, fresh buying power, and operating
   external alerts.
3. Manually arm the session switch only after every readiness item is green.
4. Use normal strategy decisions; do not force an entry merely to exercise the
   broker.
5. Compare every submit, partial fill, fill, cancel, Partial Sell, Sell All,
   position quantity, stop, and card projection against KIS truth.
6. Disarm the session switch and prove another mutation cannot cross the
   broker boundary.
7. Stop on any ambiguity, stale feed, reconciliation mismatch, lease loss,
   alert failure, or duplicate identity. Reconcile forward; never retry an
   uncertain mutation.

`FULL_LIVE` releases the entry allowlist/notional envelope only after an
explicit operational promotion decision. Unattended operation still requires
a complete-session live feed/reconnect/reconciliation/alert record and an
independent external heartbeat watchdog.
