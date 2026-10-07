# PC and No-IP health check — 2026-10-07

All user-facing times below are Korea Standard Time (KST). The user chose to
keep the PC running; no cloud server was provisioned or purchased.

**Status checked at approximately 13:22 KST: PC trading services are operational,
but this is not an all-clear.** The configured No-IP hostname has no DNS
address. The web hourly-chart regression, competing cache-copy job, and
unattended startup rollback risk were addressed and verified.

## Verified operational checks

| Check | Evidence |
| --- | --- |
| PC access | Authenticated WinRM and Tailscale access succeed; PC has been booted since October 5 at 08:00 KST |
| Trading process | One desktop process family; executor `ACTIVE`, generation 4014, all published readiness flags true |
| Heartbeat | Advanced between the read-only checks; sampled ages 205 and 104 seconds, within the configured 300-second heartbeat limit |
| Execution ownership | PC is the canonical execution owner; the old laptop's failed runtime row is historical |
| Broker reconciliation | At approximately 13:00 KST, fresh read-only KIS holdings/open-order/history/reservation reads were complete, with no errors |
| Holdings | All five broker holdings match canonical quantities: ALAB 8, BE 10, CYPH 897, SIMO 10, VNCE 184 |
| Orders and exits | No open broker orders, nonterminal execution orders, pending operator commands, or stop-latched exit cards at the check |
| Stops | All five open positions have a positive configured active stop |
| Last holding prices | Final KIS holding snapshot prices are above all five configured stops; these are not certified fresh live trade events while the market is closed |
| KIS token | Cached production token valid until October 8 at 11:13 KST; the broker reads also succeeded |
| Web access | Local HTTP and laptop-to-PC Tailscale HTTPS return 200; connected web readiness check passes |
| Remote listener | Read-only `PING` receives `PONG v3` |
| Services | MySQL, Tailscale, No-IP DUC, and WinRM are running and configured for automatic startup |
| Power | Mains-power sleep and hibernation timeouts are zero; scheduled automatic PC shutdown is disabled |
| Startup and refresh | Morning routine has logon and 08:00 daily triggers; today's refresh finished successfully at 08:49 KST |
| Market cache | Daily SPY data reaches the completed October 6 session; hourly source has 5,841 symbols and SPY's final October 6 regular-session bar |
| Disk | C: has approximately 29.7 GB free at the system inventory |
| Backup | Daily Supabase backup invocation active at 09:15 KST and reported succeeded today; today's downloaded coordination archive was separately restored and compared in isolation |
| Database/storage size | Supabase database approximately 16.8 MB and private object storage approximately 3.7 MB |

The 300-second heartbeat is a process-status publication interval. It does not
change the approved 15-second KIS event-age limit for automatic entries.

## Issues found and actions

### No-IP hostname is unavailable — unresolved

The configured wake URL uses `tonyhdkim.ddns.net`. Queries to Cloudflare and
Google public DNS return a `ddns.net` SOA response without an A or AAAA address.
Google DNS-over-HTTPS also returns no answer. The No-IP updater Windows service
is running, but that alone does not establish hostname/account health.

The router link consequently cannot be reached by its configured hostname.
An expired, removed, or changed hostname must be checked in the No-IP account;
the account status and exact cause have not been verified. The browser-control
tool fails before opening a browser, with “failed to write kernel assets,” so
account inspection/repair could not be performed here. A question about the
current hostname and account status is pending with the user.

