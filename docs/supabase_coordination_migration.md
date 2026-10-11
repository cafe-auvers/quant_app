# Shared coordination store migration

Desktop execution and the PC-hosted mobile web app must use the same canonical
store. PostgreSQL is the default and sole supported shared-cloud backend.
Historical market data remains in PC MySQL. The retired TiDB/MySQL coordination
backend is rejected; it cannot reconnect or silently select a different authority.
The optional web Supabase projection feature is separate and does not select
the execution coordination store.

## Connection and permissions

Copy the Session pooler host and port from the project's Connect dialog. Use
port 5432 for IPv4 session pooling, with a private `quant_coordination` schema.
Keep session pooling: search path, UTC time zone and transactional row locks
are part of the connection contract. TLS uses `verify-full` and the official
Supabase public root certificate in `config/certificates`.

Store non-secret `COORD_DB_BACKEND`, `HOST`, `PORT`, `NAME`, `SCHEMA`, and
`SSL_CA` in runtime JSON. Keep only `COORD_DB_USER` and `COORD_DB_PASSWORD`
in the runtime `.env`. `SUPABASE_DB_PASSWORD` is an administrator bootstrap
credential and must never be used by the mobile browser.

The provisioning tool creates an empty private schema, revokes public, anon
and authenticated access, and creates a fresh application login with table
DML and sequence access. The runtime role cannot create roles/databases,
bypass row security or create objects in the coordination schema. Future
schema upgrades require administrator provisioning before application rollout.
Do not expose this schema through PostgREST or add it to browser API schemas.

## Provisioning and PostgreSQL recovery

The TiDB-to-Supabase cutover is complete. Current tools connect only to
PostgreSQL. A previously verified JSON export remains usable for recovery,
but exporting from the retired cloud requires the historical release or vendor
export tools; do not point current coordination tools at a MySQL source.

Prepare separate private administrator and application credential files and a
target runtime JSON. These files do not replace active settings. For example:

```powershell
python scripts/migrate_coordination_store.py provision --runtime TARGET_RUNTIME_JSON --credentials ADMIN_ENV --app-role quant_coordination_app --app-credentials APP_ENV
```

1. Test the exact candidate commit, including the opt-in PostgreSQL integration
   suite. Obtain a reviewed release bundle for that exact desktop commit.
2. Identify the current canonical PostgreSQL store. Do not reconstruct it from
   local JSON state or an outdated pre-cutover backup.
3. Stop every desktop writer and mobile web supervisor, and suspend scheduled
   restarts during the cutover. Verify process exit on PC and laptop. Back up
   the active Git identity, runtime settings, credentials and local state.
4. Export all coordination tables under a repeatable-read transaction:

   ```powershell
   python scripts/migrate_coordination_store.py export --runtime SOURCE_RUNTIME_JSON --credentials SOURCE_ENV --snapshot BACKUP_JSON --writers-stopped
   ```

5. Restore the backup into the empty target using the administrator connection:

   ```powershell
   python scripts/migrate_coordination_store.py restore --runtime TARGET_RUNTIME_JSON --credentials ADMIN_ENV --snapshot BACKUP_JSON --writers-stopped
   python scripts/migrate_coordination_store.py verify --runtime TARGET_RUNTIME_JSON --credentials APP_ENV --snapshot BACKUP_JSON --writers-stopped
   ```

   Export rejects missing/unknown tables or columns. Restore locks the target,
   refuses nonempty tables, preserves every row and ID, verifies every value
   before commit and advances PostgreSQL sequences. Checksum and per-table
   counts are evidence; keep the private backup outside Git.
6. Switch the PC and any enabled peers to the same PostgreSQL settings and
   application credential. Run `scripts/sync_pc_env.ps1` to preserve the
   credential-only schema and regenerate `.env.pc`. Web reads its canonical
   connection from the configured PC repository. Do not leave one active writer
   configured for a different canonical store.
7. Deploy the reviewed exact release, restart the PC and web processes, and
   verify store identity, fresh runtime heartbeat, ownership, broker read-only
   reconciliation, canonical card counts, mobile board and passive planning.
   Old lease tokens must fail after release/reclaim. Migration never enables
   live trading or publishes executable intent; operator activation remains
   a separate action governed by the existing release/session controls.

Before any target writes, rollback can restore original settings and checkout
while both sides remain stopped. After a target write, the original source backup
is stale: export the new canonical store and verify reverse restoration before
changing authority. Never restart the stale source as a second writer.

