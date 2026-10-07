# Codex implementation prompt — Quant Web, localhost milestone

> **Implementation status — 2026-10-02:** This file preserves the original
> localhost safety baseline. The implemented connected milestone now also has
> separately allowlisted canonical Watchlist/Buylist/breakout writes, guarded
> Buy Today activation/removal, closed-session plan publication, immediate
> optimistic UI feedback, authenticated web-client invalidations, and typed
> PC/laptop desktop refresh pulses. The browser still never constructs a
> broker, owns the execution lease, changes risk, or controls workstation
> power. Current behavior is normative in
> [docs/web_operator_sync.md](docs/web_operator_sync.md),
> [docs/execution_operator_control.md](docs/execution_operator_control.md), and
> the web worktree's `docs/web_user_setup.md`.

## Objective and priority

Work in the existing `cafe-auvers/quant_app` repository. Implement a usable, private, responsive web dashboard that runs locally first. This is the next project priority because the current PyQt5 workflow is uncomfortable. Do not defer it until Gates 4–5 close.

This is an implementation task, not just an architecture proposal. Deliver working code, tests, launch instructions, and a maintained task list. Build in small verified increments. When external credentials are missing, finish the local implementation, report the exact integration blocker, and do not fabricate successful integration evidence.

The user reviews 200–300 stocks daily, usually both 1D and 1H. The core workflow is:
Scanner or symbol search → charts → drawings → Watchlist → breakout/planning → Buylist.

The web app becomes the primary review/planning interface. Keep the existing PyQt application functional during migration; do not rewrite or relocate the executor in this milestone.

## 1. Inspect before changing anything

Read `AGENTS.md` wherever applicable, `README.md`, `PROJECT_ARCHITECTURE.md`, and these documents when present:
- `docs/current_order_logic.md`
- `docs/orb_buyboard_planning.md`
- `docs/execution_operator_control.md`
- `docs/supabase_coordination_migration.md`
- `docs/kanban_architecture.md`
- `docs/activation_gate_specification.md`
- `docs/web_operator_sync.md`

Inspect the real scanner repositories, chart-data loaders/renderers, drawing persistence, state-sync/pulse protocol, typed board commands, planning services, and tests. Known areas include `src/infrastructure`, `src/services`, `src/core`, `src/ui/charts`, and `src/ui/mixins/chart_command_routing_mixin.py`. Verify actual paths rather than inventing them.

Record the current SHA and baseline tests. Use a feature branch/worktree; do not reset user changes, switch an active executor's checkout, overwrite secrets, or alter running services. Prefer an isolated `.venv-web` so adding UI dependencies cannot change the running executor's environment. Do not push/merge unless separately requested.

Create `docs/web_localhost_plan.md` and `docs/web_localhost_todo.md` immediately. Track TODO / IN_PROGRESS / DONE / BLOCKED, dependencies, changed files, and verification evidence. Add the user-owned setup tasks in `docs/web_user_setup.md`. Keep these updated throughout implementation.

## 2. Stack and process boundaries

Use Python for application services, API, UI orchestration, publishers, command handling, launchers, and tests. Default to FastAPI plus NiceGUI for a Python-authored responsive UI, preserving clean service boundaries. Pin compatible dependencies using the project's existing dependency conventions and verify supported Python versions.

Reuse the existing Lightweight Charts/browser rendering assets where viable. Adapt their Qt bridge to an authenticated web transport. Browser-required HTML/CSS, chart assets, manifest, and service-worker resources must stay presentation-only. Do not introduce a React/Node/TypeScript or Deno backend; do not move strategy/risk logic into browser code.

Run one local web service on `127.0.0.1:8080` by default. Make port/base URL configurable. Use a dedicated launcher such as `python scripts/run_web.py`; never invoke `main.py` to start the web app. The web process must not instantiate QApplication, MainWindow, the trading runtime, a real broker, or a KIS WebSocket.

Extract only the smallest headless read/planning services needed from Qt-coupled modules, preserving existing behavior and callers with characterization tests. Do not import a Qt window to obtain a method. Keep blocking DB/provider/compression work off the web event loop using bounded workers. Do not launch duplicate publishers or pollers during reload.

