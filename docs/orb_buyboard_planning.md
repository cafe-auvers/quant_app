# Buy Board ORB Planning

The Buy Board is the only operator-facing ORB planning surface. The former
Watchlist tab, its AI analysis, snapshots, bulk scoring table, and embedded ORB
matrix are not part of the active UI. Watchlist membership itself remains a
lightweight, passive planning workflow in the stock sidebar, Scanner, and
TradingView.

This page explains planning controls. [Current Order Logic](current_order_logic.md)
is authoritative for breakout confirmation, passive limit submission,
Entry Pending, higher-score replacement, fills, rejections, and EOD behavior.

## Passive Watchlist workflow

- In **Scanner**, select a result and click **Add selected to Watchlist**.
- In **TradingView**, load a symbol and click **Add to Watchlist (W)**, or use
  the `W` shortcut.
- Choose **Watchlist** in the stock sidebar to review saved candidates. From
  there, **Move to Buylist** performs the explicit passive-stage promotion;
  **Remove from Watchlist** removes an unwanted candidate.
- On a Watchlist chart, drawing or clearing a breakout target keeps the symbol
  in Watchlist. **Move to Buylist (Q)** is the separate promotion action.
- A Buylist card can be returned with **Move to Watchlist** from its Buy Board
  context menu or the TradingView queue-stage control.

Watchlist membership is persisted and included in the normal cross-device plan
sync. It does not create an ORB candidate, subscribe to execution quotes, or
place an order. Live ORB monitoring begins only after the symbol is explicitly
published/activated in **Buy Today**.

Add, move, and remove actions require the shared coordination database and an
explicitly selected production account. If either is unavailable, the action
leaves both the canonical card and local Watchlist/Buylist mirrors unchanged;
there is no offline promotion that can later overwrite a newer device change.

## Buffer %

`Buffer %` sits in the Buy Board header immediately left of `Engine`. It uses
percent units: `0.10` means a fractional buffer of `0.001` (0.10%). `0` is a
valid value and round-trips unchanged.

The field is a default for a **newly queued** plan. It is intentionally not a
live control:

- editing it does not modify an existing Buylist or Buy Today card;
- the one-minute Buy Today ORB refresh reuses each symbol's persisted buffer;
- a manual ORB-window lock keeps the persisted buffer;
- switching Execution Owner cannot substitute the new device's local header
  value; and
- published planning views therefore evaluate the same persisted buffer on
  laptop and PC.

The active UI does not provide an in-place buffer replacement for an existing
queued or Buy Today plan. **Remove from Today** changes the card lifecycle but
does not make the header overwrite that plan's persisted buffer. Set the value
before the symbol is first queued, and treat it as immutable for that plan. If
the persisted buffer is wrong, do not publish or execute that plan on the
assumption that removing and re-activating it applied the new header value.
Changing the header alone is not a planning mutation and is not handed to the
executor.

The active broker path does not use the old
`max(orb_high, breakout_price * (1 + buffer_pct))` formula. Its finalized
passive zone uses the raw canonical breakout price as documented in
[Current Order Logic](current_order_logic.md). Buffer remains persisted
planning metadata and may affect compatibility planning displays; it must not
be interpreted as the broker limit or live confirmation price.

After a symbol is added to Watchlist, drawing its first target creates or
updates the passive canonical Watchlist plan and snapshots the current header
buffer. Drawing or revising a target on an existing Watchlist card keeps it in
Watchlist. **Move to Buylist** is the explicit promotion step. Revising a
non-empty target retains that plan's existing buffer. An explicit chart
**Clear** clears the passive target without promoting it; this is distinct from
**Remove from Today**, which preserves the published plan and its buffer.

## Buy Today context actions

Right-click a card in **Buy Today** for two separate ORB views.

### ORB Combinations...

This is a read-only diagnostic matrix. It expands the queue's 1m, 5m, and 30m
ORB structures across eight risk cases (0.25% through 2.00%), for 24 total
combinations. It shows valid, forming/unavailable, and invalid choices; the
`Valid combinations only` checkbox is a view filter, not a plan selection.

The matrix uses the card's persisted buffer and the equity embedded in the
queue sizing snapshot so every row describes one coherent plan snapshot. Each
candidate also persists the date of its newest source bar in New York market
time. A window whose bars are not from the current session is shown as
`NOT_AVAILABLE` and can never appear green, even if a cache refresh happened
today. A hard-rejected candidate stays invalid.

