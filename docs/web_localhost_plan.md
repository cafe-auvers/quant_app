# Localhost Web Dashboard Plan

Status: **LOCAL SANDBOX COMPLETE; EXTERNAL CHECKS BLOCKED**  
Baseline: `8437fded0a112fcdab424e99e12aeeba2ab63ad1` on 2026-10-01  
Feature worktree: `codex/localhost-web` (`C:\Users\tonyh\Documents\quant_app_web`)

## Priority and scope

The authenticated localhost dashboard provides the daily Scanner/search →
1D/1H charts → drawings → Watchlist → breakout → Buylist workflow while the
existing PyQt application and execution engine remain intact. Its mobile shell
has Summary, Market Pulse, Chart, and Buy Board destinations, with a centered
Home shortcut and Chart as the initial destination.

CONNECTED mode can act as a delegated controller after verifying that either
the stable `Mobile Web` identity or the exact hosting-desktop identity owns
Operator Control. It can activate/remove an
unsubmitted Buy Today card and publish the complete pre-market planning
snapshot through existing typed services. It never arms trading, transfers
ownership, submits/cancels/replaces a broker order, changes global risk limits,
or controls workstation power.

## Baseline evidence

- Repository SHA: `8437fded0a112fcdab424e99e12aeeba2ab63ad1`.
- Active checkout branch at inspection: `fix/gate3-journal-append`.
- Active checkout changes: untracked `CODEX_LOCALHOST_WEB_PROMPT.md` and
  `USER_SETUP_CHECKLIST.md`; neither file was changed or moved.
- Python: 3.12.4. The tracked lock supports Python 3.11 and 3.12.
- `pytest tests -q`: **3033 passed**, one unrelated protobuf deprecation
  warning, 228.18 seconds.
- Bare `pytest -q`: **collection blocked** by Windows `PermissionError` while
  traversing the existing protected directory
  `artifacts/pytest_gate2_readiness_20260909`. This is a baseline environment
  issue, not a test failure; scoped test commands use `pytest tests -q`.

## Architecture

```text
browser / installed PWA
        |
        | authenticated HTTPS/HTTP + Origin/Host + CSRF checks
        | authenticated invalidation WebSocket
        v
Static HTML/JavaScript shell on FastAPI
        |
        +-- read API --> scanner/universe + chart bundle interfaces
        |                  +-- deterministic DEMO adapter (default)
        |                  +-- read-only local SQLite mirror adapter (opt-in)
        |
        +-- sandbox commands --> isolated data/web/web_state.db
        |                         planning + drawings + idempotency + audit
        |
        +-- current chart cache --> data/web/chart_cache/
        |
        +-- connected adapters (explicitly enabled only)
                           +-- Supabase auth/chart/drawings projection
                           +-- existing canonical planning services

existing PyQt main.py / runtime / broker / KIS WebSocket
        (never constructed or launched by the web process)
```

FastAPI owns authenticated request and command boundaries and serves the
responsive static shell. There is no server-driven UI hydration. A small
authenticated WebSocket carries invalidation hints only; clients refetch
canonical state. TradingView Lightweight Charts is served from the repository's
vendored presentation asset. Domain, risk, sizing, lifecycle, and broker logic
remain Python-side and are not copied into browser code.

## Modes and authority

### SANDBOX (default)

- Isolated SQLite planning/drawing/command/audit persistence.
- Isolated local chart-bundle cache.
- Deterministic fixtures labelled `DEMO` when no read-only data source is
  configured.
- Watchlist, breakout, Buylist, and non-executable Buy Today previews persist
  locally and always display `NOT SYNCED TO EXECUTOR`.

### CONNECTED (opt-in)

- Read-only real market/scanner data comes from the explicitly configured PC
  SQLite mirror. Canonical Watchlist/Buylist/Buy Today/breakout state is read
  from TiDB with authenticated invalidation/change-pulse updates, a bounded
  revision fallback, and no schema bootstrap.
- Non-executable Buy Today drafts remain available when guarded operator
  operations are not enabled.
- Canonical TiDB planning writes default off. This workstation's local config
  explicitly enables only Watchlist add/remove, Watchlist/Buylist movement,
  and breakout Set/Clear through existing typed services with authenticated
  actor, account/environment, command ID, expected revision, lifecycle checks,
  CAS, and a durable local web audit. Breakout edits revalidate that either the
  stable `Mobile Web` identity or the exact hosting-desktop identity owns
  Operator Control on every request.
