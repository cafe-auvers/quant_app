# TiDB Cloud Coordination Store

## Purpose

TiDB Cloud stores only the small, safety-critical state that both trading
devices must agree on:

- Execution Owner and Operator Control;
- published app-state revisions and pending operator commands;
- TradeCards, execution ownership, order/command journals, and reservations;
- runtime readiness, leases, migration state, and external-alert evidence.

Daily/hourly/intraday prices, chart indicators, scanner metrics, and other
historical data stay in PC MySQL and the laptop SQLite mirror. The one-second
quote, ORB, stop, and order-reconciliation loop also remains local. TiDB is a
coordination authority, not a market-data transport.

At the time of this design, TiDB Cloud Starter includes 5 GiB row storage,
5 GiB columnar storage, and 50 million RUs per month. Keep the spending limit
at zero so quota exhaustion rejects or throttles work instead of creating a
bill. Recheck the current
[Starter plan limits](https://docs.pingcap.com/tidbcloud/select-cluster-tier/)
and [Serverless RU FAQ](https://docs.pingcap.com/tidbcloud/serverless-faqs/)
before production use because cloud quotas can change.

## Configuration

Create a TiDB Cloud Starter cluster and a small database such as
`quant_coordination`. In the TiDB Cloud **Connect** dialog select a standard
MySQL connection and place its SQL host, port, username, password, database,
and optional CA path into each machine's gitignored `.env` using the
`COORD_DB_*` variables in `.env.example`.

TiDB Cloud management or Data Service API keys are not SQL login credentials
and are not used by the desktop application. Never add either kind of secret
to Git, documentation, screenshots, or logs.

The connection is TLS-authenticated, uses a three-connection pool with one
overflow slot, verifies the certificate hostname, and recycles connections
after four minutes. Startup provisions only the coordination tables; it does
not create historical-data tables. The recycle period stays below the
documented 340-second AWS idle timeout; see TiDB's
[standard connection guidance](https://docs.pingcap.com/tidbcloud/connect-via-standard-connection-serverless/).

## Traffic budget

The August 2026 Starter allowance is **50 million RUs/month per instance**.
An RU is not one SQL statement. TiDB adds storage reads/writes, SQL CPU, and
network egress: the public-endpoint rates include 1 RU per 64 KiB read payload,
1 RU per 2 KiB write payload, 1 RU per 3 ms SQL CPU, and 1 RU per 1 KiB read
egress. Row-store writes are replicated and each duplicate counts. Use the
official [Starter pricing details](https://www.pingcap.com/tidb-cloud-starter-pricing-details/)
for the current formula.

### Implemented quota controls

- The one-second quote/ORB/stop loop is local and sends no per-tick SQL.
- A price-only change stays in memory. A TradeCard JSON row is written only
  when a plan, lifecycle, order, stop, warning, or other durable decision
  changes.
- The Buy Board performs one four-table revision statement per minute. If the
  token is unchanged it transfers no card JSON and does not rebuild the UI.
- After a change, the whole projection is four bulk reads: cards, ownership,
  owned orders, and external orders. It is **four reads for 89 cards**, not
  three reads per card. The current 89-card recovery snapshot is about 230 KiB.
- Both active and standby runtimes check the compact card revision once per
  minute. A write made by the same process invalidates its cache immediately.
- A switch of Execution Owner is not allowed to wait for that minute: the
  target force-loads all cards and installs current quote subscriptions and
  stops before it can publish `ACTIVE`.
- Protective offline ownership evidence is one bulk ownership read every ten
  seconds while positions exist, not one query per card.
- Unchanged runtime readiness uses one revision read plus one UPDATE; it no
  longer selects the same device row before and after every heartbeat.
- Each running desktop publishes its compact `main.py` process heartbeat to
  TiDB every 30 seconds. An existing heartbeat is one UPDATE, with no
  SELECT-before-UPDATE or separate COMMIT statement. This is independent of
  the PC MySQL probe, so a laptop remains an eligible Execution Owner while
  the PC is off.
- An unchanged runtime-readiness heartbeat is likewise one atomic autocommit
  UPDATE. State transitions, handoffs, ownership changes, commands, orders,
  and broker evidence retain their explicit transactions.
- Repository fetch/list helpers use read-only connection scopes. Broker
  reconciliation and board projection reads therefore do not emit a COMMIT
  for data they did not change.
- Exact broker fills, status changes, identity/recovery changes, and absence
  evidence are still persisted immediately. An otherwise unchanged working
  order refreshes its durable audit timestamp at most hourly; an unchanged
  terminal order is not rewritten. The in-memory/account readiness proof
  remains on the normal reconciliation cadence.
- A minute-triggered canonical Buy Board refresh performs one revision query
  and one four-table projection only when that token changed. It no longer
  runs the local compatibility bootstrap, duplicates the TradeCard payload
  read, or repeats the revision query. Explicit local planning changes retain
  the bootstrap path.
- A recent successful runtime-readiness UPDATE is reused as the writable-store
  proof. The separate no-op writable transaction remains only as a startup,
  recovery, or missing-heartbeat fallback.
- The external watchdog still receives its 30-second heartbeat, but successful
  heartbeat audit rows are compacted to one every five minutes. Failure and
  recovery status transitions are always recorded.
- Planning/control state sync is once per minute. Publishing, operator commands,
  control-button actions, broker-boundary checks, and owner activation use
  their immediate paths and do not wait for that display-sync timer.
- Each minute's live-control, Operator Control, and planning-revision display
  refresh is one conditional SELECT. It returns payload text only for the two
  tiny control rows; the larger planning documents contribute revision numbers
  only. This replaces at least one separate control query per device per minute.

The minimum cadences are hard floors in `execution_config.py`; an accidental
environment value cannot turn the background loops back into one-second cloud
polls.

| Coordination work | Hard cadence |
| --- | ---: |
| Local quote/ORB/stop evaluation | 1 second, no TiDB request |
| Operator-command pickup, regular session | 1 second, active executor only |
| Lease proof | 10 seconds, active executor only |
| Protective ownership proof | 10 seconds, one bulk read only while positions exist |
| Writable probe | 60-second fallback; normally satisfied by readiness write |
| Runtime readiness heartbeat | 30 seconds per running device |
| `main.py` process heartbeat | 30 seconds per running device |
| Card and Buy Board revision checks | 60 seconds per running device |
| Planning/control display sync | 60 seconds per running device |
| Operator-command pickup outside regular session | 60 seconds |
| Alert queue check | 30 seconds; successful DB audit every 5 minutes |

### Supported worst-case calculation

This is the deliberately pessimistic deployment envelope, not the expected
daily pattern:

- PC and laptop run continuously for a 30-day month;
- one active executor and one warm standby;
- 89 cards and up to two production KIS accounts;
- 22 regular sessions of 6.5 hours;
- a protective position exists continuously, so the ownership proof always
  runs;
- every scheduled task fires, even when there is no user change.

| Background source | SQL statements/month |
| --- | ---: |
| Fallback writable probes (assumes every fallback fires) | 86,400 |
| Two-device readiness revision + heartbeat UPDATE | 345,600 |
| Two-device `main.py` process heartbeats | 172,800 |
| Active-owner lease proof | 259,200 |
| Two-device card revision checks | 86,400 |
| Regular/off-hours operator-command checks | 549,420 |
| Bulk protective ownership proof | 259,200 |
| Alert queue plus compacted heartbeat audits | 190,080 |
| Two-device Buy Board revision checks | 86,400 |
| Planning/control state sync | 216,000 |
| Minute account-reconciliation relational reads, two accounts | 259,200 |
| Transaction COMMITs for fallback writable probes | 86,400 |
| **Scheduled total** | **2,597,100** |
| **With 25% reconnect/scheduling margin** | **3,246,375** |

For capacity planning, this project applies a conservative **8 RU per small
scheduled statement**. After batching the control and revision display reads
and removing separate COMMIT statements from the two single-UPDATE heartbeat
paths, that deliberately conservative calculation reserves about **26.0
million RUs/month**, leaving about **24.0 million
RUs** for real state transitions, bulk projection payloads, order journals,
TiDB background jobs, and measurement error. Ten thousand separately rendered
material changes would add roughly 5 million public-endpoint egress RUs at the
current 230 KiB card collection, still within that reserve. Real board changes
coalesce by cycle/minute, so normal usage should be substantially lower.

This is the supported worst case and is designed below the 50 million quota.
No finite quota can cover an unbounded event storm, a loop introduced by a
future regression, manually lowering source-code cadence floors, or unrelated
queries run in the TiDB console. Execution must not depend on pretending those
cases are bounded. The query-count tests therefore enforce constant-cost board
projection, revision-only idle refresh, and no price-only TradeCard writes.

### Production verification and quota guardrail

The first two-device production sample on 2026-08-21 exposed a higher idle
rate than the original paper estimate: the dashboard was roughly 18--22 RU/s
and showed a 20.19 RU/s point. The sorted SQL Statements view identified the
avoidable work clearly: about 1,100 COMMITs consumed roughly 2,990 RU; runtime
and process heartbeat writes ran about every 15 seconds; existing process
heartbeats used SELECT + UPDATE; and repository list/fetch calls committed
read-only transactions. The cadence and transaction changes documented above
were made from that evidence. Treat the 26.0M calculation as a design budget,
not proof of the post-change bill; only a new measured interval can provide
that proof. Storage was not the problem: the same production check found 18
coordination tables, roughly 349 rows, and about 0.008 MiB of row/index data.

The second pass traced the remaining cascade behind that sample. An unchanged
exact broker order was advancing only its observation timestamps, which bumped
the order version; the Buy Board then detected that version and performed two
revision aggregates plus two full TradeCard reads around an idempotent local
bootstrap. Observation-only writes are now coalesced/removed as described
above, and a changed minute refresh uses one aggregate plus one projection.
The control/revision display reads are also now one compact conditional query
instead of separate control and revision fetches. This should materially
outperform the deliberately conservative 26.0M model,
which still assumes that every fallback writable probe fires.

TiDB states that SQL queries, bulk operations, and its own background jobs all
consume RUs. `EXPLAIN ANALYZE` reports statement RU but excludes gateway egress;
the authoritative total is the cluster **Usage this month** pane, and the
**Diagnosis > SQL Statements** view identifies high Total/Mean RU statements.
See the official [Starter RU FAQ](https://docs.pingcap.com/tidbcloud/serverless-faqs/).

After deploying the optimized version to both devices, record **Usage this
month**, run both continuously for 24 hours, then subtract the starting value:

```text
observed_24_hour_RU = ending_usage_this_month - starting_usage_this_month
projected_monthly_RU = observed_24_hour_RU * 30
```

The operational acceptance target is at most **35 million projected RUs**.
Investigate at 35 million and preserve at least 10 million RUs for execution
and TiDB background work. Keep the spending limit at zero if a bill is never
acceptable, but understand the safety trade-off: TiDB documents that quota
exhaustion denies new connections and throttles existing ones. Reaching the
limit can therefore stop ordinary trading coordination; free-tier monitoring
is an operational requirement, not an optional cost report. See
[quota behavior](https://docs.pingcap.com/tidbcloud/serverless-faqs/)
and [spending-limit controls](https://docs.pingcap.com/tidbcloud/manage-serverless-spend-limit/).

## Safe cutover

1. Rotate any credential that has been pasted into chat or another exposed
   location.
2. Stop `main.py` on both devices outside the regular session.
3. Add the same `COORD_DB_*` SQL values to both `.env` files.
4. Start the intended first Execution Owner by itself. On a brand-new,
   unclaimed coordination store it performs read-only broker reconciliation
   and publishes `STANDBY_READY`; it cannot submit an entry in this bootstrap
   state.
5. Choose that device as Execution Owner. The readiness-fenced claim creates
   the first exact lease; only then does the owner run schema/card migration,
   perform another broker reconciliation, and become `ACTIVE`.
6. Start the second device and wait for its fresh `STANDBY_READY` identity.
7. Explicitly choose Operator Control. Publish Today's Plan before market
   open and verify both devices display the same revisions and Buy Today
   cards.
8. Run a paper/controlled-live validation before relying on unattended PROD
   execution.

If the first device reports `New entries remain blocked until post-migration
reconciliation completes` while the shared migration row is still
`NOT_STARTED` and Execution Owner is unassigned, update to a runtime containing
the first-owner bootstrap fix. Do not manually edit the migration row or mark
reconciliation complete: the active owner must produce that evidence through
the normal broker reconciliation path.

Do not configure TiDB on only one device. When `COORD_DB_*` is present, an
unreachable coordination store fails closed; the application does not fall
back to private SQLite or PC MySQL and risk creating two authorities.

## PC-off behavior

If PC MySQL goes offline while TiDB remains reachable, the laptop changes
historical reads to its local mirror. TiDB-backed ownership, operator commands,
TradeCards, execution journals, and ordinary execution remain available. If
TiDB itself becomes unreachable, new entries and ordinary commands fail
closed; only the existing bounded emergency protection policy can apply.
