# Quant App: the plain-language tour

If you know nothing about trading software, start here. The
[large-picture HTML tour](project_tour.html) presents the same ideas visually.

## The whole project in one sentence

Quant App helps one person find U.S. stocks, plan a trade, and—only when many
independent safety checks agree—ask KIS to place an order.

```text
market data -> scanner -> chart -> Watchlist -> Buylist -> Buy Today
                                                        |
                                             safety gates all pass
                                                        |
                                            KIS order -> reconciliation
```

An accepted order is not automatically a fill. The app checks KIS again and
uses broker evidence to decide what actually happened.

## Why there are two computers

- The **PC** normally stores and refreshes the large market-history database.
- The **laptop** keeps a pull-only market-data safety mirror.
- Both connect to one small shared coordination database for plans, controls,
  cards, commands, orders, and runtime readiness.
- Exactly one running process is the **Execution Owner**. Only it may cross the
  broker boundary.
- **Operator Control** is the device allowed to send the next human command.
  It can be different from the Execution Owner.

`PC: On`, `DB: On`, `Listener: On`, and `main.py: On` answer different health
questions. They do not assign execution ownership. A normal transfer requires
the target's fresh shared `STANDBY_READY` record and 7/7 readiness. The current
steady heartbeat is every 240 seconds and stays fresh for 300 seconds.

## What Live Trading means

There are two layers:

1. Each machine has a private `TRADING_ENABLED` lock in its `.env`. False means
   that machine is always locked off.
2. The shared database holds the durable Live Trading ON/OFF switch seen by
   both machines that are locally permitted.

Even when both layers allow trading, an order still needs the correct Execution
Owner, exact lease, fresh KIS reconciliation and quotes, a valid plan, available
capital, risk approval, mutation budget, and no ambiguous prior order.

So yes: if the laptop is Execution Owner, the PC cannot execute an order merely
because its screen says Live Trading is enabled.

## How an entry actually works

The current strategy confirms a breakout first, then places a passive order
below the market:

```text
1m/5m/30m opening range closes
  -> fresh KIS trade moves above both ORB high and breakout price
  -> submit BUY limit at the selected execution price (ORB high by default)
  -> card moves to Entry Pending
  -> wait for a broker-confirmed pullback fill
  -> card moves to Open Position and uses that order generation's ORB low
```

The breakout event submits the resting limit; it does not fill the order. New
passive entries are not cancelled after 15 seconds. If a later ORB closes,
confirms, and has a strictly higher score, a completely unfilled working order
can upgrade only by cancelling the old order, confirming zero fills, and then
submitting one linked replacement with the same quantity. Any fill or uncertain
cancel stops the replacement.

The formula and edge-case reference is
[Current Order Logic](current_order_logic.md).

## Three easily confused features

### Watchlist removal

Watchlist is a passive membership flag. A symbol may also have an independent
Buylist card, stop, order, or position evidence. Removing it from Watchlist
clears only that passive membership; it does not erase the other evidence.

### 1D and 1H drawings

The daily and hourly split panes in one app share one logical drawing. Edit or
delete it in either pane and both update. When a 1H endpoint lands on a weekend
or after the last session, the daily picture shows it on the next available
daily bar while preserving the original 1H timestamp.

Chart drawings are local files. This split-pane synchronization is not
laptop-to-PC drawing replication.

### Leadership score

The large 0-100 score on the chart is only:

```text
60% Market RS rank + 40% Industry Peer RS rank
```

It is not a buy score, not Market Context, and not the raw relative-to-SPY
percentage. Different windows and units mean a high Leadership rank can appear
beside a negative SPY-relative display. `CONTEXT: UNKNOWN` means required
market/segment/sector/industry data is missing; it does not become known merely
because Leadership is high.

Use **Details** to check the snapshot date, Market RS, Industry Peer RS, peer
count/basis, and context components. The formula is descriptive and has not yet
been validated as a profit forecast. `STRONG` means strong rank under the
formula—nothing more.

## Why the exact Git commit matters

`KIS_RUNTIME_COMMIT_SHA` is the full 40-character output of:

```powershell
git rev-parse HEAD
```

It must describe the clean checkout actually running on the device and match
the reviewed capability manifest and exact-head Gate-1 evidence. A new commit,
even documentation-only, creates a new SHA and makes an older exact-commit
approval inapplicable. Never commit `config/runtime.local.json` or capability
evidence containing private data.

## Before a live session

Confirm all of these, not just one green button:

- both devices run the same reviewed clean commit;
- the manifest, digest, runtime SHA, and Gate-1 evidence match that commit;
- the intended target is 7/7 `STANDBY_READY`, then becomes `ACTIVE` owner;
- Operator Control is assigned deliberately;
- the local lock and shared Live Trading switch show the intended state;
- symbol and notional are inside the controlled-live envelope;
- reconciliation, KIS feed, quotes, buying power, risk, and alerts are current;
- there is no ambiguous or unresolved order identity.

If one required check is missing, the correct outcome is no order.

## Where to go next

- [Execution Owner and Operator Control](execution_operator_control.md)
- [Current Order Logic](current_order_logic.md)
- [Leadership and Market Context](market_alignment.md)
- [Supervised Controlled-Live Pilot](controlled_live_pilot_runbook.md)
- [TiDB Cloud Coordination Store](tidb_coordination_store.md)
- [Database Architecture](database_architecture.md)
- [Main README](../README.md)
