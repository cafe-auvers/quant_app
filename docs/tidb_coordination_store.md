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
- The external watchdog still receives its 30-second heartbeat, but successful
  heartbeat audit rows are compacted to one every five minutes. Failure and
  recovery status transitions are always recorded.
- Planning/control state sync is once per minute. Publishing, operator commands,
  control-button actions, broker-boundary checks, and owner activation use
  their immediate paths and do not wait for that display-sync timer.

The minimum cadences are hard floors in `execution_config.py`; an accidental
environment value cannot turn the background loops back into one-second cloud
polls.

| Coordination work | Hard cadence |
| --- | ---: |
| Local quote/ORB/stop evaluation | 1 second, no TiDB request |
| Operator-command pickup, regular session | 1 second, active executor only |
| Lease proof | 10 seconds, active executor only |
| Protective ownership proof | 10 seconds, one bulk read only while positions exist |
| Writable probe | 15 seconds per running device |
| Runtime readiness heartbeat | 15 seconds per running device |
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
| Writable probes | 345,600 |
| Two-device readiness revision + heartbeat UPDATE | 691,200 |
| Active-owner lease proof | 259,200 |
| Two-device card revision checks | 86,400 |
| Regular/off-hours operator-command checks | 549,420 |
| Bulk protective ownership proof | 259,200 |
| Alert queue plus compacted heartbeat audits | 190,080 |
| Two-device Buy Board revision checks | 86,400 |
| Planning/control state sync | 302,400 |
| Minute account-reconciliation relational reads, two accounts | 518,400 |
| **Scheduled total** | **3,288,300** |
| **With 25% reconnect/scheduling margin** | **4,110,375** |

For capacity planning, this project applies a conservative **8 RU per small
scheduled statement**. That reserves about **32.9 million RUs/month** for the
entire continuously running background workload, leaving about **17.1 million
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

TiDB states that SQL queries, bulk operations, and its own background jobs all
consume RUs. `EXPLAIN ANALYZE` reports statement RU but excludes gateway egress;
the authoritative total is the cluster **Usage this month** pane, and the
**Diagnosis > SQL Statements** view identifies high Total/Mean RU statements.
See the official [Starter RU FAQ](https://docs.pingcap.com/tidbcloud/serverless-faqs/).

After both devices have run for 24 hours, calculate:

```text
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
4. Start one device first. Wait for `Shared online coordination database
   connected` and allow schema/card migration and broker reconciliation to
   finish.
5. Start the second device and wait for its fresh `STANDBY_READY` identity.
6. Explicitly choose Execution Owner and Operator Control. Publish Today's
   Plan before market open and verify both devices display the same revisions
   and Buy Today cards.
7. Run a paper/controlled-live validation before relying on unattended PROD
   execution.

Do not configure TiDB on only one device. When `COORD_DB_*` is present, an
unreachable coordination store fails closed; the application does not fall
back to private SQLite or PC MySQL and risk creating two authorities.

## PC-off behavior

If PC MySQL goes offline while TiDB remains reachable, the laptop changes
historical reads to its local mirror. TiDB-backed ownership, operator commands,
TradeCards, execution journals, and ordinary execution remain available. If
TiDB itself becomes unreachable, new entries and ordinary commands fail
closed; only the existing bounded emergency protection policy can apply.
