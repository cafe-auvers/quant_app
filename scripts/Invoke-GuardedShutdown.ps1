<#
.SYNOPSIS
    Waits for quant_app historical refreshes to finish before initiating a
    Windows shutdown.

.DESCRIPTION
    This script is the action used by Configure-AutomaticShutdown.ps1. It
    detects live historical.py processes rather than trusting status files,
    which can be stale after an interrupted run. It waits only up to
    MaxRefreshWaitMinutes; if a refresh is still alive at that point, it exits
    without shutting the PC down. That avoids killing a partial refresh and
    avoids leaving the Task Scheduler action running indefinitely.
#>

[CmdletBinding()]
param(
    [ValidateRange(0, 3600)]
    [int]$WarningSeconds = 300,

    [switch]$ForceCloseApplications,

    [ValidateRange(0, 1440)]
    [int]$MaxRefreshWaitMinutes = 180,

    [ValidateRange(5, 300)]
    [int]$PollSeconds = 60
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$LogDir = Join-Path $RepoRoot "data\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogPath = Join-Path $LogDir "guarded_shutdown.log"

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
        Write-Log "Cannot inspect running processes: $($_.Exception.Message). Shutdown cancelled to avoid interrupting a refresh." "ERROR"
        return [pscustomobject]@{ Success = $false; Processes = @() }
    }
}

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
        Write-Log "Historical refresh still running ($($processDetails -join ', ')) after $MaxRefreshWaitMinutes minute(s). Shutdown cancelled; no refresh was terminated." "WARN"
        exit 2
    }

    Write-Log "Historical refresh active ($($processDetails -join ', ')); waiting up to $MaxRefreshWaitMinutes minute(s) before shutdown."
    Start-Sleep -Seconds $PollSeconds
}

$warningMinutesText = if ($WarningSeconds -ge 60) { "{0:N1} min" -f ($WarningSeconds / 60) } else { "$WarningSeconds sec" }
$message = "Automatic scheduled shutdown in $WarningSeconds seconds ($warningMinutesText). Save your work now. " +
           "To cancel, open an elevated Command Prompt or PowerShell and run:  shutdown /a"
if ($ForceCloseApplications) {
    Write-Log "ForceCloseApplications is ON: shutdown.exe will force-close apps that would otherwise block shutdown." "WARN"
}

Write-Log "No active historical refresh found; initiating shutdown with a $WarningSeconds second warning."
if ($ForceCloseApplications) {
    & "$env:SystemRoot\System32\shutdown.exe" /s /t $WarningSeconds /c $message /f
} else {
    & "$env:SystemRoot\System32\shutdown.exe" /s /t $WarningSeconds /c $message
}
if ($LASTEXITCODE -ne 0) {
    Write-Log "shutdown.exe exited with code $LASTEXITCODE." "ERROR"
    exit $LASTEXITCODE
}
