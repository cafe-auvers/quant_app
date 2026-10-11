# Breakout Lab on localhost

Double-click `Launch_Breakout_Lab.bat`, or run `python scripts/run_replay.py`.
Open <http://localhost:8765>. This independent research server binds to loopback
and does not start a broker or the trading engine. MySQL uses the existing
repository configuration. The main desktop app can continue running separately.

## Daily workflow

1. Select any prepared New York session from January 1, 2024 onward and load
   Scanner 2, the main study scanner. Holidays move to the next available session,
   within seven days. The preparation indicator shows the ready date range.
2. Daily data is read from local MySQL first. Saved bars have no expiry and
   remain usable after restarting the app, without Yahoo access. Existing daily
   cache data is also reused even if it predates the replay import manifest.
   A historical scan with its session and indicator warmup already stored does
   not refresh Yahoo when today's date advances. Yahoo is a fallback for missing
   local dates or missing warmup, in batches of up to 40 symbols. Initial missing
   history can include 2024 warmup and data through the latest completed session.
   Newer requested sessions fetch only the missing suffix; an isolated missing
   session fetches only that day. There is no seven-day overlap download.
   Downloads are filtered to their missing date range before saving, so existing
   bars are preserved. Empty or failed ranges wait six hours before retrying;
   this retry timer never expires successfully saved bars. Failed extensions
   preserve the completed import manifest and all earlier data. Symbols lacking
   a valid bar on the selected session are omitted from the eligible population.
3. Select a matching stock. Replay resumes at the first unreviewed session.
   Left/right buttons or arrow keys move one stock session at a time. Right
   records a pass if there is no decision, then reveals one new daily candle.
   Choose **Backtesting** to hide daily candles, later saved annotations, and
   intraday dates after the selected replay day. **Historical study** shows all
   available daily candles and saved observations, and allows any available
   intraday session. Its **Selected day** picker changes the day's metrics,
   observations and paper balance; a blue chart guide marks that date. Historical
   arrows and date jumps do not automatically mark days as reviewed. Switching
   modes preserves the selected day, saved work and independent trade studies.
   Your mode choice is saved per session and reused when selecting another stock.
   Viewing all history is logged as a reveal, so switching back to Backtesting
   does not relabel later observations as blind. Blank future space is still
   available for drawing in both modes.
4. Use **Expect a breakout** (`E`) to predict the **next** session before seeing
   it. In Backtesting, advance to that day to inspect its intraday candles.
   In Historical study, **Study expected entry - 5m** opens that next session
   directly. The full intraday session is shown at once. Later edits are logged
   as retrospective revisions. After you
   advance the daily replay to the target day, mark **Actual breakout** or
   **No breakout** as a separate assessment.
5. Classify the shape, write observations, draw a resistance level or trend line,
   or click two points with **Measure %**. Save observations before leaving;
   navigation also saves pending observations. `S` saves a chart packet.
6. Open **My database** for hindsight research, linked expected-day outcomes,
   snapshots, and CSV export. This view intentionally includes all saved results.

Coverage is keyed by `(symbol, trading session)`, independent of scanner run.
**Reviewed date** means the selected scan session has a recorded decision.
**Complete** means every cached session from that scan date to that stock's latest
session has been reviewed. Newly imported sessions can make a formerly complete
replay incomplete. A cursor jump or a high-water mark alone does not count as
reviewing missing sessions.

## Scanner presets

**Scanner 1** retains its original rules and calculations. **Scanner 2** is the
leadership-study preset: ADR >= 2.4%, average dollar volume over 20 sessions >=
$3 million, and a percentile >= 98 in any of the 21-, 63-, or 126-session returns.
Its ADR is `100 * mean(High / Low - 1)` over 20 complete sessions. ADR >= 5% is
tagged **Fast**, without excluding slower qualifying stocks. TI65 and relative
strength versus SPY remain display metrics, and consolidation is reviewed manually.
Scanner2 ranks use average ranks for ties and exclude insufficient histories per
timeframe; a recent listing can qualify through its 1-month return. Percentiles
are calculated before liquidity or ADR filters. The liquidity and ADR thresholds
are study choices; the 1/3/6-month leadership scan follows Qullamaggie's published
breakout guide.

