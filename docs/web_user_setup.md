# Localhost Web Dashboard User Setup

The local SANDBOX workflow is the first acceptance target. Supabase, public
hosting, Docker, and a domain are not required.

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

Do not place passwords, session secrets, database credentials, or Supabase
secret keys in tracked files, screenshots, or chat. The launcher generates a
local session secret if one is absent and never modifies `.env` or `.env.pc`.

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
    "activate_buy_today", "deactivate_buy_today", "publish_today_plan"
  ]
}
```

Keep both write allowlists empty for read-only installations. Enable the
operator list only on the web host whose exact local device identity is meant
to hold Operator Control; the server rejects it elsewhere at request time.

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
Buy Today card and publish Today's Plan only while the hosting PC's exact
identity owns Operator Control. A separate Execution Owner receives an
append-only queued command; the browser never assumes its identity. Plan
publication is rejected during regular market hours and reuses the six-document
revision/CAS/read-back workflow. No browser endpoint places an order, transfers
ownership, changes risk, or controls power.

## Local cache, backup, and drawing import

The cache keeps one current gzip bundle and one manifest per
symbol/timeframe under `data/web/chart_cache/`. Its default retention is 350
symbols; Watchlist/Buylist and retained searched symbols are pinned. Rebuild a
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

## Optional private phone access (later)

`localhost` on a phone means the phone itself. Keep the Python host running and
use private Tailscale Serve HTTPS if phone access is needed. Add only the exact
private hostname to trusted hosts/origins and authentication redirects. Do not
enable Funnel, router port forwarding, public buckets, or exposed SQL ports.

A desktop mobile viewport is emulation, not a real iPhone test. Installing the
PWA installation does not make the authenticated local API available offline.

## Known limitations and blocked checks

- Supabase Auth/RLS/Storage/Realtime and two-browser cloud synchronization are
  unverified until a user-owned development project and credentials exist.
- Canonical CONNECTED reads and explicitly allowlisted writes are supported.
  New installations keep writes off until the account/environment, hosting-PC
  identity, Operator Control, revision recovery, and rollback behavior are
  verified. The SANDBOX workflow is unaffected.
- The deterministic DEMO and HTTP/API paths have local evidence. Real mirror
  parity needs a configured mirror. Real iPhone/PWA testing needs a trusted
  private HTTPS origin and is not claimed by desktop viewport emulation.
- The dashboard shell is static and has no server-driven UI hydration. It uses
  one authenticated invalidation WebSocket; canonical data still comes from
  normal API reads. Its limited service worker caches presentation assets only;
  offline planning edits are deliberately disabled and never queued for
  background replay.

See [Web/PWA Operator Synchronization](web_operator_sync.md) for the complete
confirmation, pulse, fallback, and execution-safety contract.