## Usage and backup

The October 7 dashboard showed 41.02 GB against the 5 GB Free egress allowance.
The full TradeCard read had returned about 8.37 million rows; October 5 and 6
traffic was almost entirely Shared Pooler egress. Whole-collection refreshes
after individual changes, rather than database size or chart downloads, were
the main source.

Card, order-history and state readers now keep immutable, process-local raw
snapshots. Every repository read checks the canonical database in one statement.
Known identities, versions and timestamps are sent into PostgreSQL as a single
JSON parameter; only changed payloads and deletion markers are returned. The
single statement observes committed rows coherently, including late commits;
it does not use a timestamp watermark. Decoded models are private to each caller.
Failed reads remain errors, and force-refresh still performs a database check.
Restore/schema invalidation clears snapshots. CAS writes, row locks, controls,
leases, broker checks and market-data timing remain authoritative.

The read-only cloud probe measured 744,079 payload bytes for the initial 181
cards and zero returned card rows on eleven subsequent unchanged reads. A
300-card budget regression allows three consumers, one changed card per five
seconds during 22 six-and-a-half-hour sessions, two fresh polls per change,
2 KiB protocol allowance per poll, daily cold starts, hourly off-hours polls
and 300 MiB for backups. That modeled workload stays below 4 GB per month.
This is a workload budget, not an unlimited-use guarantee: additional devices,
more mutations, cold starts and other Supabase products must be monitored in
the organization dashboard. Already accumulated usage remains until the next
billing cycle. No paid plan or resource is enabled by this change.

### October 8 egress follow-up

The organization dashboard recorded 16.197 GB on October 5, 21.372 GB on
October 6, and 2.695 GB on October 7, almost entirely Shared Pooler egress.
October 7 was about 87% lower than October 6, but still too high for the
5 GB monthly Free allowance. The final single-card optimization was deployed
at 16:45 KST on October 7, so that daily bucket includes earlier code.
The organization dashboard, rather than query counts or database size, is
the source for billed daily bandwidth.

The October 8 audit verified the deployed card and individual state readers:
over a 755-second sample, 151 collection reads and 1,059 individual state
reads returned no changed payload rows. The same sample still downloaded full
device details and full command audit records on each five-second state sync.
Historical counters also showed 1.53 million rows from repeated ownership-list
reads; those lists were not active in this particular sample.

The follow-up source changes batch six state keys into one fresh differential
statement, replacing a revision query plus six separate pulls. They also apply
differential reads to ownership lists and device lists. Device comparison checks
all confirmation and detail fields as well as generation and heartbeat time:
handoff confirmations intentionally do not advance the heartbeat. The status
display now selects only command identity, type, symbol, status, and creation
time; executor and audit readers retain complete command records.

A read-only check of the new code against the existing Supabase schema loaded
six state rows, 64 ownership rows, and two device rows initially; ten subsequent
reads of each returned zero rows. Serialized command history fell from 3,813 to
523 bytes while preserving the visible history. These are returned-data checks,
not billed bandwidth measurements; SQL metadata and transport overhead still
count. Tests cover peer updates, removals, caller isolation, invalid state JSON,
read failures, and handoff changes without a heartbeat update.

These follow-up changes require deployment and process restart before they
affect running consumers. Daily usage before that cutover measures the previous
release. Evaluate a full dashboard day after cutover, aiming below roughly
167 MB/day for a 30-day 5 GB cycle, with additional headroom for activity and
backups. The earlier card-only model does not establish a production-wide
monthly quota guarantee. Private query-counter and probe evidence is in the
gitignored `artifacts/supabase_usage_20261008` directory.

The current app contains no TiDB connection path. Old deployed releases must
continue to use the same Supabase settings until upgraded.
Monitor Supabase database size and egress in its dashboard. Keep dated verified
coordination exports on the PC and an encrypted off-PC backup; the Free plan
does not provide managed automatic database backups. Credentials and trade
records must remain private.

`python scripts/backup_coordination_store.py` writes a dated, checksummed backup
under the gitignored `data/coordination_backups` folder using a repeatable-read
transaction. The deployed PC task now retains a weekly Sunday 09:15 KST offline
copy; daily backup creation runs in Supabase. The local script refuses the
retired MySQL coordination backend. A recovery backup taken while
writers are active must not be substituted for the stopped-writer cutover
export. Retention and encrypted off-PC replication are operator policies;
the script does not delete older backups.

### Daily cloud recovery backup

