# Cloud migration status — 2026-10-07

The user subsequently chose to **keep the PC running**. Cloud provisioning is
no longer being pursued. No cloud server was provisioned or purchased. See
[the PC and No-IP health check](pc_health_check_2026-10-07.md) for current
operational findings. The migration investigation below is retained as history.

Status updated at approximately 12:46 KST. **The full migration is not complete.**
The user authorized a full migration today within free limits, starting with
Supabase and then Oracle. Oracle API access now works, and Tokyo is verified as
the tenancy home region. No target server is available: Oracle reports
`OUT_OF_HOST_CAPACITY` for both tested Always Free A1 configurations.

## What is already cloud based

The active PC uses Supabase PostgreSQL for the canonical coordination store.
The inspection found 20 tables and 181 TradeCards. Ownership, commands,
orders, reservations, and shared planning state are already there.

The configured web address uses Tailscale HTTPS. No-IP is used by the optional
router wake link, rather than the trading database or the current web address.
Supabase database migration does not replace a Python executor or web server.
No-IP account deletion or subscription cancellation was not performed.

## Completed today

| Work | Verified result |
| --- | --- |
| Inspect active Supabase database | 16,758,451 bytes at the initial measurement |
| Inspect private object storage | 3,723,904 bytes; both existing buckets private |
| Verify daily cloud backup schedule | Active at 09:15 KST; today's scheduled invocation succeeded |
| Create and download a fresh coordination recovery backup | HTTP 200; 274,537 compressed bytes |
| Test backup authentication | Unauthenticated invocation denied with HTTP 401 |
| Restore coordination backup in isolation | All 20 tables restored; every payload value matched; 181 TradeCards |
| Export complete PC MySQL recovery archive | Single-transaction dump succeeded; routines, events, triggers, schema, and data included |
| Verify compressed market archive | Gzip round trip matched the raw SQL SHA-256 and byte count |
| Prepare Oracle connection keys | Separate RSA API signing and Ed25519 SSH key pairs; private files outside the repository in a restricted local folder |
| Prepare Oracle allowance preflight | Read-only inventory tool; refuses incomplete inventory, non-home region, or excess allocations |
| Create active Oracle configuration | Saved outside Git; private key and supplied fingerprint match; restricted folder permissions inherited |
| Verify Oracle API access | Authentication succeeded; Tokyo home-region subscription is READY |
| Install isolated Oracle SDK | OCI 2.187.2; dependency check passed; trading app environment unchanged |
| Test allowance preflight | 12 tests passed, including SDK keyword-only storage calls and cross-compartment/domain accounting |
| Execute live Oracle preflight | Passed; zero existing A1 OCPUs, memory, instances, boot/block volumes, and volume backups in the inspected home-region scope |
| Check Oracle A1 host capacity | Both 2 OCPU / 12 GB and 1 OCPU / 6 GB returned OUT_OF_HOST_CAPACITY in Tokyo's subscribed availability domain |

The fresh coordination archive was created at 10:49:47 KST. Its SHA-256 is
`df06b335398d0d65368eb61fe5c67f0b7de3a112b94e1276f7f07e7139eda34a`.
The projected 31-slot backup storage is about 8.51 MB at this snapshot size.

The market archive completed at 11:02:29 KST. It contains 1,529,970,003 bytes
of raw SQL compressed to 280,415,490 bytes. Its SHA-256 is
`a38a1d6dacb9c90e3da93dbf3b39856906ea0813a1c9b5aa66cf487c326fa618`.
It is on the PC at `quant_evidence/cloud_migration_20261007/market-recovery.sql.gz`.
This is a live recovery archive, rather than a stopped-writer cutover export.
Restoration into an Oracle target remains unverified.

## Free limits and unresolved issues

