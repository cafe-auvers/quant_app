<#
.SYNOPSIS
    Creates/updates/removes/enables/disables a Windows Task Scheduler task
    that shuts this PC down at a configured time on configured days.

.DESCRIPTION
    The scheduled task's action is a direct call to the built-in shutdown.exe
    with /t <WarningSeconds>, which makes Windows show its own "this PC will
    shut down in N seconds" notification to whoever is logged in -- that IS
    the warning, and it's what makes `shutdown.exe /a` able to cancel it.
    /f (force-close applications) is never passed unless -ForceCloseApplications
    is explicitly given, so by default apps with unsaved work can block/delay
    shutdown rather than losing data silently.

    The task runs as SYSTEM with the highest run level so it fires whether or
    not anyone is logged in, and re-running this script with the same
    -TaskName updates the existing task in place (Register-ScheduledTask
    -Force) instead of creating a duplicate.

    Registering, removing, enabling, or disabling the task requires an
    elevated (Administrator) PowerShell session; the script checks this up
    front and fails fast with a clear message instead of a confusing
    Task Scheduler error.

.PARAMETER ShutdownTime
    24-hour "HH:mm" time to shut down, e.g. "23:00".

.PARAMETER DaysOfWeek
    One or more of Monday..Sunday, or "Everyday" for all seven days.

.PARAMETER WarningSeconds
    Seconds of warning before shutdown (shutdown.exe /t). Default 300 (5 min).
    0 means shut down with no warning window.

.PARAMETER TaskName
    Scheduled task name. Default "Automatic-PC-Shutdown".

.PARAMETER ForceCloseApplications
    Passes /f to shutdown.exe, force-closing apps that would otherwise block
    shutdown (risk of unsaved work loss). Off by default.

.PARAMETER TestMode
    Instead of the normal weekly trigger, schedules a ONE-TIME shutdown
    ~3 minutes from now (as "<TaskName>-Test") so you can watch the full
    warning/shutdown flow without waiting for the real scheduled time. This
    will actually shut the PC down unless you cancel it with `shutdown /a`.

.PARAMETER RemoveTask
    Deletes the named scheduled task.

.PARAMETER DisableTask
    Disables (without deleting) the named scheduled task.

.PARAMETER EnableTask
    Re-enables a previously disabled task.

.EXAMPLE
    # Create/update: shut down at 23:00 on weeknights, 5 min warning
    .\Configure-AutomaticShutdown.ps1 -ShutdownTime 23:00 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday

.EXAMPLE
    # Every day, 10 min warning
    .\Configure-AutomaticShutdown.ps1 -ShutdownTime 23:30 -DaysOfWeek Everyday -WarningSeconds 600

.EXAMPLE
    # See the full flow fire in ~3 minutes (real shutdown -- cancel with shutdown /a if needed)
    .\Configure-AutomaticShutdown.ps1 -TestMode -WarningSeconds 60

.EXAMPLE
    .\Configure-AutomaticShutdown.ps1 -DisableTask
    .\Configure-AutomaticShutdown.ps1 -EnableTask
    .\Configure-AutomaticShutdown.ps1 -RemoveTask
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^([01]\d|2[0-3]):([0-5]\d)$')]
    [string]$ShutdownTime,

    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Everyday")]
    [string[]]$DaysOfWeek,

    [ValidateRange(0, 3600)]
    [int]$WarningSeconds = 300,

    [string]$TaskName = "Automatic-PC-Shutdown",

    [switch]$ForceCloseApplications,
    [switch]$TestMode,
    [switch]$RemoveTask,
    [switch]$DisableTask,
    [switch]$EnableTask
)

$ErrorActionPreference = "Stop"
Import-Module ScheduledTasks -ErrorAction SilentlyContinue

# --- logging -------------------------------------------------------------

$LogDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogPath = Join-Path $LogDir "shutdown-scheduler.log"

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

# --- elevation check -------------------------------------------------------

$IsElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsElevated) {
    Write-Log "This script must be run from an elevated (Administrator) PowerShell window -- registering a task that runs as SYSTEM requires it. Right-click PowerShell -> Run as administrator, then re-run this command." "ERROR"
    exit 1
}

# --- action: remove / disable / enable -------------------------------------

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
        Write-Log "Disabled scheduled task '$TaskName'. It will not run until re-enabled."
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

# --- build the shutdown action (shared by normal mode and test mode) -------

