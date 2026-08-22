# TradingView Fundamentals Upgrade — Implementation Record

> **Status:** Implemented. This file preserves the original feature brief and
> completion criteria. For maintained runtime behavior, see
> [PROJECT_ARCHITECTURE.md](../PROJECT_ARCHITECTURE.md), especially Charting,
> and [Leadership and Market Context](market_alignment.md). Do not treat future
> tense below as unfinished functionality without verifying the code and tests.

The original brief requested a production-quality **earnings-data subsystem,
earnings chart overlay, upcoming-earnings indicator, and stock profile
watermark** in:

```text
cafe-auvers/quant_app
```

The feature is for the existing **TradingView-style chart tab** built with TradingView Lightweight Charts.

Do not create a separate earnings dashboard, earnings table, stock-profile page, sector page, or industry page.

The final user experience must remain centered on the existing price chart.

---

# 1. OBJECTIVE

For the currently selected stock, the TradingView-style chart must show:

1. historical earnings events shown as TradingView-style `E` flags in a fixed event lane at the bottom of the price chart,
2. an O’Neil-style earnings trend line within the main price panel,
3. compact quarterly EPS growth labels such as `124/56`,
4. the next expected earnings date,
5. a warning when earnings are expected within the next 14 calendar days,
6. company name,
7. sector,
8. industry,
9. a large TC2000-inspired stock-information watermark in the middle of the price chart,
10. a persistent top-left earnings summary that carries the most recently reported earnings result forward until a newer report replaces it.

Example stock-information watermark:

```text
NVDA
NVIDIA Corporation
Technology Sector
Semiconductors
```

The watermark must have:

- large text,
- a translucent gray background,
- centered alignment,
- low enough opacity that candles remain visible,
- no effect on mouse, keyboard, crosshair, drawing, or chart navigation.

---

# 2. FIRST INSPECT THE CURRENT REPOSITORY

Before modifying code, inspect the current branch and verify the actual implementation.

Known likely integration points include:

```text
src/ui/charts/controller_data_flow.py
src/ui/charts/controller_layout.py
src/ui/charts/controller_plotting.py
src/ui/charts/render_lightweight.py
src/ui/charts/render_local.py
src/ui/charts/models.py
src/ui/charts/data_service.py
src/ui/charts/render_metrics.py

src/infrastructure/database/schema.py
src/infrastructure/database/repositories/
src/infrastructure/database/mirror_engine.py
src/infrastructure/database/mirror_copy.py
src/infrastructure/database/mirror_reconciliation.py
src/infrastructure/database/__init__.py

src/ui/workers.py
src/utils/data_loader.py
requirements.txt
tests/
```

Verify all assumptions before coding.

In particular, confirm:

- how the TradingView tab loads chart data;
- how `_render_tradingview_chart_view()` is called;
- how `_generate_tradingview_lightweight_chart_html()` receives data;
- the installed Lightweight Charts API version;
- how series markers are currently rendered;
- how future whitespace is generated;
- how chart refresh keys work;
- how asynchronous workers prevent stale-symbol updates;
- how SQLAlchemy Core tables are defined;
- how MySQL and SQLite are supported;
- how the PC-to-laptop local mirror is configured;
- how table reconciliation and mirror freshness are tested;
- which `yfinance` APIs work under the repository’s actual version constraint.

The current dependency constraint appears to be:

```text
yfinance>=0.2.44,<0.3
```

Do not depend on a newer yfinance API without first confirming compatibility.

Do not upgrade yfinance to a new major version merely for this feature unless there is a demonstrated blocking incompatibility.

Do not place substantial new logic in:

```text
src/ui/main_window.py
```

---

# 3. NON-NEGOTIABLE DATA DESIGN

Use physically separate database tables.

At minimum, create:

```text
stock_profiles
earnings_events
```

A synchronization/manifests table may also be added if required:

```text
fundamental_sync_state
```

or separate equivalents such as:

```text
stock_profile_sync_state
earnings_sync_state
```

The exact sync-state structure may follow existing repository conventions.

## Required separation

```text
price_history
    └── contains OHLCV only

stock_profiles
    └── contains company, sector, and industry metadata

earnings_events
    └── contains quarterly and upcoming earnings information
```

Do not add company name, sector, or industry columns to:

```text
price_history
earnings_events
chart_indicators
scanner_metrics
```

Do not add EPS or earnings-event columns to:

```text
stock_profiles
price_history
```

The separate tables must be joined or aggregated through the normalized application symbol.

---

# 4. SYMBOL IDENTITY

Use the application’s canonical symbol as the database join key.

Examples:

```text
NVDA
AAPL
BRK.B
```

Do not use the TradingView-qualified display symbol as the database key:

```text
NASDAQ:NVDA
NYSE:BRK.B
```

Do not assume the app symbol, TradingView symbol, Yahoo symbol, and KIS symbol are always identical.

Where necessary, retain:

```text
symbol
provider_symbol
```

For example, a Yahoo provider symbol may require:

```text
BRK-B
```

while the application’s canonical symbol remains:

```text
BRK.B
```

Use the existing symbol-normalization and provider-symbol conversion infrastructure where possible.

The following tables must join logically through the same canonical symbol:

```text
price_history.symbol
chart_indicators.symbol
stock_profiles.symbol
earnings_events.symbol
```

---

# 5. STOCK PROFILE DATABASE TABLE

Create a dedicated stock profile table, following the project’s SQLAlchemy Core conventions.

Conceptually:

```text
stock_profiles
--------------
symbol                     primary key
provider_symbol

company_name
short_name

quote_type
exchange
market
currency
country

sector_name
sector_key

industry_name
industry_key

category
fund_family

profile_status
source

last_checked_at
last_successful_sync_at
created_at
updated_at
```

Modify field lengths and exact names to match repository conventions.

## Required fields

At minimum, persist:

```text
symbol
company_name
sector_name
industry_name
source
last_checked_at
updated_at
```

Also store provider keys when available:

```text
sector_key
industry_key
```

These keys will be useful later for:

- sector-relative-strength analysis,
- industry-relative-strength analysis,
- scanner filters,
- group ranking,
- group momentum studies.

## Profile status

Use an explicit status or equivalent approach so that a symbol with no profile data is not downloaded repeatedly on every chart navigation.

Conceptually:

```text
OK
PARTIAL
UNAVAILABLE
```

