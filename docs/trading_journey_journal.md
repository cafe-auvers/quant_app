# Trading day journal: comments in chat, draft, confirmation, posting

The user shares simple thoughts about the trading day **here in chat**. The
assistant turns those comments into one journal entry and adds daily and
5-minute charts for the stocks bought that day. The user reviews it and confirms
before publication. Social platforms will be chosen after the first draft.

## The daily workflow

1. The app collects confirmed fills, the recorded ORB selection, chart snapshots,
   account-equity observations and daily benchmark/macro movements in the background.
2. The user comments in chat: how the day went, what they noticed, why they acted,
   or anything they want recorded. There is no required writing form.
3. The assistant checks the New York trading session, reads its recorded facts,
   and writes a plain first-person entry from those comments.
4. The assistant attaches the purchased stocks' daily and 5-minute charts and
   shows the complete draft for review.
5. The user requests changes or confirms the finished entry. The social
   destinations are also selected before posting.
6. The assistant posts the confirmed version through the chosen connected
   accounts. A saved draft, local approval, or chart packet is not a posted entry.
   Posting links or receipts should be reported after actual publication.

Use few adjectives. Keep the user's meaning and voice. Do not add motivations,
emotions, lessons, or performance claims they did not provide. Recorded fill/ORB
facts can be added as facts; distinguish them from personal interpretation.
Include losing trades when discussed. A day with no purchases can still have a
journal entry.

## The app's role

The **Journal** tab is a day view of purchase charts and saved journal drafts.
It does not ask the user to write separate fields for every trade.

- **Refresh day data** collects the available trade facts, charts and market context.
- **Prepare day for chat** creates a packet of the selected session's purchase
  facts, NAV/P&L and market context, daily/5-minute chart files, and the journal-writing instructions.
- **Open saved journal** opens the chat-written entry with embedded charts.

Buy and sell facts are retained in the packet, but the attached graph pairs
cover stocks **bought on that trading day**. End-of-day views are used when
available; otherwise the captured entry-day view is labelled. One pair of graphs
per purchased stock is included, with all recorded buy orders kept as facts.

There is no in-app AI writer or automatic promotional text generator.
The journal is drafted by the assistant in the chat, using the user's comments.

## Daily NAV, P&L and market context

The journal records existing successful broker account-equity reads in USD.
Each account/environment stays separate, including on days with no trades.
The latest regular-session observation is retained; the first fresh broker read
within one hour after the close is frozen as the post-close observation. Existing
runtime account refreshes supply these reads automatically. If that runtime is
not running, refresh the Dashboard account after the close to collect it.
These are observed account values, not verified official closing NAV statements.
The existing account total may include KRW cash, Korean holdings and FX movement;
it is not performance attributable solely to the Qullamaggie method. Reconcile
the figures, including liabilities, with broker statements for a public track record.

Daily P&L is current post-close account equity minus the **previous NYSE session's**
post-close equity minus net external cash flows. Deposits are positive and
withdrawals negative. Missing previous-session NAV is not replaced by an older
day. Cash flows are initially unknown, not assumed zero. The assistant can use
`confirm_journal_cash_flow(store, session_date, account_key=..., net_external_flow_usd=...,
confirmation=...)` in `src/services/journal_context.py` when the user states the
cash-flow facts in chat, including an explicit zero. This applies to the period
between the two recorded NAV observations and does not approve publication.
Until then the journal shows NAV change separately, with daily P&L pending.

The displayed return is daily P&L divided by previous post-close equity. It is
not a time-weighted return when intraday deposits or withdrawals occur. Fees,
dividends and FX already affecting account equity are included through the
account-value change, without adding them twice. This is separate from the
existing Health chart's cumulative realized/unrealized P&L history.

The default daily market table includes:

- SPY (S&P 500), QQQ (Nasdaq-100), IWB (**Russell 1000**, as requested), IWM
  (Russell 2000), RSP (equal-weight S&P 500) and SMH (semiconductors).
- VIX level and its changes in points and percent.
- Federal Reserve 2-year and 10-year Treasury yields via FRED, with changes in
  **basis points**, not percentage returns.
- TLT Treasury bond prices, UUP dollar proxy, GLD gold proxy and USO oil-futures proxy.

