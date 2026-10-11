# Watchlist and Buy List

## Watchlist

Watchlist is a persisted, passive planning stage exposed through the shared
stock sidebar, Scanner actions, and chart actions. The former dedicated
Watchlist tab is not part of the current UI.

Watchlist records can carry symbol/name, planning price, structural breakout,
source, notes, and timestamps. Adding a candidate does not create an order or
arm monitoring.

## Phone Monitor

The bottom Watchlist button opens the Monitor table by default. It covers
Watchlist-only stocks as well as Buylist and Buy Today, on the existing minute
refresh. Best ORB requires price, position-bound, and opening-liquidity passes.
The + button uses normal explicit promotion/publication permissions; reading
the table does not authorize execution.

## Buylist

Buylist represents a more committed planning/compatibility stage and supplies
symbols to Buy Board bootstrap and ORB evaluation. Production account identity
must remain isolated. Queue-backed statuses and broker-confirmed holdings are
not interchangeable with local planning labels.

## Safe movement

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

Current release reference: [Opening liquidity and mobile workflow (2026-10-08)](../opening_liquidity_mobile_release_2026-10-08.md). Dated reports and archived plans retain their original scope.
