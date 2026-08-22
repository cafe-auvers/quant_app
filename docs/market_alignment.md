# Leadership and Market Context

The TradingView-style chart shows a read-only Leadership and Market Context
overlay. It describes a stock's own relative strength separately from the
market, segment, sector, and industry environment. It does not submit or
authorize orders, block trades, change Buy Board membership, or alter entries,
stops, risk, sizing, broker selection, or execution-engine behavior.

## Daily data flow

The standalone 1D refresh owns the calculation:

```text
completed daily OHLCV (stocks, SPY, and Market Pulse proxies)
  -> chart indicators
  -> scanner metrics and cross-sectional ranks
  -> stock-profile sector, industry, and market-cap enrichment
  -> Leadership and Market Context batch
  -> atomic published-batch manifest
```

The phase runs after scanner metrics and stock-profile enrichment. The service
loads histories, scanner rows, profiles, and proxy configuration in batches;
it does not query or download once per stock. A batch for the same completed
market date and feature version is skipped unless `--force-derived` is used.
The 1H refresh never invokes alignment calculation.

The shared 1D download includes SPY and active tickers from
`config/market_pulse_instruments.json`. This avoids a second market-data
provider or a chart-triggered download. Missing proxy history remains unknown.

## Leadership

```text
Leadership = 0.60 * Market RS + 0.40 * Industry Peer RS
```

The app reuses the scanner's stable `growth_rank_1m` as Market RS when it is
available. That field is the cross-sectional percentile rank (higher is
stronger) of the completed 21-session return and uses the scanner's existing
`rank(pct=True, method="max")` definition. Alignment eligibility reuses the
existing Setup 1 liquidity floor: latest volume at least 40,000 shares and
dollar volume at least $35,000, with at least 21 history rows.

If the stored scanner rank is unavailable, the fallback is:

```text
0.50 * percentile(63-session return)
+ 0.30 * percentile(126-session return)
+ 0.20 * percentile(252-session return)
```

Fallback returns require complete history at one as-of date and prefer
adjusted close when reliable. Missing history stays unavailable. Cross-sectional
ties are assigned the deterministic average percentile.

Industry Peer RS is the selected stock's Market RS percentile within its
canonical industry. At least five eligible constituents are required. A
smaller industry falls back to its sector when the sector has at least five
eligible constituents; the row records `sector_fallback`. If neither group is
large enough, Leadership remains unavailable and Market RS is not reweighted.

Only the displayed score is rounded (half up). Labels use the centralized
boundaries: 80-100 Strong, 60-79 Moderate, and 0-59 Weak.

## Classification and proxies

The hierarchy is Market -> Segment -> Sector -> Industry -> Stock. The batch
uses `stock_profiles` as the canonical sector/industry source. Yahoo or the
bulk Nasdaq profile refresh also records market cap and its as-of date.

The centralized segment thresholds and existing Market Pulse proxies are:

| Segment | Market cap | Proxy |
| --- | ---: | --- |
| Mega-Cap | at least $200B | MGK |
| Large-Cap | $10B to under $200B | SPYG |
| Mid-Cap | $2B to under $10B | MDYG |
| Small-Cap | $300M to under $2B | IWO |
| Micro-Cap | under $300M | IWC |

Sector and industry proxy names are matched deterministically to active Market
Pulse display names, with narrow aliases for common provider spellings such as
Finance/Financials and Basic Materials/Materials. Themes never select a peer
group and never influence Leadership.

When no exact industry proxy exists, the batch constructs an equal-weight
daily-return basket from eligible constituents. It requires five constituents
and at least 60% coverage (never fewer than five valid returns) per session.
Unavailable returns are excluded, not replaced with zero. A normalized index
is derived consistently from basket returns and the constituent count and
coverage are persisted. Current classifications are not point-in-time, so the
basket must not be interpreted as survivorship-bias-free historical playback.

## Context rules

Each component stores its input values, three individual condition results,
passed-condition count, and state. Green means three passes, yellow two, red
zero or one, and unknown means classification or required data is missing.
Unknown is never converted to red.

- MKT (SPY): close above SMA20, close above SMA50, and five-session return
  above zero.
- SEG: proxy close above SMA20, five-session return above zero, and
  five-session return above SPY.
- SEC: proxy close above SMA20, five-session return above SPY, and its
  20-session return percentile across active sectors at least 70.
- IND: proxy or basket index above SMA20, five-session return above its parent
  sector, and its 20-session return percentile across available industries at
  least 70.

Green/yellow/red score 2/1/0. With four available components, 7-8 is Strong,
5-6 Supportive, 3-4 Mixed, and 0-2 Weak. A red MKT caps the result at Mixed.
With exactly one unknown, the available points are normalized to an eight-point
maximum and the result is marked provisional. Fewer than three available
components produces Unknown.

## Persistence and chart lookup

`stock_market_alignment_daily` stores one row per symbol, completed date, and
feature version. It includes scalar display/filter fields plus
`calculation_details_json`, which contains every raw value and condition used
by the details panel. `market_alignment_batches` is the publication manifest.
Rows and the manifest are upserted in one transaction, so a failed force
recalculation rolls back and the previous published snapshot remains visible.

Feature version `1.0` identifies the formulas and thresholds documented here.
A formula, eligibility, proxy, or classification-policy change should advance
the version rather than silently reinterpret stored rows.

Chart selection executes one indexed local query joined to a published batch.
It never calls yfinance or KIS, refreshes Market Pulse, performs a migration,
or calculates universe/group values. The overlay uses the stored details and
does not reconstruct calculations. It shows a stale badge when the snapshot
date is older than the latest expected completed market session. A missing
snapshot displays N/A and four gray unknown indicators.

## Interpretation and limitations

Leadership and context are descriptive decision-support metrics, not forecasts
or guarantees of profitability. They do not authorize or block orders.
Thresholds and weights should be validated later against historical signals
and outcomes. Known limitations include non-point-in-time classifications,
unavailable context where Market Pulse lacks a matching proxy/history, and no
historical score playback in this version.