- Buy Today and full-plan publication use a separate explicit operator
  allowlist. Authority is revalidated for every request. Commands apply
  directly only when Operator Control and Execution Owner are the same desktop;
  otherwise they enter the append-only operator command queue for the Execution
  Owner.
- Full-plan publication is closed-session only, checks that canonical Buy
  Today targets match the saved execution queue, and reuses the existing
  six-document revision/CAS/read-back workflow.
- Unsupported or unverified authority remains visibly read-only. The browser
  never receives the PC identity credential or a broker connection.
- The initiating browser renders planning/Buy Today changes optimistically.
  Success reconciles from canonical read-back; failure rolls back. Other open
  browser/PWA sessions receive authenticated invalidations, and typed external
  pulses trigger scoped PC/laptop desktop refreshes without a manual reload.

Sandbox data is never uploaded automatically when the mode changes.

## Data and chart correctness

- Daily view: latest 750 completed bars where available.
- Hourly view: six calendar months, loaded independently of daily data.
- Indicator warm-up precedes display trimming; the web layer reuses the
  existing EMA semantics and never substitutes missing values with zero.
- Each response reports actual range, bar count, source, adjustment mode,
  regular/extended-hours policy, and latest completed timestamp.
- Cache identity includes source, timeframe, adjustment, and session policy.
- One current compressed bundle exists per symbol/timeframe. Atomic replace,
  checksum validation, ETag, bounded retention, and pinned-symbol protection
  apply.

## Security model

- One local user is created explicitly with the bootstrap script; no default
  password exists.
- Argon2 password hashes and opaque, expiring server-side sessions are stored
  in the isolated SQLite database. Cookies contain only random session IDs.
- Reads and writes require authentication. Mutations also require a session
  CSRF token. Host and Origin are allowlisted; login attempts are rate limited.
- API documentation is disabled in normal launch, CORS is not wildcarded, and
  user text is validated as plain data.
- Web configuration is read from tracked `config/web.example.json` plus the
  gitignored `config/web.local.json`; `.env`, `.env.pc`, trading switches,
  leases, and live schemas are not changed.

## Process boundaries

`python scripts/run_web.py` imports only `src.web` and headless services. It
must not import `main`, create `QApplication`/`MainWindow`, construct a broker,
start the trading runtime, or open a KIS WebSocket. Blocking repository,
compression, and publication work runs in bounded workers. Reload is disabled
by default so duplicate publishers/pollers cannot be created.

## Verification strategy

- Pure pytest coverage for config precedence, authentication/session/CSRF,
  scanner/search, history coverage, cache checksums/eviction, drawings and
  tombstones/import, idempotent planning/CAS conflicts, and denied operations.
- Import-boundary tests prove the launcher cannot construct Qt, KIS, broker,
  or execution runtime objects.
- Browser verification covers authenticated desktop and 390 px layouts,
  persistent workflow, chart switching, and visible safety labels.
- A deterministic 300-symbol report records p50/p95 latency, request counts,
  compressed bytes, memory, hardware, source, cache state, and limitations.

## External integration boundaries

Local sandbox acceptance requires no Supabase account, Docker, domain, public
hosting, KIS connectivity, or live database access. Supabase and real canonical
write checks remain `BLOCKED` until the user supplies a dedicated development
project and credentials. No result will be reported as verified without real
evidence.

Any tracked change creates a new SHA and therefore requires normal activation
gate requalification before a future execution promotion. This web milestone
does not rewrite or extend historical gate evidence.

## Completion evidence

- `pytest tests -q`: **3072 passed**, one third-party Starlette/httpx
  deprecation warning, 234.90 seconds.
- Targeted web suite: **33 passed**.
- Isolated dependency graph: `pip check` reports no broken requirements;
  Python compilation and JavaScript syntax checks pass.
- Live loopback HTTP smoke: local login, authenticated page, root-scoped
  service worker, 300-row scanner, 750-bar 1D chart, sandbox Watchlist command,
  and logout all returned successfully.
- Reproducible DEMO 300-symbol/two-timeframe benchmark: 1,200 requests; cold
  navigation p50/p95 1519.76/2187.17 ms; warm navigation p50/p95
  131.92/201.69 ms; 24.82 MiB compressed chart bytes. See
  `docs/web_performance_report.md` for conditions and limitations.
- In-app desktop/mobile screenshot inspection remains blocked because this
  session had no attached `iab` browser instance. Supabase and real-iPhone
  visual inspection remain explicitly blocked; real mirror and canonical
  passive-write authority checks passed.
