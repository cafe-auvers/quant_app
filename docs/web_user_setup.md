# Localhost Web Dashboard User Setup

The local SANDBOX workflow is the first acceptance target. Supabase, public
hosting, Docker, and a domain are not required.

The current validated implementation and 20-30 minute acceptance checklist are
in [web_final_validation.md](web_final_validation.md). Use
`codex/mobile-operator-control`; do not continue feature work independently on
the already-contained `codex/localhost-web` branch.

## Local workstation

1. Use the isolated web worktree; do not install web packages into a checkout
   that is currently running the executor.
2. Create the web environment and install the verified lock:

   ```powershell
   python -m venv .venv-web
   .\.venv-web\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
   ```

3. Copy `config/web.example.json` to the gitignored
   `config/web.local.json`. Keep `mode` set to `SANDBOX`, bind to
   `127.0.0.1`, and leave canonical writes disabled.
4. Bootstrap the only local user. The command prompts securely and has no
   default password:

   ```powershell
   .\.venv-web\Scripts\python.exe scripts\bootstrap_web_user.py
   ```

5. Check configuration without changing live state:

   ```powershell
   .\.venv-web\Scripts\python.exe scripts\check_web_readiness.py
   ```

6. Launch the loopback service:

   ```powershell
   .\.venv-web\Scripts\python.exe scripts\run_web.py
   ```

7. Open `http://localhost:8080`, sign in, and confirm the header says
   `SANDBOX`, `DEMO` (unless a read-only source was configured), and
   `NOT SYNCED TO EXECUTOR`.

Do not place passwords, session tokens, database credentials, or Supabase
secret keys in tracked files, screenshots, or chat. Each login creates a
cryptographically random session token; only its SHA-256 hash is stored. The
launcher never modifies `.env` or `.env.pc`.

## Optional read-only real data

Set the local SQLite mirror path in `config/web.local.json` only after the
readiness command confirms it can be opened read-only. Coverage limitations
are displayed in the app. SANDBOX planning remains isolated even when chart
and scanner reads use real local data.

The mirror is opened with SQLite `mode=ro` and `PRAGMA query_only=ON`. Expected
tables are `scanner_metrics` (plus an optional completed-snapshot/profile
table), `price_history` for daily bars, and `hourly_price_history` for hourly
bars. Missing tables or insufficient coverage appear as `UNAVAILABLE` or
`PARTIAL`; startup never creates or migrates the mirror.

## Connected PC-to-phone mode

For a private phone/PWA that should show the PC's real state, set these values
only in gitignored `config/web.local.json`:

```json
{
  "mode": "CONNECTED",
  "pc_repository_path": "C:\\path\\to\\quant_app",
  "canonical_planning_reads": true,
  "canonical_environment": "PROD",
  "canonical_account_no": "",
  "canonical_planning_writes": true,
  "connected_passive_operations": [
    "add_watchlist", "remove_watchlist", "promote_buylist",
    "move_watchlist", "set_breakout", "clear_breakout"
  ],
  "connected_operator_operations": [
    "activate_buy_today", "deactivate_buy_today", "publish_today_plan",
    "update_orb_settings"
  ]
}
```

Keep both write allowlists empty for read-only installations. Enable the
operator list only on the private web host used for Operator Control; the
server re-verifies the current shared Operator Control owner at request time.

An empty `canonical_account_no` is accepted only when exactly one production
account exists. The server reads the PC mirror with SQLite read-only mode and
loads only the PC repository's `COORD_DB_*` credentials into a dedicated
SELECT-only TiDB adapter. It never copies those credentials to the browser or
the web database, and it never runs schema creation during adapter startup.

