# System Overview

Quant App combines six operational areas:

1. research and scanning;
2. Watchlist and Buylist planning;
3. chart review and ORB plan selection;
4. account, position, and broker reconciliation;
5. a guarded Buy Board execution runtime.
6. an authenticated web/PWA review and delegated operator surface.

```mermaid
flowchart LR
    Provider[Yahoo / KIS market data] --> Cache[(MySQL market cache)]
    Cache --> Scanner[Scanner]
    Cache --> Charts[Charts and indicators]
    Scanner --> Watchlist[Watchlist planning]
    Charts --> Watchlist
    Watchlist --> Buylist[Buylist]
    Buylist --> Board[Buy Board]
    KIS[KIS account and order APIs] --> Reconcile[Broker reconciliation]
    Board --> Gateway[Guarded execution gateway]
    Web[Phone / PWA] -->|private Tailscale HTTPS| WebHost[PC web service]
    WebHost -->|allowlisted intent| Shared[(Supabase private coordination)]
    Board <-->|canonical state / commands| Shared
    Gateway --> KIS
    Reconcile --> Board
    Cache --> Mirror[(Laptop SQLite mirror)]
```

## Implemented

The deployed shared store is private Supabase PostgreSQL, verified on 2026-10-05. The
always-on PC hosts both the guarded executor and supervised mobile web service; the optional
laptop is not needed for their operation. PC/laptop direct access and pulses use
LAN/Tailscale. TiDB is not required. See [Supabase Deployment](Supabase-Deployment).

- Daily/hourly/intraday cache and scanner workflows.
- TradingView Lightweight Charts with drawings, ORB markers, fundamentals,
  earnings, Leadership, and Market Context overlays.
- Cross-device planning and execution ownership controls.
- A four-state PyQt Operator Control selector (`PC`, `Laptop`, `Mobile`, or
  `Locked`); Mobile is visible as a distinct shared owner but is never an
  Execution Owner option.
- Immediate optimistic web/PWA actions with canonical confirmation, automatic
  peer-browser invalidation, and typed desktop refresh pulses.
- Buy Board runtime availability by default, with broker mutations still
  blocked by independent live-execution, ownership, readiness, and risk gates.
- Durable order/card/command state, conservative reconciliation, event journal,
  Health tab, and guarded KIS integration.
- Confirmed-breakout passive BUY limits and strict zero-fill, higher-score ORB
  cancel-then-replace generations.

## Optional

- Canonical MySQL market-data cache and laptop SQLite mirror.
- TLS-only coordination SQL store for independent-device control.
- KIS credentials, real-time market data, external alerting, and cloud backup.

## Disabled by default

- Live Trading administrative permission.
- Controlled/full-live broker mutation envelopes.
- KIS intraday mappings until the capability is verified.
