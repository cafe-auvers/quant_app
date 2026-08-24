# Execution Owner and Operator Control

The dashboard uses two independent shared roles:

- **Execution Owner** is the only device that may mutate live canonical state,
  consume operator commands, and cross the KIS broker boundary.
- **Operator Control** is the only device that may create new manual live
  commands. It can be assigned to PC, Laptop, or Locked.

These roles answer different questions. Execution Owner answers "which one
process may act?" Operator Control answers "which device may send the next
human instruction?" Neither role, by itself, arms live trading or bypasses
per-symbol market-data, account, risk, order-identity, or broker checks.

The roles are stored in the shared coordination database as `__main_device__`
and `__operator_control__`. When `COORD_DB_*` is configured, that authority is
the TLS-connected TiDB Cloud SQL database; otherwise the legacy deployment
uses PC MySQL. A real app window never falls back to its private SQLite
database for ownership. If the selected shared store is unavailable,
ownership and live execution fail closed.

Assigning **Execution Owner: Laptop** moves execution authority, not data
storage. With TiDB coordination configured on both devices, the PC may be
powered off: historical reads move to the laptop mirror while ownership,
commands, orders, and TradeCards remain online. See
[TiDB Cloud Coordination Store](tidb_coordination_store.md).

Each running device also publishes an explicit `device_kind` (`PC` or
`Laptop`) with its readiness record. On Windows this is derived from system
battery status, so default hostnames such as `DESKTOP-...` do not cause a
laptop to appear as a PC.

## Normal setup

1. Start `main.py` on the PC and laptop and wait for both readiness rows.
2. Set **Execution Owner: PC**.
3. Set **Operator Control: Laptop** while manual intervention is expected, or
   **Locked** when no more manual instructions should be accepted.
4. Before market open, click **Publish Today's Plan** on the Operator Control
   device. The app saves local JSON, atomically publishes watchlist, buylist,
   trade plans, and execution queue, reads all four rows back, and reports
   whether the execution owner's heartbeat is fresh.

Set the Buy Board **Buffer %** before a symbol is first queued. The field is a
planning default only: editing it does not rewrite an existing Buy Today card,
an ORB window lock, or a published execution queue row. The current Buy Board
does not provide an in-place buffer replacement for an existing plan, and
**Remove from Today** followed by re-activation does not make the new header
value overwrite that plan. Treat the persisted value as immutable and verify
it before publishing. See
[Buy Board ORB Planning](orb_buyboard_planning.md).

ORB candidates persist both the New York source-session date and the account
used for sizing. Yesterday's cached bars and a candidate sized for another
account are rejected before they can update a Buy Today card. The compatibility
ORB queue has one row per symbol, so one symbol may be active in Buy Today for
only one account at a time.

The header's **Live Trading** field includes the configured execution
envelope. For example, `CONTROLLED_LIVE: STIM, max $0.01/entry` means that
other Buy Today cards are planning-only and that an order above the displayed
cap will not be submitted. `Live Trading: Enabled` is not sufficient by
itself; confirm the mode, symbol scope, and cap shown beside it.

Live Trading has two layers. `TRADING_ENABLED` in each machine's private
`.env` is a one-way local administrative lock. If it is false on the laptop,
that laptop must show **LOCKED OFF** even when the shared switch is ON. If it
is true on the PC, the PC may display the shared switch, but it still cannot
submit unless it is the Execution Owner and every runtime/broker gate passes.
The durable ON/OFF switch itself is shared; the local `.env` locks are
intentionally not synchronized.

## Buy Today: pre-market versus market-open

Pre-market publishing and live Buy Today activation are separate paths:

- **Publish Today's Plan** atomically publishes the Watchlist, buylist, trade
  plans, and execution queue. It is a pre-market bulk synchronization step.
  Watchlist candidates remain available through the lightweight stock sidebar;
  there is no dedicated Watchlist tab.
- During the regular session, **Activate for Buy Today** becomes an
  `ADD_BUY_TODAY` operator command. It does not use the full-plan publish
  button.

The optimized **Refresh / Select ORB Plans...** dialog follows the same
planning boundary. Before market open, only the current Operator Control device
may refresh the queue snapshot or change automatic/manual ORB-window selection.
Locked, unverified, and non-owner devices get a read-only cached view. During
the regular session the dialog is read-only for every device and does not
refresh or persist selection changes; live candidate evaluation remains the
Execution Owner runtime's responsibility.

A permitted pre-market window lock/unlock is local until **Publish Today's
Plan** succeeds. Switching Execution Owner alone does not copy that queue
change; the new executor otherwise retains the last published plan snapshot.

Chart breakout Set/Clear actions are separate, version-fenced Operator Control
commands. Passive Buylist targets may be planned during market hours because
they are not executable, but a published Buy Today target is immutable during
the regular session. Premarket Buy Today edits clear the old ORB geometry, and
a stale queue target cannot overwrite or restore the canonical card.

The market-open command sequence is:

1. The Operator Control device inserts one idempotent `PENDING` request.
2. Only the current Execution Owner may claim it.
3. The executor revalidates the card version, duplicate active state,
   ownership, account, plan, and runtime facts.
4. A valid request moves the canonical card into Buy Today. The active
   runtime then monitors it and the normal entry engine decides whether an
   order may be submitted.

The executor checks this command queue immediately when the database-free
Tailscale change token advances. An old listener retains the 20-second poll;
protocol v2 uses a one-hour missed-notification fallback. Protocol v3 labels
the token `operator_commands`, so unrelated readiness, card, and plan writes
do not poll the queue. Planning/UI sync uses its matching typed pulse. An execution-owner switch also force-loads the
latest canonical cards, quote subscriptions, and stops before the target may
become `ACTIVE`; it never relies on the minute display refresh for handoff.

