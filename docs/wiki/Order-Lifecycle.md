# Order Lifecycle

```mermaid
stateDiagram-v2
    [*] --> CREATED: durable local/gateway identity reserved
    CREATED --> UNKNOWN_SUBMISSION_STATE: immediately before broker request
    UNKNOWN_SUBMISSION_STATE --> ACCEPTED: broker accepts
    UNKNOWN_SUBMISSION_STATE --> REJECTED: broker rejects unambiguously
    UNKNOWN_SUBMISSION_STATE --> WORKING: reconciliation finds live order
    ACCEPTED --> WORKING: query/reconciliation
    WORKING --> PARTIALLY_FILLED: broker evidence
    PARTIALLY_FILLED --> FILLED: remaining fill evidence
    WORKING --> CANCEL_PENDING: guarded cancel request
    CANCEL_PENDING --> CANCELLED: broker confirmation
    ACCEPTED --> FILLED: conservative account/order evidence
    REJECTED --> [*]
    CANCELLED --> [*]
    FILLED --> [*]
```

Exact internal enums differ between the legacy local ledger and guarded SQL
records, but the safety semantics are the same.

## Submission

An entry requires a fresh matching pre-trade risk decision. Durable command,
order identity, ownership, lease, mutation budget, capital reservation, and
idempotency checks occur before the broker call. The state is marked ambiguous
before sending so a crash cannot make an unknown outcome look retryable.

## Reconciliation

Broker acceptance does not update shares or cost. Order/history/position
evidence applies fills conservatively. `applied_filled_quantity` prevents the
same partial fill from being applied twice. Incomplete/ambiguous evidence
remains pending or working.

## Exits

Liquidation is not blocked by entry risk rules. Outside the U.S. regular
session, eligible manual PROD partial/full exits use the persisted KIS
market-on-open reservation policy. Broker acceptance still requires later
reconciliation.
