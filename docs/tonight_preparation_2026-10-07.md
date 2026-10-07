# US October 7 session preparation

Times are KST. This record covers preparation while the PC remains the executor.

## Passive evidence collection during live trading

The existing `LIVE_SESSION_CHECKS_ENABLED=true` collector is enabled in the
PC process. Session selection is `auto`, with evidence separated by US session
date and exact runtime commit. At 14:02 KST its October 7 report was RUNNING,
with zero errors, zero dropped batches, zero queue depth, 7,900 runtime cycles,
and an active isolated shadow runtime. The collector receives detached inputs
from the production feed; it has no broker, production database or lease access.

- Gate 2: feed health, channel acknowledgements, capacity and latency samples.
- Gate 3: isolated shadow decisions and append-only shadow evidence.
- Gate 4: actual runtime lifecycle, arm, protection and reconciliation events.

The existing passive audit task invokes the repository-owned
`scripts/audit_passive_trading_session.py`, with its next run October 8 at
07:05 KST. It checks the actual date, commit, journal integrity, full regular
session coverage, dropped batches and errors. Missing or incomplete evidence
returns exit code 2. Collection remains separate from formal gate closure;
Gates 2–4 stay NOT_CERTIFIED. No test order, forced feed interruption or
deliberate disarm is required for passive collection alongside ordinary trading.

Live trading still requires the operator's October 7 session arming. At the
13:56 KST check, the switch retained October 6 and Buy Today was empty.
The evidence collector does not authorize trading or select stocks.

## Source control reconciliation

Preserved the outstanding workspace changes on
`codex/tonight-preparation-20261007`, then integrated the deployed trading
release `12dcd79` and web release `3f602ca`. This retains the hard stop,
15-second entry-age policy, independent protective work, broker orderability,
differential collection reads and direct PC hourly charts. Pending Supabase
backup functions, PostgreSQL configuration cleanup, tests and session reports
are included. Credentials, workstation overrides, generated evidence and
Supabase CLI temporary files remain local. The operational startup guard is
also captured in `scripts/start_reviewed_pc_morning.ps1`.

## Supabase traffic issue and correction

An observed 33.72-second sample returned 166 unchanged single-card payloads
from the runtime, approximately five payload downloads per second. The
collection differential reader returned zero payload rows over seven checks.
The remaining single-card read is corrected to use a fresh differential
statement scoped to exact environment, account and symbol. Every call still
checks committed database state; unchanged payloads are not returned again.
External updates and deletions remain immediately visible, models remain
private to each caller, and a failed verification cannot return cached success.
Transactional/locking mutation reads remain unchanged.

This correction requires a reviewed deployment and restart before it changes
the running process. Database and object storage are approximately 16.8 MB
and 3.7 MB, and the daily cloud backup succeeded. The earlier migration audit
reported egress already above the Free allowance; reducing future traffic
does not reset billing-cycle usage. No paid plan is enabled by this work.
Current billed usage and plan have not yet been reverified.

Free uncached egress is 5 GB and is separate from cached egress; pooler traffic
is counted as Shared Pooler Egress. See the
[official usage documentation](https://supabase.com/docs/guides/platform/manage-your-usage/egress).
SQL row counts estimate payload traffic; they do not replace billing metrics.

## Validation

- 87 focused Python tests passed for differential reads, database configuration,
  traffic budget, collector, backups and environment migration.
- Five mocked startup-guard checks passed without executing a native Git reset.
- Four JavaScript backup tests passed, including access rejection, compression,
  stored readback/hash verification and oversized-backup rejection.
- Staged secret scan passed; generated runtime and credential files are excluded.
- Full exact-release hosted CI and deployment verification are pending.
