# KIS API Integration

## Implemented adapters

- Production account/profile discovery and paginated balance snapshots.
- OAuth token caching with locking and separate environment cache files.
- Overseas order placement, query, cancellation, and reserved U.S. sell paths.
- WebSocket approval/authentication, subscriptions, ACK tracking, trade/quote
  frame parsing, freshness, and capacity evidence.
- Daily and intraday market-data adapters behind explicit configuration.

## Separation and defaults

Production and simulation credentials/token caches are separate. The active UI
exposes production account visibility; tests use fakes, monkeypatches, recorded
redacted protocol evidence, and in-memory stores. Test success is not a
credentialed KIS certification.

KIS intraday stays disabled until endpoint, TR ID, request parameters, raw
OHLCV mappings, timestamp semantics, and capacity have been verified. Yahoo
fallback may serve research/intraday workflows but is not execution-grade
quote evidence.

## Operational rules

- Never log app secrets, tokens, full accounts, or raw sensitive responses.
- A successful placement response is broker acceptance, not a fill.
- Pagination must be complete; malformed continuations fail closed.
- Read-only account requests use bounded retries for classified transient
  network, gateway, rate-limit, and domestic-balance `APBK1350` failures.
  Permanent client/protocol errors are not retried.
- Submit/cancel/replace calls use shared mutation budgets and spacing.
- Ambiguous submission state must reconcile; it must never be retried as a new
  logical order. The read retry policy never applies to broker mutations.

Vendor capability evidence and unresolved qualifications are maintained in
the repository KIS capability matrix and Gate 2 checklist.
