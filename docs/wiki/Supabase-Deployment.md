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

The separate web service runs `3a7c14fe9cc269a2590f825e88aacafac7d98a21`
(PR #141, including PR #138/#139/#140). The far-right header remains one row: `Open in Xm` before opening,
then `Close in Xm` during trading. Cancel Sell All now withdraws an unsubmitted
exit in premarket or regular hours and preserves the holding and stop.
A SELL identity, reservation, working/ambiguous order or unresolved cancel
prevents withdrawal. No broker order is submitted or cancelled by that edit.
The web-only deployment preserved the running, armed PC executor and its
configuration; the new web SHA is not approved as an executor release.


The 02:04 KST web-only deployment on 2026-10-06 also includes the ORB display
correction (PR #139) and stage-specific Buy Board details (PR #140). Cards and
detail sheets show stock/share totals, target or held quantities, entry plan,
planned/active stop, sellable shares, average entry, working exit quantities,
retry/block reasons and estimated P&L. Price source, KST observation time and
age are explicit; Yahoo is indicative, and stale prices do not produce P&L.
Snapshots and canonical projections are reused without additional SQL queries,
quote polling or broker calls. At 02:06 KST the PC remained ACTIVE on c106449c
with live execution enabled/effective; its processes and configuration were
preserved. The combined PC correction 3a7c14fe (PR #141) supersedes the
unapproved 8efad797 package and remains pending actual reviewer approval and a
supervised restart. Its prepared activation preserves web 3a7c14fe.

At 02:55 KST on 2026-10-06, the separate web source was updated to 3a7c14fe,
preserving PC process IDs, source c106449c, credentials/configuration and Live
Trading. The Buylist now shows the current-session Buy Today rejection memo.
ODD's calculation had rejected fractional-cent 1m/30m highs as invalid order
ticks, then returned it to Buylist; mobile had omitted the stored result.
The prepared PC planner rounds automatic BUY limits down to the highest legal
tick within the passive range and retains raw ORH for breakout confirmation.
Actual cached KIS inputs replayed offline to a valid ODD 30m plan at $19.70,
117 shares and 16.28% capital; 1m/5m remain risk-invalid. No order was placed
or Buy Today intent restored by this diagnostic. Until approved activation,
the PC still uses the old planner. Verification at 02:57 KST confirmed c106449c
ACTIVE, reconciled, and Live Trading enabled/effective.

The same pending release preserves the latest KIS trade alongside stop extrema,
uses the newest observation timestamp, prevents minute refresh from repeatedly
rearming a feed-blocked card, publishes worker equity with its actual timestamp,
and shows the specific freshness block. A healthy socket does not guarantee a
fresh per-symbol trade and quote. Missing acknowledgements, stale/quiet events,
invalid timestamps/ask, or the processing queue limit still block entry after
the fix. Freshness, sizing/risk, broker, ownership and manual-arming guards
remain enforced; tomorrow does not guarantee every symbol can enter.

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