A partial result must still be persisted.

For example:

```text
company name available
sector unavailable
industry unavailable
```

must not be treated as a total failure.

---

# 6. EARNINGS DATABASE TABLE

Create a dedicated earnings-events table.

Conceptually:

```text
earnings_events
---------------
symbol
event_key

report_date
report_datetime_utc
fiscal_period_end

event_status
report_timing
is_date_estimated

reported_eps
estimated_eps
statement_diluted_eps
statement_basic_eps
eps_basis

eps_surprise
eps_surprise_pct

revenue
estimated_revenue

eps_yoy_growth_pct
previous_eps_yoy_growth_pct
eps_growth_status
previous_eps_growth_status

revenue_yoy_growth_pct
previous_revenue_yoy_growth_pct

ttm_eps

source
source_updated_at

created_at
updated_at
```

The exact schema may be adjusted according to existing DB conventions.

## Event statuses

Use normalized values equivalent to:

```text
REPORTED
EXPECTED
```

## Report timing

Use normalized values equivalent to:

```text
BMO
AMC
UNKNOWN
```

## Stable event identity

Do not use mutable future `report_date` as the sole primary key.

Expected earnings dates can change.

Use a stable event identity, for example:

```text
symbol + event_key
```

For reported quarters, derive the key primarily from the fiscal period when available.

Conceptually:

```text
FPE:2026-06-30
```

For the current expected event when no fiscal period is reliably available, a stable key such as:

```text
NEXT_EXPECTED
```

may be used and updated in place.

Choose a deterministic scheme and document it.

## Indexes

Add appropriate indexes for:

```text
earnings_events(symbol, report_date)
stock_profiles(sector_key)
stock_profiles(industry_key)
```

Follow current MySQL and SQLite compatibility requirements.

---

# 7. JOIN REQUIREMENT

Create a chart enrichment repository/service that returns profile and earnings information together.

Conceptually:

```python
@dataclass(frozen=True)
class ChartFundamentalContext:
    symbol: str
    stock_profile: StockProfile | None
    earnings_events: tuple[EarningsEvent, ...]
    next_earnings: EarningsEvent | None
    earnings_line: tuple[EarningsLinePoint, ...]
    revision_token: str
```

The chart layer should request one joined/aggregated context:

```python
load_chart_fundamental_context(
    symbol=symbol,
    chart_start=...,
    chart_end=...,
    as_of=...,
)
```

The data must be related using:

```text
stock_profiles.symbol = earnings_events.symbol
```

and the same canonical symbol used by price history.

## Important join constraint

Do not perform a flat SQL join that multiplies each price bar by every earnings row.

This is incorrect:

```text
price_history × earnings_events
```

and would duplicate candle rows.

Instead:

1. load price history as its own time series;
2. load one stock-profile row for the symbol;
3. load the relevant earnings events for the symbol;
4. combine them in a typed chart context;
5. align earnings events to chart dates in the service or rendering layer.

Use a left-join or optional-enrichment policy so missing profile or earnings data never suppresses price data.

---

# 8. DATA PROVIDER ABSTRACTION

Do not let yfinance-specific dictionaries leak into the UI, renderer, or domain logic.

Define replaceable provider interfaces.

Conceptually:

```python
class EarningsProvider(Protocol):
    def fetch_earnings(self, symbol: str) -> EarningsProviderResult:
        ...


class StockProfileProvider(Protocol):
    def fetch_stock_profile(self, symbol: str) -> StockProfileProviderResult:
        ...
```

Implement:

```text
YahooEarningsProvider
YahooStockProfileProvider
```

They may share a lower-level Yahoo/yfinance client if appropriate.

UI code must never directly call:

```python
yf.Ticker(...)
ticker.info
ticker.get_earnings_dates(...)
ticker.quarterly_income_stmt
```

The provider layer must normalize all data before persistence or presentation.

---

# 9. STOCK PROFILE DATA FROM YFINANCE

Use the installed yfinance version’s supported general-information API.

Prefer:

```python
ticker.get_info()
```

or:

```python
ticker.info
```

according to compatibility and existing project conventions.

Extract when available:

```text
longName
shortName

quoteType
exchange
market
currency
country

sector
sectorKey

industry
industryKey

category
fundFamily
```

## Name fallback

Use:

```text
company_name =
    longName
    else shortName
    else canonical symbol
```

Do not display:

```text
None
null
N/A
nan
```

inside the watermark.

## Sector and industry

Prefer provider display names:

```text
sector
industry
```

Persist provider keys separately:

```text
sectorKey
industryKey
```

Do not require `yf.Sector` or `yf.Industry` solely to render a company profile if `Ticker.info` already provides the names.

Only use the dedicated yfinance Sector/Industry classes as an optional fallback if they are confirmed to exist in the repository’s installed yfinance range.

Do not force a major dependency upgrade for those classes.

---

# 10. STOCK PROFILE DOMAIN MODEL

Create a typed immutable model consistent with the project.

Conceptually:

```python
@dataclass(frozen=True)
class StockProfile:
    symbol: str
    provider_symbol: str | None

    company_name: str
    short_name: str | None

    quote_type: str | None
    exchange: str | None
    market: str | None
    currency: str | None
    country: str | None

    sector_name: str | None
    sector_key: str | None

    industry_name: str | None
    industry_key: str | None

    category: str | None
    fund_family: str | None

    source: str
    last_checked_at: datetime
    updated_at: datetime
```

Adapt this to existing model conventions.

Do not pass raw Yahoo dictionaries into the renderer.

---

# 11. STOCK PROFILE WATERMARK

Add a TC2000-inspired profile watermark inside the existing TradingView price panel.

It must be added to the current chart HTML generated by:

```text
src/ui/charts/render_lightweight.py
```

Place it within the existing:

```html
<div id="price-panel">
```

Conceptually:

```html
<div id="price-panel">
    <div id="chart"></div>

    <div id="stock-profile-watermark">
        ...
    </div>

    <canvas id="drawing-overlay"></canvas>
</div>
```

## Required visual order

Render the following lines:

```text
symbol
company name
sector
industry
```

Example:

```text
NVDA
NVIDIA Corporation
Technology Sector
Semiconductors
```

Do not hard-code this example.

Generate it from the joined `stock_profiles` row.

## Sector presentation rule

The database should store the raw provider sector name:

```text
Technology
```

