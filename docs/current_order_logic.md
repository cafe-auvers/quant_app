# Current Order Logic

This is the canonical description of the implemented automatic entry-order
logic. It covers the active Buy Board/Kanban path for KIS overseas U.S. equity
orders. If another document summarizes entry behavior differently, this page
and the executable code/tests take precedence.

The strategy is a **confirmed-breakout, passive-pullback entry**:

```text
completed ORB + post-range breakout confirmed
                    |
                    v
submit a BUY limit below the current market immediately
                    |
                    v
Entry Pending while the order rests at the execution price
                    |
                    v
broker reports a partial/full fill after a pullback
                    |
                    v
Open Position, protected from the filled generation's ORB low
```

The breakout is permission to place the passive order. It is not a fill. A
touch of the limit also does not guarantee a fill because queue priority and
available liquidity remain broker/exchange concerns.

## 1. Planning and monitoring scope

- Watchlist and Buylist are passive stages. They do not authorize automatic
  entry, create execution subscriptions, or call KIS.
- An explicitly published/activated `BUY_TODAY` card authorizes monitoring for
  its exact production account and symbol for its scheduled NYSE session.
- The runtime evaluates finalized 1-minute, 5-minute, and 30-minute opening
  ranges independently. Each range starts at 09:30 New York time and uses bars
  in `[09:30, range end)`; it is unavailable until the range is complete.
- Candidate bars must identify the current New York session. A current refresh
  timestamp alone cannot make old bars executable.
- Automatic selection considers every complete, risk-valid candidate. An
  explicit pre-market manual window lock restricts execution to that exact
  window.
- Queue sizing provenance must match the card's canonical production account
  and breakout price.

## 2. Candidate price rules

For one finalized ORB candidate:

```python
floor_price = max(breakout_price, orb_low)
breakout_trigger = max(breakout_price, orb_high)
```

The configured passive execution price must satisfy:

```python
floor_price < execution_price <= orb_high
```

Therefore:

- `orb_high` must be strictly above both the structural breakout price and the
  ORB low; otherwise no passive execution zone exists.
- The automatic execution price defaults to that candidate's `orb_high`.
- A manual execution price may be below `orb_high`, but it must remain strictly
  above both `breakout_price` and `orb_low`.
- A manual price is never silently changed.
- U.S. equity ticks are `$0.0001` below `$1` and `$0.01` at or above `$1`.
- ORB high, ORB low, score, execution price, range-close time, and confirmation
  time are frozen for each submitted order generation.

The persisted `Buffer %` remains planning metadata and is retained across
devices, but the finalized passive-order zone and live confirmation above use
the raw canonical `breakout_price`. The active order trigger is **not**
`max(orb_high, breakout_price * (1 + buffer_pct))`.

## 3. Breakout confirmation

The opening range must close before it can qualify. Afterward, a fresh,
execution-grade KIS trade must be strictly greater than:

```python
max(breakout_price, orb_high)
```

The confirmation is timestamped and latched for that candidate. It is not
inferred from stale data, an incomplete range, a relationship between two
stored prices, or a planning-only/yfinance quote.

In automatic mode, if several candidates have confirmed breakouts before the
initial order is submitted, the runtime chooses the highest ORB score. An
equal-score tie favors the earlier timeframe. A manual window lock stays exact.

## 4. Initial order submission

After confirmation, the runtime submits immediately only if all of the
following still hold:

- the card is `BUY_TODAY` and its candidate is complete for the current
  session;
- the regular NYSE session is open;
- the KIS trade and quote streams are connected, subscribed, execution-fresh,
  and eligible;
- both `last_trade > execution_price` and `best_ask > execution_price`;
- quantity, account, exchange, buying power, capital reservation, portfolio
  risk, and controlled-live limits pass;
- canonical MySQL state, reconciliation, ownership, lease, live controls,
  release identity, mutation budgets, alerts, and all other broker-boundary
  gates pass; and
- no existing or ambiguous entry identity already blocks the symbol.

The broker request is a regular-session BUY limit at the **exact configured
execution price**. It is never changed to the current trade, ask, breakout
price, or another timeframe's ORB high.

