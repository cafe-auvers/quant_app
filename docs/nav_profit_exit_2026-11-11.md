# Automatic NAV profit exit

The user's 2026-11-11 deployment instruction authorizes this policy for all open
manual and application-entered positions, on the PC executor and mobile web UI.

- A manual holding without an existing or pending stop receives the existing
  initial safety stop: average cost minus 2% of fresh USD account NAV divided
  by full broker-held shares. User edits take priority. Application entries
  retain their own ORB/strategy stop; this safety default does not replace it.
- At first complete, fresh account reconciliation of a held position, freeze a
  profit target equal to 2% of that account's NAV. For existing positions this
  is enrollment NAV; subsequent NAV changes do not move the target.
- Once the whole position's unrealized gross profit reaches that dollar target,
  request one immediate partial sale of half the current broker holding. Whole
  shares round down; at least one share remains. One-share positions cannot be
  split and show that limitation. At $11,000 NAV, the trigger is $220 of whole
  position profit; an exactly half sale at that price realizes $110 before costs.
- Only after broker-confirmed fills complete the intended half-sale, move the
  remaining stop to cost-adjusted breakeven. Preserve any tighter active stop.
  The remaining shares continue under the active stop and manual exit controls;
  the new policy adds no further profit-sale or time-based partial exit.

The profit request uses the regular revision-checked board workflow, current
Main lease/readiness and ownership fences. Only fresh regular-session trade
quotes can trigger it. Stale quotes, book updates and protective-breach replays
cannot authorize profit taking. Stop-loss liquidation takes priority. Active
unowned, ambiguous or conflicting broker orders remain fenced; the automation
does not adopt or cancel another order behind the user's back.

The nested `nav_profit_exit` state persists its cycle identity, captured NAV,
target, requested sale quantity and cumulative broker fills per order. Repeated
polls, terminal-history replay and restarts cannot sell another half. A terminal
partial fill retries only the outstanding intended quantity, using the existing
exit retry identities and cooldown. Zero fills do not move the stop to breakeven.
An outside quantity reduction that would consume the runner leaves the objective
pending with a visible position-change warning for operator review.

A custom partial sale or a cancellation of the automatic objective overrides
automatic profit taking for that position cycle. Editing the stop does not
disable the profit target. A confirmed flat position resets the policy; a new
purchase receives a new cycle. Invalid persisted policy state blocks automatic
profit sales instead of silently re-arming them. The PC and web serializers
both retain this state; mobile projections expose only target/progress facts.

These are application-managed exits under the existing regular-session
execution policy. The app must be running with Live Trading, fresh market data
and execution readiness. Fees, gaps and actual fills can change realized P&L;
the 2% initial stop is a risk budget, not a guaranteed maximum loss.