Choose the preset above the scan date. Saved scans retain their setup name and
rules; old scans remain Scanner 1. Select **Recent scans** to load a saved queue;
startup loads the scanner choices without automatically loading an old queue.
The table and daily/intraday study show the
selected preset's ADR formula. In the desktop Quant App, choose **Setup 2** in
the Scanner tab. It uses the same persisted rules and stores the added metrics
in MySQL. The first desktop scanner2 run automatically rebuilds an older metric
snapshot from local daily bars, without downloading Yahoo data. Later runs reuse
the snapshot while its inputs are unchanged. An older snapshot keeps its original
session date and freshness label. Restart an already-running Quant App to load
the updated code. The old built-in Setup 2 rules are upgraded on loading; custom
rules are preserved. Scanner 1's legacy fields are unchanged.

## Background preparation and fast loading

The installed pipeline runs in this order:

1. **PC daily history:** `QuantApp_ReplayDailyHistory` calls the existing
   `refresh_universe_history_to_db` entry point with `study_start_date`.
   It shares the original daily refresh's lock, batch Yahoo downloader and
   `price_history` storage. Study history begins January 2023 to supply indicator
   warmup for January 2024. Only missing prefixes or suffixes are downloaded;
   responses are filtered before writing so saved dates remain intact.
2. **Laptop study preparation:** `QuantApp_ReplayStudyPreparation` waits for
   the PC history phase, then prepares every available study session for Scanner 2.
   Cross-sectional 1/3/6-month ranks use the full eligible universe for each date.
   Daily candles, moving averages, relative strength, TI65 and session metrics
   are prepared for each stock, through the latest completed session.
3. **Interactive reads:** choosing a date loads its saved scan. Choosing a
   stock reads the prepared chart file and metrics. Review coverage uses saved
   session calendars instead of counting raw price rows. These actions do not
   download Yahoo data or run the scanner again. Intraday remains on demand.
   MySQL compresses session calendars before transferring them to the laptop,
   reducing the scan queue's network payload while retaining exact review counts.

Both hidden jobs check for updates every 15 minutes and resume at Windows login.
The PC job is deployed separately under
`%LOCALAPPDATA%\quant_app\replay_maintenance`; it uses the PC's existing
configuration and Python environment. The existing desktop app can keep running.
Successfully fetched ranges have no retry expiry; unavailable prefixes retry
after 30 days and failed requests after six hours. When data and rules are
unchanged, preparation skips chart and scan calculations. A new suffix prepares
only new scan dates; earlier-history additions, rules changes and revisions to
the saved endpoint rebuild affected historical results. For a manual correction
to an older bar without an endpoint update, run preparation with `--force`.

The first full preparation takes longer than the two-second interactive target.
Until a date has been prepared, the dashboard reports progress and does not launch
a duplicate whole-universe scan. Subsequent loading reads compact indexed records
and compressed chart files. The two-second target should be measured after the
first preparation completes; `data/breakout_replay/performance.json` records local
HTTP checks, which do not include browser rendering time.
After publishing, the laptop automatically checks the first, middle and latest
prepared dates through the local scan API and loads a cold chart archive for
each. Results are saved to `data/breakout_replay/performance_ready.json`. This
creates saved scan queues without recording reviews, reveals or paper trades.
The report states whether the measured loads meet two seconds; browser rendering
and session annotations are outside this automatic check.

Laptop logs: `data/logs/replay_prepare.log` and `replay_prepare.err.log`.
PC logs: the isolated deployment's `data/logs/replay_history.log` and
`replay_history.err.log`. The dashboard polls preparation progress independently
of scan loading. Prepared files are under `data/breakout_replay/prepared/`, backed
by `breakout_replay_prepared_daily`, `breakout_replay_prepared_scans` and
`breakout_replay_preparation` in MySQL. Clearing the browser does not lose them.

Manual laptop launch: `scripts/start_replay_preparation.ps1 -Role prepare`.
Manual PC launch uses the deployed script with `-Role history -DailyRefreshRoot`
pointing to the existing PC checkout. `-InstallStartupTask` installs the
corresponding login task. Do not run the history role on the laptop.

## Historical intraday data and free plans

Your Alpaca connection was verified against AAPL on January 2, 2025: all 78
regular-session five-minute candles were returned using `feed=sip`.

Alpaca's Basic plan permits **200 historical requests/minute**. This workspace
shares one rolling limiter across concurrent requests and pagination, capped at
**180/minute** to reserve headroom. Cached sessions consume no provider requests.
The runtime override can lower the rate; values above 180 are clamped. Massive's
request spacing similarly cannot be configured faster than its free allowance.
Other independent apps using the same key have their own combined account usage.
An HTTP 429 is surfaced without switching to a paid feed.

