# User Workflow

The normal workflow narrows candidates before any execution intent exists.

1. Refresh or inspect cached market data and Health.
2. Run a scanner setup and review result metrics.
3. Open daily/hourly/intraday charts and set a structural `breakout_price`.
4. Add the symbol to passive Watchlist planning.
5. Promote it to Buylist when planning is ready.
6. Review the 24 ORB combinations or run the optimized pre-market selector.
7. Activate the selected plan for Buy Today.
8. Treat board gestures as revision-fenced requests, not fills.
9. Let the runtime and broker reconciliation own pending/open/closed lifecycle
   changes.
10. Monitor position, partial-exit, stop, and final-exit evidence in Buy Board,
    Health, and the event journal.

During regular market hours, non-execution-owner planning changes are locked;
authorized intervention commands are routed through the operator command path.
Published plan immutability and account ownership remain enforced.

No fixed profit target is used for active ORB management. The trigger is the
higher of ORB high and buffered structural breakout; exits follow the rulebook
and broker-confirmed state.
