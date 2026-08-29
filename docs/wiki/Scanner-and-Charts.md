# Scanner and Charts

## Scanner

Scanner setups are stored in `data/scanner_setups.json`. The scanner operates
on cached metrics when available and exposes results through the shared sidebar.
Universe/data acquisition and scans use background workers; result presentation
remains on the UI thread.

Rules and labels come from `src/core/scanner.py` and
`src/ui/filter_catalog.py`. Changing a threshold is a trading-rule change and
requires explicit review and tests.

## Charts

The TradingView-style experience uses bundled Lightweight Charts assets and
local HTML. Supported views include daily, hourly, 5-minute, and resampled
intraday data, with persisted drawings, breakout/position lines, volume/EMA,
RS/TI65, growth, earnings, profile, and market-alignment overlays.

Chart fundamentals are optional enrichment: missing profile or earnings data
must never suppress valid price candles. Provider refreshes are bounded and
generation-fenced so stale-symbol responses cannot overwrite the active chart.

The split 1D and 1H TradingView views share one logical line drawing: creating,
editing, or deleting it in either pane updates both panes. The stored 1H
timestamp is preserved. When an hourly endpoint falls after the last market
session or on a weekend, the 1D picture snaps that endpoint to the next
available daily-axis bar so the line remains visible.

The large 0-100 badge is Leadership only: 60% market-relative rank and 40%
industry-peer rank. It is not the chart's raw relative-to-SPY percentage, not
Market Context, and not a buy score. `CONTEXT: UNKNOWN` means required context
data is missing even if Leadership is high. Open **Details** and check the
snapshot date, both rank inputs, peer count/basis, and context components; see
[Leadership and Market Context](https://github.com/cafe-auvers/quant_app/blob/master/docs/market_alignment.md).

## ORB planning

`breakout_price` is the daily structural level. The active broker path requires
a completed current-session ORB and a fresh KIS trade strictly above:

```text
max(orb_high, breakout_price)
```

That confirmation submits a passive limit at ORB high by default; it does not
mean fill. A manual execution price is valid only when
`max(breakout_price, orb_low) < execution_price <= orb_high`.

The 24-case comparison is read-only. The optimized pre-market selector can
publish one plan when authority and timing rules permit; regular-session
published plans remain immutable.

See [Current Order Logic](https://github.com/cafe-auvers/quant_app/blob/master/docs/current_order_logic.md)
for order submission and higher-score ORB replacement.
