<#
.SYNOPSIS
    Registers a daily WakeToRun Task Scheduler task that wakes this PC
    shortly before the US market session so it can host the automatic
    laptop<->PC trading handoff.

.DESCRIPTION
    Part of the S3-sleep model (see Configure-AutomaticSleep.ps1): since
    this PC now sleeps instead of fully powering off, Windows' own
    Task-Scheduler wake timers can wake it directly -- no BIOS RTC alarm
    needed for this one (the existing 08:00 BIOS alarm stays as a
    redundant fallback for a genuine full power-off, see the runbook in
    docs/pc_sync_data_pipeline.md).

    The registered task's action is pc_wake_healthcheck.ps1, which performs
    NO git/pip updates (modifying source/deps underneath an already-running
    main.py risks a mixed-version runtime) -- it only verifies main.py and
    the remote-control listener are still running (defensive relaunch only
    if a forced reboot happened instead of a clean S3 resume) and logs the
    resume for DST-transition auditing.

    Default wake time 21:45 KST is a ~15 minute buffer before the 22:00 KST
    start of the widest-case (EDT) NYSE session window, so main.py has time
    to reconnect to MySQL/KIS before the market opens.

.PARAMETER WakeTime
    24-hour "HH:mm" local time to wake, e.g. "21:45".

.PARAMETER TaskName
    Scheduled task name. Default "QuantApp_EveningWake".

.PARAMETER RemoveTask
.PARAMETER DisableTask
.PARAMETER EnableTask

.EXAMPLE
    .\Configure-MarketHoursWake.ps1 -WakeTime 21:45
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^([01]\d|2[0-3]):([0-5]\d)$')]
    [string]$WakeTime = "21:45",

    [string]$TaskName = "QuantApp_EveningWake",

    [switch]$RemoveTask,
    [switch]$DisableTask,
    [switch]$EnableTask
)

$ErrorActionPreference = "Stop"
Import-Module ScheduledTasks -ErrorAction SilentlyContinue

$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$HealthcheckScript = Join-Path $RepoRoot "scripts\pc_wake_healthcheck.ps1"

$IsElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsElevated) {
    Write-Host "This script must be run from an elevated (Administrator) PowerShell window." -ForegroundColor Red
    exit 1
}

if ($RemoveTask) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No task named '$TaskName' exists -- nothing to remove."
    }
    exit 0
}
if ($DisableTask) {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "Disabled scheduled task '$TaskName'."
    exit 0
}
if ($EnableTask) {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "Enabled scheduled task '$TaskName'."
    exit 0
}

if (-not (Test-Path $HealthcheckScript)) {
    Write-Host "Required healthcheck script is missing: $HealthcheckScript" -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$HealthcheckScript`"" `
    -WorkingDirectory $RepoRoot

$timeParts = $WakeTime -split ":"
$atTime = (Get-Date -Hour ([int]$timeParts[0]) -Minute ([int]$timeParts[1]) -Second 0)
$trigger = New-ScheduledTaskTrigger -Daily -At $atTime

# WakeToRun is a Settings-level property, not a trigger property -- verify
# post-registration with:
#   (Get-ScheduledTask -TaskName '<name>').Settings.WakeToRun -eq $true
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -WakeToRun

$description = "Wakes the PC at $WakeTime daily (WakeToRun) ahead of the US market session " +
               "and runs pc_wake_healthcheck.ps1 (no git/pip updates -- see script header). " +
               "Managed by Configure-MarketHoursWake.ps1."

if ($PSCmdlet.ShouldProcess($TaskName, "Register/update daily wake task")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description $description -Force | Out-Null
    Write-Host "Registered task '$TaskName': wakes daily at $WakeTime."
}

try {
    $registered = Get-ScheduledTask -TaskName $TaskName
    $wakeToRunActual = $registered.Settings.WakeToRun
    if ($wakeToRunActual -ne $true) {
        Write-Host "WARNING: WakeToRun did not persist as expected (got '$wakeToRunActual'). This board/OS combination may not support it as configured -- verify manually." -ForegroundColor Yellow
    } else {
        Write-Host "Verified: WakeToRun is enabled on '$TaskName'."
    }
} catch {
    Write-Host "Could not verify WakeToRun setting: $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "-- Reference commands -----------------------------------------------------"
Write-Host "View task        : Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Verify WakeToRun : (Get-ScheduledTask -TaskName '$TaskName').Settings.WakeToRun"
Write-Host "Run it right now : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Disable          : .\Configure-MarketHoursWake.ps1 -DisableTask"
Write-Host "Enable           : .\Configure-MarketHoursWake.ps1 -EnableTask"
Write-Host "Delete           : .\Configure-MarketHoursWake.ps1 -RemoveTask"
Write-Host ""
Write-Host "Also required (once, outside this script): powercfg /change standby-timeout-ac 0" -ForegroundColor Yellow
Write-Host "so Windows' own idle-sleep timer doesn't interfere with the scheduled sleep task." -ForegroundColor Yellow
