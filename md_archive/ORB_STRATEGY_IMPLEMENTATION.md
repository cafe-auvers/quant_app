# ORB Strategy Implementation Plan

## Goal

Add an Opening Range Breakout workflow for the watchlist. The app should fetch intraday data every 5 minutes during the U.S. regular session, calculate opening-range high/low for configurable windows, and flag symbols that break the range or meet saved target prices.

## Strategy Rules

Supported ORB windows:

- `1m`: first 1 minute after 9:30 AM New York time
- `5m`: first 5 minutes
- `30m`: first 30 minutes
- `1h`: first 60 minutes

Normal chart/cache data should be 5-minute OHLCV. The only 1-minute value needed is the first-minute high/low after market open for the `1m` ORB reference.

Signal state:

- `breakout = up` when latest close is above ORB high.
- `breakout = down` when latest close is below ORB low.
- `target_met = true` when latest close is at or above the watchlist `target_price`.

## Current Code Added

- `src/core/orb.py`
  - Pure strategy helpers for ORB range calculation, breakout detection, target checks, and intraday resampling.
- `src/api/kis_intraday.py`
  - KIS intraday adapter boundary and row normalizer.
  - The exact KIS overseas minute endpoint/TR_ID still needs confirmation before live fetching is wired.
- `src/ui/main_window.py`
  - Added an **Intraday Charts** tab limited to watchlist symbols.
  - The Intraday Charts tab sits directly to the right of the main Charts tab and follows the same chart layout pattern.
  - Chart action buttons sit below the chart to preserve horizontal plotting space.
  - Main Charts has a timeframe selector for `1D` or `1H`.
  - Main Charts has a split-screen option that shows `1D` and `1H` views side by side.
  - In split-screen mode, the `1H` pane is filtered to the same visible date window as the `1D` pane so it acts as a more detailed view of the same period.
  - Main Charts 1-hour view first loads historical rows from MySQL `hourly_price_history`, then falls back to cached 5-minute intraday bars resampled to `1h`, then yfinance `1h` if cache is missing.
  - Drawings and target prices are shared by symbol between the `1D`, `1H`, split-screen, and Intraday Charts views.
  - Intraday chart settings include checkboxes for Volume and EMA lines. RS vs SPY is shown as disabled until intraday SPY comparison data is cached.
  - Intraday charts currently fetch 5-minute data through the existing yfinance loader and resample locally to `30m` or `1h`.
  - The chart interval is selectable: `5m`, `30m`, `1h`; default is `1h`.
  - The chart window is selectable: `1D`, `3D`, `5D`, `7D`; default is `7D`.
  - Intraday data is saved to MySQL `intraday_price_history` for faster refresh.
  - Intraday cache is pruned to keep only the latest 7 days.
  - Intraday chart rendering is cache-first. Network refresh runs in a background worker so the Dashboard does not freeze.
  - Dashboard has an `Update Watchlist Intraday` button that refreshes 5-minute intraday cache for all watchlist symbols in one background job.
  - Dashboard has a `Live Data Auto Refresh` toggle with a configurable minute interval. When enabled, it refreshes watchlist intraday cache during U.S. regular market hours only.
  - Intraday yfinance failures are retried and logged per symbol, so one failed download such as `CRL` should not crash the app.
  - Background worker references are kept until Qt emits `finished`, and active workers are checked on app close to avoid `QThread: Destroyed while thread is still running`.
  - Target price is shared with the main Charts tab through `watchlist.target_price`.
  - Drawings are shared with the main Charts tab through `data/chart_drawings.json`.
  - Intraday rendering keeps up to 2000 bars visible by default so 7-day intraday drawings can map to daily chart dates.
  - Saved drawing lines are clipped to the visible chart window instead of being dropped when one endpoint is outside the current view.
  - Saved drawing lines are editable: click a drawing to show endpoint handles, drag either endpoint, and the updated line persists.
  - Newly drawn lines receive endpoint handles immediately, so they can be edited without reloading the chart.
  - Daily Charts render both endpoint handles for drawings created from intraday timestamp keys.
  - Intraday Charts map daily drawing dates to the first/last intraday bar on that date so daily chart drawings are visible intraday.
  - Intraday rendering is explicitly marked as intraday instead of inferred from duplicate dates, so `1h` charts still map daily drawings correctly.
  - Date-only daily drawing endpoints clamp to the available intraday cache span when the exact daily date is outside the cached range.
  - Drawing and target persistence refreshes the other chart tab for the same symbol without reloading the actively edited chart.
  - Intraday fetch attempts are cooldown-protected per symbol/window to prevent repeated immediate yfinance refresh loops.
  - Target and drawing saves persist state without reloading the chart HTML, keeping chart interactions responsive.
  - Adding a watchlist symbol prefetches intraday cache; removing a watchlist symbol deletes its intraday cache rows.
  - Up/Down arrow keys move through watchlist symbols and redraw the intraday chart.
  - Opening Intraday Charts switches the left sidebar source to Watchlist.
  - Latest intraday price updates the active Trade Plan entry price and recalculates stop as 8% below latest price.
  - Trade Plan now has SIM/PROD account selectors and can apply the selected KIS account value to position sizing.
  - Trade Plan includes a vertical ORB Position Plan table with metrics as rows and a two-row header: each column repeats risk % on the first header row and ORB timeframe (`1m`, `5m`, `30m`) on the second.
  - Each ORB risk/timeframe column has a top-row checkbox. Selecting a valid column applies its entry, stop, rounded shares, and risk % to the Trade Plan form.
  - Default ORB risk cases are `0.25%`, `0.50%`, `1.00%`, `1.50%`, and `2.00%`. If the typed `Risk %` is different, that case is added to the table too.
  - ORB plan conditional formatting marks invalid plans red when shares are below `1`, capital % is outside `10%` to `35%`, stop loss % is greater than or equal to ADR %, or SL/ADR is outside `15%` to `66%`.
  - `Show valid plans only` is checked by default and hides ORB risk/timeframe columns with any invalid red condition.
  - ORB recommendation scoring favors SL/ADR near `65%`, capital % near `22.5%`, and lower risk %. The table displays recommendations as `Excellent`, `Good`, `OK`, or `Invalid`.
  - ORB risk/timeframe columns are sorted by recommendation so the most recommended visible plan appears first.
  - After the ORB table refreshes, the best visible valid recommendation is auto-selected and applied to the Trade Plan form. Ties prefer the lower risk %.
  - ORB entry is the opening-range high and stop is the opening-range low. Take profit is not required for ORB planning; the table shows saved target price separately when one exists.
  - The ORB table shows current price, total risk, risk per share, fractional shares, total investment, capital %, ADR %, stop loss %, and SL/ADR.
  - ORB shares are rounded up to whole shares before calculating investment, capital %, and validity filtering.
  - ORB sizing uses `Account Size USD` and `Risk %`; for example, `$2,600` at `0.50%` risks `$13`.
  - Intraday refresh stores normal `5m` bars plus only the latest opening `1m` bar for the `1m` ORB row, avoiding full 1-minute history storage.
  - Scanner has an optional `Score by ORB recommendation` phase. For manual scanner runs, symbols that pass the normal filters are fetched through the intraday yfinance cache, scored with the same ORB recommendation logic as Trade Plan, and sorted by `ORB Score` in the Scanner table and stock sidebar.
