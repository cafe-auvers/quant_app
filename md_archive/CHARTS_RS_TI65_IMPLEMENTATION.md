# Charts Tab: Relative Strength, TI65, and Spike Markers

## New Edit

Implement the PineScript indicator below in the app's **Charts** tab. The chart should display relative strength versus SPY, a smoothed RS baseline, RS score values, TI65 background states, 9M volume markers, +/-4% spike markers, and RS crossover markers.

To keep chart rendering instant, create a newly calculated database table that stores these indicator values ahead of time. The Charts tab should read from the calculated table instead of recomputing the full indicator series every time a symbol is plotted.

## Calculated Database Requirement

Create a calculated indicator table keyed by `symbol` and `date`, derived from existing `price_history` rows plus SPY reference data.

Suggested table name: `chart_indicators`

Suggested columns:

- `symbol`, `date`
- `relative_strength`: `close / spy_close`
- `rs_sma_50`: 50-period SMA of relative strength
- `rs_score_current`: percent rank of relative strength over 252 bars
- `rs_score_yesterday`: prior-day RS percent rank
- `rs_score_week`: RS percent rank 5 bars ago
- `rs_score_month`: RS percent rank 21 bars ago
- `pct_change_today`
- `avg_7`, `avg_65`, `ti65`
- `is_ti65_bullish`: `ti65 >= 1.05`
- `is_ti65_bearish`: `ti65 <= 0.95`
- `is_9m_volume`: `volume >= 9000000`
- `is_plus_4pct_change`: daily change `>= 4.0`
- `is_minus_4pct_change`: daily change `<= -4.0`
- `is_rs_cross_up`: relative strength crosses above RS SMA
- `updated_at`

## Charts Tab Behavior

When a user plots a symbol, load OHLCV data and matching `chart_indicators` rows. Render the RS pane using the precomputed fields:

- RS line green when above `rs_sma_50`, red when below.
- SMA baseline in black.
- Filled RS/SMA area colored green or red.
- TI65 bullish/bearish background bands.
- Optional 9M volume marker at the bottom of the pane.
- Filled circles on RS line for +/-4% price moves.
- Triangle marker when RS crosses above the SMA.
- Summary table with current, yesterday, 1-week, and 1-month RS scores.

## Source PineScript

