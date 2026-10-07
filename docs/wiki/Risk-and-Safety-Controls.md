# Risk and Safety Controls

No single switch authorizes an order. The production mutation boundary
requires the applicable combination of:

- `TRADING_ENABLED` administrative permission and the canonical in-app/shared
  Live Trading control;
- `BUYBOARD_ENGINE_ENABLED` plus an allowed live-execution envelope;
- current execution-owner lease/token/epoch;
- durable `KANBAN` ownership matching `KANBAN_STRATEGY_INSTANCE_ID`;
- writable canonical execution state and emergency journal;
- fresh, complete, account-specific broker reconciliation;
- verified execution-grade quote/subscription/capacity health;
- mutation budget and request spacing;
- duplicate/open/ambiguous-order checks;
- capital availability/reservation;
- a fresh complete-fingerprint pre-trade risk decision for entries.

## Fail-closed invariants

- Invalid, blank, stale, or unavailable gates block mutation.
- Exits are not prevented by entry sizing approval.
- Broker acceptance never means fill.
- Entry Pending and Closed are reconciliation-owned.
- Laptop mirror data is never promoted into canonical market data.
- Handoff does not auto-arm Live Trading.
- Unknown submission/persistence outcomes are not automatically retried.
- Optimistic browser state and change notifications are never execution
  evidence; canonical response and broker reconciliation remain authoritative.
- Automatic screen refresh does not convert `STARTING` or `STANDBY_READY` into
  execution authority and does not bypass an unwritable store or `LEGACY`
  symbol ownership.
- External orders remain unowned until deliberately adopted.
- A passive BUY is submitted only while both fresh last trade and best ask are
  above its exact limit; reaching the limit before submission blocks ordinary
  entry instead of converting it to a marketable order.
- ORB replacement requires a later strictly higher score, zero fills, unchanged
  quantity, authoritative old-order cancellation, and post-cancel revalidation.
- The stop always uses the ORB low belonging to the generation that actually
  filled.

## Opening liquidity

New entries and replacements require at least the shared minimum average
opening shares/minute (default 200). Count completed minutes from 09:30 New
York, cap at 30, and include empty minutes in the denominator. Missing or
invalid evidence blocks buys; protective exits are exempt. Later volume cannot
rescue an illiquid first 30 minutes. See Current Order Logic for exact rules.

## Strategy/risk behavior

Do not change scanner rules, ORB trigger logic, position sizing, risk caps,
partial-exit timing, EMA exit rules, or stop policy as a refactor. Such changes
require explicit strategy approval, characterization tests, and updated
rulebooks.

## Controlled-live posture

Controlled live restricts entry to exact active canonical Trade Cards and a
maximum per-entry notional. Symbols are database state, never `.env` values,
and the local JSON recovery snapshot is not broker authority. It is an
additional envelope, not a bypass. Follow the supervised pilot runbook and KIS
evidence checklist; default remains disabled.

See [Current Order Logic](https://github.com/cafe-auvers/quant_app/blob/master/docs/current_order_logic.md)
for the exact entry and replacement invariants.

Current release reference: [Opening liquidity and mobile workflow (2026-10-08)](../opening_liquidity_mobile_release_2026-10-08.md). Dated reports and archived plans retain their original scope.
