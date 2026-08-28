<#
Run this ON THE ALWAYS-ON PC, from a PowerShell prompt opened in the repo
root, logged in as the account that will auto-login (Administrator not
required -- this registers a task that runs interactively as you, which is
what lets main.py's GUI actually show up).

Registers an "at logon" Task Scheduler task that runs
scripts/pc_morning_routine.ps1 (git sync -> gated 1d/1h refresh -> launch
main.py) every time this account logs in.

As of the S3-sleep automatic-handoff setup (see
Configure-AutomaticSleep.ps1 / Configure-MarketHoursWake.ps1), this also
registers a second, Daily @ 08:00 trigger with WakeToRun enabled -- a normal
S3 resume does NOT fire AtLogOn (the interactive session was never logged
out, just suspended), so without this second trigger the 08:00 morning data
refresh would stop firing on every day except after a genuine reboot. When
main.py survived S3, the routine automatically uses refresh-only resume mode:
no Git/venv mutation and no duplicate app launch. AtLogOn is kept as a
defensive fallback for a genuine reboot, where the full update path is safe.

Usage:
    .\scripts\setup_pc_morning_task.ps1
#>

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$RoutineScript = Join-Path $RepoRoot "scripts\pc_morning_routine.ps1"
if (-not (Test-Path $RoutineScript)) {
    throw "Could not find $RoutineScript -- run this from an up-to-date checkout on the PC."
}

$TaskName = "QuantApp_MorningRoutine"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RoutineScript`"" `
    -WorkingDirectory $RepoRoot
$atLogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\$env:USERNAME"
$dailyWakeTrigger = New-ScheduledTaskTrigger -Daily -At "08:00"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -WakeToRun

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger @($atLogonTrigger, $dailyWakeTrigger) -Settings $settings `
    -Description ("Runs pc_morning_routine.ps1 at logon AND daily at 08:00. " +
        "Cold boot: git/env/dependencies, refresh, launch. S3 resume with main.py " +
        "already running: refresh only, with no checkout mutation or duplicate launch.") -Force | Out-Null

Write-Host "Registered task '$TaskName' -- fires at logon for $env:COMPUTERNAME\$env:USERNAME, and daily at 08:00 (wakes the PC if asleep)."
Write-Host ""
Write-Host "-- Reference commands -----------------------------------------------------"
Write-Host "Run it right now (don't wait for a logon) : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "View status                                : Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Routine's own log                          : $RepoRoot\data\logs\pc_morning_routine.log"
Write-Host "Disable                                    : Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "Delete                                      : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
