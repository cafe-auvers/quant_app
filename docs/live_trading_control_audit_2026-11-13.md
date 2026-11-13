# Live Trading control audit

The shared PC control was persistent ON at revision 123 throughout the
November 13 POAS trade. KIS confirms a manual purchase of 600 shares at $3.32,
followed by the app's STOP_LOSS sale of all 600 at $3.2996. The sell order was
acknowledged at 12:28:43 UTC and reconciled FILLED at 12:29:03 UTC. Gross P&L
was -$12.24 before fees. This is a separate trade from November 12's
900-share POAS position.

The November 12 maintenance pause was 16:36:57 to 16:39:25 UTC, after that
day's POAS exit. There is no OFF audit transition during either POAS sale.
Broker-boundary checks re-read the durable control. A toolbar display alone
does not establish which permission was effective when an order was sent.

The old toolbar could present a process-local cached OFF or an observer's
different release pin as an undifferentiated disabled state. It now projects
the saved choice separately from a local release block, refreshes its memory
projection each second, and ignores delayed older control revisions. Every
control write emits the existing cross-process notification after commit.
Observer ON actions target the current approved executor release.

An approved, ready execution owner with no saved choice initializes a durable
ON choice once. Existing choices, including explicit OFF, survive restarts,
session changes and handoffs. A compare-and-set prevents concurrent OFF from
being overwritten. Administrative locks, exact-release approval, lease,
broker reconciliation, ownership and broker-boundary checks remain required.