```pinescript
//@version=6
indicator("Relative Strength vs SPY + TI65 + 4% Spike Markers", overlay=false, precision=4, max_bars_back=500)

// =====================
// INPUTS
// =====================
comparativo       = input.symbol("AMEX:SPY", title="Reference Symbol", group="Relative Strength")
periodo_suavizado = input.int(50, title="RS SMA Period", minval=1, group="Relative Strength")
lookback_rs       = input.int(252, title="RS Score Lookback", minval=1, group="Relative Strength")

showTI65          = input.bool(true, "Show TI65 Background", group="Trend Intensity")
show4Pct          = input.bool(true, "Show +/-4% Spike Markers", group="Trend Intensity")
show9MVol         = input.bool(false, "Show 9M Volume Bars", group="Trend Intensity")

ti65BullColor     = input.color(color.new(color.green, 70), "TI65 Bullish Color", group="Trend Intensity")
ti65BearColor     = input.color(color.new(color.red, 70), "TI65 Bearish Color", group="Trend Intensity")
plus4Color        = input.color(color.green, "+4% Up Marker Color", group="Trend Intensity")
minus4Color       = input.color(color.red, "-4% Down Marker Color", group="Trend Intensity")
vol9MColor        = input.color(color.black, "9M Volume Bar Color", group="Trend Intensity")

// =====================
// RELATIVE STRENGTH vs SPY
// =====================
precio_ref      = request.security(comparativo, timeframe.period, close)
fuerza_relativa = precio_ref != 0 ? close / precio_ref : na
sma_fr          = ta.sma(fuerza_relativa, periodo_suavizado)

// =====================
// RS SCORE (0-100)
// =====================
rs_score(val) =>
    ta.percentrank(val, lookback_rs)

score_actual = rs_score(fuerza_relativa)
score_ayer   = rs_score(fuerza_relativa[1])
score_semana = rs_score(fuerza_relativa[5])
score_mes    = rs_score(fuerza_relativa[21])

// =====================
// TREND INTENSITY (TI65)
// =====================
pctChangeToday    = close[1] != 0 ? ((close - close[1]) / close[1]) * 100.0 : na
avg7              = ta.sma(close, 7)
avg65             = ta.sma(close, 65)
ti65              = avg65 != 0 ? avg7 / avg65 : na
is9mVol           = volume >= 9000000
isPlus4PctChange  = not na(pctChangeToday) and pctChangeToday >= 4.0
isMinus4PctChange = not na(pctChangeToday) and pctChangeToday <= -4.0

bgcolor(showTI65 and ti65 >= 1.05 ? ti65BullColor : na, title="TI65 Bullish")
bgcolor(showTI65 and ti65 <= 0.95 ? ti65BearColor : na, title="TI65 Bearish")

// Optional 9M volume bars in same pane
plotshape(show9MVol and is9mVol, title="9M Volume", style=shape.square, location=location.bottom, color=vol9MColor, size=size.tiny)

// =====================
// RS PLOTS
// =====================
color_nube = fuerza_relativa > sma_fr ? color.new(color.green, 70) : color.new(color.red, 70)
plot_fr    = plot(fuerza_relativa, title="Relative Strength", color=(fuerza_relativa > sma_fr ? color.green : color.red), linewidth=2)
plot_sma   = plot(sma_fr, title="Base SMA", color=color.black, linewidth=1)
fill(plot_fr, plot_sma, color_nube, title="RS Fill")

// =====================
// +/-4% FILLED CIRCLE MARKERS ON RS LINE
// =====================
plot(show4Pct and isPlus4PctChange ? fuerza_relativa : na, title="+4% Up Spike", color=plus4Color, style=plot.style_circles, linewidth=3)
plot(show4Pct and isMinus4PctChange ? fuerza_relativa : na, title="-4% Down Spike", color=minus4Color, style=plot.style_circles, linewidth=3)

// =====================
// CROSSOVER TRIANGLES
// =====================
plotshape(ta.crossover(fuerza_relativa, sma_fr), title="RS Cross Up", style=shape.triangleup, location=location.bottom, color=color.blue, size=size.tiny)

// =====================
// TABLE
// =====================
var table rsTable = table.new(position.top_left, 2, 5, bgcolor=color.new(color.black, 80), border_width=1, border_color=color.gray)

if barstate.islast
    table.cell(rsTable, 0, 0, "Period", text_color=color.white, text_size=size.small)
    table.cell(rsTable, 1, 0, "Score (0-100)", text_color=color.white, text_size=size.small)

    color_score = score_actual > 70 ? color.green : score_actual < 30 ? color.red : color.orange

    table.cell(rsTable, 0, 1, "Current", text_color=color.white)
    table.cell(rsTable, 1, 1, str.tostring(math.round(score_actual)), bgcolor=color_score, text_color=color.white)

    table.cell(rsTable, 0, 2, "Yesterday", text_color=color.white)
    table.cell(rsTable, 1, 2, str.tostring(math.round(score_ayer)), text_color=color.white)

    table.cell(rsTable, 0, 3, "1 Week Ago", text_color=color.white)
    table.cell(rsTable, 1, 3, str.tostring(math.round(score_semana)), text_color=color.white)

    table.cell(rsTable, 0, 4, "1 Month Ago", text_color=color.white)
    table.cell(rsTable, 1, 4, str.tostring(math.round(score_mes)), text_color=color.white)
```

## Handover Summary

### Current Task State

The Charts tab has been extended from a static local SVG chart into an interactive charting workspace. It now supports cached RS/TI65 indicators, chart settings, saved target prices, saved drawings, symbol dropdown filtering, market-data freshness status, and constrained chart navigation.

### Files Changed

- `src/ui/main_window.py`
  - Charts tab UI controls, symbol dropdown, settings checkboxes, target/drawing tools, QWebChannel bridge, chart SVG/JavaScript rendering, pan/zoom/navigation, market-data freshness text.
