# Intraday monitor manual review

The connected web monitor runs every 60 seconds during the NYSE regular
session even with no browser open. Outside regular hours it refreshes while
a client is active. Yahoo outages retain the existing bounded backoff.

Early today is an advisory review list. It records a fresh ORB price,
position and opening-liquidity pass when price is at or above breakout,
above the ORB low, within 0.5 ADR of the planned entry and not more than
0.5 ADR above VWAP. It does not require the stricter Strong label. The
list retains an observed early review after conditions change or price
extends. It never activates Buy Today or submits an order. The existing
explicit publication controls and execution checks remain mandatory.

Each row shows first observed ORB and early-review times in New York time.
Strong now filters on the current condition. Strong today lists stocks that
qualified Strong at least once this New York session at their current saved breakout
level, even when they later become Extended, Weak or stale. It sorts by first
Strong time. The First seen column shows that time when one has been recorded;
otherwise it shows the first ORB observation. No early-review pass is treated
as proof of a Strong condition. The list also includes verified reconstructed
readings from before the current monitor startup, labelled Reconstructed.
Condition details show first Strong, first Extended and observed transitions.
These records persist in `monitor_history.sqlite3` in the private web data
directory. They are scoped by New York session, symbol and breakout price.
Live recording starts when monitoring runs. A separate background worker checks
the earlier regular session using already cached minute and aggregate bars,
SPY and the actual sector ETF, daily ADR, the saved breakout and ORB settings,
and an explicitly captured account-equity basis. It does not download data or
submit planning intent. A Strong result requires the same full-data condition
checks as the live monitor. Aggregate proof cannot be used before its candle
closes, and a repair cannot make a stale quote appear newer. Candidate search
may carry a close to find possible minutes; only the complete evaluator can
establish a reading. Incomplete candidates never become reconstructed Strong.

Reconstructed first/last times, intervals, metric evidence and input fingerprints
persist separately in `monitor_strong_replays`, preserving the actual live
observation log. Changed source evidence or settings trigger recalculation.
Historical edits to levels/settings and actual earlier screen labels are
unknown; the UI states those assumptions. A changed breakout or New York
session starts a separate history. Missing historical evidence remains unknown.
Unqualified history is revisited at 15-minute boundaries as cached evidence
arrives. Once a reconstructed Strong reading is found, later live observations
extend the day's record; revisions to its earlier input evidence still trigger
recalculation. A live Strong observation bounds the earlier replay search.

Sparse advisory minute data is checked against completed Yahoo 5-minute
and hourly candles. Zero-volume minutes are restored only when aggregate
volume and OHLC reconcile exactly, with a same-session prior price. A
repair cannot invent an opening price, consume an unfinished aggregate,
allocate unexplained positive volume or create a newer quote timestamp.
Checks are cached, capped at 24 symbols per interval per refresh, and
failed symbols wait 15 minutes before another aggregate request.

Unresolved missing closes can produce a marked `~` current ER estimate
using only reported closes, a valid regular-session open and endpoint,
and at least five reported closes. Its coverage and missing count remain
explicit. Unknown movement may make this estimate look smoother than the
complete path. Estimated data cannot satisfy the full-data Strong or Leading
classification. Invalid prices or an unavailable opening/endpoint remain
unavailable. The execution-grade provider does not consume these repairs or
estimates.
