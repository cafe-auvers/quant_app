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
Strong now filters on the current condition. Strong today lists stocks observed
Strong at least once this New York session at their current saved breakout
level, even when they later become Extended, Weak or stale. It sorts by first
Strong time. The First seen column shows that time when one has been recorded;
otherwise it shows the first ORB observation. No early-review pass is treated
as proof of a Strong condition.
Condition details show first Strong, first Extended and observed transitions.
These records persist in `monitor_history.sqlite3` in the private web data
directory. They are scoped by New York session, symbol and breakout price.
Recording starts when monitoring runs; earlier signals are not backdated or
silently reconstructed. A changed breakout starts a separate history.

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