- `src/utils/db_loader.py`
  - `chart_indicators` table, RS/TI65 calculation, indicator refresh/load helpers, latest cache date helper.
- `tests/test_core_behaviour.py`
  - Regression tests for indicators, chart HTML, target price, drawings, erase, navigation, dropdown filtering, data freshness, and DB helpers.
- `main.py`
  - Qt MIME warning filter and delayed `MainWindow` import.
- `AGENTS.md`
  - Contributor guide for this repository.
- `CHARTS_RS_TI65_IMPLEMENTATION.md`
  - Implementation notes and this handover.

Runtime/local state files used by the app:

- `data/watchlist.json`: stores chart target prices via `target_price`.
- `data/chart_drawings.json`: stores saved chart drawings by symbol.
- `data/tab_options.json`: controls which chart tabs are visible. The legacy `Charts` and `Intraday Charts` tabs may be hidden, but they are intentionally retained as safe fallbacks.
- MySQL `price_history`: stores OHLCV cache.
- MySQL `chart_indicators`: stores precomputed RS/TI65 chart indicator rows.

### Chart Tab Migration And Fallback Policy

- `TradingView Chart` is the primary chart rendering surface for daily, hourly, and 5-minute chart workflows.
- Legacy `Charts` and `Intraday Charts` are kept as safe fallbacks, even when hidden through `data/tab_options.json`.
- Any chart behavior that affects shared state should be updated in both the TradingView renderer and the legacy chart paths while the legacy tabs remain in the codebase. This includes target prices, saved drawings, symbol selection, timeframe/data loading, indicator visibility, and cache refresh assumptions.
- Do not treat hidden legacy tabs as deleted code. If chart state formats change, update the fallback renderers and related tests at the same time so they remain usable during TradingView/CDN/PyQtWebEngine regressions.

### Implemented Behavior

- MySQL refresh now fetches `SPY` plus the stock universe and calculates `chart_indicators`.
- Dashboard summary shows whether cached market data is up to date using a 7:00 AM KST cutoff and weekday rollback.
- Charts tab symbol input is an editable dropdown with prefix filtering.
- Chart settings can show/hide volume, RS vs SPY, EMA lines, ADR, and 1M/3M/6M growth metrics.
- Target price can be set with `Set Target Price (T)`, dragged, deleted with `X`, and persists to the watchlist.
- Trendlines can be drawn with `Draw Line (D)`, erased one by one with `Erase Drawing (E)`, or cleared with `Erase All`.
- Drawings are anchored by date and price, so they move with new historical bars.
- Drawings can extend up to 5 future weekdays beyond the last real candle.
- Chart can pan left/right, zoom with the mouse wheel, use arrow keys, and reset with `Full View (A)`.
- Bottom range navigator shows the full time range and current visible window, with draggable handles.

### Current Errors

- No current failing tests.
- No current compile errors.
- The app still depends on `PyQtWebEngine` for the interactive chart features. Text fallback cannot provide SVG/JavaScript interactions.

### Test Results

Last verified commands:

```powershell
pytest -q
# 24 passed in 12.59s

python -m compileall main.py src tests -q
# passed
```

### Assumptions

- Daily market-data refresh should normally be run at or after 7:00 AM KST.
- U.S. market holidays are not modeled in the freshness check; weekends are rolled back to Friday.
- `SPY` is the RS reference symbol.
- Chart indicator calculations rely on cached `price_history` rows and matching SPY dates.
- A MySQL cache refresh is required to populate `chart_indicators` after schema/code changes.

### Next Steps

- Run **Refresh MySQL Cache** in the app to populate `SPY`, one year of OHLCV history, and `chart_indicators`.
- Manually verify Charts tab interactions in `QWebEngineView`: pan, wheel zoom, bottom navigator, target set/drag/delete, draw/erase line, and future drawing up to 5 weekdays.
- Consider moving more chart rendering into persistent JavaScript if further smoothness is needed.
- Consider adding U.S. holiday awareness to the market-data freshness check.