Suggested additive layout, adapted to existing repository conventions:
- `src/web/`: configuration, authentication, pages, API, request/response models, events, presentation assets.
- `src/services/web_*`: headless application adapters only where a shared service does not already exist.
- `scripts/run_web.py`, `scripts/check_web_readiness.py`, `scripts/publish_web_snapshots.py`.
- `tests/web/`, `docs/web_*`, and isolated gitignored `data/web/` state.

## 3. Safety and operating modes

Implement two explicit modes, always visibly labelled:

SANDBOX, default:
- Use isolated local SQLite planning/drawing data and a local chart-cache directory.
- Reuse domain rules with sandbox persistence; never make the sandbox a production authority.
- Allow read-only imports of real market history/scanner outputs when configured. Otherwise use deterministic, explicitly labelled demo fixtures.
- Watchlist/Buylist/planning edits persist locally but are labelled NOT SYNCED TO EXECUTOR.
- Never silently upload sandbox plans when switching modes.

CONNECTED:
- Read real market data through existing local repositories or configured Supabase adapters.
- coordination database reads are allowed only with explicit configuration; shared planning writes default OFF.
- Implement an independently enabled allowlist of passive canonical planning commands through the existing services. Require authenticated operator identity, explicit account/environment, valid current Operator Control where required, expected revisions, and existing lifecycle fences.
- Keep a separate operator allowlist for guarded Buy Today activation/removal and closed-session plan publication. Revalidate that either the stable Mobile Web identity or the exact hosting-desktop identity owns Operator Control for every request; never grant the browser Execution Owner authority.
- Do not impersonate the PC's operator identity. Unsupported authority combinations must remain read-only with a clear reason.

In both modes, this milestone must refuse ARM, FULL_LIVE promotion, direct BUY/SELL/CANCEL/REPLACE, ownership transfers, global risk-limit changes, and machine power actions at the server boundary—not merely hide buttons.

With connected operator operations disabled, Buy Today remains a read-only
canonical view plus a clearly non-executable local draft/preview. When the
separate operator allowlist and exact Operator Control identity are verified,
the web surface may use the existing canonical workflow to activate/remove an
unsubmitted Buy Today card and publish the complete plan outside regular market
hours. It must still refuse broker calls, execution-lease transfer, arming, and
all gate bypasses.

Do not modify `.env`, `.env.pc`, live runtime configuration, live database schemas, trading switches, current leases, or protection of existing positions. New web settings go in a separate gitignored file with a checked-in placeholder template. Do not stop an existing executor as part of web development.

## 4. Data ownership and local-first adapters

Keep each authoritative store unambiguous:
- PC MySQL: existing full market history, indicators, and scanner calculations.
- Laptop SQLite mirror: existing read-only market-data fallback, with its actual coverage limits.
- Supabase private PostgreSQL coordination schema: canonical TradeCards, Watchlist/Buylist/Buy Today, breakout targets, orders, reservations, ownership, and execution state.
- Optional Supabase API features: Auth, mobile scanner/metadata projections, chart object storage, and ordinary visual drawings; these are separate from private execution coordination.

The canonical coordination store already uses Supabase PostgreSQL. Do not create a second production Watchlist/Buylist in a public projection schema. Do not expose private production ledgers or secrets through browser APIs.

Provide clean interfaces for market/scanner reads, current chart-bundle storage, drawings, identity, and planning commands. Supply working local implementations and optional Supabase implementations. Core localhost acceptance must not require a Supabase account, Docker, public hosting, or a domain. Missing optional credentials must not prevent sandbox startup.

Use explicit configuration precedence and read-only DB connections. Do not call legacy startup/bootstrap code that creates tables or writes production state while constructing a read adapter.

## 5. The actual review interface

Build a polished review workspace, not a screen full of diagnostics:
- Desktop: collapsible stock list, large 1D/1H chart workspace, compact planning panel, status strip.
- Phone portrait: one large chart with 1D/1H switch; accessible previous/next buttons and planning drawer.
- Wide/landscape: optional split 1D/1H charts.
- Preserve selected symbol, scanner/setup, sorting, filters, list position, timeframe, and chart range across ordinary refreshes.
- Use readable contrast, adequate touch targets, and no clipped controls at 390px width.

Scanner:
- Load existing completed scanner results rather than rescanning on page navigation.
- Preserve existing strategy names, filters, ranking, and values; do not invent a strategy or scoring method.
- Show source/session date and freshness. Last completed data is acceptable only when clearly identified.
- Support keyboard next/previous on desktop without intercepting typing, and touch navigation on mobile.
- Symbol search must cover the configured instrument universe, not only scanner matches. Resolve symbol/exchange identity and aliases through existing mappings.