The presentation layer may render:

```text
Technology Sector
```

Do not store the formatted `"Technology Sector"` string in the database.

## Required style

Use a centered translucent gray panel.

Conceptually:

```css
#stock-profile-watermark {
    position: absolute;
    left: 50%;
    top: 48%;
    transform: translate(-50%, -50%);
    text-align: center;
    pointer-events: none;
    user-select: none;
    background: rgba(...gray..., 0.25 to 0.40);
    border: 1px solid rgba(...gray..., low opacity);
    border-radius: 4px;
    padding: 14px 24px;
    z-index: appropriate value;
    max-width: 70%;
}
```

Use the existing chart theme instead of introducing unrelated colors.

The symbol must be the largest line.

Conceptually:

```text
symbol:       32–42 px, bold
company name: 18–24 px
sector:       15–20 px
industry:     15–20 px
```

Make the font responsive for split-screen and small chart windows.

## Interaction safety

The watermark must not:

- capture clicks;
- block crosshair movement;
- block drawing tools;
- block target-price interaction;
- block wheel zoom;
- block drag scrolling;
- block keyboard shortcuts.

Use:

```css
pointer-events: none;
```

and preserve the drawing overlay’s existing z-index behavior.

## Missing fields

If profile loading is incomplete:

- always show the symbol;
- show company name when available;
- omit missing sector or industry lines;
- do not display placeholder text such as `None`;
- do not prevent the chart from rendering.

## Non-equity fallback

When `quote_type` indicates an ETF or fund and sector/industry is unavailable, a reasonable fallback is:

```text
SPY
SPDR S&P 500 ETF Trust
ETF
Large Blend
```

using:

```text
quote_type
category
```

Do not fabricate stock sector or industry classifications for ETFs.

## HTML safety

Escape every provider-derived string before inserting it into HTML.

Do not trust company names, sector names, industry names, or exchange values as safe markup.

---

# 12. EARNINGS DATA SOURCES

Use yfinance as the initial provider.

Inspect and normalize data from appropriate supported APIs, including where available:

```python
ticker.get_earnings_dates(...)
ticker.earnings_dates
ticker.quarterly_income_stmt
ticker.get_income_stmt(freq="quarterly")
ticker.calendar
```

Use each source for its appropriate purpose.

Conceptually:

- earnings dates: report date, expected date, estimated EPS, reported EPS, surprise;
- quarterly statements: diluted EPS, basic EPS, revenue, fiscal period;
- calendar: next expected earnings date where available.

Do not assume all fields exist for every symbol.

Do not assume all yfinance DataFrames have the same orientation or column names across versions.

Normalize them in one provider adapter.

---

# 13. CONSISTENT EPS BASIS

Do not mix incompatible EPS bases within the same growth sequence.

Possible EPS bases include:

```text
Yahoo reported event EPS
GAAP diluted EPS
GAAP basic EPS
```

For the compact earnings growth labels, prefer a consistent series in this order:

1. Yahoo reported event EPS, if enough consecutive quarters exist;
2. diluted EPS, if enough consecutive quarters exist;
3. basic EPS, if enough consecutive quarters exist.

Do not compute:

```text
current quarter from reported event EPS
prior-year quarter from GAAP diluted EPS
```

unless there is a documented, validated reason.

Store the chosen basis:

```text
eps_basis
```

The same basis should be used for:

- quarterly YoY growth;
- previous-quarter YoY growth;
- TTM EPS line.

---

# 14. EPS GROWTH CALCULATION

For each reported quarter \(t\), calculate:

```text
current quarter YoY EPS growth =
(EPS_t - EPS_t-4) / abs(EPS_t-4) × 100
```

The second number is the previous quarter’s YoY growth:

```text
previous quarter YoY EPS growth =
(EPS_t-1 - EPS_t-5) / abs(EPS_t-5) × 100
```

The compact label:

```text
124/56
```

means:

```text
current event’s YoY EPS growth = 124%
previous quarter’s YoY EPS growth = 56%
```

Store raw numeric values:

```text
eps_yoy_growth_pct = 124.2
previous_eps_yoy_growth_pct = 56.4
```

Format only at the presentation layer:

```text
124/56
```

Do not persist `"124/56"` as the canonical data.

## Formatting

- round to whole percentages;
- omit `%` in the compact chart label;
- retain `%` in detailed tooltips;
- positive values do not require a `+`;
- negative values retain `-`.

Examples:

```text
124/56
83/41
-12/37
```

Handle values too large for the layout without breaking the chart, for example:

```text
999+/56
```

or another documented compact representation.

---

# 15. EPS EDGE CASES

Do not blindly calculate misleading growth percentages.

Create a pure, documented calculation function that returns:

```text
numeric growth
status
display token
```

Handle at minimum:

## Prior-year EPS equals zero

```text
growth = None
display = N/A
```

## Negative to positive

```text
prior-year EPS < 0
current EPS > 0

status = TURNAROUND
display = TURN
```

## Positive to negative

```text
prior-year EPS > 0
current EPS < 0

status = LOSS
display = LOSS
```

## Both negative

Do not present an enormous percentage as though it were normal positive earnings growth.

Use a documented status such as:

```text
NEG
N/M
```

or another clear compact representation.

Example labels may become:

```text
TURN/56
LOSS/43
N/A/37
N/M/22
```

Do not fabricate numeric values.

---

# 16. REVENUE GROWTH

Also store quarterly revenue when available.

Calculate:

```text
Revenue YoY =
(revenue_t - revenue_t-4)
/
abs(revenue_t-4)
× 100
```

Store:

```text
revenue_yoy_growth_pct
previous_revenue_yoy_growth_pct
```

Revenue growth does not need to be permanently shown beside every earnings marker initially.

Expose it in the earnings tooltip and retain it for future use by:

- scanners,
- Episodic Pivot research,
- CAN SLIM-style research,
- sector and industry comparisons.

---

# 17. TTM EARNINGS LINE

Add an earnings line inside the main price-chart panel.

Do not create a separate earnings pane.

Use trailing-four-quarter EPS:

```text
TTM EPS_t =
EPS_t
+ EPS_t-1
+ EPS_t-2
+ EPS_t-3
```

Use the same consistent EPS basis selected for quarterly growth.

## Point-in-time rule

The new earnings value becomes available only from the actual report date.

Example:

