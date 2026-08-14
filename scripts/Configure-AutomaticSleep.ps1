<#
.SYNOPSIS
    Creates/updates/removes/enables/disables a Windows Task Scheduler task
    that puts this PC to sleep (S3) at a configured time on configured days.

.DESCRIPTION
    New, separate task/script from Configure-AutomaticShutdown.ps1 /
    Invoke-GuardedShutdown.ps1 (which fully powers the PC off) -- this is
    part of the S3-sleep + Task-Scheduler-WakeToRun model that lets the PC
    also be awake through the US trading session, so the automatic
    laptop<->PC monitoring handoff has something to hand off to. The old
    shutdown task is meant to stay registered but DISABLED as a rollback
    path (see the runbook in docs/pc_sync_data_pipeline.md) rather than be
    deleted outright.

    The scheduled task runs Invoke-GuardedSleep.ps1, which waits for both a
    live historical.py refresh AND a safe cross-machine handoff state (no
    in-flight PROD position/order, no reconciliation in progress) before
    calling Win32 SetSuspendState. Either guard can defer or cancel a given
    sleep cycle; Task Scheduler simply tries again next trigger.

    The task runs as SYSTEM with the highest run level, same as the
    shutdown task, and re-running this script with the same -TaskName
    updates the existing task in place.

.PARAMETER SleepTime
    24-hour "HH:mm" time to sleep, e.g. "10:00".

.PARAMETER DaysOfWeek
    One or more of Monday..Sunday, or "Everyday" for all seven days.

.PARAMETER TaskName
    Scheduled task name. Default "Automatic-PC-Sleep".

.PARAMETER MaxRefreshWaitMinutes
    Passed through to Invoke-GuardedSleep.ps1. Default 180.

.PARAMETER MaxHandoffWaitMinutes
    Passed through to Invoke-GuardedSleep.ps1. Default 30.

.PARAMETER TestMode
    Registers a ONE-TIME sleep ~3 minutes from now (as "<TaskName>-Test")
    instead of the normal weekly trigger, so you can watch the guard/sleep
    flow without waiting for the real scheduled time. It will actually
    suspend the PC unless a guard defers it -- have physical/remote access
    ready to confirm the resume works before relying on this.

.PARAMETER RemoveTask
.PARAMETER DisableTask
.PARAMETER EnableTask

.EXAMPLE
    .\Configure-AutomaticSleep.ps1 -SleepTime 10:00 -DaysOfWeek Everyday

.EXAMPLE
    .\Configure-AutomaticSleep.ps1 -TestMode
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^([01]\d|2[0-3]):([0-5]\d)$')]
    [string]$SleepTime,

    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Everyday")]
    [string[]]$DaysOfWeek,

    [string]$TaskName = "Automatic-PC-Sleep",

    [ValidateRange(0, 1440)]
    [int]$MaxRefreshWaitMinutes = 180,
    [ValidateRange(0, 1440)]
    [int]$MaxHandoffWaitMinutes = 30,

    [switch]$TestMode,
    [switch]$RemoveTask,
    [switch]$DisableTask,
    [switch]$EnableTask
)

$ErrorActionPreference = "Stop"
Import-Module ScheduledTasks -ErrorAction SilentlyContinue

$LogDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogPath = Join-Path $LogDir "sleep-scheduler.log"

function Write-Log {
    param([string]$Message, [ValidateSet("INFO", "WARN", "ERROR")][string]$Level = "INFO")
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Add-Content -Path $LogPath -Value $line
    switch ($Level) {
        "ERROR" { Write-Host $line -ForegroundColor Red }
        "WARN"  { Write-Host $line -ForegroundColor Yellow }
        default { Write-Host $line }
    }
}

$IsElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsElevated) {
    Write-Log "This script must be run from an elevated (Administrator) PowerShell window. Right-click PowerShell -> Run as administrator, then re-run this command." "ERROR"
    exit 1
}

try {
    if ($RemoveTask) {
        $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $existing) {
            Write-Log "No task named '$TaskName' exists -- nothing to remove." "WARN"
            exit 0
        }
        if ($PSCmdlet.ShouldProcess($TaskName, "Remove scheduled task")) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Log "Removed scheduled task '$TaskName'."
        }
        exit 0
    }
    if ($DisableTask) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Log "Disabled scheduled task '$TaskName'."
        exit 0
    }
    if ($EnableTask) {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Log "Enabled scheduled task '$TaskName'."
        exit 0
    }
} catch {
    Write-Log "Failed: $($_.Exception.Message)" "ERROR"
    exit 1
}

