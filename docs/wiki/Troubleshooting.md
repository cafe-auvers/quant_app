# Troubleshooting

## Mobile board sync fails or returns 502

The current mobile API and executor are both on the PC. Confirm the PC is awake and signed
in, phone Tailscale is connected, and `QuantApp_WebDashboard` is running. Check the loopback
web server on port 8080, Tailscale Serve, and `data/logs/web_service.log`. A cached page can
remain visible after the upstream web server stops. The laptop being off is not a
dependency.

If login works but canonical board reads fail, check the configured Supabase PostgreSQL
backend, Session-pooler host/port, verified CA, restricted role, and PC readiness. Check
that the web deployment and PC use the same store/account/environment. Do not restore the
stale TiDB source, buy more TiDB quota for this app, or bypass database/readiness checks.
See [Supabase Deployment](Supabase-Deployment).

## Remove from Buylist stays pending or returns

Wait for the authoritative response. The mobile row stays visible until removal is
confirmed; a failure displays its reason. A revision conflict must reload current canonical
state. A flat legacy planning stop is not a working stop order, while actual orders,
positions, unresolved exposure, and protection still block unsafe removal. Do not
repeat-click or delete local JSON to force a move.

## App opens without database data

- Check Health, MySQL credentials in `.env`, and MySQL connection settings in `config/runtime.local.json`.
- Confirm network/Tailscale reachability and MySQL grants/TLS policy.
- On a laptop, confirm `data/local_mirror.db` exists and mirror freshness is
  acceptable.
- The UI is allowed to run without MySQL; execution paths may still fail closed.

## Scanner or chart is empty

- Verify the selected source/universe and latest cache date.
- Run the appropriate 1D/1H refresh; inspect its status file/log.
- Confirm the symbol normalization and interval/source in the database.
- KIS intraday must remain disabled if mappings are unverified; research may
  fall back to Yahoo where supported.

## Buy Board is read-only or blocked

Read the readiness tooltip. Common causes are engine disabled, no current
lease/ownership, stale account reconciliation, missing WebSocket ACKs, stale
quotes, database write failure, mutation budget, unresolved order, or Live
Trading off. Fix the underlying cause; never bypass a gate.

`RESTRICTED: Device state is STARTING`, an unconfirmed-writable-store warning,
or `execution owner is LEGACY` describes separate gates. The first two now
refresh automatically when the runtime state or writability result changes;
the ownership warning clears only after the canonical workflow legitimately
assigns the symbol to the matching Kanban strategy. Refreshing the page cannot
make any of those gates pass.

## Phone action succeeded but another screen looks stale

Do not repeat the action blindly. Check the initiating browser's final status:
pending/optimistic state is not confirmation, while a canonical success or a
durably queued command is. Other browser/PWA sessions should receive an
authenticated `/live-updates` invalidation and desktops should consume a typed
coordination pulse automatically.

- Confirm the web host and desktop are using the same canonical coordination
  store and account/environment.
- Confirm the web session is still authenticated and the browser reconnected
  its WebSocket after a network interruption.
- Confirm the PC listener and desktop `main.py` process are running.
- Inspect the canonical card/command revision before retrying.
- Allow the bounded revision fallback or restart a stopped/outdated process;
  normal operation does not require a manual page refresh.

An offline desktop performs an initial canonical read when it starts, so it
does not depend on receiving every historical pulse.

## PC is on but cannot become Execution Owner

`PC: On`, `DB: On`, `Listener: On`, and `main.py: On` are separate health
signals. The owner selector reads the target's `runtime_device_state` from the
shared coordination database and requires a fresh, eligible identity; the
actual transfer additionally requires `STANDBY_READY` and all readiness gates.
The normal row cadence is 240 seconds with a 300-second freshness fence.

- Confirm both devices use the same `COORD_DB_*` values.
- Confirm the PC shows `7/7 — STANDBY_READY` continuously.
- Restart a peer that is running an older build, then wait for readiness.
- Read the exact selector error: "not registered" means no matching shared
  identity; "registered, but stale/ineligible" means the row exists but cannot
  participate.

## Live Trading differs between laptop and PC

The shared ON/OFF switch is synchronized. Each machine also has a private
`TRADING_ENABLED` administrative lock in `config/runtime.local.json`. A laptop with that value
false stays **LOCKED OFF** even if the shared switch is ON. A PC with it true
still does nothing unless it is the Execution Owner and every final gate
passes. Environment files are deliberately not synchronized.

## Watchlist button or split drawing looks wrong

- `W` changes passive Watchlist membership only. Removing that membership
  from a Buylist card must not erase its Buylist card, stop, order, or position.
- 1D and 1H split panes share a drawing. A weekend/off-session 1H endpoint is
  shown on the next available daily bar while its original 1H timestamp stays
  stored.

## Order state is ambiguous

Do not submit again. Query/reconcile the existing durable client/broker
identity, review account-wide open/history/reserved orders and positions, and
keep the card pending until evidence is unambiguous.

## A confirmed breakout did not move to Entry Pending

Breakout confirmation alone is not sufficient. Check the card memo and
readiness tooltip, then verify all of the following:

- the ORB range is complete, current-session, and based on fresh KIS evidence;
- a fresh KIS trade printed strictly above
  `max(breakout_price, orb_high)`;
- the fresh last trade and best ask are still strictly above the passive limit
  when submission is attempted;
- the limit satisfies
  `max(breakout_price, orb_low) < execution_price <= orb_high`;
- the regular session is open and ownership, reconciliation, risk, capital,
  quote-freshness, route, mutation-budget, and Live Trading gates pass.

If last trade or best ask has already reached the passive limit, the card stays
armed with `EXECUTION_LEVEL_ALREADY_REACHED`; the engine deliberately does not
convert the order to market, stop, or a marketable limit. See
[Current Order Logic](https://github.com/cafe-auvers/quant_app/blob/master/docs/current_order_logic.md).

## KIS rejected an entry with APBK0656

`APBK0656` is a routing/configuration rejection, not an ORB-strategy rejection.
The rejected broker identity is cleared and the plan should remain in Buy Today
during its retry cooldown. Verify the exchange route/account configuration and
wait for a fresh gated retry. It must not be moved to Buylist merely because of
this code. A different definitive broker rejection can return a zero-position
card to Buylist with its rejection memo.

## A higher-score ORB did not replace a working order

Replacement is intentionally strict. It is allowed only from 1m to 5m/30m or
from 5m to 30m, with the same score-model version and a strictly higher score at
0.1 precision. The old order must be exactly working, completely unfilled, and
have its full original quantity remaining. The later range, breakout, passive
price zone, session, quote, ownership, risk, capital, and broker gates must all
be valid. Any fill, equal/lower score, ambiguous state, or failed gate keeps or
fences the current generation.

Never manually submit the proposed replacement. The engine must first obtain
authoritative KIS cancellation with zero fills, revalidate, and then submit one
linked order for the same quantity. If cancellation is uncertain, the card
stays `CANCEL_PENDING` and no new BUY is authorized.

## Credential or runtime changes are missing

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_pc_env.ps1
```

Credential `.env` values and runtime-local overrides win. Generated `.env.pc`
intentionally blanks all `MYSQL_*` credentials.

## Qt/WebEngine rendering problems

Do not force global software rendering unless the machine requires it. Confirm
PyQtWebEngine is installed from the lock and review the application log for
actionable errors.