```text
fiscal quarter end: 2026-06-30
earnings report date: 2026-08-05
```

The updated TTM EPS must first appear on:

```text
2026-08-05
```

not:

```text
2026-06-30
```

This is required to prevent look-ahead bias.

## Daily alignment

For a daily chart:

1. calculate TTM EPS at each reported earnings event;
2. anchor the new value to the report date;
3. forward-fill it across subsequent chart dates;
4. retain the value until the next report changes it.

Do not backfill future knowledge into earlier dates.

---

# 18. EARNINGS LINE SCALE

The earnings line must have its own secondary price scale.

Do not plot EPS against the candle price scale.

Do not allow the earnings series to compress, expand, or distort the candlesticks.

Use the Lightweight Charts equivalent of:

```text
priceScaleId: earnings
```

or the correct API supported by the project’s bundled Lightweight Charts version.

The earnings scale may be hidden or positioned on the left if that produces the cleaner UI.

Requirements:

- right-side stock price scale remains authoritative for candles;
- earnings scale is independent;
- earnings line is visible within the main price panel;
- chart autoscaling of candles is unchanged;
- earnings line may be enabled or disabled through chart options;
- the renderer must tolerate negative TTM EPS values.

Verify the actual bundled Lightweight Charts API before implementation.

Do not assume a method exists without checking the included JS version.

---

# 19. HISTORICAL EARNINGS MARKERS

Render historical earnings directly on the price chart using TradingView-style compact `E` markers.

Conceptual result:

```text
E
124/56
```

For every historical report:

- anchor the marker to the actual report date;
- show `E`;
- show the compact EPS-growth pair when available;
- place the `E` flag in a dedicated event lane at the bottom of the main price chart, immediately above the time axis;
- keep the marker's vertical position fixed to the bottom event lane while zooming, scrolling, or changing the price scale;
- do not position the marker at the candle close, candle low, TTM EPS value, or another price-coordinate value;
- do not obscure candle bodies.

The horizontal coordinate is the report date. The vertical coordinate is a viewport-relative bottom lane, not a data-series price.

A dot by itself inside the candle area does not satisfy this requirement. The marker must visibly contain the `E` glyph and resemble the corporate-event flags shown along the bottom of a TradingView chart.

## TradingView reference appearance

Match the supplied TradingView reference design closely:

```text
       /----\
      /  E   \
      |      |
      \--v---/
---------|---------------- bottom event/volume baseline
```

The ASCII sketch is conceptual. The rendered badge should use the compact TradingView treatment visible in the reference:

- an outlined tag/shield shape rather than a circle or plain text glyph;
- clipped or angled upper corners;
- a small centered downward pointer/tail;
- the pointer visually meets the bottom event/volume baseline at the report date;
- a dark or transparent interior that remains compatible with the existing dark chart theme;
- a clearly legible centered uppercase `E` using the same color as the outline;
- approximately 24–30 CSS pixels wide and 28–34 CSS pixels tall, adjusted for device-pixel ratio;
- a crisp outline of approximately 1.5–2 CSS pixels;
- no glow, large filled disk, or unrelated pin/dot behind the badge.

Layer the badge above the volume histogram and bottom baseline so volume bars remain visible around it but never cover the `E`, outline, or pointer. The badge may overlap the lower volume region as in TradingView, but it must not overlap time-axis text.

Use earnings-surprise color when reliable reported and estimated EPS values exist:

```text
reported EPS > estimated EPS  -> teal/green outline and E
reported EPS < estimated EPS  -> red outline and E
reported EPS = estimated EPS  -> neutral gray outline and E
missing or non-comparable EPS  -> neutral gray outline and E
future estimated event         -> neutral/expected-event accent
```

Use the application's existing accessible theme colors where possible. Do not infer a beat or miss when either comparable EPS value is absent, and do not use color as the only way to expose the underlying result; the persistent summary and event details must retain the numeric values.

The bottom event lane must:

- remain inside the chart area and above the time-axis labels;
- remain visually stable when the chart autoscales;
- not change candle or earnings-series autoscaling;
- allow hover or click inspection without blocking crosshair, drawing, zoom, or scroll interactions;
- preserve the exact report-date alignment as bar spacing changes.

Use the marker API supported by the current Lightweight Charts build.

Depending on version, that may be:

```text
series.setMarkers(...)
```

or:

```text
createSeriesMarkers(...)
```

Verify first.

Do not modify candle OHLCV data to create event markers.

If the installed marker API cannot create a viewport-fixed bottom lane, use a lightweight HTML/canvas overlay or supported chart primitive synchronized to the chart time scale. Do not fall back to a price-anchored dot.

## Density behavior

When zoomed in:

```text
E
124/56
```

may be shown.

When zoomed far out:

- prioritize the `E` marker;
- hide or reduce overlapping growth-pair text;
- keep full details available through hover or click.

Do not allow a large number of quarterly labels to make the chart unreadable.

When multiple events would overlap in the bottom lane, retain each event's date mapping and use deterministic collision handling such as compact spacing, stacking, or hiding secondary growth text. Do not move an event to a different date.

---

# 20. PERSISTENT EARNINGS SUMMARY AND TOOLTIP

Extend the existing top-left chart information and crosshair behavior without replacing current functionality.

## Persistent top-left summary

The top-left earnings table must not appear only when the crosshair is exactly on an earnings date.

Treat each reported earnings result as effective from its report date until the next reported earnings event. Conceptually:

```text
report A date <= selected chart date < report B date -> show report A
report B date <= selected chart date < report C date -> show report B
```

Required behavior:

- on initial chart load, show the latest reported earnings event known as of the latest chart date;
- after an earnings report, keep showing that report on every following trading day until a newer reported event becomes available;
- when a newer report arrives, replace the persistent summary with the new result;
- an upcoming or estimated earnings date must not replace the last reported result;
- when the crosshair moves through history, show the latest reported event whose report date is on or before the crosshair date;
- moving across ordinary non-earnings days must carry the applicable earnings result forward instead of clearing the table;
- before the first known report, show no earnings summary rather than leaking a future result backward;
- when the crosshair leaves the chart, restore the latest reported earnings summary;
- rerenders, resizing, zooming, scrolling, split-view changes, and ordinary price updates must not make the summary disappear.

This carry-forward selection must use the report date as the knowledge-availability boundary so historical crosshair inspection does not introduce look-ahead bias.

