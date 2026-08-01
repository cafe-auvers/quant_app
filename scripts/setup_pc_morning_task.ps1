<#
Run this ON THE ALWAYS-ON PC, from a PowerShell prompt opened in the repo
root, logged in as the account that will auto-login (Administrator not
required -- this registers a task that runs interactively as you, which is
what lets main.py's GUI actually show up).

Registers an "at logon" Task Scheduler task that runs
scripts/pc_morning_routine.ps1 (git sync -> gated 1d/1h refresh -> launch
main.py) every time this account logs in -- i.e. every morning, right after
the BIOS wake + auto-login.

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
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Runs pc_morning_routine.ps1 (git sync, data refresh, launch main.py) at logon." -Force | Out-Null

Write-Host "Registered task '$TaskName' -- fires at logon for $env:COMPUTERNAME\$env:USERNAME."
Write-Host ""
Write-Host "-- Reference commands -----------------------------------------------------"
Write-Host "Run it right now (don't wait for a logon) : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "View status                                : Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Routine's own log                          : $RepoRoot\data\logs\pc_morning_routine.log"
Write-Host "Disable                                    : Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "Delete                                      : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