- `src/utils/db_loader.py`
  - `price_history` remains the daily-history table used by scanners and RS/TI65 indicator calculations.
  - `hourly_price_history` stores full historical 1-hour OHLCV rows using the primary key `(symbol, timestamp, source)`.
  - Hourly refresh checks the latest cached timestamp per symbol before fetching. Symbols with no hourly cache get a full `730d` yfinance backfill; current symbols fetch a smaller recent window and only insert new bars.
  - Existing daily-only `price_history` tables are migrated by adding `interval='1d'` and extending the primary key when MySQL cache initializes.
  - Added `intraday_price_history` table helpers: save, load, and 7-day prune.
  - `Refresh MySQL Cache` refreshes both daily and 1-hour history. Daily rows continue to power scanner metrics and RS/TI65 indicators; hourly rows power the Charts tab `1H` timeframe.
  - Dashboard also has a dedicated `Update 1H Data` button that refreshes only `hourly_price_history` and reports the latest cached 1-hour timestamp.
- `tests/test_core_behaviour.py`
  - Unit tests for ORB range, breakout, resampling, KIS intraday row normalization, account-value extraction, and trade-price updates.

## Account-Aware Position Sizing

Trade Plan sizing is now profile-aware:

- Select SIM or PROD.
- Select the configured KIS account.
- Click `Use KIS Account Value`.
- The app fetches a KIS snapshot, reads KRW account value, converts it to USD with the `USD to KRW` input, and updates `Account Size USD`.
- On startup, the dashboard now preloads all configured SIM and PROD account snapshots sequentially and applies the selected Trade Plan account value automatically when the snapshot is available.