Supabase Free includes a 500 MB database, 1 GB of file storage, and 5 GB of
uncached egress. The inspected PC MySQL store occupies 1,315,045,376 bytes.
The complete relational market store therefore exceeds the Supabase database
allowance. A compressed recovery archive would fit object storage, but object
storage does not provide the existing SQL history reads or run Python services.
[Supabase pricing](https://supabase.com/pricing).

**Overall free quota compliance has not been established.** The prior October 6
audit recorded 23.78 GB of egress. Current billing usage could not be read through
the available management credential: organization usage and subscription
endpoints returned HTTP 401. This was a read-only attempt; no upgrade was made.

The new query sample found 50 full-card reads returning 9,050 rows in 252.8
seconds, plus 1,239 single-card reads. The average card payload was about 4,111
bytes. This is approximately 37 MB of payload from full-card reads alone in
that sample, before protocol overhead. The four board source tables and the
published runtime state/generation were unchanged across a separate 11:02–11:09
KST comparison. Repeated downloads of unchanged data remain an unresolved
bandwidth issue. This measurement does not identify one proven caller-level
root cause, and no traffic fix is claimed as deployed.

Do not weaken stop processing, current broker reconciliation, leases, or
broker-boundary checks to make a quota estimate pass. Before retaining Supabase
as the final live database, remove unnecessary downloads and measure the actual
remaining traffic. If the necessary workload still exceeds Free, colocating
PostgreSQL with the executor on Oracle must be evaluated as part of the full
cutover. An existing billing-period overrun cannot be erased by changing polling.

The current Oracle documentation lists 2 total Ampere A1 OCPUs, 12 GB RAM,
1,500 OCPU-hours and 9,000 GB-hours monthly, with 200 GB of combined boot/block
storage in the home region. The prepared candidate uses 2 OCPUs, 12 GB RAM, and
a 50 GB boot volume. Existing tenancy allocations and available capacity must
be rechecked before creation. Oracle may reclaim idle Always Free instances; startup,
recovery, and trading readiness must be verified rather than assuming permanent
availability. [Oracle Always Free documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

The live allowance inventory now passes for the prepared candidate, but host
capacity is separate from an account quota. Capacity reports at approximately
12:04-12:05 KST returned `OUT_OF_HOST_CAPACITY` for both 2 OCPU / 12 GB and
1 OCPU / 6 GB A1 requests. No capacity reservation, VM, network, or volume was
created. This does not establish that all other free shapes are unavailable.
The full trading workload has not been validated on the 1 GB E2 micro shape.

## Hosting reassessment at 12:25 KST

Oracle A1 remains a possible free target, but it cannot be treated as a
dependable way to complete an urgent migration today. The latest capacity
report at 12:18 KST again returned `OUT_OF_HOST_CAPACITY` for both tested A1
sizes. Authentication and the allowance inventory are working; more account
setup will not resolve that capacity result. No target VM has been created.

Supabase is a separate constraint: the small coordination database fits its
database allowance, while the full market database does not. Repeated unchanged
downloads remain unresolved, and overall Free usage has not been established.
Changing the executor host alone does not eliminate that traffic.

Other reviewed free offerings do not provide an already validated replacement.
Render Free web services sleep after inactivity and have ephemeral local
storage; its documentation advises against production use. Google's recurring
free Compute Engine allowance is an e2-micro in selected US regions with 30 GB
of standard persistent disk and 1 GB of outbound data transfer. The trading
workload has not been qualified on that configuration.
[Render Free limitations](https://render.com/docs/free),
[Google Cloud Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features).

The recommended interim route under the strict free-hosting budget is to retain
the existing PC executor and market database, use the existing Tailscale serving
path, and resolve and measure unnecessary Supabase downloads. This retains PC
power, internet, and availability dependencies. It is not a completed full-cloud
migration. The authorized PC-independent migration remains outstanding and
requires an available target plus Linux/ARM, data restoration, execution
ownership, reboot recovery, and cutover validation.

## Paid alternatives and Google trial evaluation

The user considered OVH VPS-1 and then asked about Google Cloud. No final paid
purchase or new hosting account has been completed. No execution owner has
changed.

An unauthenticated OVH ASIA catalog and stock check at 12:42 KST reported
`vps-2027-model1` Linux available in Singapore. The catalog lists a $5.35 USD
monthly base price with zero commitment, $5.08 with six-month prepayment, or
$4.54 with annual prepayment, before tax. The Linux OS and local storage
add-ons have zero recurring price. The one-day backup add-on lists $0.50 with
a current promotion reducing it to zero; checkout must confirm that promotion
and the final account-specific total. Public stock information is not a
reservation. Evidence is in `ovh-public-preflight.json` in the artifact folder.
[OVH public catalog](https://ca.api.ovh.com/1.0/order/catalog/public/vps?ovhSubsidiary=ASIA),
[OVH public stock](https://ca.api.ovh.com/1.0/vps/order/rule/datacenter?ovhSubsidiary=ASIA&planCode=vps-2027-model1).

The browser runtime was retried but still failed before opening a page with
the kernel-assets path error. Account signup and checkout have not been
automated. No OVH-specific SSH key was generated before the user asked about
Google instead. The local machine has neither an installed WSL distribution
nor a discovered Docker command, so Linux qualification has not occurred here.

Google Compute Engine could host the executor, both database engines, and the
web server on a Linux VM after deployment qualification. Its eligible-new-user
trial supplies $300 credit for up to 90 days, ending earlier if exhausted.
Without an upgrade, trial resources stop at expiry. This is a temporary testing
route, not an ongoing zero-cost full-hosting commitment.
[Google trial and Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features).

The recurring free VM allowance is an e2-micro in selected US regions, with
1 GB RAM and 0.25 sustained CPU. The full application has not been qualified
on that size. Standard external IPv4 is $0.005 per hour, with only one free
hour per month in the published network pricing. As a paid price reference,
the pricing page's Iowa e2-medium rate is $0.03350571 per hour: approximately
$24.46 for 730 hours of VM compute, before disk, IPv4, other traffic, and tax.
This is not a Seoul-region quote.
[Machine specifications](https://docs.cloud.google.com/compute/docs/general-purpose-machines#e2_shared-core),
[VM pricing](https://cloud.google.com/products/compute/pricing/general-purpose),
[Network pricing](https://cloud.google.com/vpc/network-pricing).

## Google cost estimate from the current PC sample

A read-only PC resource sample at 12:46-12:47 KST found approximately 2.32 GiB
of committed private memory and 0.62 GiB resident memory across the relevant
Python, MySQL, and child processes. These Windows measurements were taken
outside the US session. Individual process resident-memory peaks summed to
1.15 GiB, but those peaks were not simultaneous. Neither the sample nor that
sum establishes peak-session requirements, and PostgreSQL currently runs in
Supabase. A 4 GB Linux VM is a starting candidate, not a qualified minimum.

For one standard Ubuntu VM running all services and both databases for 730
hours/month, the verified Iowa price reference is approximately:

| Item | Monthly USD before tax |
| --- | ---: |
| e2-medium, 4 GiB RAM, shared CPU | 24.46 |
| 40 GiB balanced persistent disk | 4.00 |
| One in-use external IPv4 | 3.65 |
| Subtotal before backups and internet egress | 32.10 |

A $1-2 backup-storage allowance is a planning assumption, not measured Google
usage. Monthly internet egress is not known; Standard Tier has a published
200 GiB/month allowance and must be explicitly selected, while Premium is
the default and has different charges. Network performance must be tested
before selecting a tier for execution. Colocating databases and services
would keep their SQL traffic on the VM, so today's Supabase egress is not a
direct measurement of the eventual Google internet bill.

The current planning budget is $35-45/month before tax for a 4 GB deployment
with low external traffic. This is not a verified Seoul quote or a hard bill
cap. Region rates, full-session CPU/memory demand, backup retention, and actual
external traffic remain to be checked. An 8 GiB e2-standard-2 has an Iowa
compute reference of $48.92/month, or about $56.56 with the same disk and IPv4,
before backups, egress, and tax. No Google resource or paid account was created.
The estimate excludes Cloud SQL, load balancing, NAT gateways, premium OS
licenses, and extra project/service charges.

Resource evidence and calculations are in `google-workload-sample.json`,
`google-workload-family.json`, and `google-cost-estimate.json` in the artifact
folder. Pricing sources are the Google VM, disk, and network links above.

## Remaining work

1. Retry A1 capacity in the verified Tokyo home region when capacity changes.
2. Refresh the tenancy-wide allowance inventory before resource creation; the
   first live inventory passed, but it must not be reused indefinitely.
3. Provision only eligible resources; stop if free capacity is unavailable.
4. Verify Linux/ARM dependencies, executor startup, data refresh, and web hosting.
   Current application validation targets Windows; Linux deployment is not yet
   certified by this preparation.
5. Restore the market archive into the target and compare tables, row counts,
   history watermarks, scanner results, and representative charts.
6. Resolve the repeated cloud downloads and measure egress before claiming the
   final Supabase workload fits Free. Decide the final canonical database route
   from that evidence and retain one writable authority.
7. Perform a stopped-writer cutover, carrying current trading state, controls,
   broker identity, and stop coverage. Give the Oracle process a fresh device
   identity and verify single execution ownership, broker reconciliation, and
   market data readiness before allowing execution there.
8. Verify phone/web operation with the PC independent of that serving path;
   verify reboot recovery, backups, and rollback from the new canonical store.
9. Retire the old serving path after those checks pass. Moving data into a
   backup bucket alone must not be reported as a completed full migration.

## Oracle access preparation

Active access is now configured at
`C:/Users/tonyh/AppData/Local/quant_app/oracle/config`. The user supplied the OCI
configuration values; the agent filled the prepared private key path and
verified that the RSA public/private key pair and fingerprint match. API
authentication succeeded at approximately 12:00 KST and confirmed Tokyo as the
READY home region. Private key contents were not logged or committed.

The first live inventory attempt exposed a preflight-script SDK compatibility
issue: current BlockstorageClient list methods require keyword arguments.
Storage calls were corrected, a cross-compartment/domain regression test was
added, all 12 preflight tests passed, and the live inventory then succeeded.
This preparation-script fix did not alter the trading application.

Earlier browser attempts remained blocked:

At approximately 11:31 KST, the user supplied the Tokyo console URL
`https://cloud.oracle.com/?region=ap-tokyo-1`. The browser automation failed before
loading the console, including after a runtime reset, with
`failed to write kernel assets: The system cannot find the path specified. (os error 3)`.
The public web reader returned no console content. Neither route established
the user's account activation, tenancy home region, or available resources.
A local file check found the prepared keys and `config.example`, but no active
OCI configuration in the prepared folder or `%USERPROFILE%/.oci/`.

The user then explicitly requested that the agent perform the setup in Chrome.
Both browser control runtimes were reset and retried; each still failed before
initialization with the same kernel-assets error. Local diagnostics confirmed
that the configured runtime executables, Node module folder, temporary folders,
and working directory exist. No repair was identified from these checks.
At that point, Oracle API registration and account inspection had not been
completed; the blocker was the agent's browser runtime. API authentication subsequently
provided a working route without browser automation.

Prepared files are under `%LOCALAPPDATA%/quant_app/oracle/`. The public API key
is registered and authenticated. The configured private key path is
`C:/Users/tonyh/AppData/Local/quant_app/oracle/oci_api_key.pem`; the active
configuration is saved as
`C:/Users/tonyh/AppData/Local/quant_app/oracle/config`.
[Oracle API signing key setup](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm).

## Evidence

Local evidence and preparation tools are in
`artifacts/cloud_migration_20261007/`. The PC export and copied verification
metadata are in `C:/Users/tonyh/quant_evidence/cloud_migration_20261007/`.
Recovery payloads remain private and excluded from Git.

No application release, execution owner, risk settings, stop rule, live session
activation, or broker orders were changed by this migration preparation. No paid
plan or Oracle resource was enabled. The existing PC runtime remains the executor.