The free plan can query historical consolidated SIP bars if the end time is at
least 15 minutes old. This app only requests completed sessions. It explicitly
sets `feed=sip`; it does not silently substitute the single-exchange IEX feed.
Paper-account keys use `https://data.alpaca.markets`, even when the configured
account endpoint is `https://paper-api.alpaca.markets/v2`.

Intraday **Auto** checks the permanent local archive before selecting an online
provider. When several sources are saved, it uses the source with the most bars;
ties prefer Alpaca SIP, then Massive, then Yahoo. It never combines their candles.
Explicit source selection also reads saved bars before checking credentials.
Changing API keys or losing connectivity does not invalidate archived sessions.
Concurrent requests for the same missing symbol/day share the first fetch.
The historical-data check also reuses its saved AAPL session and explicitly
reports when it verifies the local archive rather than making a provider request.

Store credentials only in `.env`:

```dotenv
ALPACA_API_KEY_ID=
ALPACA_API_SECRET_KEY=
# Optional alternative:
MASSIVE_API_KEY=
```

The supplied `alapaca_key` / `alpaca_secret` entries were normalized to these
credential names without changing values. The supplied public `alpaca_endpoint`
was moved to `ALPACA_ENDPOINT` in the gitignored runtime override. The existing
credential synchronizer preserves the keys and continues to blank MySQL values
in `.env.pc`. No credentials are sent to the browser or exported with research.

Massive Stocks Basic is another free option: consolidated minute aggregates,
two years of history, five API calls/minute. Its connector spaces requests by
13 seconds. That history window moves over time. Yahoo supports recent 5m
sessions without credentials; **30m has the same historical restriction** and
does not recover January 2025.

Provider documentation checked October 9, 2026:

