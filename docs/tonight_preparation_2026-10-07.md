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

The authenticated Supabase CLI lists two other projects, but does not list
the trading project. Its management login therefore cannot verify this
project's current plan, billed egress or any quota/grace-period notice.
The trading project's actual database and documented metrics endpoint are
reachable. Current resource health and billing allowance are separate checks;
an account with access to the trading project's dashboard is still needed
for the latter.

A read-only production probe of the new code made 60 canonical reads across
the five holdings: five initial payload rows, followed by zero payload rows
over the remaining 55 fresh checks. No broker calls or canonical writes were
made. This verifies the new read path before deployment.

Fresh SQL/metrics observations around 14:09–14:13 KST show 15–16 connections
against `max_connections=60`, no blocked locks, no active queries or idle
transactions older than 15 seconds, and zero database deadlocks. CPU busy
averaged approximately 3.7% and then 1.1% over two samples; memory used after
subtracting available memory was approximately 53–58%. These observations
show no current compute/connection saturation; they do not establish live
session peaks or remaining billing allowance.

The earlier health report's full-query/11.6 MB estimate was incorrect: its
delta comparison keyed only on statement ID while PostgreSQL records
separate counters for different database users. Aggregating identical IDs
correctly eliminates the apparent repeated full-collection downloads. The
actual repeated single-card downloads remain measurable and are the issue
addressed here. The exposed pooler network counter did not advance during
the sampled interval, so it is not treated as proof of zero egress.

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
- Initial hosted CI on `ef4be9f` passed 3,551 tests per Python version and
  found one outdated credential-schema assertion: it omitted the new
  server-only `SUPABASE_SERVICE_ROLE_KEY`. The assertion is corrected; the
  template remains credential-only.
