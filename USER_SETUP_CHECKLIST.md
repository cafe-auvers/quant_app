# Your setup checklist — Quant Web localhost

Prepared: October 1, 2026. The localhost application is the first milestone; cloud integration is optional until the local workflow is working.

> **Current connected-mode update — 2026-10-02:** The original sandbox-first
> steps below remain the safe installation baseline. After that acceptance,
> connected mode may separately allow canonical passive planning and guarded
> mobile Buy Today/plan-publish operations. Actions update the initiating
> browser immediately, roll back on failure, and automatically invalidate peer
> browsers and PC/laptop desktop projections. Manual refresh is not required.
> This does not grant execution authority; read
> [docs/web_operator_sync.md](docs/web_operator_sync.md) before enabling writes.

## A. Before starting Codex

- [ ] Open the existing `cafe-auvers/quant_app` checkout on the PC or laptop where you will develop. Give Codex `CODEX_LOCALHOST_WEB_PROMPT.md`.
- [ ] Use an isolated development worktree/branch and Python environment. Do not run dependency upgrades or branch changes in a checkout serving active trading positions.
- [ ] Tell Codex to use the existing configured market-data source or SQLite mirror read-only. It must detect actual coverage and must not print database credentials.
- [ ] Run the generated local-user bootstrap and launch commands. Create your own local login; do not use a default password.
- [ ] Open `http://localhost:8080` on the development machine. Keep sandbox mode and shared canonical writes OFF for the first acceptance test.

You do not need to buy a domain, subscribe to hosting, create a Supabase project, install Docker, or configure Wake-on-LAN for this first test. The prompt requires local adapters so these are not blockers.

## B. Optional Supabase setup, after the local interface works

### Project and account

- [ ] Sign in to Supabase or create an account. Create a dedicated development project such as `quant-web-dev`, separate from unrelated applications.
- [ ] Select Northeast Asia (Seoul) / `ap-northeast-2` when available. Save the database password in a password manager. Select a development plan based on the limits shown in your dashboard; no paid upgrade is required by this implementation specification.
- [ ] Enable strong authentication/MFA for your Supabase dashboard account. This administrator login is separate from the application user created below.

### App authentication

- [ ] In Authentication → Users, create your application user through the dashboard's user-management action. Use email/password initially to avoid making email-link delivery a prerequisite for development. Copy that user's UUID.
- [ ] Disable public/new-user signup and anonymous sign-in. The Python API and database policies must also allow only your user UUID; disabling signup alone is not authorization.
- [ ] In Authentication → URL Configuration, set the local Site URL to `http://localhost:8080`. Add the exact callback/reset URLs implemented by Codex. A planned callback can be `http://localhost:8080/auth/callback`, but it must match the actual application route.
- [ ] Later, add the exact private HTTPS origin/callback used for phone access. Do not add unrestricted production wildcard redirects.

### Connection values

- [ ] Copy the Project URL and publishable API key from the project's connection/API settings into Codex's generated gitignored web configuration.
- [ ] Set the allowlisted application-user UUID in that configuration. Generate the web session secret locally using the provided setup tool.
- [ ] Only when enabling the snapshot publisher, create/store its server secret key in the separate protected publisher configuration. Do not put privileged keys in frontend configuration, browser storage, Git, screenshots, or chat.

Expected configuration concepts, with exact names supplied by Codex:

| Value | Purpose |
|---|---|
| `SUPABASE_URL` | Project API endpoint |
| `SUPABASE_PUBLISHABLE_KEY` | Application API key; not a substitute for user authentication |
| `WEB_ALLOWED_USER_ID` | Your application-user UUID |
| `WEB_SESSION_SECRET` | Locally generated web-session protection secret |
| `SUPABASE_SECRET_KEY` | Privileged publisher/server credential only |
| `WEB_CANONICAL_PLANNING_WRITES` | Remains false until a separate explicit integration test |

The Postgres database password is not an API key. Existing TiDB/MySQL credentials stay separate; do not replace them with Supabase values.

### Database and chart storage

