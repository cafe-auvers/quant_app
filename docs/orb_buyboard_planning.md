# Buy Board ORB Planning

Buy Today publication offers **Cancel**, **Publish to Buy Today**, and
**Publish as EP**. Both publication choices use the same card and workflow.
The EP label selects only the stop/ADR validity bounds and scoring ideal:
normal defaults are 15% / 65% / 90%, and EP defaults are 50% / 100% / 150%.
Both profiles can be edited in shared ORB settings on desktop and web/mobile.
The ADR measurement, stop placement, position-sizing formula, and other entry
and exit checks are unchanged. Previously saved cards use the normal profile.

Buy Board prices are indicative observations, labelled with their source, timestamp,
and age. When Yahoo is delayed or a refresh fails, the last available price remains
visible. Estimated P&L and distance to the active stop also remain visible, marked
`stale` until a later quote replaces them through the normal minute refresh.
These display estimates do not satisfy automatic entry freshness requirements.

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
- Drawing or editing a breakout target does not add the symbol to today's
  Watchlist. The target remains available after Watchlist membership expires or
  is removed.
- **Move to Buylist (Q)** is a separate action and is rejected until the symbol
  has a positive breakout price.
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

## Sparse KIS opening history

The verified KIS `xymd`/`xhms` timestamps carry explicit New York provenance,
including through the cache. A late local 13:30 or 14:30 bar is never interpreted
as a UTC opening bar. Arbitrary configured field mappings cannot establish this
provenance. Invalid downloaded rows prevent sparse-range authorization.

Some KIS minute-chart responses have no bar at exactly 09:30. This does not
move the opening window: 1m remains 09:30–09:31 ET, 5m remains 09:30–09:35,
and 30m remains 09:30–10:00. Bars are never filled or borrowed from a later
minute to manufacture a missing short-window range.

A longer window may use its actual KIS bars when a backward-paged download
reaches before the session open and through the window end. The planner binds
that coverage to a digest of the opening rows. The PC historical cache stores
bars and the optional `intraday_history_coverage` record in the same transaction;
the record is keyed by symbol, interval and source. This table is local MySQL
cache metadata, separate from Supabase coordination. Older caches continue
using the original exact-09:30 check until refreshed. Truncated downloads,
malformed broker rows, wrong symbols/sessions, unfinished windows, changed
cached opening rows, and non-KIS fallback data cannot use the sparse-history
permission. The proof is also rebound when 1m bars are resampled to 5m.

An empty completed window explains its exact time bounds and the first
available bar. When every automatic candidate is blocked, the Buy Today card
shows the reasons for all windows; an explicit manual window keeps its own
reason. Price-zone, sizing, fresh WebSocket, broker, ownership and live-switch
checks remain enforced.

CURV diagnostic on 2026-10-05: direct KIS NYS responses paged into the previous
session and still began at 09:43 ET. No 1m/5m opening bars were returned. Its
actual 30m range was $2.51/$2.48, while the saved breakout was $2.52; that
calculated plan is rejected because there is no passive entry zone above the
breakout and at or below ORH. This is a dated data/plan diagnosis, not an entry
recommendation or authorization to change the breakout. No live card or order
was changed by the diagnostic replay.

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

## Connected web/PWA feedback

An allowlisted connected chart/planning action renders immediately as pending
on the initiating browser. Canonical revision, lifecycle, session, account, and
Operator Control checks still run on the server. A success replaces the pending
view with canonical state; a failure rolls it back. Authenticated browser
invalidations and typed desktop pulses update the other running surfaces, with
revision polling/startup reads as recovery. A manual refresh is not required
and cannot turn a rejected plan into executable intent.

Buy Today activation through this surface uses the same canonical workflow and
may claim eligible `KANBAN` ownership, but it does not place an order. Execution
still belongs to the separately gated `ACTIVE` Execution Owner. See
[Web/PWA Operator Synchronization](web_operator_sync.md).

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

## Mobile Buy Board risk and NAV allocation

The card and its detail sheet show the selected risk budget as a percentage
of NAV. Today and Entry also show estimated planned risk and planned allocation
as both percentages and dollars. Planned risk is target shares multiplied by
the difference between execution price and the planned stop; planned allocation
is target shares multiplied by execution price. An unsized plan stays labelled
`Not sized`.

