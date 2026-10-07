# Opening liquidity and mobile workflow — 2026-10-08

This release combines the configurable opening-volume minimum, compact mobile
Monitor, dedicated Settings page, and durable ORB entry-generation protection.
It includes the existing EP publication and cancellation fixes in the source
history. Deployment authorization is the operator's instruction to update all
Markdown and deploy all changes after reporting no open positions or Buy Today.

## Minimum opening liquidity

The default minimum is **200 shares per minute**, adjustable alongside the shared
ORB position bounds. It measures shares, not the number of trades. Zero disables
the check. The same shared value applies to the 1m, 5m, and 30m entry windows.

Use existing intraday volume from 09:30 New York time through the most recent
completed opening minute, capped at 10:00. Divide the cumulative volume by all
completed minutes, including empty minutes. At five completed minutes, 1,000
shares passes; at 30 completed minutes, 6,000 shares passes. Earlier windows can
qualify before minute 30. Premarket, unfinished bars, previous sessions, and
volume after 10:00 do not count. This is an average across elapsed opening
minutes, not a minimum for each individual bar.

Missing, invalid, or insufficient evidence blocks new entries and replacements.
Candidate selection and final pre-trade approval enforce the same minimum.
Existing positions and protective sells are unaffected. No extra volume feed,
trade-count API, market-cap classification, or broker polling is added.

## Mobile Monitor and navigation

The Watchlist button opens a compact Monitor covering the union of Watchlist,
Buylist, and Buy Today symbols, including Watchlist-only stocks. Each stock has
one row: **Stock | Today % | Breakout | Best ORB | 1M % | 3M %**, plus the explicit
Buy Today action. The monthly columns use 21 and 63 completed trading sessions.
The existing daily-data batch supplies their baselines; it loads six months of
history once per session. Today % is blank for an older quote date.

Best ORB is the highest-scoring case that passes breakout confirmation, position
bounds, and opening liquidity across 1m/5m/30m windows and eight risk cases.
Stale observed confirmations are marked with an amber asterisk. Viewing a row
does not activate a plan. The + action retains the existing breakout, Operator
Control, and normal/EP publication confirmations. Monitoring refreshes on the
existing one-minute cycle while a client is visible and pauses after inactivity.

The bottom bar always contains **Menu, Watchlist, Chart, Buy Board, and stock
arrows**. The bottom-left hamburger opens **Home** and **Settings** with a gear
icon. Home shows the daily overview and Market Pulse. Settings contains shared
ORB bounds, the volume minimum, Operator Control, scanner setup, and chart
preferences. Only Save shared settings persists ORB edits; switching pages keeps
unsaved values and does not change Operator Control. Existing ownership and
revision checks still apply.

## Entry generation protection

A confirmed, executable candidate takes precedence over an unconfirmed
higher-scoring planning candidate. Durable entry-attempt identity freezes the
selected ORB geometry through submission, cancellation, and reconciliation.
The chosen window is persisted before broker I/O, so a refresh cannot attach an
order identity or filled stop to a different planning generation.

## Validation and deployment

Run the full tests under the locked Python environment, the mobile Playwright
regressions, repository hygiene, dependency checks, and exact-commit hosted CI.
Use `pytest tests -q` in a workspace containing archived repository copies under
artifacts; broad root collection can collect those copies accidentally.
Keep pytest's temporary directory outside the repository: Gate-4 journal tests
deliberately enforce that operational evidence cannot live inside a checkout.

Before restarting, obtain fresh read-only broker and canonical proof of zero
positions, working or unresolved orders, reservations, and Buy Today cards.
Back up canonical and local web state, then close the desktop normally before
changing source. Preserve private credentials, persisted plans, and customized
risk values. Deploy one immutable clean commit to the PC desktop and private web
checkout, restart their existing scheduled tasks, and verify the served asset
hashes over local HTTP and phone HTTPS.

Deployment does not authorize a new trading session. Keep Live Trading OFF;
exact-release capability review and the existing execution gates must pass
before later arming. Do not relabel old protocol or session evidence as a new
independent approval. Historical qualification reports and dated incident
documents retain their original scope. Deployment evidence belongs in the
gitignored `artifacts/opening_liquidity_deploy_20261008` bundle and the equivalent
PC evidence directory, without committing private state.