Charts:
- Interactive candlesticks, volume, the indicators already supported by the app, earnings markers/data when available, company/sector/industry, existing RS/context metrics, canonical breakout overlay, and ordinary drawings.
- Reuse indicator definitions and chart semantics. Do not replace an existing EMA with an SMA or invent missing earnings/factor values.
- Missing data must display UNAVAILABLE/PARTIAL rather than fabricated zeros.
- Keep the chart mounted while changing symbols; do not rerender the entire page for each candle set or pointer event.

## 6. Historical coverage and correctness

Publish/display up to the latest 750 completed daily bars, approximately three years, where the source has them.

For 1H, target the latest six calendar months of the configured session data. The previously discussed 800 bars was an estimate, not an exact six-month rule. Actual counts depend on sessions and bar alignment. Make lookback configurable without changing strategy parameters.

Show actual start/end dates, bar counts, source, adjustment mode, session policy, and last completed bar. A new listing or limited provider history must be shown honestly. A current unfinished candle must be separately labelled, not counted as a completed bar.

Preserve the source's regular/extended-hours and hourly aggregation conventions. Use exchange-session dates for daily bars and timezone-aware timestamps for intraday bars. Test DST, shortened sessions, duplicate timestamps, gaps, and unsorted inputs.

Reuse indicators computed with sufficient prior history before trimming the visible window. Never compute the 200-period indicator from an already truncated range without the necessary warm-up. Preserve split/adjustment consistency across OHLCV, indicators, and annotation coordinates. Cache keys must include material source/adjustment changes.

## 7. Loading, publication, and cache behavior

Automatically prepare chart data for the union of the latest scanner results, Watchlist, Buylist, Buy Today, positions, and retained manually searched symbols. Expose a bounded retention policy for unreferenced symbols; never evict a pinned planning/position symbol accidentally.

For a manually searched symbol not cached:
1. Return available local/source data immediately when possible.
2. Otherwise enqueue one deduplicated bounded job using existing data-provider code.
3. Show QUEUED / LOADING / READY / PARTIAL / FAILED with useful reasons.
4. Load 1D first; load 1H independently so it cannot delay the first chart.
5. Repeated search must not start duplicate provider requests.

Do not fetch KIS through a second web-owned session. Provider refresh jobs use the existing non-execution acquisition path and preserve its rate limits. If the data host/provider is unavailable, state that; do not promise a maximum 20-second load.

The publisher must be an isolated, resource-bounded job, not part of order/stop evaluation. Add an opt-in post-refresh hook without modifying production schedules automatically.

Keep ONLY ONE CURRENT chart bundle per instrument/timeframe. Do not retain daily cloud archives or backup generations. A checksum/revision is metadata, not another retained history copy. Existing mandatory trading audit evidence is separate and must not be deleted.

Use compressed, validated chart bundles; benchmark actual serialized bytes rather than assuming an earlier size estimate. Local file replacement must be atomic. For cloud overwrite, serialize writers per object and use a publication state/checksum protocol: readers must never accept a partial/mismatched new object as READY. Storage upload and manifest DB update are not one transaction; handle failures between them explicitly.

Supabase can serve stale CDN content after an overwrite. Implement and test freshness verification, bounded retry/cache bypass, and a visible updating/unavailable state. A changed manifest alone does not prove a returned object is current. Do not solve this by retaining multiple cloud versions.

Separate price bundles from drawings and planning: drawing a line must not re-upload history. Use bounded client/server caches, ETags/checksums, and correct compression headers; prevent double decompression. Prefetch only the second timeframe and a small next/previous symbol window. Ignore late responses for an older selection. Match manifests to bytes actually served; a signed URL or changed query string alone is not a freshness guarantee.

Measure warm/cold render latency, provider-job latency, request counts, compressed bytes, and memory over a 300-symbol navigation run. Treat performance values as measured targets, not guarantees. No SQL on crosshair/pan/zoom. Repeated unchanged views must not refetch whole tables.

## 8. Planning commands and synchronization

Implement real forms and service calls for passive Watchlist add/remove, Watchlist ↔ Buylist, and permitted set/clear breakout/planning edits. Reuse the actual domain command names and constraints; do not invent a parallel state machine or raw-write canonical table fields from routes.