## Summary contents and event details

For a reported event, show available fields such as:

```text
Earnings
Report date: 2026-08-05
Fiscal period: 2026-06-30

Reported EPS: 1.24
Estimated EPS: 1.10
Surprise: 12.7%

EPS growth: 124% / 56%
Revenue growth: 48% / 37%
TTM EPS: 3.82
```

Only show fields that exist.

Do not show:

```text
None
nan
null
```

Do not break existing OHLC, volume, RS, target-price, buy-price, or stop-loss tooltip behavior.

Hovering or clicking a bottom `E` flag may temporarily show that event's full details. When that interaction ends, restore the carry-forward result selected by the current crosshair date, or the latest result when no crosshair is active.

---

# 21. UPCOMING EARNINGS — NEXT 14 DAYS

For the current symbol, determine whether an expected earnings report falls within:

```text
0 <= days_to_earnings <= 14
```

Use calendar days, not trading sessions.

For U.S.-listed stocks, calculate against the U.S. market date:

```python
as_of_date = now.astimezone(ZoneInfo("America/New_York")).date()
days_to_earnings = (report_date - as_of_date).days
```

Do not use the KST calendar date directly, because Korea may already be on the next calendar day while the U.S. market is still trading.

Expose:

```text
next_earnings_date
days_to_earnings
has_earnings_within_14d
report_timing
is_date_estimated
```

## Inclusive boundary

These must be flagged:

```text
today
+1 day
+3 days
+14 days
```

This must not be flagged:

```text
+15 days
```

---

# 22. UPCOMING EARNINGS BADGE

When earnings are within 14 days, show a compact badge in or near the existing chart header.

Examples:

```text
E Today
E Tomorrow
E 3d
E 12d
```

Where report timing is known:

```text
E 3d AMC
E Tomorrow BMO
```

Do not create a large warning panel.

Do not automatically block an entry.

Do not alter trading, order, risk, ORB, buylist, or buy-board logic as part of this task.

The badge is informational only.

---

# 23. FUTURE EARNINGS MARKER

The existing TradingView renderer already appears to support future chart whitespace.

Reuse the existing future-whitespace/time-point mechanism.

When the next expected earnings event is within 14 days, render a future `E` marker at the actual expected date where the chart library supports it. Use the same fixed bottom event lane as historical earnings flags; do not place the future marker at an arbitrary price level.

Conceptually:

```text
candles through Aug 21 | future whitespace | E on Aug 27
```

Requirements:

- do not create fake OHLC candles;
- do not invent volume;
- do not shift an earnings event to another day merely to fit the chart;
- do not treat a future date as confirmed when the provider only supplies an estimate.

If a future marker cannot be rendered reliably, retain the `E 6d` header badge and document the limitation.

Do not corrupt price data to force the marker.

---

# 24. FUTURE DATE REVISIONS

Upcoming earnings dates are mutable.

When Yahoo changes an expected date:

1. update the expected event idempotently;
2. retain historical reported events;
3. log the date change;
4. invalidate the chart enrichment cache;
5. rerender the chart only if that symbol is still active.

Example log event:

```text
earnings_upcoming_date_changed
symbol=NVDA
old_date=2026-08-24
new_date=2026-08-26
```

Treat expected future dates as estimated unless the provider supplies reliable confirmation metadata.

---

# 25. CHART OPTIONS

Extend the typed chart options in:

```text
src/ui/charts/models.py
```

Conceptually add:

```text
show_earnings_events = True
show_earnings_line = True
show_stock_profile_watermark = True
earnings_horizon_days = 14
```

Preserve unknown options as the current model does.

If the existing TradingView control row has suitable checkbox patterns, add a compact:

```text
Earnings
```

control that enables/disables historical earnings markers and the earnings line.

The stock profile watermark should default to enabled.

Do not add a large separate settings dialog solely for these features.

Ensure the chart refresh key includes relevant visibility settings.

---

# 26. CONTROLLER INTEGRATION

Extend the existing TradingView data-flow path rather than bypassing it.

Likely target:

```text
src/ui/charts/controller_data_flow.py
```

The current flow conceptually appears to be:

```text
load_tradingview_chart()
    -> _render_tradingview_chart_view()
    -> load price history
    -> load indicators
    -> _generate_tradingview_lightweight_chart_html()
```

Extend it to:

```text
load_tradingview_chart()
    -> load one ChartFundamentalContext for current symbol
    -> render primary chart
    -> render split chart using the same context
```

## Split-screen requirement

When split screen is enabled, do not execute duplicate Yahoo requests for the left and right chart.

Load or retrieve the enrichment context once for the symbol and pass the same immutable data to both render calls.

Recommended behavior:

- daily panel: earnings line, historical markers, upcoming badge, stock watermark;
- intraday panel: stock watermark and upcoming badge;
- intraday historical earnings markers may be shown only when timing can be represented safely;
- do not force a daily TTM line into an intraday pane if it produces misleading behavior.

---

# 27. RENDERER API

Extend the renderer with normalized, typed data.

Conceptually:

```python
_generate_tradingview_lightweight_chart_html(
    symbol,
    history,
    *,
    options=None,
    drawings=None,
    storage_symbol=None,
    indicators=None,
    stock_profile=None,
    earnings_events=None,
    earnings_line=None,
    upcoming_earnings=None,
    target_price=None,
    buy_price=None,
    stop_loss=None,
    interaction_settings=None,
)
```

Adapt the exact API to the repository’s style.

The renderer must not:

- query a database;
- call yfinance;
- calculate network freshness;
- mutate domain records.

The renderer may:

- format normalized values;
- escape HTML;
- convert normalized events to chart marker JSON;
- render the watermark;
- render earnings series and badges.

---

# 28. ASYNCHRONOUS LOADING

Do not run Yahoo requests on the PyQt main UI thread.

Follow the project’s existing worker/thread pattern.

Recommended stale-while-revalidate behavior:

1. load cached profile and earnings data from DB;
2. render the chart immediately with cached data;
3. determine whether profile or earnings data is stale on a background worker;
4. start a background refresh only if needed;
5. upsert normalized provider results;
6. invalidate the relevant chart refresh key;
7. rerender only if the same symbol is still active.

If no cached enrichment exists:

1. render price chart normally;
2. initially show only the symbol watermark;
3. start background profile/earnings retrieval;
4. update the chart after successful retrieval if the symbol remains selected.

