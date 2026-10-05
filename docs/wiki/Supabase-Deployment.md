# Supabase Deployment

Verified installation snapshot: **2026-10-05**.

The deployed PC, laptop, and mobile/web setup is independent of TiDB.

| Path | Current service |
| --- | --- |
| Boards, plans, settings, commands, orders, reservations, ownership, readiness, alerts | Supabase PostgreSQL, private `quant_coordination` schema |
| Historical prices and scanner cache | MySQL on the always-on PC |
| Laptop market mirror | Local SQLite, pulled from PC MySQL |
| Mobile website | PC-hosted Python web service through private Tailscale HTTPS |
| Direct PC/laptop access and change pulses | LAN/Tailscale, authenticated listener and WinRM |
| Daily coordination backup | Verified Supabase snapshot written on the PC |
| Former TiDB database | Optional retained migration archive; no runtime calls or automatic fallback |

## What must stay on

Keep the PC powered on, awake, and signed in. It owns execution and hosts the
mobile web server. The laptop can be off. The phone needs Tailscale access to
the existing PC HTTPS origin; there is no new website from the database move.
Find the exact private address in `config/web.local.json` → `base_url`.
Supabase does not host the Python web application.

## Configuration and authority

The participating deployments use `COORD_DB_BACKEND=postgresql`, the project's
IPv4 Session pooler on port 5432, database `postgres`, and private schema
`quant_coordination`. Non-secret connection settings belong in
`config/runtime.local.json`; runtime username/password belong only in `.env`.
TLS verifies the official Supabase CA. A restricted application role has table
DML and sequence privileges, with no schema-create privilege. Browser roles
cannot access this private schema.

Optional Supabase Auth/Storage/Realtime/public web projections are separate
features. Their web feature flag may remain off while the shared PostgreSQL
store is active. PC `MYSQL_*` remains the historical-price connection, not TiDB.
If Supabase is unavailable, shared writes and new entries fail closed.

## Trading and mobile actions

The approved deployed release is
`aad7f204c9e8a908e5cf753df63e71a081c1dac2`. The user manually armed the
2026-10-05 session; the PC reported `ACTIVE` and executor readiness passed.
This dated snapshot does not certify future sessions, actual fills, or new
qualification of Gates 2–5. Recheck current release/session, broker truth,
quotes, plan, ownership, and risk/capital gates before trading.

Shared ORB settings matched the PC's applied settings. Passive mobile Buylist
removal was verified. Removal waits for canonical confirmation, reports
failures, and preserves real order/position/protection fences.

Documentation changes are kept separate from the armed trading checkout.
Pulling a newer Git SHA, even for documentation, requires the existing exact
release review and operator activation process before live execution resumes.

## PC tasks and recovery

- `QuantApp_StartMainOnDemand`: desktop executor.
- `QuantApp_WebDashboard`: supervised background web service.
- `QuantApp_MorningRoutine`: 08:00 KST data routine; skips Git/dependency
  changes while the desktop app is already running.
- `QuantApp_CoordinationBackup`: **09:15 KST**, verified snapshots under
  ignored `data/coordination_backups/`.
- `Automatic-PC-Shutdown`: disabled for this always-on deployment; AC sleep
  and hibernation are also disabled.

Manual backup: `python scripts/backup_coordination_store.py`. Preserve encrypted
off-PC copies separately; the daily task itself writes local backups.
Web login/session/drawing/draft SQLite state and local JSON need their separate
backups too.

All **20 coordination tables** were copied and verified before target writes.
The old TiDB source is now stale and must not become a second writable store.
It can be retired after retaining the verified export. Its spending cap was
left unchanged; this app no longer requires any TiDB quota. Supabase uses the
existing Free project, whose actual usage limits still need monitoring.

Use [Troubleshooting](Troubleshooting), [Synchronization](Synchronization),
[Configuration](Configuration), and [Operations and Monitoring](Operations-and-Monitoring)
for daily checks. The repository's migration procedure is
[Shared coordination migration](https://github.com/cafe-auvers/quant_app/blob/master/docs/supabase_coordination_migration.md).
