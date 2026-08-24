# KIS WebSocket Symbol Keys

## Purpose

The exact KIS trade/quote subscription key for a symbol is operational state,
not a credential and not a process-lifetime environment setting. The runtime
reads the gitignored file:

```text
data/kis_ws_symbol_keys.json
```

The file is a plain JSON object:

```json
{
  "AAPL": "DNASAAPL"
}
```

Only live-verified keys belong in this file. The application never guesses an
exchange prefix.

## Safe commands

Run these from the repository root:

```powershell
python scripts/manage_kis_ws_symbol_keys.py validate
python scripts/manage_kis_ws_symbol_keys.py show
python scripts/manage_kis_ws_symbol_keys.py set AAPL DNASAAPL
python scripts/manage_kis_ws_symbol_keys.py remove AAPL
```

`set` and `remove` serialize concurrent writers, validate the complete map,
keep a rolling `.bak`, and replace the file atomically. They do not connect to
KIS and do not edit `.env` or `.env.pc`.

## Intraday behavior

- Adding a key, or correcting a key for a channel that is currently missing
  or rejected, is discovered by the next Buy Board runtime cycle. No process
  restart is required.
- A malformed, partially written, unreadable, or temporarily missing file
  never clears the running map. The process retains its last-known-good keys
  and reports the configuration error.
- Removing or changing the key of a currently ACKed symbol never tears down
  that healthy subscription. The old key remains pinned while the symbol is
  active. The new map becomes effective after the symbol leaves the active
  board and is later added again.
- A failure for one newly added symbol does not unsubscribe or starve existing
  healthy symbols.

These rules make intraday additions safe without turning a configuration typo
into a market-data outage for an open position.

The Controlled Live entry allowlist is a separate authorization boundary.
Adding a WebSocket key makes market data available; it does not by itself add
the symbol to `KIS_CONTROLLED_LIVE_SYMBOLS` or authorize a BUY.

## One-time migration

On a checkout that still has the legacy environment value, run:

```powershell
python scripts/manage_kis_ws_symbol_keys.py migrate-env
python scripts/manage_kis_ws_symbol_keys.py validate
```

Migration copies the complete legacy map into the separate file and refuses
to overwrite a conflicting reviewed key. It deliberately leaves `.env` and
`.env.pc` untouched so the running old process and rollback commit remain
safe.

After the new code has passed preflight and one controlled restart, remove the
legacy `KIS_WS_SYMBOL_KEYS_JSON` line from `.env` and `.env.pc` once. The
tracked `.env.example` no longer contains that setting, so startup environment
synchronization will not add it again.

Older commits require the legacy environment value. Preserve an encrypted
environment backup until the new runtime has been accepted if rollback to an
older commit must remain possible.