If the last trade or ask is already at or below the limit, the ordinary passive
order is not submitted. The card remains monitored with
`EXECUTION_LEVEL_ALREADY_REACHED`; the system does not convert the request to a
market, stop, stop-limit, or marketable-limit order.

Example:

```text
breakout price       97.00
1-minute ORB low     95.00
1-minute ORB high   100.00
execution price     100.00
last trade          105.00
best ask            105.01
```

The breakout has been confirmed and the order starts passively below the
market, so the system submits the BUY limit at `100.00`. It then waits for KIS
to report execution; it does not wait to submit until the market returns to
`100.00`.

## 5. Board and broker states

Submission and execution are different state changes:

```text
Buy Today
  -> durable submit accepted, discovered, duplicate, or ambiguous
Entry Pending
  -> broker-confirmed partial or full fill
Open Position
```

- Broker acceptance means only that KIS accepted the request. It does not add
  shares or create a position locally.
- A newly submitted passive entry has no 15-second automatic cancellation or
  repricing deadline. It may remain working until a fill, explicit cancel,
  safe replacement, broker expiry/rejection, or end-of-day cleanup.
- Every heartbeat reconciles tracked orders. Ambiguous submission state remains
  fenced in Entry Pending and is never blindly submitted again.
- Any confirmed fill moves the visible card to Open Position and protects only
  the quantity actually filled. A remaining entry quantity may continue only
  under the existing guarded completion policy.
- A user/EOD cancellation is two phase: request cancel, then wait for broker
  confirmation. Zero fill returns to Buylist; any fill becomes Open Position;
  uncertainty remains Entry Pending.

### Broker rejection behavior

- A definitive ordinary broker rejection proves that no order is working. A
  zero-position card is cleaned and returned to Buylist with a memo.
- KIS `APBK0656` is treated as a verified exchange-routing/configuration
  failure, not a strategy rejection. The card remains in Buy Today, clears the
  rejected order identity, enters retry cooldown, and may retry with a new
  stable identity after route verification.
- An ambiguous timeout, transport loss, or post-broker persistence failure is
  not a rejection. It remains unresolved and blocks duplicate submission until
  reconciliation proves the result.

## 6. Automatic higher-score ORB replacement

A completely unfilled working order may upgrade to a later timeframe:

```text
1m -> 5m
1m -> 30m
5m -> 30m
```

Automatic downgrades are not allowed. A later candidate qualifies only when:

- its range is closed and current-session data is complete;
- its post-range breakout is confirmed;
- its score version matches the active generation;
- its score is strictly higher at the established 0.1 score precision;
- its execution zone and tick are valid;
- fresh last trade and best ask are both above its execution price;
- the session, risk, capital, exposure, ownership, database, lease, live, and
  mutation-budget gates pass;
- the active broker order is exactly `WORKING`, has zero fills, and its
  remaining quantity equals its original quantity; and
- replacement quantity equals the original submitted quantity.

Equal or lower scores keep the current order. A compatible manual execution
price carries forward unchanged; an incompatible manual price rejects the
upgrade without cancelling the existing order.

### Strict cancel-then-replace sequence

```text
qualify and fully prevalidate later candidate
  -> persist replacement intent and stable identities
  -> request cancellation of the old order
  -> wait for authoritative KIS cancellation with zero fills
  -> revalidate quote, session, risk, and capital
  -> submit exactly one linked replacement generation
  -> promote the later ORB fields only after replacement submission succeeds
```

The replacement is never submitted before the old cancellation is confirmed.
The old and new orders keep separate client/broker identities and immutable ORB
specifications under one parent entry intent. The capital transition remains
reserved so cancellation does not expose funds to an unrelated entry.

The replacement uses the later candidate's timeframe, ORB high, ORB low,
score, breakout trigger, execution price, and a new generation number, while
keeping the same quantity. A later replacement can itself upgrade again if a
still-later candidate strictly beats its active score.

### Replacement edge cases

- **Fill before or during cancellation:** abort replacement, reconcile the old
  fill, and protect it using the old generation's ORB low.
- **Cancellation rejected:** do not submit the replacement; the old order
  remains authoritative.
