<#
.SYNOPSIS
    Runs on the daily QuantApp_EveningWake trigger, right after the PC
    wakes from S3 sleep ahead of the US market session.

.DESCRIPTION
    Deliberately does NOT run git fetch/reset or pip install -- unlike
    pc_morning_routine.ps1 (which still does that, and is now meant to be
    triggered only when main.py is confirmed stopped, e.g. via the 08:00
    WakeToRun trigger's own freshness gating in run_daily_refresh.py, or a
    deliberate manual maintenance run). Modifying source code or the active
    virtualenv underneath an already-running trading process risks a
    mixed-version runtime: already-imported modules stay old while later
    imports/subprocesses see new files.

    This script only:
      1. Confirms main.py is still running (S3 preserves process state, so
         it normally will be) -- relaunches it ONLY if it's genuinely not
         running, which means a forced reboot happened instead of a clean
         resume.
      2. Confirms pc_remote_control_listener.py is still running, same
         defensive-relaunch logic.
      3. Logs the resume event (timestamp, whether a relaunch was needed)
         for auditing wake reliability across DST transitions -- the wake
         time is a fixed calendar time, so it drifts relative to the actual
         NYSE session boundary twice a year; this log is how you'd notice
         if that drift ever mattered (it shouldn't, since actual trading
         gating comes from the app's own session-open logic, not PC wake
         timing).
#>

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$LogDir = Join-Path $RepoRoot "data\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogPath = Join-Path $LogDir "pc_wake_healthcheck.log"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

function Test-ProcessRunning {
    param([string]$Pattern)
    try {
        $procs = @(
            Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                $_.CommandLine -and $_.CommandLine -match $Pattern
            }
        )
        return $procs.Count -gt 0
    } catch {
        Write-Log "Could not inspect running processes: $($_.Exception.Message)"
        return $false
    }
}

Write-Log "Wake healthcheck starting."

$mainRunning = Test-ProcessRunning "(?i)\bmain\.py\b"
if ($mainRunning) {
    Write-Log "main.py is already running -- normal clean S3 resume, no relaunch needed."
} else {
    Write-Log "main.py is NOT running -- this looks like a forced reboot rather than a clean resume. Relaunching."
    $venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
    $pythonExe = if (Test-Path $venvPython) { $venvPython } else { "python" }
    $mainScript = Join-Path $RepoRoot "main.py"
    try {
        Start-Process -FilePath $pythonExe -ArgumentList "`"$mainScript`"" -WorkingDirectory $RepoRoot `
            -RedirectStandardOutput (Join-Path $LogDir "main_py_stdout.log") `
            -RedirectStandardError (Join-Path $LogDir "main_py_stderr.log")
        Write-Log "Relaunched main.py."
    } catch {
        Write-Log "Failed to relaunch main.py: $($_.Exception.Message)"
    }
}

$listenerRunning = Test-ProcessRunning "(?i)pc_remote_control_listener\.py"
if ($listenerRunning) {
    Write-Log "pc_remote_control_listener.py is already running."
} else {
    Write-Log "pc_remote_control_listener.py is NOT running -- relaunching."
    $venvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
    $pythonExe = if (Test-Path $venvPython) { $venvPython } else { "python" }
    $listenerScript = Join-Path $RepoRoot "scripts\pc_remote_control_listener.py"
    if (Test-Path $listenerScript) {
        try {
            Start-Process -FilePath $pythonExe -ArgumentList "`"$listenerScript`"" -WorkingDirectory $RepoRoot `
                -RedirectStandardOutput (Join-Path $LogDir "pc_remote_control_listener_stdout.log") `
                -RedirectStandardError (Join-Path $LogDir "pc_remote_control_listener_stderr.log")
            Write-Log "Relaunched pc_remote_control_listener.py."
        } catch {
            Write-Log "Failed to relaunch pc_remote_control_listener.py: $($_.Exception.Message)"
        }
    } else {
        Write-Log "Listener script not found at $listenerScript -- skipping relaunch."
    }
}

Write-Log "Wake healthcheck complete. main.py running=$mainRunning listener running=$listenerRunning"