Every shared write needs:
- server-derived authenticated actor and approved device/control context;
- explicit instrument/account/environment and expected card/ownership/readiness revisions as applicable;
- durable command ID/idempotency key;
- payload validation, current lifecycle/control checks, and atomic compare-and-swap persistence;
- an authoritative committed response and audit record.

Same command ID plus same payload must not reapply. Same ID plus changed payload must reject. Stale revisions return a conflict with the current state; do not silently use last-writer-wins.

Respect session restrictions, immutable active targets, existing orders/positions/reservations, and the separate Operator Control rules. Display position-size calculations as estimates unless fresh exact-account inputs meet current requirements. Never equate a draft with executable authorization.

After commit, reuse/bridge existing typed change notifications. Other clients reload canonical state. Supabase Realtime is not enabled for the private coordination schema; preserve the existing authenticated bridge and add bounded revision-based recovery. Do not introduce one-second coordination database polling or per-card query fan-out.

Render permitted planning and Buy Today actions optimistically on the
initiating browser before waiting for persistence. Replace the pending state
with the canonical response on success and roll it back visibly on failure.
Use an authenticated invalidation WebSocket for other open web/PWA sessions and
typed external pulses for PC/laptop desktop projections. Notifications carry
scope/revision hints only; receivers refetch canonical state. Normal operation
must not require a manual refresh.

Show DRAFT, SAVING, SAVED TO CANONICAL STORE, CONFLICT, and UNAVAILABLE accurately. Only show EXECUTOR OBSERVED when a real revision acknowledgement supports it. Losing a notification must not lose the committed edit. If the executor is offline, distinguish canonical persistence from executor pickup.

## 9. Ordinary chart drawings

Preserve the existing line model and daily/hourly sharing behavior, after verifying current code/tests. Store stable drawing IDs, instrument, original anchor times/prices, visibility/timeframe metadata, revision, author, and deletion state. Use data coordinates, never screen pixels or transient bar indexes.

Ordinary drawings are visual only. Moving/deleting one must never modify a TradeCard breakout or stop. Set Breakout Price remains a separate explicit planning command.

Local mode persists drawings independently of chart snapshots. Supabase mode uses one canonical drawing store with authenticated updates, optimistic concurrency, and deletion tombstones to prevent resurrection after reconnect. Tiny revision/tombstone records are not historical chart-bundle archives.

Implement import of `data/chart_drawings.json` with dry-run reporting, stable IDs, idempotency, and source preservation. Identify conflicts across PC/laptop files instead of overwriting them. Add a small opt-in desktop sync adapter so existing PyQt charts can participate without creating a second authoritative drawing store. Do not enable it in a running deployment automatically.

Test create/edit/delete across two browser sessions, reload, offline/reconnect, and desktop adapter round trips. Preserve original hourly anchors when displaying daily projections; do not save projected daily coordinates back over intraday anchors.

## 10. Authentication, Supabase, and PWA

Local mode requires a secure one-user bootstrap/login, no default password, established password hashing/session libraries, server-side sessions, and a generated local session secret. No blanket localhost authentication bypass. Protect HTTP routes and UI/WebSocket event handlers, including reads. Validate Host/Origin, implement CSRF protection for state changes, limit login attempts, and avoid wildcard credentialed CORS. Restrict proxy trust. Protect docs/diagnostic routes and redact logs. Escape user text, reject script/HTML payloads in drawings, and use a tested Content Security Policy compatible with the chart/framework assets.

Supabase mode must authenticate and authorize a single allowlisted user UUID. Validate tokens using supported verification, not unverified JWT decoding. A publishable key is not user authentication. Session expiry, refresh, revocation/logout, and denied access must be handled without silently falling back to local bypass.

Generate reviewed, versioned migrations for the minimum needed scanner/metadata projection, chart manifest, drawings, and allowlist structures. Enable RLS and grants explicitly, including Storage policies. Prefer caller-scoped reads/writes; keep privileged publisher credentials isolated server-side. Test anonymous denial, wrong-user denial, permitted-user access, and protected fields. Secret/service credentials bypass RLS and must never enter frontend code, logs, source control, or cached responses.

Use a PRIVATE `chart-cache` bucket. Default cloud integrations OFF until configured. A project URL/publishable key is separate from a server secret key and from a Postgres password. Generate a placeholder environment template and clear dashboard setup instructions. Do not require Supabase Edge Functions or a production hosting provider.