- **Cancellation uncertain:** enter `CANCEL_PENDING`, alert, and reconcile. Do
  not assume cancellation and do not submit a second BUY.
- **Post-cancel revalidation fails:** record
  `REPLACEMENT_ABORTED_AFTER_CANCEL`; submit neither an unsafe replacement nor
  an automatic recreation of the old order.
- **Replacement submission becomes ambiguous:** preserve its new stable
  identity as `REPLACEMENT_SUBMISSION_UNRESOLVED` and reconcile; never blindly
  POST again.
- **Restart mid-replacement:** durable commands and generation identities are
  recovered. The resume path can submit only when the old order is
  authoritatively cancelled with zero fills, and it never issues a second
  cancel.

## 7. Fill and stop behavior

The stop reference comes from the broker-order generation that actually fills:

```text
1m order fills        -> 1m ORB low
5m replacement fills -> 5m ORB low
30m replacement fills -> 30m ORB low
```

For partial fills, only the filled quantity is protected. Incremental fills
update the protected quantity and actual average price. An old-generation fill
during replacement cannot inherit the proposed later-generation stop.

## 8. End-of-day behavior

- Buy Today with no durable order returns to Buylist after its scheduled
  regular session. Its final reason is retained for the date-based Daily
  Summary; an all-timeframe ORB rejection also retains its diagnostic snapshot.
  Transient execution fields are cleared.
- A prior-session Buy Today card is retired on the next startup even if the app
  missed the closing bell. Weekend/holiday activations are repaired to the
  next NYSE trading day.
- If all 1m, 5m, and 30m plans are conclusively invalid and no entry/order/
  reservation/position evidence exists, the card safely returns to Buylist as
  ORB Rejected.
- Entry Pending requests cancellation during the EOD safety window. It moves
  only after broker confirmation: zero fill to Buylist, any fill to Open
  Position, uncertainty stays Entry Pending.
- Open Position remains open. EOD stops trying to complete any unfilled entry
  remainder and keeps the filled position protected.

## 9. What must never happen

- Never infer a fill from price crossing or broker acceptance.
- Never use yfinance or stale/cached display data to authorize a live order.
- Never submit at the current price merely because price is above ORB high.
- Never submit a passive entry after its limit has already been reached.
- Never place old and replacement entry orders at the same time.
- Never change quantity during an ORB timeframe upgrade.
- Never release or reuse an ambiguous order identity as though it were absent.
- Never move a working/uncertain Entry Pending card to Buylist on local
  assumption alone.
- Never let `.env` or `.env.pc` contain live symbol lists; active symbols come
  from canonical Trade Cards and their dedicated runtime data.

## 10. Implementation and verification map

| Concern | Primary implementation |
|---|---|
| Passive price zone and score comparison | `src/core/orb_entry_logic.py` |
| Current-session candidate construction | `src/core/execution_queue.py` |
| Breakout latching and replacement qualification | `src/services/trade_card_orb_bridge.py` |
| Initial submission, reconciliation, and replacement state | `src/services/trading_engine.py` |
| Risk/capital composition and restart resume | `src/services/buyboard_runtime.py` |
| Durable guarded cancel-then-replace | `src/services/execution_command_gateway.py` |
| EOD reset and two-phase cancellation | `src/services/eod_trading_service.py` |
| Candidate and breakout selection | `tests/test_passive_pullback_orb.py`, `tests/test_execution_queue.py`, `tests/test_trade_card_orb_bridge.py` |
| Submission, replacement, and recovery | `tests/test_trading_engine.py`, `tests/test_execution_command_gateway.py`, `tests/test_entry_attempt_manager.py`, `tests/test_buyboard_runtime_worker.py` |
| End-of-day lifecycle | `tests/test_eod_trading_service.py` |

Related operator documentation:

- [Buy Board ORB Planning](orb_buyboard_planning.md)
- [Kanban Architecture](kanban_architecture.md)
- [Execution Owner and Operator Control](execution_operator_control.md)
- [Controlled-Live Pilot](controlled_live_pilot_runbook.md)
- [Wiki Order Lifecycle](wiki/Order-Lifecycle.md)
