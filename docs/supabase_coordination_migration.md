# Shared coordination store migration

**Completed deployment: 2026-10-05.** PC desktop, laptop configuration, and the PC-hosted
web deployment now select the same private Supabase PostgreSQL store. All 20 source tables
were copied and verified before runtime writes; mobile settings, passive Buylist removal,
fresh PC readiness, and post-activation backup were checked. The user manually armed the
approved current session. TiDB is now an optional stale archive, with no ongoing app calls
or fallback. See [Deployed setup](deployed_setup.md). The repeatable procedure below remains
the migration/recovery runbook, not unfinished setup.

Desktop execution and the PC-hosted mobile web app must use the same canonical
store. `COORD_DB_BACKEND=postgresql` selects PostgreSQL; the default `mysql`
retains existing TiDB deployments. Historical market data remains in PC MySQL.
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

## Verified cutover

Prepare separate private administrator and application credential files and a
target runtime JSON. These files do not replace active settings. For example:

```powershell
python scripts/migrate_coordination_store.py provision --runtime TARGET_RUNTIME_JSON --credentials ADMIN_ENV --app-role quant_coordination_app --app-credentials APP_ENV
```

1. Test the exact candidate commit, including the opt-in PostgreSQL integration
   suite. Obtain a reviewed release bundle for that exact desktop commit.
2. Restore source TiDB access if quota blocks reads. Do not reconstruct the
   canonical store from local JSON snapshots or old migration backups.
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
   configured for TiDB and another for Supabase.
7. Deploy the reviewed exact release, restart the PC and web processes, and
   verify store identity, fresh runtime heartbeat, ownership, broker read-only
   reconciliation, canonical card counts, mobile board and passive planning.
   Old lease tokens must fail after release/reclaim. Migration never enables
   live trading or publishes executable intent; operator activation remains
   a separate action governed by the existing release/session controls.

Before any target writes, rollback can restore original settings and checkout
while both sides remain stopped. After a target write, the original TiDB backup
is stale: export the new canonical store and verify reverse restoration before
changing authority. Never restart the stale source as a second writer.

## Usage and backup

The board checks a compact revision after its short cache expires and downloads
card payloads only when that revision changes. Forced reads after a mobile edit
remain authoritative. Broker gates, leases and execution checks keep their
existing freshness requirements.

Once every writer has moved, the app makes no ongoing TiDB coordination calls.
Monitor Supabase database size and egress in its dashboard. Keep dated verified
coordination exports on the PC and an encrypted off-PC backup; the Free plan
requires independent exports/off-site backup planning rather than relying on
paid-plan daily backup access; see
[Supabase backup documentation](https://supabase.com/docs/guides/platform/backups).
Credentials and trade records must remain private.

`python scripts/backup_coordination_store.py` writes a dated, checksummed backup
under the gitignored `data/coordination_backups` folder using a repeatable-read
transaction. The deployed `QuantApp_CoordinationBackup` task runs daily at
09:15 KST on the PC. It refuses the MySQL backend to avoid automatic TiDB usage.
A recovery backup taken while
writers are active must not be substituted for the stopped-writer cutover
export. Retention and encrypted off-PC replication are operator policies;
the script does not delete older backups.

Official references: [connection modes and TLS](https://supabase.com/docs/guides/database/connecting-to-postgres),
[plan limits](https://supabase.com/pricing).