- [Alpaca plans and request limits](https://docs.alpaca.markets/us/docs/about-market-data-api)
- [Alpaca free historical SIP access](https://docs.alpaca.markets/us/docs/market-data-faq)
- [Alpaca bars and pagination](https://docs.alpaca.markets/us/reference/stockbars)
- [Massive Basic plan](https://massive.com/pricing?product=stocks)
- [Yahoo intraday restrictions](https://github.com/ranaroussi/yfinance/blob/main/yfinance/multi.py)

## Charts and ORB

The chart follows the existing dark green design: teal/red candles, yellow
SMA 10, cyan SMA 20, and purple SMA 50. The larger relative-strength pane
includes its 50-session SMA and SPY zero baseline. Green/red circles mark
daily moves of at least +4% / at most -4%; labels thin out when zoomed out,
while the circles remain. Hover a daily candle for its exact percentage move.
The background is green when TI65 is above 1.05, red below 0.95, and clear
otherwise. All calculations use only revealed sessions.

**Focus chart** (`F`) expands the price and relative panes across the window.
TI65 remains in the metrics and background shading; its separate mini-chart
returns with **Restore workspace**. Expectation/pass buttons remain available
in focus mode. **Record details** returns to the observations panel. The badge
uses scanner1's as-of-close trend intensity; it does not claim historical
market/sector alignment data.

Chart-indicator checks: `node --test tests/js/test_replay_indicators.cjs`.

Charts use the locally bundled **TradingView Lightweight Charts 4.2.3** with
custom drawing and replay controls. Daily candles include volume, 10/20/50 SMAs,
stock/SPY relative performance, TI65 (7-day SMA / 65-day SMA), and ±4% daily
close-to-close markers. Scanner1's trend intensity is its existing EMA50-based
score, a separate measure from TI65. ADR and 21-/63-session returns reuse the
production scanner calculation. Cross-sectional growth ranks are calculated
before scanner filters, over symbols with a bar on the historical scan session.
Daily imports use the production cache's dividend/split-adjusted Yahoo convention.
Intraday providers return unadjusted session OHLC. Their historical prices can
differ after later corporate actions; the source and timeframe stay explicit.

Selecting a prepared stock loads its complete daily chart and session metrics
from the local compressed archive, including all future study sessions. If a
prepared archive is unavailable, the existing MySQL fallback computes indicators
once and prepares metrics for the current session plus the next 50 sessions.
The daily history stays in the server's bounded memory cache. Daily
responses still contain only candles through the replay cursor. Stepping updates
the selected stock's table status directly, without refreshing the entire
scanner queue. Selecting the stock again picks up newly stored local bars.
Paper stop checks also reuse already loaded intraday bars in a bounded memory
cache; selecting an earlier candle does not truncate that cached session.

An intraday session shows all available candles at once, fitted to
the chart width. Horizontal panning and time-axis zoom are disabled; there are
no intraday replay arrows. Resizing the window refits the entire session.
Session timestamps are shown in New York time, including daylight saving time.
The **Entry / chart** selector offers 5 or 30 minutes. The 30-minute view
aggregates the stored five-minute OHLC and volume into half-hour candles;
switching intervals reuses the same local archive. Entries and stops are still
resolved on the underlying five-minute candles. A chart marker retains its
actual fill time, and the execution-candle selector remains explicit about its
five-minute precision. Buys are available once the selected opening window
has finished (the 09:55 candle closes at 10:00 in the 30-minute mode).

**First 5 min** and **First 30 min** show gain from the 09:30 session open to
the respective window close at 09:35 or 10:00. The calculation is
`(window close / session open - 1) * 100`. Each card also shows the peak gain
inside that window, so an early spike remains visible even if it faded by
the close. These percentages exclude the overnight gap. Missing opening bars
show Unavailable; later prices cannot change the opening statistics. The
selected entry window, opening statistics, trade actions and snapshots are
recorded in MySQL. Snapshot filenames and gallery labels use the selected
chart interval.
In Historical study, the **Session** selector lists all available daily dates
for the stock from January 2024 through the latest completed session. In
Backtesting, its dates stop at the selected daily replay day. **Go to date** selects a date
directly; either control automatically loads that day's five-minute chart.
This includes dates beyond the daily replay cursor, so actual entry days can
be studied in hindsight. The daily cursor stays where it was. Opening a later
session records it as revealed. Later changes to earlier observations are
logged as retrospective revisions, while the original decision stays in the
event history. The view
labels this as **Historical study**. Intraday snapshots use the selected
session's historical metrics and record the mode used at capture. Intraday drawings remain
available when studying their session again, including observations added
after seeing later days; their recorded observation dates keep them out of an
earlier blind daily replay.

The ORB entry is the selected opening range's high plus the persisted
`orb_buffer_percent`. The stop is the range's low. The 5-minute range uses the
09:30 candle; the 30-minute range requires all six candles from 09:30 through
09:55. Triggers are checked only after that opening range ends.
**Stop % / prior-session ADR %** is checked
against the user's persisted normal Stop/ADR bounds; capital is excluded.
An **Entry** marker appears at the first post-opening candle that reaches the
ORB level when the stop check passes. A **Stop loss** marker appears at the
first subsequent stop touch; a gap opening at/above entry can also stop in that
same candle. Stop touches before entry are ignored. Failed stop checks or an
unreached ORB level show **No entry**. These are rule-based study events, not
broker executions, and are logged with the intraday evidence and snapshots.
Stop/ADR values above 100% are orange; values above 150% are red. Configured
eligibility bounds remain unchanged. Five-minute OHLC cannot establish price
order inside a candle: if a candle opens below entry and touches both entry and
stop, show an orange **Entry / stop order unclear** marker rather than claim a
definite stop time. Missing opening
bars produce no ORB verdict; gaps are not synthesized. Daily post-session ADR
and a later low of day are never used to improve the entry check.

## Paper trades and NAV

The intraday toolbar includes **Buy**, **Partial sell**, and **Sell all**.
Each independent trade study starts with $10,000 and $100 initial stop risk.
Capital never compounds between stocks or between separate studies of the
same stock. **New $10,000 study** creates a separate saved record, even on the
same entry day. **Saved study** reopens an earlier study's actions and NAV.
A study with an open position follows that position into later days; when it
closes, its result is preserved. The next entry uses a fresh study. Rewinding
to 2024 starts a separate study and leaves 2025 results intact. Studies and
review coverage remain shared across overlapping scanner runs.

Existing cumulative paper accounts remain under **Earlier cumulative account**
in the saved-study selector, available for review. Their actions, account
records and snapshots are preserved. New trades use independent studies.
Select a five-minute candle from the time selector or click it
with Inspect, then set the fill price and stop percentage. Fill prices must
fall within that candle's recorded range. Partial sales default to 50% of the
open position and accept another percentage.

Buys use whole shares and risk at most 1% of current NAV at the stop, subject
to available cash. Additional buys retain the existing stop and use only the
remaining position risk budget. NAV is cash plus the open shares valued at
the selected candle's close. The daily workspace shows the latest reconciled
session-close NAV; the expandable history records fills, realized P&L and
session NAV. Manual trades within a study must follow chronologically.
Earlier candles remain available to inspect its account as it was then;
start a new study to test a different earlier entry. A closed study cannot
compound a new buy into its ending balance.

Every later revealed session checks an open position against its locally
archived, unadjusted five-minute candles. Daily navigation leaves a missing
archive's stop check pending, preserving the last verified NAV. Opening the
intraday study or explicitly checking the paper account loads missing sessions
from the connected provider and saves them locally. A stop touch closes the remaining shares at
the stop price; a gap below the stop fills at the opening price. Gap losses can
exceed the planned 1% risk. If entry and stop occur within the same candle and
their order is unknown, the simulator assumes a stop conservatively and shows
a warning. Existing stops are checked before manual actions on that candle.
Missing sessions leave the check pending and block later trades; incomplete
coverage is labeled and uses only available candles.

Paper fills have Buy, Partial sell, Sell all and Stop markers. The orange
**Paper stop** line is independent of the ORB study's entry/stop levels and
eligibility test. The ORB cards retain their explicit ORB labels. Accounts,
manual actions, derived fills and stop events persist in MySQL; intraday
snapshots include the session account summary and fills. The simulator is
long-only, cash-funded and uses no fees, slippage or corporate-action position
adjustments. Its five-minute OHLC fills are study assumptions.

## Persistence and replay cutoffs

`breakout_replay_*` tables store scans, results, per-day reviews, replay sessions,
maximum revealed dates, drawings, snapshot metadata, import manifests, missing
date-range request records, intraday archives, and an append-only event log. Daily backfill uses the existing Yahoo
`price_history` cache and its bulk upsert helper. Intraday bars remain in their
own permanent archive, independent of the live cache's pruning policy. KIS bars
are not substituted for Yahoo/Alpaca/Massive bars.

Snapshots are PNGs under
`data/breakout_replay/snapshots/SYMBOL/YYYY-MM-DD/`, named
`SYMBOL_DATE_TIMEFRAME_DECISION_ID.png`. The matching database row stores the
replay cutoff, view mode, visible history end, metrics, and review. Historical
captures add `_historical` to the filename. Daily packets include RS and TI65 panels,
drawings, and observations. The Library contains direct links to stored files.

In Backtesting, the backend truncates daily prices and indicators at the replay
cursor and rejects future intraday requests before fetching any bars. Historical
study returns the complete locally loaded daily archive. The
chart adds 120 future trading-date placeholders without OHLC or indicators,
so lines, levels and measurements can extend into future space. Drawing dates
do not reveal prices or advance the replay. Drawings retain their creation date
and the furthest session already observed.

Right-click a daily chart date and choose **Check intraday** to study it without
moving the replay cursor. The same menu offers **Mark expected breakout**.
The observations panel also provides **Mark a specific expected day**. A passed
day can be revised after later sessions were seen. Original decisions remain in
the audit events, and revised records carry a **Retrospective** label and the
observation date. The ordinary expectation button still records the next
session. Review exports include whether the observation was blind or revised.

Later outcome assessments record their
as-of session and are hidden in Backtesting when rewinding before that assessment. The workspace
cannot erase a pattern the user has already seen; revisited days are labeled.
Unreviewed earlier dates may still be passed so an overlapping scan is navigable;
these records identify the later observation date.
Intraday annotations and snapshots retain both their chart date and the daily
replay date when they were created, so later notes on an older session stay hidden.
Intraday drawings also record the last visible candle when created and remain
hidden when stepping back before that candle. Both chart toolbars offer undo.

The universe is all locally cached symbols. It is not a reconstructed historical
exchange listing: missing delisted stocks and retrospectively adjusted Yahoo
prices limit point-in-time accuracy. Company fundamentals and capital tests are
outside this practice workflow. The scanner rules were retained; the reference
library informed setup-shape labels and first-leg/breakout percentage drawing.

## Validation

`pytest tests/test_breakout_replay.py tests/test_replay_paper.py -q` covers causal metrics, percentile
population, per-session coverage, retrospective revisions, deferred assessments,
full intraday sessions and archive reuse, ORB entries/stops/no-entry states, free-plan request
limits and pagination, named snapshots, and localhost mutation checks. Paper
checks cover risk sizing, partial/full exits, gap and subsequent-day stops,
missing data, chronological replay, idempotent requests, saved-account
recovery, independent studies, preserved earlier accounts, 2024 replay,
future-space drawings and selected-study snapshot evidence. Chart checks use
`node --test tests/js/test_replay_indicators.cjs`.