If this is a Free No-IP hostname, account confirmation is required every 30
days, including when the updater is running. See the
[official No-IP confirmation guide](https://www.noip.com/support/knowledgebase/confirm-my-hostname-free-account-support-question-day).

Trading API calls, Supabase coordination, and the active Tailscale web URL do
not use this No-IP hostname. Keeping the PC on does not require router wake.
Actual wake-from-sleep and router login were not tested.

### Hourly web data path regressed — fixed and deployed

Both desktop and web had been deployed on clean release
`12dcd792bd042ce22a5fe6bf959a7c8d8072d0fd`. That web version does not recognize
the existing `pc_hourly_reads=true` setting and lacks the previously deployed
direct PC hourly reader. The original fix was in web release `49e5985`; it was
lost when the web was redeployed from a different branch.

Deployed web release `3f602ca38b3e5f1c8bdcff80ce78311e79730203` restores the
read-only hourly source on top of `12dcd79`, preserving the differential
Supabase reads and the PC's trading fixes. The cache conflict was resolved so
chart publication validates the snapshot actually generated.

Verification:

- PC web suite: **177 passed, 22 skipped**.
- Read-only staged live-data check: MySQL session read-only is ON; ZWS, SIMO,
  RKLB, AAPL, ACCV, and SVIA hourly charts reach the completed October 6 session.
- BRR's old September 21 data is correctly labelled STALE; no fresh bars are
  fabricated. Daily charts remain available.
- All five hosted checks for the exact new release passed, including both full
  Python test jobs and Gate 1 simulation:
  [GitHub CI](https://github.com/cafe-auvers/quant_app/actions/runs/37570107084).
- Deployed at 13:19 KST. The same live-data chart checks passed again on the
  deployed web source; local HTTP and Tailscale HTTPS return 200. Desktop
  process IDs, executor generation, ownership, and live-control revision were
  preserved. Both checkouts remain clean. Desktop stays on `12dcd79`; only the
  web service was restarted onto `3f602ca`.

### Competing SQLite mirror writers — removed from the web data path

The 10:00 hourly copy failed at 10:05 after 1,270 of 5,841 symbols. Its old
diagnostics retained only `OperationalError`, so the original exception cannot
be recovered. An isolated retry of the failed group succeeded.

Added scrubbed error details and bounded retries to the existing operational
helper. Its **five focused tests passed**, covering retry limits, fatal errors,
row accounting, and cancellation when the regular session opens.

The subsequent run at 13:02 reproduced the failure and identified
`sqlite3.OperationalError: database is locked`. It failed while initializing
mirror tracking after three attempts. The desktop's periodic safety backup
and the separate full hourly-copy job both write the same SQLite file. The
desktop backup's 15-minute schedule also aligns with the observed collision.

Restored direct read-only MySQL hourly charts and disabled
`QuantApp_WebHourlyMirrorSync` after successful deployed verification. Its
Disabled state was checked again at 13:22 KST. The desktop's own safety backup
remains in place. This removes the second mirror writer from the web chart
requirement; no complete 5,841-symbol mirror-copy success is claimed.

### Unattended restart could select older code — startup guard installed

The existing morning routine resets to `origin/master` when the desktop is
not running. The remote default branch is still `aad7f20`, older than the
deployed trading fixes. Today's refresh-only resume avoided this reset, but a
later logon or recovery could otherwise lose the deployed source.

Installed a permanent startup guard under
`C:\Users\tonyh\AppData\Local\quant_app\operations\start_reviewed_pc_morning.ps1`
and routed the morning scheduled task through it. The guard replaces only the
routine's default-branch hard-reset target with the immutable configured
`KIS_RUNTIME_COMMIT_SHA`. It refuses dirty checkouts, unexpected reset targets,
and replacements that would change an already-loaded morning script. Other
Git calls and the existing dependency, startup, liveness, and refresh checks
are retained.

Five mocked Git checks passed without executing a native reset. The PC
read-only check resolved the current approved `12dcd79` release. Scheduled
task triggers, principal, and settings were compared before/after and
preserved. The desktop executor was not restarted. A real reboot was not
performed; the guard applies to the scheduled morning task, not direct manual
invocation of the unwrapped repository script.

## Items still requiring attention

- **Tonight readiness recheck at 13:56 KST:** the PC executor remains ACTIVE
  with all published readiness flags true. Fresh KIS reads again completed
  without errors; all five holdings match canonical quantities and no open
  broker orders or pending/stop-latched exit cards were found. However, live
  control still has `session_date=2026-10-06` and revision 60, and BUY_TODAY
  remains empty. The deployed broker's submission boundary checks the
  effective shared live control for both BUY and SELL orders, so October 7
  session arming is required for automatic order submission, including stop
  exits. No session was armed or trading state changed by this recheck.
  Evidence: `artifacts/pc_health_20261007/tonight_readiness.json`.
- **October 7 US session arming:** the shared live switch retains the October 6
  session authorization and current PC release. It is ineffective for the next
  session until normal daily arming is completed. No future session was armed
  by this health check. There are currently no BUY_TODAY cards; planning and
  Buy Today activation also remain operator choices.
- **No-IP account/hostname repair:** confirm the intended hostname and restore
  its DNS address in the account, then repeat DNS and router-link checks.
- **Supabase monthly traffic:** current plan, current billed traffic, and free
  allowance compliance remain unverified because management billing access
  was denied. The differential collection-read fix is deployed; repeated
  single-card downloads remain in the running release. The earlier 196-second
  estimate of 2,829 rows / 11.6 MB is superseded: its comparison matched only
  `queryid` and confused identical statement IDs recorded under different
  database users. The corrected October 7 preparation sample aggregates those
  counters and finds 688 actual single-card payload downloads in approximately
  140 seconds, with no recurring full-collection payload downloads in that
  interval. SQL row counts are not billing measurements. See
  `docs/tonight_preparation_2026-10-07.md` for the new correction and validation.
- **Source coverage:** morning refresh reported 31 unavailable/stale hourly
  symbols. Reference and sampled current symbols passed; universal freshness
  for every symbol is not claimed. BRR remains an identified stale source.
- **Formal Gates 2, 3, and 4:** remain NOT_CERTIFIED. Closed-market health and
  simulated checks do not establish live-session feed latency or execution
  qualification. The passive audit schedule is enabled for October 8 at
  07:05 KST; it does not itself arm trading.
- **Crash recovery:** web has a supervisor and scheduled restart policy. The
  desktop on-demand task has no configured failure restart count; logon and
  morning health checks can relaunch it. Continuous desktop crash recovery
  has not been established by this check.
- **Tailscale device key:** current PC key expires January 29, 2027 at 08:47
  KST. It is healthy now, with no reported Tailscale health errors.

## Evidence and scope

Final read-only runtime, broker, system, and startup evidence:
`artifacts/pc_health_20261007/health_audit.final.json`, `system-final.json`,
`startup-guard-install.json`, and `deployment.json`.

Earlier read-only audit and cache-helper evidence:
`artifacts/cloud_migration_20261007/pc-health-audit.json`,
`hourly-probe.json`, `cloud-health-before.json`, `cloud-health-after.json`,
and `cloud-health-read-rate.json`.

Web restoration evidence: `artifacts/pc_health_20261007/`, with a clean isolated
worktree, PC test log, staged/deployed live-data validation, and deployment
record. Private configurations and credentials remain outside tracked files.

This check issued no broker mutations, canonical trading writes, live-switch
changes, shutdowns, wake commands, or PC executor restarts. It updated the web
service, disabled the redundant cache-copy task, and guarded the scheduled
morning source selection. Original task configuration and copy-helper evidence
were saved before changes.
