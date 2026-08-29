# User Workflow

The normal workflow narrows candidates before any execution intent exists.

1. Refresh or inspect cached market data and Health.
2. Run a scanner setup and review result metrics.
3. Open daily/hourly/intraday charts and set a structural `breakout_price`.
4. Add the symbol to passive Watchlist planning.
5. Promote it to Buylist when planning is ready.
6. Review the 24 ORB combinations or run the optimized pre-market selector.
7. Activate the selected plan for Buy Today.
8. Let a fresh post-range KIS trade above both ORB high and breakout confirm the
   plan; the runtime then submits a passive limit at the configured execution
   price while the market remains above it.
9. Treat board gestures, breakout confirmation, and broker acceptance as
   distinct from a fill.
10. Let the runtime and broker reconciliation own pending/open/closed lifecycle
   changes.
11. Monitor position, partial-exit, stop, and final-exit evidence in Buy Board,
    Health, and the event journal.

During regular market hours, non-execution-owner planning changes are locked;
authorized intervention commands are routed through the operator command path.
Published plan immutability and account ownership remain enforced.

No fixed profit target is used for active ORB management. Confirmation is a
fresh trade strictly above `max(orb_high, breakout_price)`; the passive order
rests at ORB high by default and fills only on broker evidence. A later,
strictly higher-scoring ORB may replace a completely unfilled working order
through strict cancel-then-replace. Exits follow the rulebook and
broker-confirmed state.

See [Current Order Logic](https://github.com/cafe-auvers/quant_app/blob/master/docs/current_order_logic.md).