In this mode the phone receives current Scanner/search/chart data and
canonical Watchlist, breakout, Buylist, and Buy Today projections. Compact
authenticated WebSocket invalidations refresh other open web/PWA sessions,
while typed external change pulses refresh the PC/laptop desktop projections.
The 10-second revision/status check remains a missed-event fallback. Manual
refresh is not required for routine changes. Passive planning and mobile
operator operations are separate explicit allowlists. When the latter is
enabled, an authenticated phone can activate/remove an unsubmitted canonical
Buy Today card, publish Today's Plan, and edit the six shared ORB bounds only
while its selected device owns Operator Control. ORB settings use revision/CAS
writes; typed change pulses make the PC and laptop reload the same `settings`
document automatically. A separate Execution Owner receives queued
broker-facing commands; the browser never assumes its identity. Plan
publication is rejected during regular market hours and reuses the six-document
revision/CAS/read-back workflow. No browser endpoint places an order, transfers
execution ownership, or controls power.

## Local cache, backup, and drawing import

The cache keeps one current gzip bundle and one manifest per
symbol/timeframe under `data/web/chart_cache/`. Its default retention is 350
symbols; local retained symbols and canonical Watchlist, Buylist, Buy Today,
and position symbols observed by the web projection are pinned. Rebuild a
bounded cache without starting the UI:

```powershell
.\.venv-web\Scripts\python.exe scripts\publish_web_snapshots.py --limit 300
```

Back up `data/web/web_state.db` only while the web process is stopped (or use a
SQLite-aware backup tool). That file contains password hashes, sessions,
sandbox plans, drawings, tombstones, command idempotency, and audit rows. Chart
cache files are reproducible and need not be backed up. This backup is separate
from production trading evidence and never replaces it.

Legacy drawings are inspected first and applied only on an explicit second
command. Stable IDs make a repeat import idempotent, while conflicting source
files are reported instead of silently overwriting one another:

```powershell
.\.venv-web\Scripts\python.exe scripts\import_web_drawings.py --dry-run data\chart_drawings.json
.\.venv-web\Scripts\python.exe scripts\import_web_drawings.py data\chart_drawings.json
```

## Acceptance workflow

- Review at least 20 consecutive symbols in 1D and 1H.
- Confirm chart source, actual dates, bar counts, session policy, adjustment,
  and freshness are visible and honest.
- Search for a symbol outside the current scanner list.
- Create, update, and delete a line; confirm breakout planning is unchanged.
- Set a breakout without adding to Watchlist, refresh/restart, and confirm it
  persists. Confirm Buylist rejects a symbol with no breakout, then accepts it
  after a positive breakout is saved.
- Open a second browser session and confirm a stale planning/drawing revision
  is rejected instead of overwriting newer state.
- With operator operations disabled, confirm Buy Today remains a draft.
- With operator operations enabled on a test account, confirm activate/remove
  is immediate in the UI, reconciles after backend confirmation, and appears
  automatically on a second browser and running desktop. Force a rejection and
  confirm rollback. Broker-facing commands are queued when required by split
  Operator Control/Execution Owner roles.
- Confirm Today's Plan cannot publish during the regular session and that a
  successful closed-session publish changes no broker order state.
- Confirm there is no ARM, order, ownership, global-risk, or
  workstation-power action.

## Optional Supabase setup (later)

After local acceptance, create a dedicated development Supabase project and:

- create one application user and record its UUID;
- disable public signup and anonymous access;
- apply only the reviewed migrations generated by this repository;
- keep the `chart-cache` bucket private and apply the generated RLS/Storage
  policies;
- put the project URL, publishable key, and allowlisted user UUID only in the
  gitignored web configuration;
- keep any privileged publisher key server-side and separate;
- leave canonical planning writes disabled in new installations until the
  account scope and permitted Operator Control identity (`Mobile Web` or the
  hosting desktop) are verified. Supabase
  publication itself grants no operator authority; connected Buy Today remains
  separately controlled by the reviewed operator allowlist, and broker/lease,
  risk, and power endpoints remain denied.

