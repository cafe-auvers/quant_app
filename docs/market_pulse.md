# Market Pulse

Market Pulse is a read-only market-awareness page positioned after Scanner. It compares ETF proxies for broad growth segments, sectors, and industries/themes so a scanned stock can be reviewed in context. It is decision support only: strength in a proxy does not guarantee a profitable trade, and the feature has no order, strategy-state, Buy Board, risk, or broker mutation path.

## Universe configuration

The tracked universe is [`config/market_pulse_instruments.json`](../config/market_pulse_instruments.json). Each record contains:

- `section`: `market_segments`, `sectors`, or `industries_themes`
- `display_name`
- `ticker`
- `display_order`
- `is_active`

To add an ETF, add a unique `(section, ticker)` record and choose its display order. To remove it from refreshes without losing prior database snapshots, set `is_active` to `false`. The same ticker may appear in more than one section intentionally.

## Data and metric conventions

Refresh uses the application's existing batched yfinance history loader through a replaceable provider interface. The completed-session path requests two years of daily history in batches, applies a 15-second request timeout and two bounded retries, and does not use per-row fallback requests. Its `auto_adjust=True` series is used consistently as the reference series; the existing `price_history` table stores it through the established daily OHLCV path, including `adj_close`.

`Intraday %` is additive and comes from a second batched yfinance request for recent 5-minute bars. It compares the latest usable 5-minute close with the latest completed-session close. If minute data is unavailable, the cell remains `N/A` while the completed-session metrics remain usable and unchanged.

The `Refresh` button runs the full completed-session, holdings, relative-strength, and intraday workflow. `Refresh Intraday` calls only the yfinance 5-minute path against the latest cached completed-session closes; it preserves daily, weekly, monthly, 52-week, component, status, and rank values exactly as stored. Both actions share one refresh lock, so they cannot overlap.

Only dates at or before the latest expected completed US session are considered. Daily session dates remain exchange calendar dates and are not shifted through Korea time. Duplicate dates are coalesced deterministically. For a reference price `p[t]`, returns are stored as decimals:

```text
daily          = p[t] / p[t-1]  - 1
weekly         = p[t] / p[t-5]  - 1
monthly        = p[t] / p[t-21] - 1
intraday       = latest 5-minute close / completed-session close - 1
above 52W low  = p[t] / min(last 252 sessions) - 1
below 52W high = p[t] / max(last 252 sessions) - 1
```

The current session is included in the 252-session low/high window. A metric is `N/A` when its exact positional observation is missing or there are too few rows; missing data is never replaced by zero. Percentage formatting occurs only in the Qt model, so an internal value of `0.0127` displays as `+1.3%` rather than `127%`.

Daily rank is calculated independently inside each section, descending by daily return with ticker as the deterministic tie-breaker and missing returns last. Sorting a visible table does not change that stored daily rank or download new data.

## Refresh and cache flow

The tab loads `data/market_pulse_snapshot.json` immediately. This small atomic local projection allows cached rendering before optional MySQL initialization or network access. Refresh then runs on a dedicated `QThread`:

1. Load active configuration and any available raw daily history from `price_history` in one universe query.
2. Request all unique ETF tickers through the batched daily provider and the batched yfinance 5-minute provider.
3. Resolve the modal latest completed session across returned instruments. A small number of lagging symbols are marked stale/unavailable instead of moving the whole dashboard backward.
4. Calculate every row and daily rank off the UI thread.
5. Upsert raw daily bars into the existing `price_history` table, then upsert metric snapshots including `intraday_return`.
6. Atomically replace the local display cache and finally swap the visible table models.

Only one refresh may run at a time. A second rapid click is suppressed. The previous snapshot remains visible throughout loading and after a timeout, empty response, total provider failure, or database error. Partial ticker failures retain valid rows and show concise row/status warnings; detailed exceptions and row counts go to application logs. There is no polling loop.

## Persistence schema

The existing schema-guard convention creates these optional market-cache tables idempotently during MySQL initialization (and in SQLite tests):

- `market_pulse_instruments`, unique on `(section, ticker)`
- `market_pulse_snapshots`, unique on `(instrument_id, as_of_date)`

Snapshot rows include the requested metrics plus row status, error detail, and the source session date needed to explain partial/stale data. Re-running a refresh for the same market date updates the existing rows; it does not create duplicates. No second raw-price subsystem is introduced.

## Known limitations

- The first version uses ETF proxies rather than constituent breadth, correlation, news, sentiment, or an economic calendar.
- The repository's lightweight recurring NYSE holiday calendar cannot anticipate exceptional exchange closures; such a day produces a stale warning and a later retry rather than an intraday bar.
- Data availability and symbol continuity depend on Yahoo Finance. Delisted or renamed ETFs remain visible as unavailable until configuration is updated.
- There is no configuration editor; edit the tracked JSON file and restart the application.
- The current application theme is light-only, so the page follows that existing theme rather than introducing a separate dark-theme system.
