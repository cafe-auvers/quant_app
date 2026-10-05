# Quant Web Final Validation

**Deployment chronology update — 2026-10-05:** the private Supabase coordination cutover and
PC/mobile checks are recorded in [Deployed setup](deployed_setup.md). Older dated commits,
test counts, gate outcomes, and blockers below remain historical evidence for their own
scope; they are not current store configuration or a new full-session qualification. Never
treat a readiness snapshot or documentation update as certification of a later gate.

Validated implementation SHA: `c882a22dc3417a3c034f74e33a32c062a01d1c14`

Branch: `codex/mobile-operator-control`

Recorded: 2026-10-02 KST

This SHA is the immutable executable-code target for this report. The report
and documentation cleanup are committed as a documentation-only child, so the
handoff message must also identify that final branch HEAD. This avoids claiming
that a commit can contain its own SHA.

## A. Final branch state

- `origin/master`: `02a5346fe630d305231d8788ca047a8829fece28`.
- Validated implementation: 5 commits ahead, 0 behind `origin/master`.
- Changed paths at the implementation SHA: 121 relative to `origin/master`.
- `origin/codex/localhost-web` is fully contained: its SHA and merge base are
  both `58b563ab886eb3301e9600d94c3a6e1ca9895c59`.
- No second web merge, master merge, branch deletion, credential change, live
  trading flag change, or workstation-service change was performed.

Environment: Windows 11 `10.0.26200`, Python 3.12.4, pip 24.0, clean temporary
venv installed with hashes from `requirements.lock`.

## B. Meaningful fixes

- Validates chart cache entries against source revision, adjustment mode,
  session policy, schema, daily-bar setting, and hourly-month setting.
- Enforces cache retention after runtime writes and preserves local retained
  symbols plus canonical Watchlist, Buylist, Buy Today, and position symbols
  learned from planning/Buy Board projections.
- Correctly registers and updates the PWA worker, caches only live
  presentation asset paths, and never queues API or offline mutations.
- Adds bounded in-browser chart-paint timing for symbol navigation and 1D/1H
  switching without transmitting telemetry.
- Reduces Buy Board recovery polling from 3 seconds to 15 seconds while
  retaining authenticated WebSocket invalidation and the 10-second status
  revision recovery path.
- Shows source, actual range, bar count, completeness, adjustment policy, and
  session policy in the existing compact chart context rather than inventing
  values or adding a large diagnostics panel.
- Retries the intermittent Windows event-loop socket-pair bootstrap error in
  the web process and test process only. Desktop production policy is unchanged.
- Adds an additive Supabase migration requiring both drawing ownership and the
  user allowlist on UPDATE, including after allowlist removal.
- Adds reproducible authenticated HTTP benchmarking and read-only real-mirror
  validation scripts.

## C. Architecture verified

```text
Browser / installed PWA
  -> session + CSRF + Host/Origin checks
  -> FastAPI static-client and typed command boundary
  -> market source / canonical services / repositories
       -> read-only PC SQLite market mirror
       -> canonical TiDB planning and operator intent
  -> desktop Execution Owner
  -> existing guarded execution gateway
  -> KIS
```

`scripts/run_web.py` builds no `QApplication`, `MainWindow`, `KisBroker`,
execution gateway, execution workflow runtime, `BuyboardRuntimeWorker`, KIS
execution WebSocket, or execution lease owner. A subprocess test checks both
imports and constructed service objects. Web modules do not call Qt-owned
business methods. The only remaining UI-path coupling is reuse of the vendored
Lightweight Charts file under `src/ui/static/vendor`; it is a static asset, not
Qt business logic.

## D. Safety and route audit

All API routes require the documented authentication boundary. Mutations also
require CSRF and an allowed Origin. `OPERATOR_INTENT_WRITE` means typed human
intent only; it is not broker execution.

| Route | Class | Effect |
|---|---|---|
| `/`, `/login`, `/service-worker.js`, `/reset-ui` | READ | Static presentation; reset is transitional retired-tab cache cleanup |
| `/live-updates` | READ | Authenticated invalidation hints; no authoritative payload |
| `GET /api/v1/auth/csrf`, `POST /api/v1/auth/login`, `GET /api/v1/session`, `POST /api/v1/auth/logout` | READ | Local session lifecycle only |
| `GET /status`, `/scanner`, `/search`, `/charts/*` | READ | Health, ranked scanner, search, current chart bundles |
| `GET /planning*`, `/planning-history`, `/buy-today-drafts`, `/buyboard` | READ | Sandbox/canonical projections |
| `POST /planning/{symbol}/commands` | PASSIVE_PLANNING_WRITE | Allowlisted Watchlist, breakout, and Buylist CAS command |
| `POST/DELETE /planning/{symbol}/buy-today-preview` | PASSIVE_PLANNING_WRITE | Explicitly non-executable local/shared draft |
| `POST /planning/{symbol}/activate-buy-today` | OPERATOR_INTENT_WRITE | Guarded canonical Buy Today intent; no broker call |
| `GET/POST /operator/control` | READ / OPERATOR_INTENT_WRITE | Read or assign manual Operator Control; never Execution Owner |
| `POST /operator/board-actions` | OPERATOR_INTENT_WRITE | Typed, revision-fenced Kanban intent; queued when required |
| `POST /operator/publish-today-plan` | OPERATOR_INTENT_WRITE | Closed-session typed plan publication |
| `GET /operator/commands/{id}` | READ | Command status only |
| `GET/POST/PUT/DELETE /drawings*` | READ / PASSIVE_PLANNING_WRITE | Revisioned visual drawings and tombstones only |
| any mutation under `/execution`, `/orders`, `/ownership`, `/risk`, `/power` | FORBIDDEN | Explicit HTTP 403 catch-all |

