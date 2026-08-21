# Buy Board ORB Planning

The Buy Board is the only operator-facing ORB planning surface. The former
Watchlist tab, its AI analysis, snapshots, bulk scoring table, and Watchlist
navigation actions are not part of the active UI.

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
- published plans therefore evaluate the same buffered breakout on laptop and
  PC.

The active UI does not provide an in-place buffer replacement for an existing
queued or Buy Today plan. **Remove from Today** changes the card lifecycle but
does not make the header overwrite that plan's persisted buffer. Set the value
before the symbol is first queued, and treat it as immutable for that plan. If
the persisted buffer is wrong, do not publish or execute that plan on the
assumption that removing and re-activating it applied the new header value.
Changing the header alone is not a planning mutation and is not handed to the
executor.

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

## Execution boundary

Once a current-session, risk-valid ORB is `WAITING_BREAKOUT`/armed, a fresh
execution-grade live trade crossing its frozen ORH promotes the best eligible
crossed candidate and enters the guarded submission path immediately; it does
not wait for the one-minute planning refresh. Automatic mode checks every
eligible 1m/5m/30m candidate, while a manual window lock remains exact. A
daily-breakout level by itself, or a missing current-session ORB, never arms an
entry.

Neither dialog submits an order. The execution runtime still requires the
published Buy Today card, current-session ORB data, account-matched sizing, a
fresh qualifying price, valid sizing, fresh total equity, current buying power,
Execution Owner authority, Operator Control rules for manual commands, live-trading enablement,
reconciliation, and every broker-boundary fence described in
[Execution Owner and Operator Control](execution_operator_control.md).

The hidden `WATCHLIST` lifecycle value and `watchlist.json` remain readable for
non-destructive migration and cross-version state compatibility. They do not
create a Watchlist tab, visible board column, live subscription, or alternate
execution path.