The `coordination-backup` Edge Function and Supabase Cron now run daily at
09:15 KST (`15 0 * * *` UTC), without the PC. The SQL migration
`20261006_005_cloud_coordination_backup.sql` installs a coherent, read-only
export of all 20 coordination tables. The schema remains private. Only
`service_role` can call the export and backup-record RPCs; anonymous and ordinary
authenticated users cannot. Gateway JWT verification remains enabled, and the
function passes the caller's credential to the protected RPC without elevating
it. The scheduling token is encrypted in Supabase Vault.

The private `coordination-backups` bucket has 31 fixed day-of-month slots.
An upload overwrites only that day's slot. At most 31 files of 1 MiB each can
be created by this function. The export rejects schema drift; the function
rejects a raw snapshot above 8 MiB or a compressed archive above 1 MiB. It hashes
the stored bytes after downloading them and records success only on a match.
At one daily successful run, its maximum data output is roughly 300 MiB per
month, including snapshot transfer and verification, with about 31 invocations.
These bounds cover this job, not unrelated application traffic or manual calls.

The verified 2026-10-06 archive was approximately 224 KB compressed and contained
157 TradeCards. Current-size storage for all 31 slots is about 7 MB; the measured
snapshot and readback traffic is approximately 105 MB per 31 runs. This is a
custom application backup, not Supabase's paid managed database backup service.
It does not include Auth users, historical market data, or other Storage objects.
Weekly PC copies remain useful if the cloud account itself becomes unavailable.

Deployment requires the existing server-role JWT in the credential-only `.env`
as `SUPABASE_SERVICE_ROLE_KEY`, plus the existing bootstrap database password
while installing the SQL/schedule. Never copy these credentials to browser or
ordinary web configuration. After applying the SQL and deploying the function:

```powershell
python scripts/schedule_cloud_coordination_backup.py --url https://PROJECT_REF.supabase.co
```

The deployment was verified by an actual Cron run, HTTP 200, stored-byte hash,
and restoration of every table into isolated SQLite. The daily schedule was
restored after that check. To download a specific verified slot using the hash
recorded in `quant_cloud_jobs.coordination_backup_slots`:

```powershell
python scripts/download_cloud_coordination_backup.py --url https://PROJECT_REF.supabase.co --slot 6 --checksum STORED_FILE_SHA256 --output data/coordination_backups/recovery.json
```

The download validates both file and payload hashes, every table and column,
and typed timestamps, then writes the existing recovery format. It does not
restore into the running database. Use the stopped-writer recovery procedure
above for an actual restore. The slot hash changes when that slot is overwritten.
Future coordination schema changes must update the cloud export migration and
the function's expected table count too.

### Free-only migration boundary

No plan upgrade or paid resource was enabled. The approximately 1.3 GB MySQL
market-history store exceeds Free PostgreSQL's 500 MB allowance. Its authoritative
daily/hourly bars, raw-data updates, scanner/indicator computation, broker
executor, and current web hosting remain on the PC. Existing Supabase coordination
authority and private chart objects remain in place. The historical mirror and
Tailscale remain in use. Compressed historical archives were not uploaded or
switched into the live read path after the free-only quota check.

The new backup job fits Free's storage and invocation limits on its own. The
existing project has already exceeded its egress allowance, so neither this
change nor the current measurements establish that the whole application can
operate reliably within Free. Free can restrict service on quota overrun;
the migration must not automatically upgrade or enable paid usage.

## Retiring the old cloud resource

Verify every writer uses Supabase and publishes a fresh readiness record there.
Keep the stopped-writer source export and a verified current PostgreSQL backup
outside Git before deleting the old cloud cluster. Deleting the cluster also
removes its cloud backups; archived local exports remain recovery material.
MySQL market history and Tailscale networking are independent and remain in use.

The watchdog audit setting is now
`EXTERNAL_WATCHDOG_COORDINATION_AUDIT_SECONDS`. Startup/environment sync migrates
the previous provider-specific key without losing its configured value. The
existing peer protocol/profile value and polling cadences are preserved so the
cleanup does not require a simultaneous PC/laptop upgrade.

Official references: [connection modes and TLS](https://supabase.com/docs/guides/database/connecting-to-postgres),
[plan limits](https://supabase.com/pricing).

Current release reference: [Opening liquidity and mobile workflow (2026-10-08)](opening_liquidity_mobile_release_2026-10-08.md). Dated reports and archived plans retain their original scope.