Chart navigation must remain responsive while moving rapidly through symbols:

- keep a short-lived, symbol-scoped in-memory cache of normalized profile and earnings context;
- keep provider-freshness database checks off the PyQt main thread;
- bound concurrent per-symbol refresh workers so rapid navigation cannot create an unbounded worker backlog;
- query only the daily/hourly bars and indicator rows that the chart can render;
- load the vendored Lightweight Charts library from one stable local URL so Chromium can reuse its resource and compiled-script caches;
- create bottom `E` badge DOM nodes once per page and reposition them through one animation-frame callback during pan, zoom, or resize;
- never trigger two full chart renders for one Previous/Next navigation action.
- render database-cached earnings in the same initial page as ADR and RS/TI65;
- never force-reload the visible chart when a background provider refresh completes;
- let the unattended daily refresh gate run the supplemental earnings phase even when price and indicator caches are already current.

---

# 29. RACE-CONDITION SAFETY

Rapid symbol navigation must not leak data between charts.

Example:

```text
AAPL -> NVDA -> TSLA
```

An old AAPL worker result must never populate the NVDA or TSLA chart.

Use the application’s existing equivalent of:

```text
request generation ID
request token
symbol ownership check
worker cancellation
```

On worker completion, verify:

```text
result.symbol == currently displayed canonical symbol
request_generation == active_generation
```

before rerendering.

This requirement applies to both:

- earnings data;
- stock profile data.

---

# 30. REFRESH POLICY

Do not query yfinance on every chart repaint.

## Stock profile

Profile information changes infrequently.

Suggested default:

```text
fresh for 30 days
```

When stale:

- display cached information immediately;
- refresh asynchronously.

For `UNAVAILABLE` results, use a negative-cache interval such as:

```text
24 hours
```

so unsupported symbols are not repeatedly queried.

## Historical earnings

Suggested default:

```text
refresh if last successful sync is older than 24 hours
```

or when a report is reasonably expected to have occurred since the last sync.

## Upcoming earnings

Refresh at least daily.

When the expected report is within 14 days, a shorter interval is acceptable:

```text
6–12 hours
```

Do not retry aggressively against Yahoo.

Use bounded retries, backoff, and timeouts consistent with existing infrastructure.

---

# 31. HISTORICAL DATA ACCUMULATION

The database must become more complete over time.

When Yahoo returns fewer historical quarters than the database already contains:

```text
DB history > current Yahoo response
```

do not delete the older DB records.

Historical earnings events must accumulate permanently unless an explicit maintenance migration corrects bad data.

Provider synchronization must:

1. query existing events;
2. normalize provider data;
3. upsert new or corrected events;
4. preserve older valid events;
5. update the current expected event;
6. never replace the full table with the latest provider response.

---

# 32. IDEMPOTENT WRITES

Repeated synchronization of the same data must not create duplicates.

Test:

```text
sync once
sync same response again
```

Expected:

```text
same row count
same event identities
updated timestamps only where appropriate
```

Profile upserts must also be idempotent.

Do not rewrite unchanged rows unnecessarily if that would cause avoidable mirror synchronization or chart rerendering.

Consider using a normalized payload fingerprint or field comparison where helpful.

---

# 33. LOCAL MIRROR AND PC/LAPTOP SYNC

The repository uses the PC MySQL database as the canonical market-data cache and a laptop SQLite local mirror.

The new supplemental data must participate in that architecture.

Add the required tables to schema initialization and local-mirror handling.

At minimum, inspect and update as appropriate:

```text
src/infrastructure/database/schema.py
src/infrastructure/database/mirror_engine.py
src/infrastructure/database/mirror_copy.py
src/infrastructure/database/mirror_reconciliation.py
src/infrastructure/database/__init__.py
```

Add tables such as:

```text
stock_profiles
earnings_events
```

to:

```text
MIRRORED_TABLES
_RECONCILE_TABLE_SPECS
```

using appropriate:

```text
primary keys
partition columns
watermark columns
revision columns
```

Likely revision/watermark field:

```text
updated_at
```

but inspect existing mirror semantics before deciding.

Add new Boolean fields such as:

```text
is_date_estimated
```

to any Boolean-reconciliation handling required by the current mirror implementation.

## Mirror behavior

Preserve the existing intended direction:

```text
PC MySQL -> laptop SQLite
```

Do not turn profile or earnings data into runtime order state.

Do not make trading execution depend on successful mirror synchronization of these supplemental tables.

When the PC DB is unavailable, the laptop should use cached mirrored profile and earnings data when available.

---

# 34. FAILURE BEHAVIOR

Stock profile and earnings data are supplemental research data.

Failure to retrieve or parse either dataset must never:

- crash the chart;
- prevent candles from loading;
- prevent RS indicators from loading;
- block scanner operation;
- block watchlist operation;
- block buylist operation;
- block buy-board operation;
- block KIS operations;
- block order submission;
- alter an open position;
- alter stop-loss monitoring.

Required fallback:

```text
price chart continues normally
cached profile used if available
cached earnings used if available
missing enrichment omitted
error logged
```

Catch failures at provider and service boundaries.

Do not hide programming errors with unbounded blanket exceptions, but keep network/provider failures isolated from chart rendering.

---

# 35. LOGGING

Add concise structured logging or the nearest project-equivalent events.

Examples:

```text
stock_profile_cache_hit
stock_profile_sync_started
stock_profile_sync_completed
stock_profile_sync_failed

earnings_cache_hit
earnings_sync_started
earnings_sync_completed
earnings_sync_failed
earnings_upcoming_date_changed

chart_fundamental_context_loaded
chart_fundamental_context_refresh_scheduled
```

Include:

```text
symbol
provider symbol
event date where relevant
record count
cache age
```

Do not log on every mouse movement, chart crosshair event, or repaint.

Do not log sensitive KIS or DB credentials.

---

# 36. TESTS — STOCK PROFILE

Add unit and integration tests covering at minimum:

## Provider normalization

```text
longName available
longName missing, shortName available
both names missing
sector and industry available
sector key and industry key available
partial metadata
ETF metadata
completely unavailable profile
```

## Persistence

```text
idempotent stock profile upsert
partial result persisted
stale profile identified correctly
unavailable result negative-cached
updated sector replaces stale sector
updated industry replaces stale industry
```

## Join behavior