- Final exact-release CI on `639cc747d0298dcabd1f26390abb7930d4f3e5fc`
  passed all five jobs: repository hygiene/secret scan, dependency audit,
  Python 3.11 and 3.12 suites (3,552 passed and 29 skipped each), and Gate 1
  deterministic simulation. See the
  [CI run](https://github.com/cafe-auvers/quant_app/actions/runs/37575642531).
- The actual PC's staged checkout is clean. Its readiness check passed 19
  checks; two remaining failures both require actual exact-release approval
  of the capability manifest. It made no broker mutation or KIS connection.
- Twenty-three staged web planning/hourly-chart tests passed in the actual
  web Python environment. Fifty-six credential/configuration checks passed
  after the schema assertion correction.

The reviewed deployment candidate remains the exact code commit `639cc747`.
This report's later documentation-only commit does not change that candidate.
The immutable bundle, exact CI/Gate 1 evidence, staged checkout and supervised
rollback/restart scripts are prepared locally and on the PC. The capability
manifest is still AWAITING_OPERATOR_ACCEPTANCE; no approval, independent
review or formal Gates 2–4 certification has been manufactured.

The 14:23 KST read-only KIS/canonical recheck found ALAB 8, BE 10, CYPH 897,
SIMO 10 and VNCE 184, matching the canonical quantities, with no working
orders or unsettled broker commands. It did not disarm, release the lease or
send a broker order. The repository's E4 shutdown fence requires explicit
acceptance of the brief unprotected exposure during a supervised restart
(`src/core/runtime_readiness.py`); exact-release review is also required
(`src/core/release_identity.py` and `gate2/capabilities.py`). Existing earlier
release approvals do not approve this new code commit.

Until that approval and supervised deployment, the PC remains on `12dcd792`
and the web on `3f602ca`; the new single-card correction is not active in
production. After deployment, remeasure payload traffic and resource health
during ordinary live trading. The earlier excess billed egress still needs
dashboard verification. The October 7 session also needs operator arming;
the deployment restore helper only restores the previously armed date.

At approximately 14:12 KST, both active append-only evidence journals passed
integrity audit. The report correctly remains INCOMPLETE_PASSIVE_COLLECTION
while the collector is RUNNING before the session; this is not a collection
failure. The only enabled gate-related task with a future run is the passive
post-session audit. No competing gate soak or shadow WebSocket runner is
scheduled for tonight.

The final collection recheck at 14:38 KST still showed RUNNING, 10,006 cycles,
zero collector errors, zero dropped batches and an empty queue, with the
isolated shadow active. Actual Gate 4 counts were 266 lifecycle comparisons,
1,330 position-protection observations, one runtime activation and one
manual-arm observation. These are recorded observations, not fabricated
trades or a certificate. The passive audit remained Ready for October 8
at 07:05:05 KST. Both active repositories and the prepared checkout were clean.

## Approved deployment and verification, 16:43–16:52 KST

The operator answered "yes ok to restart" to the exact `639cc74` deployment
and supervised open-position restart question. The private approval record
binds that instruction to the complete commit and the five reviewed position
limits. Unchanged protocol capability evidence was carried forward under
procedural operator approval; independent technical review and formal Gates
2–4 certification are not claimed.

Fresh checks at 16:43 and again just before maintenance confirmed the same
five quantities, matching KIS and canonical state, with no working orders or
unsettled broker commands. A coherent canonical backup was saved before the
lease release. The approved staged preflight passed with no failures.

Both PC and web deployed `639cc747d0298dcabd1f26390abb7930d4f3e5fc` at
16:45:37. Credentials and all existing risk settings were preserved. Executor
activation waited for the normal readiness fence; the guarded transfer
succeeded at 16:47:13. Before restart, the operator's switch was already
armed for October 7 at revision 61 (this changed after the earlier afternoon
check). The restore helper restored that existing session for the new exact
release at 16:47:47, revision 63. It did not authorize a different session.
No broker order or cancellation was sent by this maintenance.

The 16:49 read-only post-restart audit passed:

- PC runtime ACTIVE on the exact release, with all ten readiness checks true,
  including market data, reconciliation and execution readiness.
- PC owns the execution lease; October 7 is armed for this release.
- KIS holdings match ALAB 8, BE 10, CYPH 897, SIMO 10 and VNCE 184, with no
  working orders. All five active stop prices match the fresh backup.
- Buy Today remains empty; no stock was added by maintenance.
- Both deployed repositories are clean. Web responds HTTP 200. The scheduled
  morning guard selects `639cc74`, preserving the approved release.

The new evidence directory is
`C:\Users\tonyh\quant_evidence\tonight_preparation_20261007\session\2026-10-07_639cc747d029`.
At 16:50 its collector was RUNNING, with an active isolated shadow, zero
errors, zero dropped batches and zero queue depth. Both append-only journals
passed integrity audit. Gate 4 had recorded ten lifecycle comparisons and
50 position-protection observations. The old release's evidence remains
preserved. The post-session task reads the current configuration and release,
so it will audit this directory without arming or promoting a gate.

An approximately 111-second post-deployment production sample measured
546 fresh single-card checks, **zero returned card payload rows**, and 22
collection checks with zero card payload rows. The old repeated single-card
payload download statement had no further calls in this window. CPU busy
averaged 4.53%, memory used excluding available memory was 53.6%, and the
last observation showed 16 connections against 60, zero blocked locks,
zero queries/idle transactions older than 15 seconds and zero deadlocks.
The correction is now active and measurable in the running process.

These measurements cover ordinary pre-session operation. The collector will
capture live-session behavior; neither live peaks nor the current billing
allowance are established by this sample. Previously accumulated excess
egress is not reset, and current plan/usage/grace status still requires access
to the trading project's dashboard. The exposed pooler send counter remains
unchanged and is not used as proof of zero billed egress.

Private evidence includes `deployment.json`, `supervised_release.json`,
`owner_activation.json`, `live_session_restored.json`, `post_restart_health.json`,
`collection_readiness.json` and `supabase_post_deploy_comparison.json` under
`artifacts/tonight_preparation_20261007/`.

Current release reference: [Opening liquidity and mobile workflow (2026-10-08)](opening_liquidity_mobile_release_2026-10-08.md). Dated reports and archived plans retain their original scope.
