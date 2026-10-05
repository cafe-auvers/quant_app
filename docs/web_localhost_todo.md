# Localhost Web Dashboard Task List

Updated: 2026-10-05

Status values: `TODO`, `IN_PROGRESS`, `DONE`, `BLOCKED`, `OPTIONAL`.

| Status | Owner | Work item | Dependencies | Affected files / evidence |
|---|---|---|---|---|
| DONE | Developer | Inspect architecture, safety docs, scanner/chart/drawing/planning implementations | None | `docs/web_localhost_plan.md`; SHA and baseline recorded |
| DONE | Developer | Consolidate the integration branch | Verified Git graph | `codex/mobile-operator-control`; `codex/localhost-web` already contained |
| DONE | Developer | Run historical baseline tests | Existing environment | Historical 2026-10-01 result: 3033 passed; not the current count |
| DONE | Developer | Add pinned FastAPI/Uvicorn/auth dependencies and isolated `.venv-web` | Python 3.11/3.12 compatibility | hash-locked install; `pip check` clean |
| DONE | Developer | Implement web config, bootstrap auth, sessions, Host/Origin/CSRF/rate limits | Local state directory | `src/web/config.py`, `src/web/auth.py`, bootstrap/readiness scripts |
| DONE | Developer | Implement local planning/drawing SQLite store with CAS, idempotency, audit, tombstones | Authenticated actor | `src/web/store.py`, web tests |
| DONE | Developer | Separate breakout targets from current Watchlist membership and require breakout before Buylist | Shared planning rules + sandbox UI | canonical and sandbox services; rollover preserves breakouts; focused regressions passed |
| DONE | Developer | Implement DEMO and read-only local market/scanner adapters | Existing schemas and indicator semantics | `src/web/market_data.py`, coverage tests |
| DONE | Developer | Connect phone/PWA reads to the PC market mirror and canonical Supabase PostgreSQL TradeCards | PC repository path and coordination credentials | Current mirror: 132 ranked Setup 1 matches, representative 1D/1H payloads, 54 canonical planning symbols; `scripts/validate_web_mirror.py` |
| BLOCKED | User + Developer | Final physical mobile 1D/1H and gesture acceptance | Real iPhone/private HTTPS origin | Static/API coverage passes; physical device acceptance is not claimed |
| DONE | Developer | Add bounded canonical revision polling and PC runtime-heartbeat status | Read-only configured PostgreSQL adapter | Browser refreshes planning after revision changes; missing heartbeat remains `UNKNOWN` |
| DONE | Developer | Add immediate optimistic planning/Buy Today feedback and automatic cross-device refresh | Connected writes, authenticated session, coordination pulse path | `/live-updates` invalidations, typed external pulses, rollback paths, and runtime state/writability `board_changed` regression coverage |
| DONE | Developer | Share non-executable Buy Today drafts across web clients | Authenticated shared web store | Phone/laptop two-client create, observe, cancel test with connected operator operations disabled; canonical activation is a separate guarded allowlist |
| DONE | Developer | Implement one-current-bundle cache, publisher, retention, readiness | Market adapter | local/cloud publication protocol and failure tests |
| DONE | Developer | Build responsive static review workspace and PWA shell | API + chart asset | no server-driven hydration; authenticated invalidation WebSocket and service worker root scope verified |
| DONE | Developer | Replace server-driven shell and optimize symbol navigation | Static shell + chart cache | visible timeframe only; direct gzip cache hits; no stock-list rebuild; delayed neighbor prefetch |
| DONE | Developer | Implement drawing import and opt-in desktop adapter seam | Local drawing store | import/sync modules and tests |
| DONE | Developer | Hard-deny execution, live activation, ownership, risk, and power endpoints | FastAPI boundary | API boundary tests |
| DONE | Developer | Add optional Supabase adapter contracts and reviewed migrations | Local behavior complete | Auth/Storage/drawing adapters; RLS/private-bucket migrations |
| OPTIONAL | User | Configure Supabase web Auth/Storage/public projections if desired | Separate from the active private coordination schema | SQL coordination migration is complete; these web features are not required for mobile board sync |
| BLOCKED | Developer + User | Verify Supabase auth, RLS, private Storage, overwrite freshness, and two-browser sync | Supabase credentials and project | Integration report must remain BLOCKED until run |
| DONE | Developer + User | Enable canonical passive CONNECTED writes | Canonical PostgreSQL scope, permitted Operator Control identity, explicit allowlist | Watchlist/Buylist/breakout round-trip and idempotency tests; live authority verification passed |
| DONE | Developer | Add mobile workspace navigation and delegated operator controller | Verified Mobile Web or hosting-desktop Operator Control, explicit operator allowlist | Home with Market Pulse, list selector, Chart, Buy Board, Stocks controls; direct/queued Buy Today tests and guarded plan publish test |
| DONE | Developer | Run targeted/full regression and two 300-symbol benchmarks | Final implementation SHA | 96 web tests and 3141 total tests passed; DEMO and read-only mirror reports current |
| BLOCKED | User + Developer | Capture/inspect desktop and 390 px in-app browser screenshots | In-app browser must be attached | Browser controller reported no available `iab`; do not claim screenshot acceptance |
| DONE | Developer + User | Migrate all 20 coordination tables to private Supabase PostgreSQL | Stopped writers, verified export/restore, restricted runtime role, exact approved release | 2026-10-05 cutover; PC/mobile settings and passive removal verified; user armed current session; daily 09:15 KST backup |
| DONE | Developer | Final documentation and exact launch/readiness/import commands | Local verification | README, architecture, setup, performance report |

## Current blockers

The TiDB-to-Supabase migration is complete, not a remaining blocker. Current PC/mobile
operation uses the always-on PC and the same private phone URL, with shared settings and
passive Buylist removal verified. See [Deployed setup](deployed_setup.md). Outstanding
physical-phone and optional Auth/Storage tests below must not be confused with the verified
PostgreSQL store.

- Bare repository-root `pytest -q` may traverse an existing protected
  artifact directory. `pytest tests -q` is the valid baseline command.
- Canonical CONNECTED reads and the six allowlisted passive writes are enabled.
  Watchlist/Buylist changes use canonical domain services and CAS revisions;
  breakout Set/Clear additionally require the permitted stable `Mobile Web`
  or exact hosting-PC identity to own Operator Control. No execution ownership
  transfer occurs and the phone never receives desktop credentials.
- Guarded mobile planning and Buy Today actions display pending state, then
  reconcile with canonical state or roll back. Passive Buylist removal keeps
  its row visible until authoritative confirmation and reports failure. Authenticated invalidations and
  typed desktop pulses are the normal update path; canonical/shared-draft
  revision recovery runs as a fallback. No manual refresh is required. The
  fallback shared draft remains non-executable.
- During the historical 2026-10-02 validation session, the in-app Browser
  capability had no attached browser instance. Live HTTP smoke testing passed; visual desktop
  and 390 px screenshot inspection remains explicitly blocked.
