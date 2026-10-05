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
`7ea107ece347ad41d954e8d2a533ef7d740a2f3f`. The operator approved the
2026-10-05 deployment; the PC reported `ACTIVE` and executor readiness passed.
The operator manually armed the new release. Verification at 20:56 KST confirmed
live execution enabled and effective for the 2026-10-05 session, with 13 Buy Today
plans. Future release changes require fresh manual arming.
This dated snapshot does not certify future sessions, actual fills, or new
qualification of Gates 2–5. Recheck current release/session, broker truth,
quotes, plan, ownership, and risk/capital gates before trading.

Shared ORB settings matched the PC's applied settings. Passive mobile Buylist
removal was verified. Removal waits for canonical confirmation, reports
failures, and preserves real order/position/protection fences.

The separate web service runs `784741039f86b2bd1ca6e5a3595b5c21d15e3f97`.
Its far-right header stays on one row and shows `Open in Xm` before opening,
then `Close in Xm` during trading, using the exchange calendar and a local browser timer. This web-only
update preserved the armed PC executor and its configuration. Mobile Watchlist
additions ask for a missing breakout price,
confirm its saved revision, and show errors beneath the list buttons. Buy Today
still requires explicit activation confirmation.

Passive Gate 2/3/4 collectors now run on the PC alongside live trading, using
the existing feed and copied inputs. The collectors have no production broker,
transport, database, or lease access. Their dated report updates every ten
seconds under `quant_evidence/live_checks_20261005/session/checks_report.json`.
Verification found zero collector errors or dropped batches, and the collector
recorded the actual `MANUAL_ARM` event.
These diagnostics explicitly say `NOT_CERTIFIED`. Full Gate 2 fault probes,
Gate 3 branch/fence replay and reviewed chain, and Gate 4's three supervised
dates/lifecycle/disarm coverage still need formal qualification. The old gate
launcher remains disabled because it would stop live trading and load an
obsolete release. See [the deployed collector documentation](https://github.com/cafe-auvers/quant_app/blob/7ea107ece347ad41d954e8d2a533ef7d740a2f3f/docs/live_session_checks.md).

Documentation changes are kept separate from the armed trading checkout.
Pulling a newer executor Git SHA, even for documentation, requires the existing exact
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
