# KIS Protocol Capability Matrix

Status: **IN PROGRESS — read-only credentialed evidence captured; mutation and live-event rows remain blocked**

This is the Workstream 0 evidence gate for the production KIS adapters. A
row may be treated as verified only when its finding is supported by either a
credentialed observation against the real API or a current first-party KIS
statement, with any account data and credentials redacted.

The provisional D1/D3/D11 adapter may be implemented inactive, but no row in
this matrix authorizes broker execution. Until the WebSocket/read-only subset
is verified, the WebSocket flags and capacities remain disabled. Until full
WS0 execution qualification and the later gates complete, all execution and
mutation settings remain disabled:

- `TRADING_ENABLED=false`
- `BUYBOARD_ENGINE_ENABLED=false`
- `KIS_WS_ENABLED=false`
- `KIS_WS_PROTOCOL_VERIFIED=false`
- `KIS_MUTATION_BUDGET_VERIFIED=false`
- all configured WebSocket and mutation capacities remain `0`

## Evidence registry

| ID | Kind | Environment | Observed | Evidence |
|---|---|---|---|---|
| WS0-E01 | Credentialed approval + subscription ACK | PROD | 2026-08-17 03:07 UTC | `tests/fixtures/kis_protocol/ws0_20260817_subscription_acks.json` |
| WS0-E02 | Credentialed approval + subscription ACK | SIM | 2026-08-17 03:09 UTC | `tests/fixtures/kis_protocol/ws0_20260817_subscription_acks.json` |
| WS0-E03 | Credentialed `inquire-nccs` / `inquire-ccnl` queries | PROD | 2026-08-17 03:08 UTC | `tests/fixtures/kis_protocol/ws0_20260817_rest_query_shapes.json` |
| WS0-E04 | Credentialed aggregate subscription-boundary probe | PROD | 2026-08-17 03:11 UTC | `tests/fixtures/kis_protocol/ws0_20260817_subscription_capacity.json` |
| WS0-E05 | Credentialed US exchange/daytime key ACK probe | PROD | 2026-08-17 03:11 UTC | `tests/fixtures/kis_protocol/ws0_20260817_subscription_acks.json` |
| WS0-E06 | Controlled non-business-day order request | SIM | 2026-08-17 03:19 UTC | `tests/fixtures/kis_protocol/ws0_20260817_sim_mutation_rejection.json` |
| WS0-O01 | Current KIS limits notice | Vendor | checked 2026-08-17 | [KIS API call-volume notice](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/d0d1a83f-6f8d-4437-9700-6d26702fd989) |
| WS0-O02 | Official KIS sample implementation | Vendor | commit `b093e42ba32d1df5f5ddad7a71cb715cbc800832` | [KIS Open Trading API](https://github.com/koreainvestment/open-trading-api) |

The committed files are redacted derived evidence. Their `source_capture_sha256`
values identify the original local capture without committing credentials,
account numbers, order identifiers, or unredacted account activity.

## External correlation key

**Required proof:** Whether `MGCO_APTM_ODNO` accepts a unique
application-supplied ID on submission.

**Status:** ⬜ Not verified

**Evidence:** The current first-party order sample includes
`MGCO_APTM_ODNO` in the request body. WS0-E06 sent a unique 12-character value
to the exact simulation host, but KIS rejected the request before order
acceptance because it was not a simulation business day.

**Finding:** Request-field presence is documented, but acceptance semantics
remain unknown. Do not use it as durable correlation evidence yet.

## Correlation recovery

**Required proof:** Whether the submitted correlation value is returned by
the submit response, `inquire-nccs`, `inquire-ccnl`, or an execution notice.

**Status:** ⚠ Partially verified

**Evidence:** WS0-E03 captured a real `inquire-ccnl` row. Its returned field
set contains `odno` and `orgn_odno`, but no `MGCO_APTM_ODNO` or other obvious
caller-correlation field.

**Finding:** Ordinary history does not expose a caller-correlation field in
the observed response shape. A controlled simulation order is still required
before concluding that correlation recovery is unsupported everywhere.

## Broker order ID

**Required proof:** Exact response field and whether it is returned on the
submission ACK or only by later queries.

**Status:** ⚠ Partially verified

**Evidence:** WS0-E03 returned a ten-character `odno` in a production
`inquire-ccnl` row. The committed evidence preserves only its length and a
stable redacted token.

**Finding:** `odno` is the observed historical broker-order identifier. Its
immediate availability on submission remains unverified.

## Broker order ID uniqueness scope

**Required proof:** Whether `odno` is unique across account, app key,
exchange, trade date, and reconnect/session boundaries, including reuse after
the broker's history-retention window.

**Status:** ⬜ Not verified — later execution qualification blocker

**Evidence:** WS0-E03 observed ten-character `odno` values on one production
account/history surface. That capture cannot establish a uniqueness domain.

**Finding:** The current durable identity
`environment:account:broker_order_id` remains provisional. Before Gate 4,
controlled accepted orders must compare identifiers across exchanges, dates,
sessions, account numbers, and app keys. This row does not block the read-only
Gate-2 WebSocket soak because Gate 2 performs zero broker mutations and grants
no cancellation authority.

## History latency

**Required proof:** Time from submit/cancel to appearance in
`inquire-ccnl`/`inquire-nccs`, measured at open, midday, and close.

**Status:** ⬜ Not verified

**Finding:** Requires controlled simulation mutations during supported order
hours. WS0-E06 reached the simulation mutation endpoint but created no order,
so it cannot provide an appearance-latency measurement.

## History completeness

**Required proof:** Date-range boundary, pagination, exchange coverage, and
presence of cancelled/rejected orders.

**Status:** ⚠ Partially verified

**Evidence:** WS0-E03 successfully queried both surfaces for NASD, NYSE, and
AMEX. `inquire-ccnl` returned three NASD rows in the selected 30-day window;
all six calls returned `rt_cd=0`. Both cursor fields were present and the
observed terminal `tr_cont` value was `D`.

**Finding:** The three configured US exchanges and terminal-page shape are
confirmed. Oldest supported date, a real continuation page, and known
cancelled/rejected rows remain to be demonstrated.

## WebSocket symbol key format

**Required proof:** Exact subscription key format for each configured US
exchange and daytime feed.

**Status:** ✅ Verified for the configured US exchanges

**Evidence:** WS0-E01/E02/E05 received `OPSP0000 / SUBSCRIBE SUCCESS` for both
`HDFSCNT0` and `HDFSASP0` with:

| Surface | Key observed |
|---|---|
| NASDAQ regular | `DNAS{symbol}` (`DNASAAPL`) |
| NYSE regular | `DNYS{symbol}` (`DNYSIBM`) |
| AMEX regular | `DAMS{symbol}` (`DAMSBTG`) |
| NASDAQ daytime | `RBAQ{symbol}` (`RBAQAAPL`) |
| SIM NASDAQ regular | `DNAS{symbol}` (`DNASAAPL`) |

**Finding:** The current runtime may use the verified regular-session mapping
for NASD/NYSE/AMEX only after the remaining protocol rows pass. Daytime
switching must be explicit; it must not silently reuse a regular-session key.

## WebSocket connection/subscription limits

**Required proof:** Maximum total registrations and sessions for one app key.

**Status:** ✅ Verified

**Evidence:** WS0-E04 registered alternating `HDFSCNT0` and `HDFSASP0`
subscriptions on one production session. Registrations 1–41 returned
`OPSP0000`; registration 42 returned `OPSP0008 / MAX SUBSCRIBE OVER`.
WS0-O01 independently states one session per app key and 41 aggregate realtime
registrations across quotes, trades, expected executions, and notices.

**Finding:** Capacity is **41 total registrations per app key/session**, not
41 per TR/channel. An `H0GSCNI0`/`H0GSCNI9` notice consumes one of those slots.
The live application enforces this as its sole broker-capacity budget; legacy
trade/quote capacity settings are not applied as independent KIS limits.

## Simulation environment differences

**Required proof:** Unsupported/different TR IDs, order types, and WebSocket
feeds in simulation versus production.

**Status:** ⚠ Partially verified

**Evidence:** WS0-E02 confirmed that simulation issues an approval key and
accepts `HDFSCNT0`/`HDFSASP0` with `DNASAAPL`. WS0-E06 sent a guarded
`VTTT1002U` AAPL buy request for one share at `$0.01`; KIS returned HTTP 200,
`rt_cd=1`, `msg_cd=40100000` (not a simulation business day), no broker order
ID, and the follow-up `VTTS3018R` query found zero matching open orders.

**Finding:** Subscription ACK parity exists for the tested pair, and simulation
enforces its own business-day calendar before order acceptance. Frame parity,
`H0GSCNI9`, supported-session order types, accepted mutation responses, and
history behavior remain open.

## Quote timestamp fields

**Required proof:** Exchange-event versus local-receive fields on real
`HDFSCNT0` and `HDFSASP0` event frames.

**Status:** ⬜ Not verified

**Evidence:** The credentialed probes occurred while the U.S. market was
closed, so they captured ACK frames but no price event.

**Finding:** Gate 2 cannot start until a regular-session capture confirms the
date/time mapping and clock-skew behavior for both channels.

## Sequence numbering

**Required proof:** Whether either channel supplies a monotonic sequence and
its reset behavior across reconnect.

**Status:** ⬜ Not verified

**Finding:** Requires real event frames plus an injected reconnect. Until
verified, no channel may be configured in `confirmed_sequence_channels`.

## Execution notice encryption

**Required proof:** `H0GSCNI0`/`H0GSCNI9` ACK key/IV behavior and decrypted
field mapping.

**Status:** ⬜ Not verified

**Finding:** `KIS_WS_HTS_ID` is not configured in this environment and no
controlled order notice was generated. This channel must remain absent from
the active subscription set.

## REST request and mutation capacities

**Required proof:** Read and mutation budgets per account/endpoint, plus the
exact pre-acceptance rate-limit response that is safe to retry.

**Status:** ⚠ Vendor-declared; mutation behavior not credential-verified

**Evidence:** WS0-O01 states production REST capacity of 18 calls/second,
simulation capacity of 1 call/second, one access/approval-key issuance per
second, and recommends 100–150 ms between concurrent calls. WS0-E03 shows six
spaced production reads succeeding, but it was not a boundary/stress test.

**Finding:** Keep `KIS_MUTATION_BUDGET_VERIFIED=false` and all mutation
capacities at zero. A controlled simulation submit/cancel/replace sequence is
still required to identify endpoint-specific behavior and prove that the
rate-limit rejection is unambiguously pre-acceptance.

## Sign-off

Full WS0 execution qualification is complete only when every required row
above is verified, all raw captures have redacted fixtures, and the resulting
adapter/configuration change has rerun Gate 1 on its exact commit. Gate 2 may
start earlier only when the WebSocket/read-only subset is verified: event-time
semantics, sequence availability/reset behavior, reconnect/resubscription,
execution-notice encryption/mapping, aggregate session accounting, and the
standalone soak reporter. Mutation correlation, broker-order uniqueness,
history latency/completeness, and mutation rate-limit evidence remain required
for later execution gates, but do not block a zero-mutation Gate-2 soak.

Current disposition: **WS0 WebSocket subset incomplete; Gate 2 blocked; live
execution unauthorized.**