Conversion is intentionally explicit. The default `USD to KRW` value is a manual fallback in the UI, not a live FX quote. If the account value is `10,000,000 KRW` and `USD to KRW = 1,388.89`, the sizing account value becomes about `7,200 USD`.

The current account-value priority is:

1. `total_evaluation_krw`
2. `cash_total_krw`
3. `d2_deposit_krw`

## Dashboard Scheduler Design

Use a `QTimer` in `MainWindow`:

- Interval: configurable in Dashboard, default 5 minutes.
- Run only during U.S. regular market hours: 9:30 AM to 4:00 PM New York time.
- Symbols: current `data/watchlist.json`.
- Profile: start with SIM or PROD selector already present in the Dashboard.
- Fetch: request latest 5-minute bars for each watchlist symbol.
- Store: persist normalized bars into a future `intraday_price_history` table or local JSON cache.
- Evaluate: call `evaluate_watchlist_orb_signals()`.
- Display: add Dashboard table columns for symbol, latest price, ORB high, ORB low, breakout, target price, target status, and last update time.
- Trade Plan sync: if the active trade symbol receives a fresh intraday price, update entry and stop-loss fields and recalculate position size.

## Database Proposal

Table: `intraday_price_history`

Columns:

- `symbol`
- `timestamp`
- `interval`: store `5m` for normal chart bars
- `open`, `high`, `low`, `close`, `volume`
- `source`: currently `yfinance`; switch to `KIS` after the KIS minute endpoint is confirmed
- `updated_at`

Primary key: `(symbol, timestamp, interval, source)`.

## KIS Endpoint Status

Daily overseas prices are already working through `src/api/kis_fetch_all_daily.py`.

For ORB, confirm the KIS overseas intraday/minute endpoint and response fields before enabling live KIS polling. Keep all raw KIS endpoint details inside `src/api/kis_intraday.py`.

The current Intraday Charts tab is not using KIS intraday data yet. It uses yfinance 5-minute bars, saves them into MySQL for fast reload, and prunes data older than 7 days. This is a temporary source until the exact KIS overseas minute endpoint/TR_ID is confirmed.

## Workload Reduction

To keep intraday charts light:

- Only fetch symbols in the watchlist.
- Store raw `5m` bars for normal charting; calculate `30m` and `1h` locally by resampling.
- Track first-minute high/low separately for the `1m` ORB reference when the KIS minute endpoint is confirmed.
- Keep only 7 days in `intraday_price_history`.
- Prefetch intraday data when a symbol is added to the watchlist.
- Delete intraday rows when a symbol is removed from the watchlist.
- On first load or missing cache, fetch the full requested window up to 7 days.
- On refresh with adequate cache, render immediately from DB and fetch only the latest 1 day in the background.
- Use `Update Watchlist Intraday` before market monitoring to prepare all watchlist caches.
- During polling, fetch only the newest 5-minute bars and upsert them instead of redownloading all symbols.

Data source choice:

- `yfinance`: easiest now, already works, suitable for chart/cache bootstrap, but less ideal for trading-critical monitoring.
- `KIS`: better long-term because it matches the broker account/API, but we need the confirmed overseas minute endpoint/TR_ID before switching.

Use yfinance as a temporary cache bootstrap and switch the `source` to `KIS` once `KisIntradayClient.fetch_overseas_1m()` is implemented.

If KIS only provides delayed/free overseas quotes for REST minute data, the scheduler can still use it for monitoring and ORB context, but it should not be treated as guaranteed real-time execution data.

Current live toggle behavior:

- Off by default.
- When enabled, starts a `QTimer` and runs one immediate refresh if the U.S. market is open.
- If outside regular U.S. market hours, it waits without fetching.
- If a refresh is already running, it skips the tick instead of starting another worker.
- The current source is still yfinance 5-minute cache because the KIS overseas intraday endpoint is not confirmed yet.

## Next Steps

1. Confirm KIS overseas 1-minute endpoint/TR_ID and response fields with a live test.
2. Implement `KisIntradayClient.fetch_overseas_1m()`.
3. Wire the 5-minute scheduler to append only missing bars after the latest cached timestamp.
4. Add Dashboard ORB monitor controls: start/stop, ORB window selector, profile selector, status table.
5. Add alert logging when `target_met` or ORB breakout changes from false to true.
