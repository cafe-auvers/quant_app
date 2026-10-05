# Watchlist and Buy List

## Watchlist

Watchlist is a persisted, passive planning stage exposed through the shared
stock sidebar, Scanner actions, and chart actions. The former dedicated
Watchlist tab is not part of the current UI.

Watchlist records can carry symbol/name, planning price, structural breakout,
source, notes, and timestamps. Adding a candidate does not create an order or
arm monitoring.

## Buylist

Buylist represents a more committed planning/compatibility stage and supplies
symbols to Buy Board bootstrap and ORB evaluation. Production account identity
must remain isolated. Queue-backed statuses and broker-confirmed holdings are
not interchangeable with local planning labels.

## Safe movement

A saved positive breakout price is required before Watchlist promotion. On
mobile, tap **Buylist** or **Buy Today** to enter a missing price in the editor.
The browser waits for the canonical price save and uses its returned revision
for promotion. **Buy Today** then asks for explicit activation confirmation
when Operator Control permits it; otherwise it creates a non-executable draft.
Canceling the editor or a rejected save prevents activation. Saving, success,
and error messages remain visible beneath the list buttons on mobile and web.

Mobile Buylist removal was verified after the Supabase cutover. Its row remains visible
while pending and disappears only after canonical confirmation. Errors are visible, stale
revisions are rejected, and delayed polls cannot resurrect a confirmed removal. This passive
operation never authorizes a broker order; existing positions/orders/protection remain
guarded.

- Watchlist to Buylist is a versioned planning action.
- Watchlist membership is independent of Buylist membership and execution
  evidence. **Remove from Watchlist** (or `W`) may clear the passive Watchlist
  flag on a Buylist card without deleting the Buylist card, stop, order, or
  position evidence.
- Buylist to Buy Today publishes one-session monitoring intent. The runtime
  still requires a completed current-session ORB, fresh breakout confirmation,
  passive execution conditions, and every safety gate before submission.
- Demotion after an entry identity exists can become a cancellation request;
  it is not an unconditional local move.
- A filled position cannot be created by editing JSON or dragging a card.
- Cross-device synchronization is revision-aware; do not hand-edit state while
  another device owns writes.
- Connected web/PWA actions render immediately as pending, then confirm or
  roll back from the canonical response. Browser invalidations and typed
  desktop pulses update the other running surfaces without a manual refresh.

See [Buy Board and Kanban States](Buy-Board-and-Kanban-States).

For current host, shared-store, and backup prerequisites, see
[Supabase Deployment](Supabase-Deployment).
