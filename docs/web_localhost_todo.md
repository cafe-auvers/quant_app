# Localhost Web Dashboard Task List

Updated: 2026-10-02

Status values: `TODO`, `IN_PROGRESS`, `DONE`, `BLOCKED`.

| Status | Owner | Work item | Dependencies | Affected files / evidence |
|---|---|---|---|---|
| DONE | Developer | Inspect architecture, safety docs, scanner/chart/drawing/planning implementations | None | `docs/web_localhost_plan.md`; SHA and baseline recorded |
| DONE | Developer | Establish isolated feature worktree | Clean target path | Branch `codex/localhost-web`; active checkout unchanged |
| DONE | Developer | Run baseline tests | Existing environment | `pytest tests -q`: 3033 passed |
| DONE | Developer | Add pinned FastAPI/Uvicorn/auth dependencies and isolated `.venv-web` | Python 3.11/3.12 compatibility | hash-locked install; `pip check` clean |
| DONE | Developer | Implement web config, bootstrap auth, sessions, Host/Origin/CSRF/rate limits | Local state directory | `src/web/config.py`, `src/web/auth.py`, bootstrap/readiness scripts |
| DONE | Developer | Implement local planning/drawing SQLite store with CAS, idempotency, audit, tombstones | Authenticated actor | `src/web/store.py`, web tests |
| DONE | Developer | Separate breakout targets from current Watchlist membership and require breakout before Buylist | Shared planning rules + sandbox UI | canonical and sandbox services; rollover preserves breakouts; focused regressions passed |
| DONE | Developer | Implement DEMO and read-only local market/scanner adapters | Existing schemas and indicator semantics | `src/web/market_data.py`, coverage tests |
| DONE | Developer | Connect phone/PWA reads to the PC market mirror and canonical TiDB TradeCards | PC repository path and coordination credentials | Real 300-row scanner, current mirror freshness, 1D/1H payloads, 35 canonical Buylist rows verified; `tests/web/test_canonical_planning.py` |
| DONE | Developer | Verify and harden mobile 1D/1H switching | Actual PC mirror + mobile browser | 390x844 Playwright run switched `A` from 1D to a rendered 1H bundle; immediate label feedback and 44px tap target added |
| DONE | Developer | Add bounded canonical revision polling and PC runtime-heartbeat status | Read-only TiDB adapter | Browser refreshes planning after revision changes; missing heartbeat remains `UNKNOWN` |
| DONE | Developer | Add immediate optimistic planning/Buy Today feedback and automatic cross-device refresh | Connected writes, authenticated session, coordination pulse path | `/live-updates` invalidations, typed external pulses, rollback paths, and runtime state/writability `board_changed` regression coverage |
| DONE | Developer | Share non-executable Buy Today drafts across web clients | Authenticated shared web store | Phone/laptop two-client create, observe, cancel test with connected operator operations disabled; canonical activation is a separate guarded allowlist |
| DONE | Developer | Implement one-current-bundle cache, publisher, retention, readiness | Market adapter | local/cloud publication protocol and failure tests |
| DONE | Developer | Build responsive static review workspace and PWA shell | API + chart asset | no server-driven hydration; authenticated invalidation WebSocket and service worker root scope verified |
| DONE | Developer | Replace server-driven shell and optimize symbol navigation | Static shell + chart cache | visible timeframe only; direct gzip cache hits; no stock-list rebuild; delayed neighbor prefetch |
| DONE | Developer | Implement drawing import and opt-in desktop adapter seam | Local drawing store | import/sync modules and tests |
| DONE | Developer | Hard-deny execution, live activation, ownership, risk, and power endpoints | FastAPI boundary | API boundary tests |
| DONE | Developer | Add optional Supabase adapter contracts and reviewed migrations | Local behavior complete | Auth/Storage/drawing adapters; RLS/private-bucket migrations |
| BLOCKED | User | Create/configure optional Supabase development project | User account and dashboard access | See `docs/web_user_setup.md` |
| BLOCKED | Developer + User | Verify Supabase auth, RLS, private Storage, overwrite freshness, and two-browser sync | Supabase credentials and project | Integration report must remain BLOCKED until run |
| DONE | Developer + User | Enable canonical passive CONNECTED writes | TiDB scope, permitted Operator Control identity, explicit allowlist | Watchlist/Buylist/breakout round-trip and idempotency tests; live authority verification passed |
| DONE | Developer | Add mobile workspace navigation and delegated operator controller | Verified Mobile Web or hosting-desktop Operator Control, explicit operator allowlist | Summary, Market Pulse, Chart, Buy Board, centered Home; direct/queued Buy Today tests and guarded six-document plan publish test |
| DONE | Developer | Run targeted/full regression and 300-symbol benchmark | Local implementation complete | 73 web tests passed; focused shared-planning regressions passed; prior full `pytest tests -q`: 3072 passed; measured report complete |
| BLOCKED | User + Developer | Capture/inspect desktop and 390 px in-app browser screenshots | In-app browser must be attached | Browser controller reported no available `iab`; do not claim screenshot acceptance |
| DONE | Developer | Final documentation and exact launch/readiness/import commands | Local verification | README, architecture, setup, performance report |

## Current blockers

- Bare repository-root `pytest -q` cannot traverse an existing protected
  artifact directory. `pytest tests -q` is the valid baseline command.
- Canonical CONNECTED reads and the six allowlisted passive writes are enabled.
  Watchlist/Buylist changes use canonical domain services and CAS revisions;
  breakout Set/Clear additionally require the hosting PC's exact local device
  identity to remain the current Operator Control owner. No ownership transfer
  occurs and the phone never receives or assumes that identity.
- Guarded mobile planning and Buy Today actions are immediate in the UI, then
  reconcile with canonical state or roll back. Authenticated invalidations and
  typed desktop pulses are the normal update path; canonical/shared-draft
  revision recovery runs as a fallback. No manual refresh is required. The
  fallback shared draft remains non-executable.
- The required in-app Browser capability is installed but no browser instance
  was attached to this session. Live HTTP smoke testing passed; visual desktop
  and 390 px screenshot inspection remains explicitly blocked.
