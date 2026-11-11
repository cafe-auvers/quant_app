# Position P&L and readable cards

Open Position, Partial Sell and Sell All show current unrealized gross P&L in
USD and as a percentage of the average purchase cost. Exit estimates are
separate from current P&L: a partial sell uses only its requested shares or the
broker-reported remaining working-sell quantity; a full liquidation estimates
all remaining held shares. Previously realized sales are not added to either.

A priced partial sell uses its limit floor. An immediate partial sell or Sell
All uses the current reported quote as an estimate, not a promised execution
price. The quantity and reference price are shown with the estimate. A latched
full exit never inherits an old partial-sell limit. Missing cost, price or share
quantity shows unavailable rather than zero profit. Estimates exclude fees.
Quote provenance and stale labels remain visible; this display adds no broker
polling and does not modify execution behavior.

Mobile cards put current P&L, expected exit P&L and the outcome at the active
stop first. Full position facts remain in the action sheet. Cards have larger
text, separate metric blocks, wrapping warnings and one page scroll instead of
a short nested list viewport. The action sheet keeps its close control visible,
scrolls to an opened editor, and uses readable inputs without Safari input zoom.
Partial-sell quantity and optional price edits update the P&L/proceeds preview
without submitting an order. Polling preserves an open draft; confirmations also
include the estimate.

Desktop cards put P&L and the exit estimate first, wrap names and metrics, and
size each list item from its actual width. The card border is scoped to the
card itself so labels do not acquire repeated borders. Partial-sell dialogs
show a reactive P&L/proceeds preview; Sell All confirmations show a reported-quote
estimate when available.

Validation includes arithmetic and unavailable-data cases, partially filled
sell quantities, stop priority, native wrapping, desktop dialog previews, mobile
320/390/820px layouts, stale/missing quotes, draft preservation, and confirmation
previews. Browser fixtures reject writes; no live test order is placed.
