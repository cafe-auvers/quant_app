# Stop and Sell All session handling

The November 11 incident showed two separate problems: premarket quotes did not
trigger app stops, and account reconciliation repeatedly cancelled healthy
Sell All orders, including broker-held next-opening reservations.

Protective stops, automatic NAV half-sales and manual exits now include KIS's
default US premarket and after-hours window: 04:00 New York time through 07:00
Korea time the following morning, on US trading days. This respects DST and
holidays. The optional later after-hours account extension and overnight/daytime
trading are excluded. ORB entry gates and regular-session bar calculations keep
their regular-session rules.

Sources checked November 11:

- [KIS trading hours](https://securities.koreainvestment.com/main/bond/research/_static/TF03ca050001.jsp)
- [KIS US order sample](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/order/order.py)

Exits during supported sessions use the existing US limit endpoint (TTTT1006U,
ORD_DVSN 00). Immediate exits require a fresh KIS bid or trade during extended
hours; an old holding valuation cannot price them. A user-priced partial exit
keeps its exact price floor. Outside the supported window, manual unpriced exits
retain the broker-held next-opening reservation path.

Reconciliation preserves one exact owned full exit covering the current holding;
the trading engine controls regular limit TTL/repricing. A next-opening
reservation is cancelled only when an executable session opens or an explicit
upgrade/withdrawal requires it. Replacement waits for confirmed cancellation and
fresh holdings. Unknown identities, conflicting external orders and unresolved
cancellations continue to fence duplicate sells.

App stops require Live Trading ON, the active PC and a functioning live feed.
The 2% NAV amount sets the initial manual stop level, not a guaranteed maximum
loss. Limit orders, gaps, liquidity and fees can change realized P&L.
