# Deployed PC, mobile, and laptop setup

Verified deployment snapshot: **2026-10-05**. This page describes the deployed
installation; fresh installations retain conservative defaults and need their
own configuration and release approval.

## What runs where

| Component | Current role | Required for normal PC/mobile operation? |
| --- | --- | --- |
| Always-on Windows PC | Runs the PyQt executor, KIS connection, local MySQL market database, and the separate supervised web service | Yes; keep it powered on, awake, and signed in |
| Supabase PostgreSQL | Canonical shared boards, settings, plans, commands, orders, reservations, ownership, readiness, and alerts | Yes |
| Laptop | Optional development/operator desktop with a pull-only SQLite market mirror | No; it can be powered off while the PC owns execution |
| Tailscale | Private phone HTTPS access and direct PC/laptop network access | Required for remote access in this installation |
| TiDB | Preserved former migration source | No; not a runtime dependency or automatic fallback |

PC-to-laptop market copying uses LAN/Tailscale and PC MySQL. Small shared
trading state uses Supabase. Direct listener pulses and WinRM use the private
network. None of these paths requires TiDB. If Supabase is unavailable,
ordinary shared writes and new entries fail closed; the app does not resume
the stale TiDB source or promote a private SQLite file into authority.

## Mobile and web access

The website is unchanged by the database migration. Use the existing PC
Tailscale HTTPS address, shaped like
`https://<pc-name>.<tailnet>.ts.net`, with Tailscale connected on the phone.
The exact private origin is in `config/web.local.json` as `base_url`.
It forwards to the PC web process on `127.0.0.1:8080`.

`QuantApp_WebDashboard` supervises the separate web checkout and restarts the
web server after exit. Supabase hosts the shared database, not this Python web
server. A 502 means checking the web host/proxy/upstream and its logs; it does
not by itself identify a database failure. The laptop being off cannot stop
the current PC-hosted website or executor.

The existing local web login, sessions, drafts, and drawings were preserved.
Optional Supabase Auth, Storage, Realtime, and public projection adapters are
separate features. `supabase_enabled=false` in web configuration does not
disable the PostgreSQL coordination connection.

## Shared database configuration

Every participating desktop and the web deployment must select the same
PostgreSQL store. Put non-secret settings in ignored
`config/runtime.local.json`, for example:

```json
{
  "COORD_DB_BACKEND": "postgresql",
  "COORD_DB_HOST": "<session-pooler-host>",
  "COORD_DB_PORT": "5432",
  "COORD_DB_NAME": "postgres",
  "COORD_DB_SCHEMA": "quant_coordination",
  "COORD_DB_SSL_CA": "config/certificates/supabase-prod-ca-2021.crt"
}
```

Use the project's IPv4 Session pooler, verified TLS, UTC server time, and the
restricted application role. Only `COORD_DB_USER` and `COORD_DB_PASSWORD`
belong in `.env`. The application role has private-schema usage, table DML,
and sequence access; it cannot create schema objects or roles. The private
schema is not exposed to browser/PostgREST roles. Administrator credentials
are provisioning-only. PC `MYSQL_*` remains a separate local historical-data
connection on port 3306.

Tracked defaults still support legacy MySQL deployments. They do not imply
that this installation uses TiDB. Preserve the local PostgreSQL overrides
when syncing environment schemas or restoring machines. See
[the migration and recovery procedure](supabase_coordination_migration.md).

## Trading and mobile edits

The PC is the deployed Execution Owner. Operator Control determines which
authenticated surface may send manual instructions. Mobile changes are
revision-fenced canonical writes or durable commands consumed by that owner;
the browser never calls KIS directly.

Shared ORB settings were read back and matched the PC's applied settings.
The mobile list buttons now show saving, confirmation, and errors beneath the
buttons. Adding a Watchlist card without a breakout price opens a price editor;
the saved price and returned revision are confirmed before promotion. Buy Today
still requires explicit activation confirmation. Canceling or a rejected save
does not activate the card.
Passive Buylist removal was verified through the deployed mobile API. Removal
waits for canonical confirmation, reports failure, and rejects stale revisions.
A legacy planning stop on a flat Buylist card is not an active stop order;
positions, working orders, unresolved exposure, and actual protection remain
guarded. See [operator synchronization](web_operator_sync.md).