ETF/fund changes use unadjusted daily market-price closes, excluding dividend
reinvestment. They are labelled proxies rather than exact underlying index,
spot commodity or dollar-index levels. Both the chosen session and previous NYSE
session must be present for a daily change; a published level can still be shown
with the daily change unavailable. Delayed rates, bond-market holidays and missing
data are labelled accordingly. Complete values are frozen with their dates, source and
capture time. Missing feeds retry hourly, and manual refresh retries immediately.
The journal shows the account return minus SPY/QQQ/IWB changes in **percentage
points** when both sides are available; this is a daily comparison, not risk-adjusted alpha.

Movement is recorded as context. Do not infer that a macro move caused a trade's
result without evidence. Add event explanations from the user's comments or
verified dated sources rather than inventing them from price changes.

## Capture and chart limits

Automatic capture runs every 60 seconds while the desktop is open. Only
confirmed filled quantity counts; order acknowledgement alone does not.
Partial fills and cancelled orders with fills are retained. Each order's original
recorded ORB context and fill observations are preserved.

Daily and completed 5-minute bars are archived with source, capture time, raw
OHLCV, and hashes. An end-of-day view is collected after the New York close plus
15 minutes, including recurring early closes. Chart data comes from available
cache data, followed by bounded Yahoo requests if enabled. Failed/missing charts
retry without affecting trading.

The current order schema has no verified exact broker execution timestamp.
Submission and fill-observation timestamps remain labelled as such. Current-day
daily candles can be partial. Missing history stays missing; late recovery is
labelled and is not treated as evidence of what was visible at entry.

## Drafts and confirmation

The assistant uses the functions in src/services/daily_journal.py:

- prepare_daily_journal_packet(store, session_date): prepare factual evidence;
  this does not create a journal.
- save_daily_journal_draft(store, session_date, comments=..., draft=...): save the
  user's original comments and the assistant's finished text, attach the charts,
  and mark the entry AWAITING_CONFIRMATION.
- confirm_daily_journal(...): record the user's explicit confirmation of the
  current revision and the selected social destinations.
- approved_daily_publication(...): return the approved text/charts for a
  publisher. This has no posting side effect.

Never call the confirmation function based on an assumption. Changes to the
draft require new confirmation. Changes to trade facts, NAV/P&L or market context, or selected chart files
invalidate publication of an earlier approval. Previous draft revisions are
retained.

No social platform is connected by this implementation. Choose destinations
after reviewing the first draft, connect their publishing access, and send only
the exact entry the user confirms. Do not mark a local draft as posted.

## Files and backups

The private, gitignored archive is data/trade_journal/:

- journal.sqlite3: trade evidence, daily NAV/cash-flow observations, market context and saved daily entries.
- trades/YYYY-MM-DD/SYMBOL-opaque-id/: order facts and chart snapshots.
- days/YYYY-MM-DD/packets/: facts and graph files prepared for chat.
- days/YYYY-MM-DD/draft.md: the journal text with purchase-chart links.
- days/YYYY-MM-DD/journal.html: a standalone journal with embedded graphs.
- days/YYYY-MM-DD/revisions/: earlier journal revisions.
- days/YYYY-MM-DD/market.json: successfully captured daily benchmark/macro values.

Open journal.html in a browser and use Print / Save as PDF when needed for a
daily collection or later book material. This is a saved journal, not an
automatic publication. User comments are private source material; public text
is the reviewed journal draft.

The archive is local to this computer. Back up the whole directory after closing
the desktop/helper. Existing trading-state cloud backups do not automatically
include this directory. Keep broker statements alongside the journal for
broker-reconciled performance figures. Internal account/order identities are
opaque hashes in the trade archive.

## Runtime and unattended capture

Non-secret settings belong in `config/runtime.local.json`:

```json
{
  "TRADE_JOURNAL_ENABLED": "true",
  "TRADE_JOURNAL_DIR": "data/trade_journal",
  "TRADE_JOURNAL_CAPTURE_SECONDS": "60",
  "TRADE_JOURNAL_CHART_NETWORK_ENABLED": "true"
}
```

The desktop normally supplies automatic chart capture. With the desktop closed,
python scripts/sync_trade_journal.py --watch can run the same read-only evidence
capture, without starting a trading session. Add --no-network for cache-only
charts and previously saved market context. The helper does not fetch broker
account values itself; NAV capture needs existing desktop broker reads.
A scheduled task has not been installed.

Use python scripts/prepare_daily_journal.py --session YYYY-MM-DD to prepare the
chart packet directly for a chat journal. This reads the local archive and does
not contact a social account.

## Validation

Use SQLite and synthetic market data for normal tests. Run the daily-journal,
chart-capture, journal-UI, daily-summary, and main-window smoke tests with pytest's
temporary directory outside the checkout.