If the current-session 1m, 5m, and 30m ORB plans all reach terminal
`REJECTED`/`RISK_INVALID` states before any BUY identity exists, the Execution
Owner automatically returns the card to Buylist. The Buylist card shows one
durable memo containing the three rejection reasons. Rejected cards leave the
Buy Today price-refresh and WebSocket subscription sets. A later activation
is accepted normally, but the same current-session rejection evidence will
return it to Buylist again; missing or still-forming ORB data never causes an
automatic return.

Within the authorized live scope, subscription capacity favors
`EXECUTE_READY` entries first, then armed/waiting-breakout entries, then ORBs
that are still forming. Working orders and position protection always retain
higher priority than any new entry.

If Operator Control is **Locked**, no request row is created and the failed
click is not saved for later. Assign Operator Control and click the action
again. Locking Operator Control does **not** pause cards already in Buy Today,
working entries, position protection, reconciliation, or automatic exits.

During the regular session, full-plan publish and planning mutations from a
non-executor are blocked. Buy Today, Cancel Entry, partial/sell-all, and stop
changes are inserted into `operator_commands`. The Execution Owner validates
and applies each command once; the requesting device displays the shared,
executor-confirmed board on its next refresh.

## Switching execution

An execution switch is rejected unless the target publishes a fresh
`STANDBY_READY` generation with healthy MySQL, KIS reconciliation, realtime
market data, command consumer, order reconciliation, synchronized revisions,
and awake/sleep-safety state. Operator Control never changes implicitly during
an execution switch.

The selector resolves the target from the shared coordination database at
click time. With the default production profile, a stable runtime publishes
every 240 seconds and its identity/readiness row remains fresh for 300 seconds.
Therefore a continuously running target showing **7/7 — STANDBY_READY** should
be eligible for an explicit transfer throughout its normal heartbeat window.
`PC: On`, `DB: On`, and `Listener: On` are separate connectivity indicators;
none substitutes for that shared readiness row. If the selector says no
runtime is registered, verify that both processes use the same `COORD_DB_*`
store and that the target is running a current build. If it says the runtime
is registered but stale/ineligible, keep `main.py` running and inspect the Buy
Board readiness tooltip.

A `PENDING` operator command may safely follow a completed owner transfer,
because only the new owner can claim it. Once a command is `ACCEPTED`,
`EXECUTING`, `BROKER_SUBMITTED`, or `PARTIALLY_FILLED`, the transfer is paused
until the command reaches a terminal state. This prevents a command from
being stranded between executors.

`STANDBY_READY` means the process has completed the successor-readiness
checks and is waiting for the execution lease. It is not an execution error
and is no longer rendered as a red per-card restriction. The selected
Execution Owner must progress to `ACTIVE` before it can mutate or submit.

## If a shared coordination database goes offline

The outage policy distinguishes continuity from authority:

- New BUY submissions, Operator Control commands, plan changes, ownership
  transfers, and other ordinary mutations close immediately.
- An executor that was already `ACTIVE` retains its last successfully loaded
  cards. Only exact protective SELL work and the cancellation required before
  that SELL can be eligible, using the last verified lease/ownership proof and
  an fsynced local emergency journal.
- That emergency authority is deliberately short: 30 seconds by default
  (`EMERGENCY_LEASE_ALLOWANCE_SECONDS`). It cannot safely become an unlimited
  offline lease because a network partition is indistinguishable from another
  device still reaching MySQL.
- A cold-started or not-yet-active laptop has no offline execution authority.
  It waits closed for MySQL rather than guessing from the recovery snapshot.
- When the shared store returns, the emergency journal is folded into canonical state and
  a full fresh KIS reconciliation must succeed before ordinary execution
  reopens. A failed database read is never interpreted as an empty card list.

The dashboard's remote **Turn Off** action is blocked during the regular
session while the Buy Board engine and Live Trading are armed. A physical or
OS-level shutdown cannot be prevented by the app. For either trading device to
run while the other is fully powered off, move the canonical MySQL service to
an independent always-on host or managed service; dual local writable databases
are not a safe substitute.

## Device identity and restart safety

Each machine keeps its UUID in the local, gitignored
`data/device_role.json`, with the normal rolling `.bak` recovery file. It is
not a cloud-synchronized planning document and must not be copied between
machines. Automated tests redirect this path to a temporary directory so a
test identity cannot replace the real workstation UUID and strand a lease.

After a restart, confirm that the Execution Owner and the fresh `ACTIVE`
runtime row refer to the same device. Selecting PC or Laptop resolves the
fresh runtime identity, not a stopped historical row.

## Operator checklist

For an intraday Buy Today instruction:

1. Confirm the intended machine is **Execution Owner** and reports `ACTIVE`.
2. Confirm the device you are using is **Operator Control**.
3. Confirm the displayed live mode includes the symbol and permits the
   intended notional.
4. Activate the card and verify the latest command becomes `COMPLETED`, not
   merely `PENDING` or `REJECTED`.
5. Verify the card is in Buy Today and inspect its badge. `EXECUTE_READY`
   means the plan is ready; an actual BUY still requires a qualifying fresh
   quote, trigger clearance, current reconciliation/buying power, the shared
   live switch, and every final broker-boundary fence.

The global live-trading switch remains an emergency disable available from
either physical device. Broker submissions retain the existing lease-token
fence and re-read shared ownership at the broker boundary.
