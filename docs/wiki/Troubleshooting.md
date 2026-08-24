# Troubleshooting

## App opens without database data

- Check Health and `MYSQL_*` values in local `.env`.
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
`TRADING_ENABLED` administrative lock in `.env`. A laptop with that value
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

## Environment changes are missing

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_pc_env.ps1
```

Existing `.env` values win. Generated `.env.pc` intentionally blanks all
`MYSQL_*` values.

## Qt/WebEngine rendering problems

Do not force global software rendering unless the machine requires it. Confirm
PyQtWebEngine is installed from the lock and review the application log for
actionable errors.
