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
and [Starter RU FAQ](https://docs.pingcap.com/tidbcloud/serverless-faqs/?plan=starter)
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

The connection is TLS-authenticated and verifies the certificate hostname.
Transactional writes use a three-connection pool with one overflow slot.
Routine reads use a separate two-connection AUTOCOMMIT pool so a checkout
emits only its SELECT: no pre-ping, isolation toggle, COMMIT, or ROLLBACK.
Both pools recycle connections after four minutes. Startup provisions only
the coordination tables; it does not create historical-data tables. The
recycle period stays below the
documented 340-second AWS idle timeout; see TiDB's
[Starter/Essential connection guidance](https://docs.pingcap.com/tidbcloud/connect-to-tidb-cluster-serverless/?plan=starter).

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
- The active runtime checks the compact card revision every three minutes and
  the standby every five minutes. A write made by the same process invalidates
  its cache immediately; ownership activation force-loads canonical cards.
- A switch of Execution Owner is not allowed to wait for that cadence: the
  target force-loads all cards and installs current quote subscriptions and
  stops before it can publish `ACTIVE`.
- Protective offline ownership evidence is one bulk ownership read every thirty
  seconds while positions exist, not one query per card.
- Unchanged runtime readiness uses one revision read plus one UPDATE; it no
  longer selects the same device row before and after every heartbeat.
- The guarded runtime's `runtime_device_state` row is also the canonical
  `main.py` liveness proof. `app_runtime_status` remains only for lifecycle
  and compatibility fallback when the guarded runtime is absent, eliminating
  a second steady TiDB heartbeat from the same process.
- An unchanged runtime-readiness heartbeat is likewise one atomic autocommit
  UPDATE. After the first full publication, stable readiness rewrites only
  `updated_at`; hostname, schema, and readiness JSON are sent again only when
  those details change. State transitions, handoffs, ownership changes,
  commands, orders, and broker evidence retain their explicit transactions.
- A legacy/fallback `main.py` heartbeat updates only `heartbeat_at`. New-profile
  runtimes do not run this duplicate steady writer.
- Repository fetch/list helpers use the dedicated read-only AUTOCOMMIT pool.
  SQLAlchemy 2.0.43 or newer is required so closing those connections skips
  DBAPI rollback. Broker reconciliation and board projection reads therefore
  emit neither COMMIT nor ROLLBACK for data they did not change, and the read
  pool omits checkout pre-pings that would otherwise add another request.
- Exact broker fills, status changes, identity/recovery changes, and absence
  evidence are still persisted immediately. An otherwise unchanged working
  order refreshes its durable audit timestamp at most hourly; an unchanged
  terminal order is not rewritten. The in-memory/account readiness proof
  remains on the normal reconciliation cadence.
- A three-minute canonical Buy Board refresh performs one revision query
  and one four-table projection only when that token changed. It no longer
  runs the local compatibility bootstrap, duplicates the TradeCard payload
  read, or repeats the revision query. Explicit local planning changes retain
  the bootstrap path.
- A recent successful runtime-readiness UPDATE is reused as the writable-store
  proof. The separate no-op writable transaction remains only as a startup,
  recovery, or missing-heartbeat fallback.
- The external watchdog receives an asynchronous five-second HTTPS pulse,
  independent of TiDB availability. The pulse itself performs no SQL.
  Successful audit rows are compacted to one per hour; failure and recovery
  status transitions are always recorded.
- Empty alert-queue reads use non-committing connections. Runtime startup
  loads all relevant OPEN alert keys once, replacing four SELECT+COMMIT pairs
  per card, and an unchanged account-reconciliation plan opens no transaction.
- Unchanged account-reconciliation comparison rows are served from process
  memory. Canonical SQL writes invalidate them immediately and a 15-minute
  TiDB refresh bounds exceptional external changes; broker truth still
  refreshes every minute.
- Planning/control state sync is once per three minutes. Publishing, operator commands,
  control-button actions, broker-boundary checks, and owner activation use
  their immediate paths and do not wait for that display-sync timer.
- Each three-minute live-control, Operator Control, and planning-revision display
  refresh is one conditional SELECT. It returns payload text only for the two
  tiny control rows; the larger planning documents contribute revision numbers
  only. This replaces at least one separate control query per device per minute.
- The regular-session operator-command pickup uses a twenty-second hard floor,
  and its empty/oldest-pending lookup is backed by one covering
  `(status, created_at, command_id)` index. This is the only cloud cadence
  relaxed in response to the second production RU sample; local quote, stop,
  and broker-boundary lease checks are unchanged.
- Guarded pending-order lookups share one canonical snapshot for two seconds
  across all heartbeat stages. This removes repeated list-then-fetch reads of
  the same order while the one-second quote/stop loop keeps running. An
  `UNKNOWN_SUBMISSION_STATE` retains its one-second reconciliation cadence.

The minimum cadences are hard floors in `execution_config.py`; an accidental
environment value cannot turn the background loops back into one-second cloud
polls.

| Coordination work | Hard cadence |
| --- | ---: |
| Local quote/ORB/stop evaluation | 1 second, no TiDB request |
| Operator-command pickup, regular session | 20 seconds, active executor only |
| Lease proof | 20 seconds, active executor only |
| Protective ownership proof | 30 seconds, one bulk read only while positions exist |
| Writable probe | 180-second fallback; normally satisfied by readiness write |
| Runtime readiness heartbeat | 45 seconds per running device |
| `main.py` process heartbeat | Folded into runtime readiness; legacy fallback only |
| External watchdog pulse | 5 seconds over HTTPS; no TiDB request |
| Active/standby card revision checks | 180/300 seconds |
| Buy Board and planning/control display sync | 180 seconds per running device |
| Operator-command pickup outside regular session | 300 seconds |
| Alert queue check | 90 seconds; successful pulse audit every 60 minutes |
| Stable pending-order snapshot | 2 seconds; unknown submissions stay at 1 second |
| Reconciliation relational cache | 900 seconds; local writes invalidate immediately |

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
| Fallback writable probes (assumes every fallback fires) | 28,800 |
| Two-device readiness revision + heartbeat UPDATE | 230,400 |
| Two-device `main.py` process heartbeats | 0 steady; lifecycle fallback only |
| Active-owner lease proof | 129,600 |
| Active/standby card revision checks | 23,040 |
| Regular/off-hours operator-command checks | 32,664 |
| Bulk protective ownership proof | 86,400 |
| Alert queue plus compacted heartbeat audits | 59,040 |
| Two-device Buy Board revision checks | 28,800 |
| Planning/control state sync | 72,000 |
| Cached account-reconciliation relational reads, two accounts | 17,280 |
| Transaction COMMITs for fallback writable probes | 28,800 |
| **Scheduled total** | **736,824** |
| **With 25% reconnect/scheduling margin** | **921,030** |

For capacity planning, this project applies a conservative **8 RU per small
scheduled statement**. The `external-pulse-v2` profile therefore budgets about
**7.4 million RUs/month**, or about **2.84 RU/s**, for scheduled work including a 25%
reconnect/scheduling margin. This leaves more than 100% headroom for real state
transitions, bulk projection payloads, order journals, TiDB background jobs,
and measurement error while keeping the cluster target at **7--9 RU/s**.
Ten thousand separately rendered
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

A subsequent regular-session sample remained near 20 RU/s after those broad
optimizations. The only scheduled one-Hz coordination statement left was the
active executor's empty operator-command pickup. Its cadence was changed to
one query every three seconds and its lookup was covered by an index. The
post-deployment regular-session idle target remained **10 RU/s or less** over
a representative interval; short transition/reconciliation spikes are
assessed separately.

The next production measurement disproved the three-second estimate: the rate
was still about 17--18 RU/s. A read-only statement-summary sample then found
1,082 standalone ROLLBACKs in 20 minutes. SQLAlchemy's implicit read
transaction was issuing a ROLLBACK whenever a normal ``engine.connect()``
scope closed, so the previous COMMIT removal had only changed the
transaction-control verb. Pool pre-pings also add network requests that do not
appear as application SELECT digests. Routine coordination reads now use a
dedicated AUTOCOMMIT pool with both pre-ping and autocommit rollback disabled,
making each read exactly one SQL request. The `external-pulse-v2` runtime
profile also blocks startup beside a fresh peer that has not published the same
profile, preventing one stale desktop process from silently preserving the old
request rate. Its regular-session command floor is twenty seconds.

The previous SQL-statements capture isolated the remaining write and startup
overhead: 95 runtime-readiness heartbeats averaged 6.70 RU, 98 `main.py`
heartbeats averaged 5.85 RU, 803 standalone COMMITs were recorded, and the
startup recoverable-alert sweep selected incidents 356 times (four alert
classes across 89 cards). `external-pulse-v2` retains the one 45-second
runtime-readiness UPDATE, retires the duplicate steady `main.py` UPDATE, and
moves fast liveness to the five-second external HTTPS pulse. The alert sweep is
one bulk read, read-only alert polling emits no COMMIT, and unchanged
reconciliation state is served from the bounded process cache.

TiDB states that SQL queries, bulk operations, and its own background jobs all
consume RUs. `EXPLAIN ANALYZE` reports statement RU but excludes gateway egress;
the authoritative total is the cluster **Usage this month** pane, and the
**Diagnosis > SQL Statements** view identifies high Total/Mean RU statements.
See the official [Starter RU FAQ](https://docs.pingcap.com/tidbcloud/serverless-faqs/?plan=starter).

After deploying the optimized version to both devices, record **Usage this
month**, run both continuously for 24 hours, then subtract the starting value:

```text
observed_24_hour_RU = ending_usage_this_month - starting_usage_this_month
projected_monthly_RU = observed_24_hour_RU * 30
```

The operational acceptance target is a sustained **7--9 RU/s**, assessed using
the TiDB Cloud one-minute average after both devices have run `external-pulse-v2`
for at least 15 minutes. Any minute above 9 RU/s during an otherwise idle
interval fails acceptance; trading transitions and reconciliation spikes are
recorded separately. Keep the spending limit at zero if a bill is never
acceptable, but understand the safety trade-off: TiDB documents that quota
exhaustion denies new connections and throttles existing ones. Reaching the
limit can therefore stop ordinary trading coordination; free-tier monitoring
is an operational requirement, not an optional cost report. See
[quota behavior](https://docs.pingcap.com/tidbcloud/serverless-faqs/?plan=starter)
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