Add the PWA manifest, icons, install metadata, and a deliberately limited service worker. Cache only approved static resources and bounded chart data. Do not cache authentication tokens, canonical planning/order responses, or mutation requests. Never background-replay a planning command after reconnect. Clear user caches on logout.

NiceGUI's server-driven interface is not a fully offline application: provide a clear disconnected/read-only/offline view, not a false promise of offline editing. Browser closure must not affect the separately running executor.

Document desktop localhost separately from phone access. On the phone, localhost means the phone, not the PC. Keep the server loopback-bound and document optional private HTTPS access through existing Tailscale Serve; do not configure public Funnel, port forwarding, or expose MySQL/PostgreSQL SQL ports. Phone PWA testing requires a trusted HTTPS origin. The local web host must remain running; Supabase storage does not host this Python application.

## 11. Monitoring and out-of-scope operations

Show read-only available positions/orders, executor role, last-seen timestamp, reconciliation state, alerts, and source freshness from stored projections. Missing/stale evidence means UNKNOWN/STALE, never a fabricated green status. Keep browser connectivity, web-service health, data-host availability, and executor health distinct.

Do not implement wake/shutdown/restart, executor handoff, live arming, or actual broker controls in this milestone. Do not replace the external watchdog with a browser heartbeat. Document these as later work, not nonfunctional buttons.

## 12. Tests, delivery, and acceptance

Use pytest and browser tests through the Python Playwright API or the framework's Python testing tools. Add deterministic local fixtures and dependency fakes. Prove that starting/stopping/reloading the web app, opening charts, drawing, and passive planning cannot construct or call a real broker or start an executor.

Required tests include: scanner parity/freshness; chart OHLCV/indicator/timezone parity; incomplete history; search outside scanner; duplicate load jobs; 1H failure without losing 1D; stale CDN hash after overwrite; interrupted publishing; chart-cache eviction; rapid symbol-switch races; drawing create/edit/delete/import; idempotent planning; same-key/different-payload rejection; revision conflicts; invalid control/account; canonical DB failure; active-card edit denial; blocked unauthorized Buy Today activation; guarded activate/remove and plan publication; optimistic ordering and rollback; authenticated WebSocket invalidation; typed desktop pulse publication; runtime state/writability projection refresh; denied execution/power endpoints; session expiry/CSRF; reconnect resync; no cloud SQL during chart gestures; and PyQt regression behavior.

Create a reproducible 300-symbol performance report with measured p50/p95, data sources, hardware, cache state, and limitations. Capture desktop/mobile viewport screenshots and inspect controls/chart behavior. Emulated mobile coverage must not be called a real iPhone test.

Deliver runnable launch, local bootstrap, read-only readiness, snapshot-publish, and drawing-import commands. Readiness diagnostics report missing items without modifying live state and never print secrets. Include environment examples, tested dependency installation instructions, migration application instructions, data-source mapping, cache policy, authentication setup, backup/import boundaries, and known limitations.

Definition of done:
1. One command starts the authenticated localhost web app without opening PyQt or contacting KIS.
2. Scanner → 1D/1H → drawing → Watchlist → breakout → Buylist works and persists in the isolated sandbox.
3. Configured real read-only data produces matching values, actual coverage dates, and no production writes.
4. Optional Supabase adapters/migrations and opt-in canonical passive-planning/drawing-sync paths are implemented and tested to the extent credentials allow; unverified external checks are explicitly BLOCKED, not marked passed.
5. Two-session concurrency and reconnect tests pass; failures do not silently overwrite plans or revive deleted drawings.
6. Guarded connected Buy Today intent may persist only through the explicit
   operator allowlist; no browser broker/power action, execution-owner claim,
   arming, or gate approval occurs.
7. Existing regression tests remain green, or failures are identified precisely without weakening tests.
8. `docs/web_localhost_todo.md` and `docs/web_user_setup.md` reflect real completion and remaining user tasks.

Update project documentation to make this localhost milestone the immediate priority. Do not edit historical gate evidence to claim it covers the new SHA; record change impact and follow the normative requalification policy before future activation.

At handoff report: files changed, exact launch commands, localhost URL, actual data mode, tested workflow, measured performance, test results, optional integration blockers, and exact user-owned setup steps. Do not end with a mockup, a roadmap-only answer, or an unsupported claim that live trading is ready.