Filled Entry cards and Open, Partial and Sell All cards show `Allocated` using
the broker-confirmed remaining shares multiplied by average fill price. A sell
request does not reduce this amount before the shares actually leave the
holding. Positions show `Risk at stop` using the positive entry-to-active-stop
loss and remaining shares; a stop above entry implies zero estimated loss from
entry. Incomplete stop coverage is labelled rather than assigned zero risk.
These estimates exclude fees and slippage.

The denominator is the account-matched USD equity snapshot already published
by the PC. Its actual fetch time accompanies the value. Snapshots older than
15 minutes, missing, invalid, or from another account cannot supply NAV
percentages. The browser also expires a previously received NAV during a
failed/offline poll; known dollar amounts remain visible. The NAV header shows
the current usable denominator. Rendering makes no broker calls and adds no
canonical database query for NAV.

## Explicit re-entry after a completed exit

Mobile Cancel Today withdraws the latest entry intent for the selected symbol.
Runtime observation revisions do not invalidate cancellation; each attempt still
locks and validates the current canonical card. Repeated cancellation is harmless.
An unsubmitted entry returns to Buylist immediately. A working or partially filled
entry remains pending until the executor confirms cancellation; filled shares and
their protective stops remain in Open Positions. The chart shows Cancelling while
broker reconciliation is pending, and a partially completed position exposes
Cancel remaining buy.

Editing an unsubmitted breakout rebuilds its entry qualification, sizing and
breakout confirmation from the revised target. Opening-range high and low remain
the observed first 1, 5 or 30 minutes. Passive execution still requires an entry
above both the breakout and opening-range low, and at or below the opening-range
high; a breakout above that high has no execution zone. Mobile rejection feedback
includes these prices. An unrelated observation revision can be retried, while a
concurrent change to the breakout or lifecycle requires reviewing the current card.

A stop hit starts liquidation. A new entry cannot start while the stock remains
held or its exit is pending. Once broker reconciliation confirms zero shares and
the completed cycle is Closed, select the stock and confirm Buy Today again on
mobile, or use Re-enter on the PC chart. The original breakout price and risk
budget are retained; editing the breakout price is no longer required.

Each explicit activation starts a fresh planning cycle. Previous entry/exit
identities, frozen ORB execution values, stop state and retry projections are
retired, while the immutable order history remains available. The PC calculates
a current-session ORB plan and creates a new broker-order identity. There is no
one-entry-per-symbol-per-day lock and no automatic reactivation after another
stop. Repeated same-day attempts still require a new user activation each time,
current ORB qualification, fresh KIS trade/quote data, account equity/buying power,
portfolio risk, execution ownership, Live Trading and broker-boundary checks.
Existing attempt-rate limits and cooldowns remain in force.

Queue refresh also retires an old FILLED compatibility lock when the canonical
card is a fresh Buy Today activation, broker-flat, and carries no durable execution
evidence. Account identity must match, and broker/open-order and canonical-order
checks must show no active order. Old candidates and breakout confirmations are
discarded together, so the next entry requires rebuilt geometry and fresh market
data. Working, ambiguous, partial and currently held execution remains fenced.

Re-entry rejects nonzero held/sellable shares, nonterminal position/entry/exit
state, pending stop changes, reserved capital, active owned/external orders and
unresolved broker commands. These checks run again before the canonical update.
Closed remains outside the ordinary drag graph; only a validated new-cycle
command can reopen it.

A Closed card can retain a historical reservation ID. The new-cycle check reads
the canonical reservation ledger and retires that reference only when its
environment/account/symbol match, its status is Consumed/Released/Expired and
both remaining reserved money and projected risk are zero. Missing, mismatched,
active or inconsistent reservations still block; an active reservation for the
symbol also blocks even if its card reference is missing. No reservation is
released by a re-entry request.

An ambiguous historical cancel stops blocking only when its exact target order
has a unique matching account/symbol identity, zero remaining quantity and a
Filled/Cancelled/Expired broker observation reconciled after that cancel was
requested. Missing, earlier or inconsistent evidence still blocks. An unresolved
submit, replace or in-flight requested cancel always blocks. This check retains
the original command and order records; it does not retry or send a cancel.