Queue sizing also persists the exact account used for equity. If the queue
snapshot belongs to a different account, neither dialog nor the execution
bridge will use it. Because the compatibility queue is still keyed by symbol,
the same symbol cannot be active in Buy Today for two accounts at once; the
second activation is rejected, and pre-existing conflicts remain
`RISK_INVALID` until one card is removed from Buy Today.

Opening or filtering this dialog performs no database write, queue lock, board
command, KIS request, or broker action.

### Refresh / Select ORB Plans...

This remains the optimized view: one candidate for each of 1m, 5m, and 30m.
Before the regular session, it can refresh the existing queue snapshot, keep
automatic best-plan selection, or explicitly lock one ORB window **only on the
device that currently owns Operator Control**. If Operator Control is Locked,
owned by the other device, or cannot be verified, the cached plans still open
for inspection but the dialog is read-only.

During the regular session the optimized dialog is read-only on every device.
It displays the cached plan snapshot and selection without recalculating,
locking, unlocking, or persisting a manual selection. The Execution Owner may
continue updating live candidate status through the execution runtime; opening
this dialog is not a second market-hours planning path.

Any permitted pre-market refresh still uses the plan's persisted buffer, never
the current local header default.

Locking a window or returning to automatic selection saves only this device's
local execution-queue planning state. It is **not** a cross-device handoff.
After either change, the Operator Control owner must click **Publish Today's
Plan** before switching Execution Owner or expecting the other machine to use
it. Until that publish completes, a different Execution Owner continues from
the last published snapshot; changing ownership does not transmit an
unpublished local lock or unlock.

## Chart target changes

TradingView's Set, Clear, Queue, and Activate controls use version-fenced
canonical planning commands; they never rely on an unsynchronized local target
as execution authority. **Add to Watchlist** persists membership; after that,
a newly drawn target creates or updates a versioned passive Watchlist card for
the explicitly selected KIS account, and **Queue / Move to Buylist** performs
the separate promotion. Clearing a passive target leaves the Watchlist card
non-executable with no breakout level.

Every Set/Clear request requires verified Operator Control and a known market
session. A published Buy Today target may be changed or cleared only before the
regular session opens. Setting it then invalidates the previous ORB geometry,
quantity, and trigger so the plan must be rebuilt; clearing it moves the safe,
zero-evidence card back to Buylist and clears every executable entry field.
During regular market hours the published target is immutable. Any existing
entry/order/reservation/cancellation/position evidence also rejects the change.

The canonical trade card always wins over local compatibility data. If a local
execution-queue target is missing or differs after a chart edit, execution is
`DATA_UNAVAILABLE` until a fresh queue snapshot matches; the stale queue can
never restore the old target or submit against it.

## Execution boundary

Once a current-session, risk-valid ORB is `WAITING_BREAKOUT`/armed, a fresh
execution-grade KIS trade strictly above
`max(orb_high, breakout_price)` latches that candidate's breakout. Automatic
mode chooses the highest-scoring eligible crossed 1m/5m/30m candidate; an equal
score favors the earlier timeframe, while a manual window lock remains exact.
The runtime immediately submits a passive BUY limit at the configured
execution price (ORB high by default) only while both last trade and best ask
remain above that limit. It does not wait for a pullback before submission, and
it never treats the breakout event as a fill. A daily-breakout level by itself,
or a missing current-session ORB, never arms an entry.

After submission, a zero-fill working order stays Entry Pending. It has no
legacy 15-second auto-cancel/reprice deadline. A later timeframe may replace it
only if its range and breakout qualify, its score is strictly higher, every
risk/capital/quote gate passes, and KIS authoritatively confirms the old order
cancelled with zero fills before the new generation is submitted.

Neither dialog submits an order. The execution runtime still requires the
published Buy Today card, current-session ORB data, account-matched sizing, a
fresh qualifying price, valid sizing, fresh total equity, current buying power,
Execution Owner authority, Operator Control rules for manual commands, live-trading enablement,
reconciliation, and every broker-boundary fence described in
[Execution Owner and Operator Control](execution_operator_control.md).

The hidden `WATCHLIST` lifecycle value and synchronized `watchlist.json` remain
the user-managed passive candidate stage. Watchlist items are accessible in the
sidebar and can be promoted to Buylist, but they do not create a dedicated tab,
visible board column, live subscription, or alternate execution path.
