<#
.SYNOPSIS
    Waits for an active historical refresh AND a safe handoff/monitoring
    state before putting this PC to sleep (S3) -- the guard action used by
    Configure-AutomaticSleep.ps1.

.DESCRIPTION
    Mirrors Invoke-GuardedShutdown.ps1's historical.py guard exactly, then
    adds a second guard specific to the automatic cross-machine trading
    handoff: it reads data\sleep_readiness.json (written every 30s by
    MainWindow's sleep_readiness_timer, see src/services/sleep_readiness.py)
    and refuses to sleep while this PC is the main device with something
    actually in flight (an open PROD position/order, or a handoff
    reconciliation pass still running).

    PowerShell cannot inspect the running Qt process directly, so it trusts
    that JSON file -- but ONLY when main.py is confirmed alive via
    Get-CimInstance; a stale-but-alive process is treated as UNSAFE (not
    proof of safety), while a missing/stale file with NO main.py process
    running at all is treated as safe (nothing left to protect). This
    asymmetry is deliberate: see the code comments below.

    Unlike shutdown.exe, Win32 SetSuspendState has no built-in countdown/
    cancel notification, so -WarningSeconds here is just a plain sleep
    delay before suspending, logged so it's visible in the log file.

.PARAMETER MaxRefreshWaitMinutes
    Same semantics as Invoke-GuardedShutdown.ps1 -- how long to wait for a
    live historical.py process before giving up on this sleep cycle.

.PARAMETER MaxHandoffWaitMinutes
    How long to wait for an in-flight PROD position/order/reconciliation to
    clear before giving up on this sleep cycle. Default 30 minutes -- this
    should normally clear on its own (a position exits, or ownership is
    handed to the other device) well within that window; if it doesn't,
    something needs a human's attention more than it needs the PC asleep.

.PARAMETER PollSeconds
    Poll interval while waiting on either guard. Default 60.

.PARAMETER WarningSeconds
    Plain delay (not a native OS countdown) logged before suspending.
    Default 60.
#>

[CmdletBinding()]
param(
    [ValidateRange(0, 1440)]
    [int]$MaxRefreshWaitMinutes = 180,

    [ValidateRange(0, 1440)]
    [int]$MaxHandoffWaitMinutes = 30,

    [ValidateRange(5, 300)]
    [int]$PollSeconds = 60,

    [ValidateRange(0, 600)]
    [int]$WarningSeconds = 60
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$LogDir = Join-Path $RepoRoot "data\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogPath = Join-Path $LogDir "guarded_sleep.log"
$ReadinessPath = Join-Path $RepoRoot "data\sleep_readiness.json"
# A snapshot older than this is only trusted while main.py is confirmed
# dead; a live-but-stalled process must never look sleep-safe just because
# its writer thread stopped updating the file.
$MaxReadinessAgeSeconds = 180

function Write-Log {
    param([string]$Message, [ValidateSet("INFO", "WARN", "ERROR")][string]$Level = "INFO")
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

function Get-ActiveHistoricalRefreshProcesses {
    try {
        $processes = @(
            Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                $_.CommandLine -and $_.CommandLine -match "(?i)\bhistorical\.py\b"
            }
        )
        return [pscustomobject]@{ Success = $true; Processes = $processes }
    } catch {
        Write-Log "Cannot inspect running processes: $($_.Exception.Message). Sleep cancelled to avoid interrupting a refresh." "ERROR"
        return [pscustomobject]@{ Success = $false; Processes = @() }
    }
}

function Test-MainPyRunning {
    try {
        $procs = @(
            Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                $_.CommandLine -and $_.CommandLine -match "(?i)\bmain\.py\b"
            }
        )
        return $procs.Count -gt 0
    } catch {
        # Unknown process state -- fail closed (assume it might be running).
        return $true
    }
}

function Test-SleepReadiness {
    <#
    Returns [pscustomobject]@{ Safe = $bool; Reason = string }.
    #>
    $mainPyRunning = Test-MainPyRunning

    if (-not (Test-Path $ReadinessPath)) {
        if ($mainPyRunning) {
            return [pscustomobject]@{ Safe = $false; Reason = "sleep_readiness.json missing but main.py is running" }
        }
        return [pscustomobject]@{ Safe = $true; Reason = "sleep_readiness.json missing and main.py is not running" }
    }

    try {
        $readiness = Get-Content -Path $ReadinessPath -Raw | ConvertFrom-Json
    } catch {
        if ($mainPyRunning) {
            return [pscustomobject]@{ Safe = $false; Reason = "sleep_readiness.json unreadable ($($_.Exception.Message)) but main.py is running" }
        }
        return [pscustomobject]@{ Safe = $true; Reason = "sleep_readiness.json unreadable but main.py is not running" }
    }

    $generatedAt = $null
    try { $generatedAt = [datetime]::Parse($readiness.generated_at).ToUniversalTime() } catch {}
    $ageSeconds = if ($generatedAt) { ((Get-Date).ToUniversalTime() - $generatedAt).TotalSeconds } else { [double]::PositiveInfinity }

    if ($ageSeconds -gt $MaxReadinessAgeSeconds) {
        if ($mainPyRunning) {
            return [pscustomobject]@{ Safe = $false; Reason = "sleep_readiness.json is stale (${ageSeconds}s old) but main.py is still running -- treated as unsafe, not proof of safety" }
        }
        return [pscustomobject]@{ Safe = $true; Reason = "sleep_readiness.json is stale but main.py is not running -- nothing left to protect" }
    }

    if ($readiness.safe_to_sleep -eq $true) {
        return [pscustomobject]@{ Safe = $true; Reason = "sleep_readiness.json reports safe_to_sleep=true" }
    }
    $detail = "is_main_device=$($readiness.is_main_device) in_flight=$($readiness.in_flight_prod_symbol_count) open_orders=$($readiness.has_open_broker_orders) reconciling=$($readiness.handoff_reconciliation_in_progress)"
    return [pscustomobject]@{ Safe = $false; Reason = "sleep_readiness.json reports safe_to_sleep=false ($detail)" }
}

# --- guard 1: historical refresh (identical to Invoke-GuardedShutdown.ps1) --

$deadline = (Get-Date).AddMinutes($MaxRefreshWaitMinutes)
while ($true) {
    $inspection = Get-ActiveHistoricalRefreshProcesses
    if (-not $inspection.Success) {
        exit 1
    }
    $refreshProcesses = @($inspection.Processes)
    if ($refreshProcesses.Count -eq 0) {
        break
    }

    $processDetails = $refreshProcesses | ForEach-Object { "PID $($_.ProcessId)" }
    if ((Get-Date) -ge $deadline) {
        Write-Log "Historical refresh still running ($($processDetails -join ', ')) after $MaxRefreshWaitMinutes minute(s). Sleep cancelled; no refresh was terminated." "WARN"
        exit 2
    }

    Write-Log "Historical refresh active ($($processDetails -join ', ')); waiting up to $MaxRefreshWaitMinutes minute(s) before sleep."
    Start-Sleep -Seconds $PollSeconds
}

# --- guard 2: cross-machine handoff / live-position safety ------------------

$handoffDeadline = (Get-Date).AddMinutes($MaxHandoffWaitMinutes)
while ($true) {
    $readiness = Test-SleepReadiness
    if ($readiness.Safe) {
        break
    }

    if ((Get-Date) -ge $handoffDeadline) {
        Write-Log "Not safe to sleep after $MaxHandoffWaitMinutes minute(s) ($($readiness.Reason)). Sleep cancelled -- this needs attention." "WARN"
        exit 3
    }

    Write-Log "Not yet safe to sleep ($($readiness.Reason)); waiting up to $MaxHandoffWaitMinutes minute(s)."
    Start-Sleep -Seconds $PollSeconds
}

# --- suspend ------------------------------------------------------------

Write-Log "All guards clear; suspending (S3) in $WarningSeconds second(s)."
if ($WarningSeconds -gt 0) {
    Start-Sleep -Seconds $WarningSeconds
}

# Re-check once more immediately before suspending -- the warning delay
# above is itself a window where a handoff could start.
$finalCheck = Test-SleepReadiness
if (-not $finalCheck.Safe) {
    Write-Log "Sleep cancelled at the last moment: $($finalCheck.Reason)." "WARN"
    exit 3
}

Write-Log "Suspending now."
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class QuantAppPowerState {
    [DllImport("powrprof.dll", SetLastError = true)]
    public static extern bool SetSuspendState(bool hibernate, bool forceCritical, bool disableWakeEvent);
}
"@ -ErrorAction SilentlyContinue

$suspended = [QuantAppPowerState]::SetSuspendState($false, $false, $false)
if (-not $suspended) {
    Write-Log "SetSuspendState returned failure (Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))." "ERROR"
    exit 1
}