```text
correct profile joined to correct symbol
missing profile does not suppress price chart
profile is not joined to another symbol
canonical symbol is used instead of TradingView display symbol
```

## Presentation

```text
field order:
symbol
company
sector
industry

sector suffix applied only in presentation
missing lines omitted
provider strings HTML-escaped
watermark pointer-events disabled
```

---

# 37. TESTS — EARNINGS CALCULATIONS

Test at minimum:

```text
normal positive YoY EPS growth
growth acceleration
growth deceleration
negative YoY growth
prior-year EPS zero
negative-to-positive turnaround
positive-to-negative loss
both periods negative
missing quarter
inconsistent EPS basis rejected
```

Verify compact output such as:

```text
124/56
-12/37
TURN/56
LOSS/43
N/A/37
```

---

# 38. TESTS — TTM EPS AND POINT-IN-TIME LOGIC

Verify:

```text
TTM EPS = Q1 + Q2 + Q3 + Q4
```

Test that a quarter ending June 30 but reported August 5 changes the line beginning August 5.

Explicitly assert that the June-quarter EPS is unavailable on:

```text
July 1
July 31
August 4
```

and available beginning:

```text
August 5
```

Test forward-fill behavior across daily chart dates.

Test negative TTM EPS.

---

# 39. TESTS — UPCOMING EARNINGS

Use a pure function that accepts an explicit `as_of_date` so tests do not depend on the machine clock.

Test:

```text
earnings today
earnings in 1 day
earnings in 3 days
earnings in 14 days
earnings in 15 days
earnings yesterday
unknown earnings date
```

Expected:

```text
0–14 days inclusive -> flagged
15+ days -> not flagged
past date -> not upcoming
```

Test the difference between:

```text
Asia/Seoul calendar date
America/New_York market date
```

around midnight boundaries.

---

# 40. TESTS — DATABASE AND MIRROR

Use isolated SQLite or temporary fixtures for normal tests.

Do not require a developer’s MySQL instance.

Test:

```text
stock_profiles table creation
earnings_events table creation
indexes
upserts
duplicate prevention
expected-date revision
historical record retention
old DB history retained when provider returns less
profile and earnings mirror table registration
mirror copy
reconciliation
freshness handling
Boolean normalization
```

Where MySQL-specific SQL exists, test construction or use existing dialect test patterns.

---

# 41. TESTS — CHART INTEGRATION

Test where practical:

```text
profile watermark is generated for the correct symbol
earnings marker dates are correct
historical earnings render as visible E flags in the fixed bottom event lane, not as price-anchored dots
historical E flags use the outlined TradingView tag/shield shape with a centered downward pointer
positive surprises use the configured teal/green event color
negative surprises use the configured red event color
missing or non-comparable surprises use a neutral event color
volume bars do not cover the E glyph, outline, or pointer
bottom E flags remain vertically stable during price autoscale, zoom, and scroll
bottom E flags do not affect candle or earnings-series autoscaling
124/56 labels correspond to the correct event
earnings line uses a separate priceScaleId
candle scale remains independent
future marker does not create fake candles
future marker uses the bottom event lane
upcoming badge is shown only within 14 days
top-left earnings summary is present on initial load when a reported event exists
top-left earnings summary carries the last report across non-earnings trading days
top-left earnings summary switches only when a newer reported event becomes effective
historical crosshair dates carry forward the latest report known on that date
crosshair dates before the first report do not expose later earnings
crosshair exit restores the latest reported earnings summary
an expected future event does not replace the latest reported result
rapid symbol changes cannot leak stale profile data
rapid symbol changes cannot leak stale earnings data
provider failure does not prevent chart HTML generation
split view does not cause duplicate provider fetches
```

Do not make normal tests depend on live Yahoo network access.

Mock provider responses.

---

# 42. VISUAL ACCEPTANCE

The final chart should conceptually look like:

```text
┌───────────────────────────────────────────────────────────────┐
│ NASDAQ:NVDA         ADR 5.8     RS Score ...       E 6d AMC  │
│                                                               │
│                       ┌──────────────────────┐                │
│                       │        NVDA          │                │
│                       │ NVIDIA Corporation   │                │
│                       │  Technology Sector   │                │
│                       │    Semiconductors    │                │
│                       └──────────────────────┘                │
│              candlesticks                                    │
│         ╭───────────────╮                                    │
│    ╭────╯               ╰────────                            │
│                                                               │
│       earnings/TTM line ────────────────╮                     │
│                                        ╰────────             │
│                                                               │
│     E                 E                  E          future E   │
│    83/41            124/56             67/124          6d     │
└───────────────────────────────────────────────────────────────┘
```

The profile watermark is centered in the price panel.

The earnings line is inside the price panel.

The earnings line has its own scale.

Historical earnings use visible `E` flags in a fixed bottom event lane rather than dots in the candle area.

The top-left earnings table continuously shows the latest reported result and replaces it only when a newer report becomes available.

Upcoming earnings within 14 days are immediately visible.

---

# 43. DO NOT DO THESE THINGS

Do not:

- create a separate earnings dashboard;
- create a separate user-facing earnings table;
- create a separate sector or industry page;
- add profile columns to price-history rows;
- add profile columns to earnings rows;
- join earnings directly onto every price-history row;
- query yfinance from HTML or JavaScript;
- query yfinance directly from UI render functions;
- query Yahoo on every chart repaint;
- run network calls on the main UI thread;
- store only formatted `124/56` strings;
- use fiscal period end as the knowledge-availability date;
- delete old earnings history because Yahoo returns fewer rows;
- fabricate company classifications;
- fabricate missing EPS values;
- fabricate earnings dates;
- generate fake OHLC candles for future markers;
- automatically block trades near earnings;
- modify KIS broker execution behavior;
- modify order state;
- modify stop-loss behavior;
- significantly rewrite unrelated chart functionality;
- upgrade major dependencies without proving necessity.

---

# 44. IMPLEMENTATION ORDER

Proceed in this order:

