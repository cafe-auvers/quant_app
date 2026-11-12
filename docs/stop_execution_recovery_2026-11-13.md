# Stop execution recovery

The November 12 New York session exposed failures after stop triggers had
already latched. Startup readiness alone did not cover these later lifecycle
failures.

- Account-wide history exceeded the ten-page query ceiling. Reconciliation
  stayed incomplete, leaving already-filled external orders and terminal
  cancellation records active in the application. Pagination now supports up
  to 100 pages and still fails closed on missing/repeated cursors or exhaustion.
  A read-only incident probe completed 25 total ledger pages, including 13
  history pages, with no broker errors.
- A manual default stop could trigger before its guarded ownership enrollment.
  Enrollment now also retries a latched stop liquidation, retaining the stop
  price, quantity and irreversible liquidation intent. Existing readiness,
  explicit ownership and conflicting-order fences remain in force.
- A cancelled amended zero-fill order could retain a false active observation.
  The same unique exact cancellation evidence now retires either an original
  or an amended order. Identity, quantity, timestamps and contradictory-fill
  checks remain unchanged.
- A late exchange rejection could race a pending cancellation. The existing
  exact broker identity is retained and the order reaches terminal REJECTED.
  A rejection contradicting confirmed fills remains blocked for review.
- Repeated ownership and external-order fences consumed repricing attempts
  despite making no broker call. Such waits now preserve the pending identity
  and attempt counter, with the normal cooldown. Actual submissions and broker
  rejections retain the existing retry lifecycle.

Emergency maintenance first reconciled explicit, freshly queried terminal
records and completed the missing default ownership assignments through the
existing repositories. The running approved Main remained the sole broker
executor. Broker-rejected retry chains were restarted only after exact terminal
proof, using fresh groups while retaining the old order audit records. Stops,
share quantities and latched liquidation intent were preserved. Private broker
evidence and recovery receipts stay outside source control.

The manual default risk remains 1% of fresh USD NAV. The 2% NAV half-profit
target, confirmed-fill breakeven behavior and automated strategy stops are
unchanged. Application-managed stops still depend on a ready executor and
cannot guarantee an execution price.
