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
- ORB replacement is never an in-place price edit: the gateway fully
  prevalidates, cancels the exact zero-fill order, confirms terminal
  cancellation, revalidates, and submits one linked generation.
- Ambiguous submission state must reconcile; it must never be retried as a new
  logical order. The read retry policy never applies to broker mutations.
- A definitive `APBK0656` order rejection is classified as exchange-routing/
  configuration failure. It does not create Entry Pending and does not demote
  the strategy as rejected; the Buy Today plan clears that identity and waits
  for a corrected-route retry.

Vendor capability evidence and unresolved qualifications are maintained in
the repository KIS capability matrix and Gate 2 checklist.

See [Current Order Logic](https://github.com/cafe-auvers/quant_app/blob/master/docs/current_order_logic.md)
for the complete entry and replacement sequence.
