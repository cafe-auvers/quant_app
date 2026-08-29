# Glossary

| Term | Meaning |
|---|---|
| Broker acceptance | KIS accepted the request; not evidence of a fill |
| Breakout confirmation | Fresh post-range KIS trade strictly above both ORB high and structural breakout |
| Breakout price | Persisted daily structural breakout level |
| Buy Today | Pre-entry card with a selected/published plan for the session |
| Canonical state | Authoritative persisted state, not a transient widget |
| Controlled live | Restricted live envelope for canonical active Trade Cards and entry notional |
| Entry Pending | System-owned state for a durable unresolved/submitted entry |
| Execution price | Exact resting BUY-limit price; ORB high by default, never the current market price |
| Execution Owner | Device/process allowed to hold the fenced execution lease |
| Gate 1 | Deterministic simulation certification |
| Gate 2 | Credentialed read-only KIS soak/evidence stage |
| KIS | Korea Investment & Securities API/broker integration |
| Live Trading | Canonical administrative permission; one gate among many |
| Local trading lock | Per-machine `TRADING_ENABLED` permission; never synchronized |
| Local mirror | Pull-only laptop SQLite market-data copy |
| Mutation budget | Submit/cancel/replace capacity and spacing control |
| Operator Control | Device allowed to issue the next human command |
| ORB | Opening Range Breakout strategy |
| ORB generation | Immutable timeframe, ORH/ORL, score, execution price, and order identity for one submitted attempt |
| Passive-pullback entry | Submit below the current market after breakout confirmation, then wait for a pullback and broker fill evidence |
| Reconciliation | Applying broker order/position evidence conservatively |
| Replacement | Strict cancel-confirm-revalidate-submit upgrade to a later, higher-scoring ORB generation |
| Revision fence | Rejects a command based on stale canonical state |
| RS/TI65 | Relative-strength and trend indicators used by charts/scanner |
| Standby ready | Dependencies are healthy and handoff-ready, but this runtime is not the active owner |
| Unknown submission state | Broker outcome is ambiguous; never safe to retry blindly |
| Watchlist | Passive persisted planning stage; no dedicated tab |