Apply the versioned SQL files in order from `supabase/migrations/` using the
dedicated development project's SQL editor or migration tooling. Review every
file before applying it. Then copy `config/web.publisher.example.json` to the
gitignored `config/web.publisher.local.json` and supply the server-only secret
there, never in `config/web.local.json` or frontend code. After RLS, Storage,
allowlist, and private-bucket checks pass, an explicit cloud publication is:

```powershell
.\.venv-web\Scripts\python.exe scripts\publish_web_snapshots.py `
  --publisher-config config\web.publisher.local.json --limit 300
```

The publisher serializes each current object, marks its manifest `UPDATING`,
uploads one overwrite, retries bounded cache-bypassed reads until the returned
compressed-byte hash matches, and only then marks it `READY`. An interruption
becomes `FAILED`; no archive generations are retained.

The publishable key is not user authentication. The database password is not
an API key. Supabase does not host the Python application.

## Private phone access from the always-on PC

`localhost` on a phone means the phone itself. Keep the Python host running and
use private Tailscale Serve HTTPS if phone access is needed. Add only the exact
private hostname to trusted hosts/origins and authentication redirects. Do not
enable Funnel, router port forwarding, public buckets, or exposed SQL ports.

For PC-only availability, run both the Python web server and Tailscale Serve
on the always-on PC. Open the **PC's** Tailscale HTTPS hostname on the phone.
A laptop's Tailscale hostname routes to that laptop, even when the trading
executor and canonical database are available on the PC. Restarting a laptop
web server restores that topology but does not provide PC-only availability.

Use a separate web deployment and environment on the PC. Set its
`pc_repository_path`, `local_mirror_path`, and `watchlist_history_path` to the
PC's local trading repository/data files. Set `base_url`, `trusted_hosts`,
and `trusted_origins` to the PC web origin. When migrating an existing web
dashboard, transfer a SQLite backup of its `web_state.db` to preserve the
existing login, drawings, and drafts; stop the old web service before the final
backup so it cannot keep accepting edits into a second database. Update the
phone's bookmark/home-screen app to the PC URL.

A desktop mobile viewport is emulation, not a real iPhone test. Installing the
PWA installation does not make the authenticated local API available offline.

Tailscale Serve forwards requests to the web process on the configured host.
If that process stops or the host reboots without restarting it, the phone can
keep showing the open dashboard while API requests fail with **502 Bad Gateway**.
The trading executor running on another PC does not keep this web host alive.

On Windows, register the web dashboard to start at logon and restart one minute
after a process failure. Run this on the machine hosting Tailscale Serve, using
the Python environment already installed for the web dashboard:

```powershell
.\scripts\setup_web_task.ps1 -PythonPath .\.venv-web\Scripts\python.exe -StartNow
```

Use an absolute `-PythonPath` if the web environment is in another checkout.
The task uses `pythonw.exe` and a small supervisor to run in the background,
and restart the web server after any exit. It preserves the configured
authentication and connection settings and appends startup/errors to
`data/logs/web_service.log`. Its runtime limit is disabled, it can run on
battery, and duplicate task launches are ignored. Stop a manually launched web
server before starting the task so that only one process owns the port.
Inspect it with `Get-ScheduledTask -TaskName QuantApp_WebDashboard`; disable it
with `Disable-ScheduledTask -TaskName QuantApp_WebDashboard`. Phone access still
requires this host to be powered on, awake, and signed in.

## Known limitations and blocked checks

- Supabase Auth/RLS/Storage/Realtime and two-browser cloud synchronization are
  unverified until a user-owned development project and credentials exist.
- Canonical CONNECTED reads and explicitly allowlisted writes are supported.
  New installations keep writes off until the account/environment, hosting-PC
  identity, Operator Control, revision recovery, and rollback behavior are
  verified. The SANDBOX workflow is unaffected.
- The deterministic DEMO, HTTP/API, and configured read-only PC mirror paths
  have current local evidence. Real iPhone/PWA testing needs a trusted
  private HTTPS origin and is not claimed by desktop viewport emulation.
- The dashboard shell is static and has no server-driven UI hydration. It uses
  one authenticated invalidation WebSocket; canonical data still comes from
  normal API reads. Its limited service worker caches presentation assets only;
  offline planning edits are deliberately disabled and never queued for
  background replay.

See [Web/PWA Operator Synchronization](web_operator_sync.md) for the complete
confirmation, pulse, fallback, and execution-safety contract.

## Intraday Watchlist monitor

The mobile bottom **Watchlist** button opens **Monitor** by default. It combines
Watchlist, Buylist, canonical Buy Today, and current Buy Today drafts into one
deduplicated list. Each stock shows its latest available price, saved breakout,
distance from that level, and quote time. Stocks that broke out today appear
first. **Broken out**, **ORB passed**, **Buy Today**, and symbol search narrow
the view; tap a stock to review its chart and use the existing planning actions.
New intraday additions enter the next refresh cycle automatically.

For each 1m, 5m, and 30m range, two results are shown separately:

- **Price breakout** passes after a regular-session minute bar, following the
  completed opening range, trades strictly above both the saved breakout and
  the opening-range high. Confirmation remains visible after a pullback; the
  headline then says **Broke out · pulled back** when below the saved level.
- **Position bounds** checks the existing passive entry geometry, uses ORH as
  the execution level and ORL as the stop, and evaluates the desktop's eight
  risk cases (0.25%–2%) against shared capital and stop/ADR bounds. A valid case
  passes independently of the price test. Failed bounds include their reason.
  Missing equity, shared settings, complete minute bars, or daily ADR show an
  unavailable result instead of a pass.

Connected mode uses a separate web-process Yahoo/yfinance worker with batches
of at most 40 symbols and four download threads. It polls once per minute
during extended US trading hours, caches completed daily ADR, pauses after
three minutes without a visible monitoring client, and avoids repeated closed
market downloads. Yahoo data can be delayed. Quote age and failed refreshes are
explicit; partial failures retain prices but suppress fresh signals. A failed
quote batch backs off for two minutes; failed daily history retries after
15 minutes. There are no broker calls, scanner runs, order submissions, or
execution-state changes from this monitor. Sandbox does not download quotes.

Restart the updated web process and desktop app to enable the monitor and
equity projection. The desktop publishes a small gitignored
`data/monitor_equity.json` file asynchronously from its existing account
snapshot; it preserves the actual account fetch time. Position checks require
an account snapshot no more than 15 minutes old. Refresh the desktop account
when the monitor reports unavailable equity. Timing overrides belong in
`config/runtime.local.json`: `WEB_MONITOR_REFRESH_SECONDS` (minimum 60),
`WEB_MONITOR_QUOTE_MAX_AGE_SECONDS` (default 180), and
`WEB_MONITOR_EQUITY_MAX_AGE_SECONDS` (default 900). These display checks are
advisory; the existing execution workflow still applies its own live gates.

On weekends and holidays, Monitor keeps saved breakout levels and the latest
available price visible with its actual quote date. Off-hours minute downloads
use a five-day lookback; missing minute quotes fall back to completed daily
closes from the existing ADR download. A daily close never supplies an intraday
breakout or ORB pass. The closed-market layout focuses on the two prices and
hides unavailable intraday checks until the next session.

Pages and their CSS/JavaScript are loaded from one release snapshot. Content
fingerprints in asset paths bypass older phone service workers immediately;
the current worker removes obsolete Quant caches and caches only the icon and
manifest. A normal refresh after restarting the web server loads the release.
Price API requests time out after 25 seconds and retry on the next check;
initial quote batches are observed through cached snapshots every five seconds.

The Executor indicator reads the current execution owner's canonical
`runtime_device_state` heartbeat and its existing freshness threshold, with
the legacy process heartbeat used only for older deployments. Starting and
standby states are amber; stale, stopped, or blocked owners remain unhealthy.
The status includes the owner, heartbeat age, and reason without changing
execution ownership or readiness gates.
