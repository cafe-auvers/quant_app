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
blocks production BUYs without an exact active canonical Trade Card or above
the per-entry cap.
Protective SELLs are not blocked by the entry cap. The shared scheduler
enforces process-wide spacing across endpoints and performs one mutation
attempt only. The low-level KIS token-expiry branches also do not repeat a
mutation in controlled-live mode; a later action must come from the durable
workflow after reconciliation, never an inline retry.

Missing, malformed, unreviewed, or mismatched values prevent the active
runtime or WebSocket service from composing.

### What `KIS_RUNTIME_COMMIT_SHA` means

This value is the complete 40-character Git identity of the exact checkout
that will run `main.py`. On each deployment device, from the repository root:

```powershell
git status --short
git rev-parse HEAD
```

The first command must print nothing. Copy the second command's full output
into `KIS_RUNTIME_COMMIT_SHA` in local, gitignored
`config/runtime.local.json`. Do not use a
short hash, pull-request number, branch name, manifest digest, or the SHA of an
older reviewed build.

The runtime SHA must agree with the current Git `HEAD`, the reviewed
capability manifest's commit, and the exact-head Gate-1 evidence. Any new
commit changes `HEAD`—including a documentation-only merge—and invalidates an
older exact-commit approval until a new manifest/evidence bundle is generated
and independently approved. This is deliberate: the app must never silently
reinterpret evidence created for a different checkout.

After setting the reviewed values, run the read-only preflight:

```powershell
python scripts/check_controlled_live_readiness.py
```

Changing runtime configuration to make this check green is not a substitute
for producing matching reviewed evidence. Never commit
`config/runtime.local.json` or an unredacted capability bundle.

The shared operational Trade Card database is the symbol boundary; symbols
never belong in `.env`. `data/trade_cards.json` is only a local recovery
snapshot and cannot authorize a broker order. Deliberately placing a reviewed
card in Buy Today authorizes that exact environment/account/symbol for
controlled-live entry. Entry Pending remains authorized while its durable
order is tracked, and an Open Position may buy only the recorded remainder of
an `ENTRY_COMPLETING` partial entry. Ordinary open positions and any exit in
progress do not authorize additional buying. A planned entry above
`KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL` remains blocked at the final broker
adapter. The dashboard header displays the mode, active cards, and cap; verify
that full text rather than relying on "Live Trading: Enabled."
Promotion to `FULL_LIVE` is a separate financial-scope decision and must not
be inferred from an ownership or Operator Control change.

The runtime prioritizes WebSocket capacity among symbols already inside this
active-card set: `EXECUTE_READY` first, then armed/waiting-breakout, then
still-forming Buy Today plans. This priority cannot
replace a missing live-verified symbol subscription key. A card whose 1m, 5m,
and 30m plans are all terminal-invalid returns to Buylist and releases its Buy
Today feed capacity.

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