$GuardScript = Join-Path $PSScriptRoot "Invoke-GuardedSleep.ps1"
if (-not (Test-Path $GuardScript)) {
    Write-Log "Required sleep guard script is missing: $GuardScript" "ERROR"
    exit 1
}

$guardArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$GuardScript`" -MaxRefreshWaitMinutes $MaxRefreshWaitMinutes -MaxHandoffWaitMinutes $MaxHandoffWaitMinutes"
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $guardArgs
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

if ($TestMode) {
    $testTaskName = "$TaskName-Test"
    $fireAt = (Get-Date).AddMinutes(3)
    $trigger = New-ScheduledTaskTrigger -Once -At $fireAt

    if ($PSCmdlet.ShouldProcess($testTaskName, "Register one-time test sleep task")) {
        Register-ScheduledTask -TaskName $testTaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
            -Description "One-time test run created by Configure-AutomaticSleep.ps1 -TestMode." -Force | Out-Null
        Write-Log "TEST MODE: task '$testTaskName' registered. It will attempt to suspend the PC at $($fireAt.ToString('HH:mm:ss')) (in ~3 minutes) unless a guard defers it. Confirm the PC actually resumes (fans off / blinking LED = true S3) before trusting this." "WARN"
        Write-Log "To remove this test task afterward: Unregister-ScheduledTask -TaskName '$testTaskName' -Confirm:`$false"
    }
    exit 0
}

if (-not $SleepTime) {
    Write-Log "-SleepTime is required (e.g. -SleepTime 10:00) unless using -RemoveTask/-DisableTask/-EnableTask/-TestMode." "ERROR"
    exit 1
}
if (-not $DaysOfWeek -or $DaysOfWeek.Count -eq 0) {
    Write-Log "-DaysOfWeek is required (e.g. -DaysOfWeek Everyday)." "ERROR"
    exit 1
}

$resolvedDays = if ($DaysOfWeek -contains "Everyday") {
    @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
} else {
    $DaysOfWeek | Select-Object -Unique
}
$dayEnums = $resolvedDays | ForEach-Object { [System.DayOfWeek]$_ }

$timeParts = $SleepTime -split ":"
$atTime = (Get-Date -Hour ([int]$timeParts[0]) -Minute ([int]$timeParts[1]) -Second 0)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dayEnums -At $atTime

$description = "Automatic sleep (S3) at $SleepTime on $($resolvedDays -join ', '). " +
               "Refresh wait: $MaxRefreshWaitMinutes min. Handoff wait: $MaxHandoffWaitMinutes min. " +
               "Managed by Configure-AutomaticSleep.ps1 -- re-run to change, or -RemoveTask to delete."

try {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $verb = if ($existing) { "Updated" } else { "Created" }

    if ($PSCmdlet.ShouldProcess($TaskName, "$verb scheduled sleep task")) {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
            -Description $description -Force | Out-Null
        Write-Log "$verb scheduled task '$TaskName': sleeps at $SleepTime on $($resolvedDays -join ', '), waits up to $MaxRefreshWaitMinutes min for refreshes and $MaxHandoffWaitMinutes min for a safe handoff state."
    }
} catch {
    Write-Log "Failed to register scheduled task '$TaskName': $($_.Exception.Message)" "ERROR"
    exit 1
}

try {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Log "Next scheduled run: $($info.NextRunTime)"
} catch {
    Write-Log "Task registered, but could not read next-run time: $($_.Exception.Message)" "WARN"
}

Write-Host ""
Write-Host "-- Reference commands -----------------------------------------------------"
Write-Host "View task        : Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Run it right now : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Test (real sleep, guards apply)  : .\Configure-AutomaticSleep.ps1 -TestMode"
Write-Host "Disable          : .\Configure-AutomaticSleep.ps1 -DisableTask"
Write-Host "Enable           : .\Configure-AutomaticSleep.ps1 -EnableTask"
Write-Host "Delete           : .\Configure-AutomaticSleep.ps1 -RemoveTask"
Write-Host "Log file         : $LogPath"
Write-Host ""
Write-Host "Reminder: keep the OLD 'Automatic-PC-Shutdown' task registered but disabled" -ForegroundColor Yellow
Write-Host "as a rollback path -- see docs/pc_sync_data_pipeline.md." -ForegroundColor Yellow