The approved PC trading release is
`c106449c46accc6f249280bea372242bb816f8d5` (PR #137). The operator approved
its supervised 2026-10-05 restart and manually armed the new release after
broker reconciliation. Verification at 00:27 KST on 2026-10-06 confirmed
`ACTIVE`, executor/broker/feed/ownership/shared-state readiness, and live
execution enabled and effective for the 2026-10-05 NYSE session.
Future executor release changes require fresh exact-release review and manual
arming. This is a dated snapshot, not proof of future sessions or fills.

This release fixes the canonical capital-reservation reader used by portfolio
risk checks. A missing optional reader engine previously hid the existing
SVIA reservation and correctly blocked subsequent BUYs as unreconciled risk.
The worker now uses its canonical engine by default, and rejected attempts
retain the reason for operator visibility. Risk and broker guards are retained.

The separate web service runs `8693afa4bb64605004cd07c1c7cea3182c5d7df7`
(PR #140, including PR #138/#139). Its far-right header stays on one row and shows `Open in Xm`
before opening and `Close in Xm` during trading. Mobile Cancel Sell All can
withdraw an unsubmitted regular-session or premarket exit to Open Position,
preserving the holding and stop and clearing retired retry state. SELL
identities, reserved shares, working/ambiguous orders and unresolved
cancellation block withdrawal. This is a canonical intent edit, with no broker
submit or cancel. The 2026-10-06 web-only update preserved the running PC
processes, approved executor source, live switch, credentials and configuration.
The web commit is not approved for activation as a trading executor.


The 02:04 KST web-only deployment on 2026-10-06 also includes the ORB display
correction (PR #139) and stage-specific Buy Board details (PR #140). Cards and
detail sheets show stock/share totals, target or held quantities, entry plan,
planned/active stop, sellable shares, average entry, working exit quantities,
retry/block reasons and estimated P&L. Price source, KST observation time and
age are explicit; Yahoo is indicative, and stale prices do not produce P&L.
Snapshots and canonical projections are reused without additional SQL queries,
quote polling or broker calls. At 02:06 KST the PC remained ACTIVE on c106449c
with live execution enabled/effective; its processes and configuration were
preserved. The separate PC correction 8efad797 remains pending actual reviewer
approval and a supervised restart. Its prepared activation preserves web 8693afa4.

At the operator's request, SVIA returned to Open at 00:09 KST with 643 shares
and its $4.24 stop covering 643 shares. No SELL was working and no broker
mutation was made. The deployed mobile API confirmed Open, 643 shares and
$4.24 at 00:27 KST. Stops and other live risk policies still apply.

Passive Gate 2/3/4 collectors run alongside live trading under
`quant_evidence/qmco_reservation_fix_20261005/session/checks_report.json`;
the earlier release's evidence remains preserved. They collect feed health,
isolated shadow decisions and actual execution events. Reports explicitly say
`NOT_CERTIFIED`; full-session fault probes, complete branch/fence replay,
reviewed gate chains and Gate 4's three supervised dates remain required for
formal closure. See [live-session checks](live_session_checks.md).

Documentation is maintained separately from that armed executor checkout.
Do not pull even a documentation commit into the active executor: exact release
identity uses the full Git SHA. Deploy a newer executor checkout only through
the existing reviewed release and operator activation procedure.

## Automation and backups

- The PC morning data routine remains scheduled at 08:00 KST. While `main.py`
  is running, its resume mode skips Git/dependency changes.
- The old `Automatic-PC-Shutdown` task is disabled. AC sleep and hibernation
  are disabled for the always-on PC deployment. Legacy sleep/handoff scripts
  remain optional tools, not required daily operating steps.
- `QuantApp_CoordinationBackup` runs at **09:15 KST** and writes verified,
  dated snapshots to ignored `data/coordination_backups/`. Manual command:

  ```powershell
  python scripts/backup_coordination_store.py
  ```

This backup covers all **20 coordination tables**, including orders, audit
records, capital account locks, and daily trading events. It checks the
payload checksum and refuses a MySQL backend. Keep private encrypted copies
off the PC according to your backup policy; the installed daily task creates
local backups, not proof of an off-PC copy. Local JSON backup and web SQLite
backup remain separate recovery layers.

## Migration and TiDB retirement

All 20 source tables were exported with writers stopped, restored into the
empty private PostgreSQL schema, and verified row-for-row and by checksum
before target runtime writes. Original runtime credentials/configuration and
local/web state were backed up. The app now makes no ongoing TiDB coordination
queries, including for PC/laptop ownership, settings, commands, or backups.

The former TiDB database was left intact as an archive. Once Supabase accepted
new writes, that source became stale; it must not be restarted as a writable
peer or used as a current rollback. It can be retired after retaining the
verified export and required recovery material. Retirement is not performed
by this documentation update.

The user's current TiDB spending cap was left unchanged. Stopping this app's
TiDB traffic does not undo already incurred usage or describe other clients.
Supabase is currently on the existing Free project; monitor its actual
storage/transfer limits instead of promising unlimited free service.

## Verification evidence

For the deployed migration release: the local supported suite passed
**3,283 tests** with **10 opt-in tests skipped**; the cloud PostgreSQL suite
passed **7 tests**. Both CI Python versions, dependency audit, hygiene/secret
scan, and deterministic Gate 1 passed for the exact merged commit in
[CI run 37266116304](https://github.com/cafe-auvers/quant_app/actions/runs/37266116304).
Migration/deployment reports and backups are private evidence, not committed
account data. No broker order or cancellation was submitted by the migration.
