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

## ORB planning

`breakout_price` is the daily structural level. Entry requires:

```text
max(orb_high, breakout_price * (1 + buffer_pct))
```

The 24-case comparison is read-only. The optimized pre-market selector can
publish one plan when authority and timing rules permit; regular-session
published plans remain immutable.
