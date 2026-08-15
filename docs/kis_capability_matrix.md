# KIS Protocol Capability Matrix

Status: **SKELETON — no rows verified yet**

This is the required output of Workstream 0
([kanban_production_readiness.md](kanban_production_readiness.md)). Every
row below must be filled in with evidence from the real KIS API (production
read-only and/or simulation) before the corresponding gated workstream
starts:

- Workstream 2's A4a/A4b (durable order ownership) is gated on the
  "External correlation key", "Correlation recovery", "Broker order ID",
  "History latency", and "History completeness" rows.
- Workstream 5's D1 (WebSocket client) is gated on the "WebSocket symbol key
  format", "WebSocket connection/subscription limits", "Quote timestamp
  fields", "Sequence numbering", and "Execution notice encryption" rows.

**This matrix cannot be filled in from this development environment** — it
has no live KIS production or simulation credentials. It must be completed
by someone with that access, running the verification method in each row
against the real API, and recording the actual (redacted) evidence here —
not an assumption based on the official sample or vendor documentation.

Redacted request/response and WebSocket frame samples gathered during
verification go in `tests/fixtures/kis_protocol/` (see that directory's
`README.md`), reused by Workstream 7's protocol tests.

---

## External correlation key

**Required proof:** Whether `MGCO_APTM_ODNO` (or any other field) accepts a
unique application-supplied ID on submission.

**Verification method:** Submit test orders in simulation with a unique
token in `MGCO_APTM_ODNO`, inspect the response.

**Status:** ⬜ Not verified

**Evidence:**

<!-- Paste redacted request/response here once verified. -->

**Finding:** _(supported / not supported / partially supported — describe)_

---

## Correlation recovery

**Required proof:** Whether the correlation-key value (if one exists, per
the row above) is echoed back in submission responses, open-order queries,
history queries, and execution notices.

**Verification method:** Cross-check one test order's token across all four
query surfaces (submission response, `inquire-nccs`, `inquire-ccnl`,
execution notice).

**Status:** ⬜ Not verified

**Evidence:**

<!-- Paste redacted request/response here once verified. -->

**Finding:**

---

## Broker order ID

**Required proof:** Exact response field name; whether it's present
immediately on submission ack or only appears later.

**Verification method:** Inspect real submission/query responses.

**Status:** ⬜ Not verified

**Evidence:**

<!-- Paste redacted request/response here once verified. -->

**Finding:**

---

## History latency

**Required proof:** Time from submit/cancel to appearance in
`inquire-ccnl`/`inquire-nccs`.

**Verification method:** Timed test submissions, repeated across a session
(open, midday, close, to capture any variance).

**Status:** ⬜ Not verified

**Evidence:**

<!-- Record measured latencies (min/median/max) here. -->

**Finding:**

---

## History completeness

**Required proof:** Max date range, pagination behavior, exchange coverage,
whether cancelled/rejected orders appear at all.

**Verification method:** Boundary-condition queries — a very old order, every
configured exchange (NASD/NYSE/AMEX), a known-cancelled order, a
known-rejected order, a page boundary.

**Status:** ⬜ Not verified

**Evidence:**

<!-- Record boundary-condition test results here. -->

**Finding:**

---

## WebSocket symbol key format

**Required proof:** Exact subscription key format per exchange (prefix,
case, delimiter).

**Verification method:** Inspect the official sample's subscribe payloads +
a live test subscribe.

**Status:** ⬜ Not verified

**Evidence:**

<!-- Paste redacted subscribe payload/ack here. -->

**Finding:**

---

## WebSocket connection/subscription limits

**Required proof:** Max symbols per connection; max connections per approval
key/account.

**Verification method:** Attempt subscribing an increasing symbol count
against sim/prod until a NACK or rejection occurs.

**Status:** ⬜ Not verified

**Evidence:**

<!-- Record the observed limit and the rejection behavior here. -->

**Finding:**

---

## Simulation environment differences

**Required proof:** Which TR IDs, order types, and WS feeds are unsupported
or behave differently in simulation vs. production.

**Verification method:** Attempt each capability in sim, record failures.

**Status:** ⬜ Not verified

**Evidence:**

<!-- List each capability attempted and its sim-vs-prod behavior here. -->

**Finding:**

---

## Quote timestamp fields

**Required proof:** Exact field(s) carrying exchange event time vs. local
receive time, on both `HDFSCNT0` and `HDFSASP0`.

**Verification method:** Inspect real WS frames.

**Status:** ⬜ Not verified

**Evidence:**

<!-- Paste a redacted real frame with the timestamp field(s) annotated. -->

**Finding:**

---

## Sequence numbering

**Required proof:** Whether any WS channel actually provides a monotonic
sequence field, and its exact semantics.

**Verification method:** Inspect real WS frames across a session, including
across a forced reconnect (does the sequence reset, and if so how is that
signaled?).

**Status:** ⬜ Not verified

**Evidence:**

<!-- Record findings, including reconnect behavior, here. -->

**Finding:**

---

## Execution notice encryption

**Required proof:** Whether/how `H0GSCNI0`/`H0GSCNI9` payloads are
encrypted; which fields survive decryption.

**Verification method:** Inspect the official sample's decrypt routine + a
live test notice.

**Status:** ⬜ Not verified

**Evidence:**

<!-- Paste a redacted decrypted notice (with the decrypt method noted). -->

**Finding:**

---

## Sign-off

This matrix is complete when every row above has a filled-in `Status: ✅
Verified` and a recorded `Finding`. Until then, Workstream 2's A4a/A4b and
Workstream 5's D1 remain blocked per
[kanban_production_readiness.md](kanban_production_readiness.md)'s
Workstream 0 gate.