- [ ] Review and apply the migrations produced by Codex to the dedicated development project. Do not manually invent tables in advance: table names, policies, and application code must match.
- [ ] Create or verify a PRIVATE bucket named `chart-cache`. Apply generated Storage policies and table Row Level Security policies; do not temporarily make the bucket public to fix access errors.
- [ ] Enable Realtime only for the tables/channels the implementation actually uses. It does not automatically subscribe to TiDB changes.
- [ ] Run the provided integration/readiness tests: permitted user succeeds; signed-out and wrong-user requests fail; upload/read/overwrite/delete behave correctly; no secret appears in the browser.
- [ ] Upload a small selection of symbols first, verify actual chart dates/indicators/checksums, then publish the relevant scanner/planning universe. Review measured storage and transfer in the dashboard rather than relying on earlier estimates.

## C. Phone/PWA access to the localhost build

- [ ] Keep the machine running the Python web server powered on and the web service running. A Supabase account does not make that local Python application independently available.
- [ ] Connect the development machine and phone to your existing private Tailscale network. Do not configure a new public port-forwarding rule.
- [ ] Configure Tailscale Serve to proxy the local web port over its private HTTPS address; verify access rules and that public Funnel is not enabled for this service.
- [ ] Add that exact hostname to application trusted origins/hosts and the relevant Supabase redirect settings. Do not disable security checks to make the proxy work.
- [ ] Open the private HTTPS address in the phone browser, sign in, and test the layout. Use the browser's Add to Home Screen/install action where supported.

`localhost` on the phone refers to the phone, not the PC. Plain LAN HTTP and a real HTTPS PWA are different test environments. An installed icon does not make a server-driven application fully offline.

## D. Your acceptance test

- [ ] Scan/filter/sort results, then review at least 20 symbols consecutively in both timeframes.
- [ ] Verify displayed values, indicator types, actual date range, data source, and freshness against the desktop.
- [ ] Search for an instrument outside the scanner; verify cached, partial-history, and unavailable-data behavior.
- [ ] Create, edit, and delete a visual line. Check that the breakout price does not change.
- [ ] Add to Watchlist, set a permitted breakout target, and move to Buylist in sandbox. Refresh/restart and confirm persistence.
- [ ] Edit one item from two browser sessions and confirm stale edits are rejected instead of silently overwriting newer state.
- [ ] Verify the UI says local/sandbox rather than synced to executor. Enable actual shared planning/operator operations only during a separately controlled test after the write path, exact account/environment, and permitted Operator Control identity (Mobile Web or the hosting desktop) are verified.
- [ ] In connected mode, confirm Watchlist, Buylist, breakout, and Buy Today update immediately, survive canonical read-back, and appear on other browser and desktop screens without manual refresh. Force a rejected/stale request and confirm the optimistic state rolls back.
- [ ] Confirm there is no browser broker-order, execution-lease transfer, ARM/global-risk, or power-control endpoint. A separately allowlisted Buy Today action records canonical monitoring intent only; it is not an order or fill.

## Not part of this milestone

Public hosting, custom domains, independent access while the web host is off,
headless executor extraction, remote arming, shutdown/restart/Wake-on-LAN, and
production gate closure remain outside this milestone. Guarded connected Buy
Today intent and closed-session plan publication are now implemented through
the existing canonical workflow; broker execution remains exclusively owned by
the separately gated desktop runtime.

## References checked on October 1, 2026

- Supabase regions: https://supabase.com/docs/guides/platform/regions
- Supabase authentication settings: https://supabase.com/docs/guides/auth/general-configuration
- Supabase user management: https://supabase.com/docs/guides/auth/managing-user-data
- Supabase redirect URLs: https://supabase.com/docs/guides/auth/redirect-urls
- Supabase API keys: https://supabase.com/docs/guides/getting-started/api-keys
- Supabase Row Level Security: https://supabase.com/docs/guides/database/postgres/row-level-security
- Supabase Storage access: https://supabase.com/docs/guides/storage/security/access-control
- Supabase overwrite caveat: https://supabase.com/docs/guides/storage/uploads/standard-uploads
- PWA installation requirements: https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable
- Private Tailscale Serve: https://tailscale.com/docs/features/tailscale-serve