There is no generic service-invocation endpoint and no browser route to place,
submit, cancel, or replace a broker order; ARM/live mode; claim Execution Owner;
change global risk; or shut down, restart, or wake a workstation. The only
search hit for `claim_kanban_ownership` is per-symbol strategy ownership during
the reviewed Buy Today workflow, not the device execution lease.

## E. Tests and startup validation

Authoritative full suite at the implementation SHA:

```powershell
python -m pytest tests -q
```

Result: **3,141 passed, 0 failed, 0 errors, 0 skipped**, one warning, 265.52
seconds. The warning is an upstream Starlette deprecation notice for importing
`httpx` through `fastapi.testclient`; it does not affect runtime behavior.

Web suite:

```powershell
python -m pytest tests\web -q
```

Result: **96 passed**, the same one warning, 26.42 seconds.

The focused operator control, planning membership, TradeCard state/repository,
Buy Board runtime, breakout, state sync, execution workflow/gateway, account
and handoff reconciliation, scanner/core behavior, chart, and web group passed
**1,033 tests**, with the same warning, in 133.60 seconds.

Additional clean results:

- `python -m compileall -q main.py src scripts tests`;
- `node --check` for `app.js`, `login.js`, and `service_worker.js`;
- `python -m pip check`;
- `scripts/check_requirements_lock.py`;
- `scripts/check_repository_hygiene.py`;
- `git diff --check`.

Clean startup used a new temporary venv, hash-locked install, copied temporary
SANDBOX config, temporary user, and hidden port-18080 server. Twenty-four HTTP
checks passed: anonymous protection, login, dashboard, scanner, search, 1D,
1H, ETag/304, CSRF, Host, Origin, logout/revocation, forced expiry, disabled
Swagger/OpenAPI/ReDoc, manifest, worker update headers, security headers, and
no password reflection. Access logging was disabled and both server logs were
empty. The temporary server was verified by PID/command line and stopped.

Two independent authenticated TestClient sessions prove optimistic writer A
causes a WebSocket invalidation, reader B refetches and converges, B commits
revision N+1, and A's stale N write receives 409 plus current canonical state.
The 10-second status/revision path remains the deliberately missed-WebSocket
fallback. Typed desktop pulse publication and desktop change-driven refresh
are covered by the focused/full suites; routine use needs no manual reload.

## F. Performance

| Source | Cold 1D p50/p95 | Independent 1H p50/p95 | Warm bundle p50/p95 | Auth HTTP p50/p95 |
|---|---:|---:|---:|---:|
| DEMO, 300 symbols | 144.00 / 224.76 ms | 191.78 / 302.26 ms | 1.11 / 1.75 ms | 26.43 / 44.13 ms |
| PC mirror, 300 symbols | 106.08 / 144.09 ms | 129.47 / 248.87 ms | 1.42 / 2.04 ms | 28.10 / 35.05 ms |

Each run used 750 requested daily bars, six calendar months hourly, 900 direct
coordinator requests, and 300 authenticated HTTP responses. All HTTP responses
had ETags. Cache size was 34.86 MiB DEMO and 17.99 MiB mirror. Process working
set grew about 11.9 MiB and 16.8 MiB respectively. See
[web_performance_report.md](web_performance_report.md) for bytes, hardware, and
limitations.

Browser paint instrumentation is present as `window.__quantWebMetrics`, but an
in-app browser was unavailable. Desktop/iPhone paint p50/p95 is therefore not
claimed and must be collected during UAT.

## G. Real-data and database checks

The configured mirror reported `AVAILABLE`, snapshot/expected date 2026-10-01,
and `CURRENT`. Setup 1 returned 132 actual matches, scores were non-increasing,
and symbols were not alphabetical. AAPL exact/company search worked.

Read-only 1D/1H samples succeeded for AAPL (large cap), ABVX (mid cap), EZPW
(small cap), SPY (ETF), ARBEW (scanner), CXAIW (non-scanner/short history), and
AIIR (earnings). Profile, sector/industry where applicable, Leadership/Context,
earnings, actual dates, completeness, and bar counts were inspected. Daily
histories with 294 or fewer bars were honestly `PARTIAL`; no 750-bar coverage
was invented. The 1,397,125,120-byte mirror size and nanosecond mtime were
identical before and after. No migration or write ran.