$warningMinutesText = if ($WarningSeconds -ge 60) { "{0:N1} min" -f ($WarningSeconds / 60) } else { "$WarningSeconds sec" }
$message = "Automatic scheduled shutdown in $WarningSeconds seconds ($warningMinutesText). Save your work now. " +
           "To cancel, open an elevated Command Prompt or PowerShell and run:  shutdown /a"

$shutdownArgs = "/s /t $WarningSeconds /c `"$message`""
if ($ForceCloseApplications) {
    $shutdownArgs += " /f"
    Write-Log "ForceCloseApplications is ON: shutdown.exe will force-close apps that would otherwise block shutdown. Unsaved work in those apps can be lost." "WARN"
}

$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\shutdown.exe" -Argument $shutdownArgs
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

# --- test mode: one-time trigger a few minutes out --------------------------

if ($TestMode) {
    $testTaskName = "$TaskName-Test"
    $fireAt = (Get-Date).AddMinutes(3)
    $trigger = New-ScheduledTaskTrigger -Once -At $fireAt

    if ($PSCmdlet.ShouldProcess($testTaskName, "Register one-time test shutdown task")) {
        Register-ScheduledTask -TaskName $testTaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
            -Description "One-time test run created by Configure-AutomaticShutdown.ps1 -TestMode." -Force | Out-Null
        Write-Log "TEST MODE: task '$testTaskName' registered. THIS PC WILL SHUT DOWN at $($fireAt.ToString('HH:mm:ss')) (in ~3 minutes) unless cancelled." "WARN"
        Write-Log "To cancel once the warning appears: shutdown /a" "WARN"
        Write-Log "To remove this test task afterward: Unregister-ScheduledTask -TaskName '$testTaskName' -Confirm:`$false"
    }
    exit 0
}

# --- normal mode: validate required params, create/update the real task ----

if (-not $ShutdownTime) {
    Write-Log "-ShutdownTime is required (e.g. -ShutdownTime 23:00) unless using -RemoveTask/-DisableTask/-EnableTask/-TestMode." "ERROR"
    exit 1
}
if (-not $DaysOfWeek -or $DaysOfWeek.Count -eq 0) {
    Write-Log "-DaysOfWeek is required (e.g. -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday, or -DaysOfWeek Everyday)." "ERROR"
    exit 1
}

$resolvedDays = if ($DaysOfWeek -contains "Everyday") {
    @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
} else {
    $DaysOfWeek | Select-Object -Unique
}
$dayEnums = $resolvedDays | ForEach-Object { [System.DayOfWeek]$_ }

$timeParts = $ShutdownTime -split ":"
$atTime = (Get-Date -Hour ([int]$timeParts[0]) -Minute ([int]$timeParts[1]) -Second 0)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dayEnums -At $atTime

$description = "Automatic shutdown at $ShutdownTime on $($resolvedDays -join ', '). " +
               "Warning: $WarningSeconds sec. ForceClose: $($ForceCloseApplications.IsPresent). " +
               "Managed by Configure-AutomaticShutdown.ps1 -- re-run that script to change, or -RemoveTask to delete."

try {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $verb = if ($existing) { "Updated" } else { "Created" }

    if ($PSCmdlet.ShouldProcess($TaskName, "$verb scheduled shutdown task")) {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
            -Description $description -Force | Out-Null
        Write-Log "$verb scheduled task '$TaskName': shuts down at $ShutdownTime on $($resolvedDays -join ', '), $WarningSeconds sec warning, ForceClose=$($ForceCloseApplications.IsPresent)."
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
Write-Host "                   schtasks /Query /TN `"$TaskName`" /V /FO LIST"
Write-Host "Run it right now : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Test (safe-ish)  : .\Configure-AutomaticShutdown.ps1 -TestMode -WarningSeconds 60"
Write-Host "Disable          : .\Configure-AutomaticShutdown.ps1 -DisableTask -TaskName '$TaskName'"
Write-Host "Enable           : .\Configure-AutomaticShutdown.ps1 -EnableTask -TaskName '$TaskName'"
Write-Host "Update           : re-run this script with new -ShutdownTime/-DaysOfWeek/etc. (idempotent, no duplicates)"
Write-Host "Delete           : .\Configure-AutomaticShutdown.ps1 -RemoveTask -TaskName '$TaskName'"
Write-Host "Cancel a PENDING shutdown that's already counting down : shutdown /a"
Write-Host "Log file         : $LogPath"
