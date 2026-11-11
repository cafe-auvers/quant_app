# Manual-position safety risk: 1% of NAV

The user's 2026-11-12 instruction reduces the initial automatic safety stop
for a manual purchase from 2% to **1% of fresh USD account NAV per stock**.
This supersedes the stop budget in the dated 2026-11-11 manual-stop and NAV
profit-exit notes. It does not change the profit objective.

```text
risk dollars = fresh USD account NAV * 0.01
stop price = average purchase cost - risk dollars / full held share count
```

At $11,000 NAV, the intended initial gross risk is $110. For 100 shares bought
at $10, the initial stop is $8.90. Tick rounding moves the stop up so it cannot
increase that budget. The supported minimum price still applies when position
value is smaller than the budget.

`MANUAL_POSITION_DEFAULT_NAV_RISK_PERCENT` defaults to `1` in tracked runtime
configuration and in the Python fallback. A workstation override is still
supported. Deployment explicitly selects `1` for the PC executor.

An existing or pending stop, including a user edit, remains unchanged. Automated
entries keep their ORB/strategy stops. New manual holdings without stops and
existing unprotected manual holdings receive the new default after fresh,
complete account reconciliation. The installed price stays fixed across
restarts, NAV changes and top-ups; additional shares can increase dollar risk.

All positions still request a single half-sale at whole-position gross profit
of **2% of enrollment NAV**, then move the remaining shares to cost-adjusted
breakeven only after confirmed fills. At $11,000 NAV, that trigger remains
$220; a half-sale at that price realizes about $110 before fees and slippage.

The initial stop uses the guarded, application-managed stop workflow and the
supported exit sessions described in
[Extended-hours exits](extended_hours_exits_2026-11-11.md). The PC must be
running, Live Trading enabled, and market data and execution ready. Gaps,
halts, liquidity, fees and actual execution can produce losses above 1% NAV.