Connected readiness read 54 canonical planning symbols. Executor projection
was `UNKNOWN`. The detached worktree's configured web state had no local user,
so that environment's readiness check correctly failed only user bootstrap;
the isolated clean environment proved bootstrap/login/startup separately.

Measured canonical SQL executions on an isolated repository-backed SQLite
engine were bounded per action: Watchlist 5, breakout 14, Buylist 7, and Buy
Today 29. Counts include authority, revision, ownership, transaction, and
read-back checks; they are not TiDB latency measurements. No one-second SQL
poll, per-card fanout, duplicate board refresh loop, or self-trigger loop was
introduced.

## H. UX hardening

- 1D and 1H remain independent, old selection responses are token-discarded,
  the chart stays mounted, and the stock list is not rebuilt on selection.
- Nearby prefetch is bounded to the other current timeframe, previous one, and
  next one; the browser cache is capped at 36 bundles.
- Compact chart metadata exposes source/range/count/completeness while missing
  values remain `UNAVAILABLE`/`PARTIAL`.
- Initiating planning actions remain optimistic with pending, confirmation,
  conflict, rollback, and canonical read-back semantics.
- Buy Board relies primarily on invalidation and uses a less aggressive
  fallback interval.
- PWA update behavior is active again without any offline write replay.

Visual desktop, 390x844 mobile, gesture, safe-area, 44-pixel target, and
physical iPhone confirmation remain user acceptance checks because no browser
controller or physical device was available in this session.

## I. Documentation normalization

Current counts, benchmark, branch, FastAPI/static architecture, startup,
connected-write boundary, Supabase status, and physical-device status were
normalized in the requested web/architecture documents. Old 3,033/3,072 and
33/73 web-test numbers are labeled historical instead of presented as current.
NiceGUI is not described as the active frontend; the one retired-tab route is
explicitly transitional compatibility.

## J. Remaining blockers

- **Code blocker:** none found for user acceptance.
- **User setup blocker:** bootstrap the local user for whichever gitignored
  `config/web.local.json`/data directory will host the accepted instance.
- **External-service blocker:** Supabase live Auth/RLS/private Storage/Realtime
  remains **BLOCKED - USER SETUP REQUIRED**. It is optional for localhost.
- **Physical-device blocker:** real iPhone/PWA install, touch/axis gestures,
  safe-area layout, and browser-paint measurements have not been observed.
- **Operational evidence:** canonical executor state read as `UNKNOWN`; this
  does not affect SANDBOX review but must be healthy before controlled live use.

## K. 20-30 minute user acceptance checklist

Safe in **SANDBOX** (about 20 minutes):

1. Sign in, sign out, sign back in; confirm no password appears in the URL.
2. Open Scanner; confirm ranked results/freshness and select 20-30 consecutive
   symbols with keyboard and previous/next controls.
3. For several symbols, switch 1D/1H from the chart label; verify latest date,
   source, bar count, range, earnings, ADR/1M/3M, and Context.
4. Search and open a non-scanner ticker.
5. Draw, move, and delete a line; extend it into future whitespace; verify it
   does not change breakout.
6. Add/remove Watchlist, set/drag/manual-edit breakout, promote/remove Buylist,
   and verify a Buylist requires breakout while breakout does not require
   today's Watchlist.
7. Create/cancel the non-executable Buy Today preview and inspect Buy Board.
8. Open a second browser/incognito session; change breakout in one and confirm
   the other updates without reload. Try one deliberately stale edit.
9. Resize to about 390x844 or use the phone; confirm no horizontal page scroll,
   bottom safe-area clearance, chart gestures, Draw, Stocks, Home, and Buy Board.
10. Reload and restart the web server; verify plans/drawings persist.
11. In the browser console, clear metrics, review several symbols and 1D/1H,
    then inspect `window.__quantWebMetrics.snapshot()`.

Requires **CONNECTED** mode and already-reviewed allowlists (another 5-10
minutes; do not enable live trading):

12. Confirm real scanner/search/1D/1H and canonical Watchlist/Buylist state.
13. Repeat one Watchlist, breakout, and Buylist action; verify PC/laptop and a
    second browser converge without manual refresh.
14. Verify Buy Today draft-only behavior with operator actions disabled. If
    intentionally enabled, verify Operator Control ownership, activate/remove,
    and plan publication status—never place an order as part of this UAT.
15. Confirm Buy Board warnings/readiness are readable and that no browser
    ARM/order/risk/power control exists.

Supabase is not required for this checklist.

## L. Merge recommendation

**READY FOR USER ACCEPTANCE**

Do not merge to master yet. Complete the physical browser/iPhone checklist and
resolve or explicitly accept the listed environment blockers first. Supabase
is optional and does not block localhost acceptance.
