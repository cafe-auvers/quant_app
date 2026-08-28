# KIS WebSocket Symbol Keys

## Purpose

The exact KIS trade/quote subscription key for a symbol is operational state,
not a credential and not a process-lifetime environment setting. Do not put
the symbol map in `.env` or `.env.pc`. Each runtime reads its gitignored local
file:

```text
data/kis_ws_symbol_keys.json
```

The file is a plain JSON object:

```json
{
  "AAPL": "DNASAAPL"
}
```

Keys in this file either come from a reviewed manual entry or are provisioned
from the local official KIS US symbol master. Automatic provisioning uses the
master's exact `KisSymbol` and its `NAS`/`NYS`/`AMS` exchange together with the
live-verified regular-session prefix for that exchange. Missing, ambiguous, or
unsupported master rows fail closed; the application never guesses an
exchange.

## Cross-device Buy Today handoff

Activating a card for Buy Today captures that symbol's locally verified key
and persists it with the canonical card. During a split Laptop Operator
Control / PC Execution Owner session, the same value travels inside the
durable `ADD_BUY_TODAY` command. Before requesting the quote/trade channels,
the executor atomically adds a missing canonical mapping to its own local
file. Therefore a newly selected Buy Today symbol does not require a second
manual edit or an application restart on the execution PC.

When a newly added symbol is not already in the local key file, activation or
the next subscription rebalance provisions it atomically from
`data/us_kis_tickers.csv`. This also repairs an already-active Buy Today card
whose earlier activation persisted an empty key. The hot-reload and canonical
handoff behavior below then applies without restarting either process.

This handoff does not weaken the local review boundary:

- A missing key does not prevent the Buy Today plan from being saved, but the
  symbol remains feed-unready and cannot authorize a BUY.
- A canonical value never silently overwrites a different local reviewed
  value. The conflict stays visible and the existing healthy mapping remains
  unchanged.
- Adding the mapping does not alter Controlled Live authorization, position
  sizing, portfolio risk, or the shared live-trading switch.
- Existing active cards created before this handoff field was deployed need
  one initial validated executor file or a later remove/reactivate cycle.

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
- A verified key arriving with a newly activated canonical Buy Today card is
  adopted on that same cycle before subscription selection.
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

The persisted active Trade Card list is a separate authorization boundary.
Adding a WebSocket key makes market data available; it does not by itself move
a reviewed plan to Buy Today or authorize a BUY.

## Environment isolation

The runtime never reads symbols from process environment values. Normal
intraday changes must use the `set` and `remove` commands above against
`data/kis_ws_symbol_keys.json`; neither `.env` nor `.env.pc` is a supported
symbol source.

For upgrade safety only, the startup environment synchronizer recognizes the
retired `KIS_WS_SYMBOL_KEYS_JSON` key from older checkouts. It validates and
conflict-checks the complete map, atomically merges it into
`data/kis_ws_symbol_keys.json`, keeps a rolling backup when replacing an
existing file, and only then removes the retired line from `.env` and
regenerates `.env.pc`. Invalid or conflicting legacy data aborts without
stripping the source value. This is a one-time file migration; application
runtime and intraday subscription selection never consult the environment.

The synchronizer also recognizes the retired `KIS_CONTROLLED_LIVE_SYMBOLS`
allowlist found on some older machines. That list no longer authorizes
anything: controlled-live scope comes only from persisted active Trade Cards.
For lossless cleanup, the synchronizer validates the old list, records it in
gitignored `data/retired_controlled_live_symbols.json` with
`authorization_effect: false`, and only then removes the environment line.