1. inspect repository architecture and current branch;
2. identify current chart and DB extension points;
3. define stock-profile domain model;
4. define earnings domain models;
5. implement pure EPS/revenue calculation functions;
6. add `stock_profiles` schema;
7. add `earnings_events` schema;
8. add repository methods and idempotent upserts;
9. add provider abstractions;
10. implement Yahoo stock-profile provider;
11. implement Yahoo earnings provider;
12. implement stock-profile caching and refresh service;
13. implement earnings caching and refresh service;
14. implement chart fundamental-context aggregation;
15. integrate new tables with local mirror/reconciliation;
16. pass profile and earnings context through TradingView controller flow;
17. render the centered gray profile watermark;
18. render historical `E` flags in the fixed bottom event lane;
19. render compact growth pairs;
20. render the TTM earnings line on a secondary scale;
21. add the persistent carry-forward top-left earnings summary;
22. add next-14-day earnings calculation;
23. add compact upcoming-earnings badge;
24. add future bottom-lane marker using existing whitespace where supported;
25. add asynchronous stale-while-revalidate behavior;
26. add race-condition protection;
27. add tests;
28. run focused tests;
29. run the complete test suite;
30. fix regressions;
31. report results.

Do not stop after producing an implementation plan.

Proceed with the implementation unless an actual technical blocker exists.

---

# 45. REPORT BEFORE CODING

Before editing, briefly report:

```text
Current TradingView chart data flow
Current chart renderer API
Current Lightweight Charts marker/series API
Current DB schema pattern
Current repository pattern
Current PC/laptop mirror pattern
Current yfinance version compatibility
Files proposed for addition
Files proposed for modification
Risks or incompatibilities found
```

Then continue directly into implementation.

---

# 46. REPORT AFTER IMPLEMENTATION

After implementation, report:

```text
Files added
Files modified

stock_profiles schema
earnings_events schema
indexes and keys

how profile rows are joined
how canonical symbols are handled
how Yahoo symbols are mapped

how company name is selected
how sector and industry are selected
how missing metadata is handled

how EPS basis is selected
how 124/56 is calculated
how negative and zero EPS are handled
how TTM EPS is calculated
how look-ahead bias is prevented

how upcoming earnings are calculated
how future date revisions are handled
how future markers are rendered

how the gray profile watermark is rendered
how HTML is escaped
how chart interactions remain available
how the earnings line avoids candle-scale distortion

refresh intervals
background-worker behavior
race-condition protection
local mirror changes
tests added
focused test results
full test-suite results
known limitations
```

For visible UI work, provide a screenshot if the environment supports running the application and capturing one. Otherwise, describe the rendered HTML/CSS result and identify any manual visual validation still required.

---

# 47. FINAL ACCEPTANCE CHECKLIST

Explicitly confirm every item:

```text
[ ] stock_profiles is physically separate from earnings_events
[ ] stock_profiles is physically separate from price_history
[ ] earnings_events is physically separate from price_history
[ ] profile and earnings data are joined by canonical symbol
[ ] no flat price_history × earnings_events join duplicates candles
[ ] company name is persisted
[ ] sector name and key are persisted
[ ] industry name and key are persisted
[ ] stock profile data participates in PC-to-laptop mirroring
[ ] earnings data participates in PC-to-laptop mirroring
[ ] profile data is not fetched on every repaint
[ ] earnings data is not fetched on every repaint
[ ] yfinance is not called from UI rendering code
[ ] provider calls do not run on the PyQt main thread
[ ] stale worker results cannot populate another symbol
[ ] the centered watermark has a translucent gray background
[ ] the watermark displays symbol, company, sector, and industry
[ ] missing fields do not render None/null/N/A
[ ] provider strings are HTML-escaped
[ ] the watermark does not intercept chart interaction
[ ] 124/56 is generated from raw quarterly EPS values
[ ] formatted 124/56 is not the canonical persisted value
[ ] EPS bases are not mixed across comparison quarters
[ ] zero and negative EPS edge cases are handled explicitly
[ ] historical earnings are anchored to report dates
[ ] fiscal-period information is not exposed before report date
[ ] TTM EPS is rendered inside the price chart
[ ] the earnings line uses an independent scale
[ ] the earnings line does not distort candle scaling
[ ] historical E flags appear in a fixed bottom event lane at correct dates
[ ] historical earnings are not rendered as price-anchored dots
[ ] E flags match the outlined TradingView tag/shield shape with a downward pointer
[ ] surprise colors distinguish positive, negative, and neutral results without fabricating a classification
[ ] E flags render above the volume histogram and do not overlap time-axis labels
[ ] bottom E flags remain stable during autoscale, zoom, and scroll
[ ] bottom E flags do not affect series autoscaling
[ ] the top-left earnings summary remains visible between report dates
[ ] the top-left summary carries forward the latest report known on the selected chart date
[ ] the top-left summary switches only when a newer reported result is available
[ ] an upcoming estimated event does not replace the latest reported result
[ ] historical crosshair inspection does not leak future earnings backward
[ ] upcoming earnings 0–14 days inclusive are detected
[ ] days-to-earnings uses the U.S. market calendar date
[ ] expected future dates are treated as mutable/estimated
[ ] no fake OHLC candles are generated
[ ] older DB earnings history is preserved
[ ] repeated synchronization is idempotent
[ ] missing enrichment cannot break price-chart loading
[ ] no trading or execution behavior was changed
[ ] full pytest suite passes
```

---

# 48. TEMPORARY RIGHT-DRAG PERCENTAGE MEASUREMENT

Replace the browser/WebEngine right-click menu inside the price chart with a temporary point-to-point percentage measurement.

Required behavior:

- pressing the right mouse button on any valid price-chart point starts the measurement;
- dragging draws a dotted line from the exact press coordinates to the current pointer coordinates;
- the label shows `(end price / start price - 1) * 100`, including a leading `+` for gains;
- both endpoints use chart-coordinate prices, not a candle's OHLC values or a selected date;
- the regular OHLC tooltip is hidden while measuring;
- releasing the right mouse button immediately removes the line and label;
- the measurement is never saved as a drawing and does not call the persistence bridge;
- left-button drawing, target-price, zoom, scroll, and earnings interactions remain unchanged;
- the native context menu must not appear inside the price chart.

Acceptance checks:

```text
[ ] right-button drag draws a dotted point-to-point guide
[ ] the percentage is calculated from the exact start and end chart prices
[ ] upward measurements have a positive value and downward measurements have a negative value
[ ] OHLC values are not used as measurement endpoints
[ ] releasing the right button clears the guide and label
[ ] the measurement is not persisted or synchronized
[ ] the browser/WebEngine context menu is suppressed in the price panel
[ ] existing left-button chart tools still work
```
