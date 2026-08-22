# Background Jobs and Automation

## In-app workers and timers

- Database/coordination initialization and recovery probes
- Scanner and market-cache status workers
- Market Pulse refresh
- Intraday single/bulk fetch
- KIS account, order query/cancel, and reconciliation workers
- Buy Board projection/command/runtime workers
- Health probe and P&L preparation
- State save/sync and local mirror copy workers
- Runtime heartbeat, readiness, and PC status timers

Workers coalesce or reject duplicate work where defined. Shutdown requests
interruption, drains tracked workers within bounds, flushes state, fences
writers, and preserves unresolved order evidence.

## Standalone market refresh

`historical.py` and `scripts/run_daily_refresh.py` own long 1D/1H refresh work
outside the desktop process. Status/lock files prevent duplicate refresh
processes and support termination/recovery reporting.

## Windows PC automation

- `pc_morning_routine.ps1`: update/environment/dependency/data routine
- `setup_pc_morning_task.ps1`: scheduled wake/run registration
- `Configure-MarketHoursWake.ps1`: evening market-hours wake
- `Configure-AutomaticSleep.ps1` and `Invoke-GuardedSleep.ps1`: readiness-gated
  sleep
- guarded shutdown scripts and remote status listener

Physical S3 wake/resume and credentialed post-resume recovery remain operational
validation tasks. Test them with Live Trading off before relying on unattended
operation.
