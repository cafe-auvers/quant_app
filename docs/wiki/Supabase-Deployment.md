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

The approved PC executor release is
`c106449c46accc6f249280bea372242bb816f8d5` (PR #137). The operator approved
the supervised 2026-10-05 restart and manually armed the new release after
broker reconciliation. Verification at 00:27 KST on 2026-10-06 confirmed
`ACTIVE`, executor readiness, and live execution enabled and effective for
the 2026-10-05 NYSE session. Future executor changes require exact-release
review and fresh manual arming. This snapshot does not certify future sessions,
actual fills, or new qualification of Gates 2-5.

This release corrects the canonical capital-reservation reader that previously
hid a pending BUY's reservation from portfolio risk checks and blocked further
entries. Rejected attempts now retain their reason. All risk/broker guards
remain in place. Shared ORB settings, mobile membership writes and execution
still use Supabase PostgreSQL; the app does not use TiDB.

The separate web service runs `08aed225c5eb1223b6148f9b19a483732567f7b0`
(PR #138). The far-right header remains one row: `Open in Xm` before opening,
then `Close in Xm` during trading. Cancel Sell All now withdraws an unsubmitted
exit in premarket or regular hours and preserves the holding and stop.
A SELL identity, reservation, working/ambiguous order or unresolved cancel
prevents withdrawal. No broker order is submitted or cancelled by that edit.
The web-only deployment preserved the running, armed PC executor and its
configuration; the new web SHA is not approved as an executor release.

The operator requested SVIA's return to Open. At 00:09 KST it returned with
643 shares and its $4.24 stop covering 643 shares; no SELL was working and no
broker mutation was made. The deployed mobile API confirmed Open, 643 shares
and $4.24 at 00:27 KST. Existing stop and live risk rules still apply.

Mobile Watchlist additions ask for a missing breakout price, confirm the saved
revision, and display failures. Buy Today requires explicit activation
confirmation. Passive Buylist removal waits for canonical confirmation and
preserves real order/position/protection fences.

Passive Gate 2/3/4 collectors run alongside live trading using the existing
feed and isolated copied inputs. Their new-release report updates under
`quant_evidence/qmco_reservation_fix_20261005/session/checks_report.json`;
prior release evidence remains preserved. These diagnostics explicitly say
`NOT_CERTIFIED`. Full Gate 2 fault probes, Gate 3 branch/fence replay and
reviewed chain, and Gate 4's three supervised dates/lifecycle/disarm coverage
still require formal qualification. The old gate launcher remains disabled.
See [the collector documentation](https://github.com/cafe-auvers/quant_app/blob/c106449c46accc6f249280bea372242bb816f8d5/docs/live_session_checks.md).

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
