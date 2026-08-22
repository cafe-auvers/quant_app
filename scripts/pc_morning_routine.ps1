<#
Runs once per PC wake, launched by an "at logon" Task Scheduler task (see
setup_pc_morning_task.ps1). Chains, in order:

  1. git fetch + hard reset to origin/master -- so this PC always runs
     whatever's actually on GitHub, never a stale local clone. This is a
     deployment target, not a dev workspace: nobody should be editing code
     here, so discarding local state is intentional and safe (see
     docs/pc_sync_data_pipeline.md).
  2. Merge the latest tracked .env.example schema into the PC's private .env
     without replacing configured values, then regenerate .env.pc.
  3. scripts/run_daily_refresh.py -- gates on whether the database's actual
     latest stored date is behind what's expected (same check the dashboard
     itself shows as "Needs refresh"), and if so, runs historical.py
     --mode 1d then --mode 1h. This self-heals multi-day gaps, not just
     "yesterday."
  4. Launches main.py (detached) so the dashboard is visible if you check
     in during the PC's short on-window.

Each step's outcome is logged; a failure in one step doesn't block the next
(e.g. a git-sync hiccup shouldn't prevent main.py from at least opening
against whatever data is already cached).
#>

$ErrorActionPreference = "Continue"

$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$LogDir = Join-Path $RepoRoot "data\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogPath = Join-Path $LogDir "pc_morning_routine.log"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

# Prefer this repo's own venv over a bare "python" on PATH. Task Scheduler
# runs this in a fresh process that never had venv\Scripts\Activate.ps1 run
# in it, so Get-Command python here can silently resolve to a system Python
# that's missing every package installed into the venv (PyQt5 included) --
# main.py would then crash on import with no visible error, since nothing
# downstream captures its output. Fall back to PATH only if there's no venv.
$VenvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $PythonExe = $VenvPython
} else {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExe) {
    Write-Log "ERROR: no venv at $VenvPython and no python.exe on PATH -- cannot run refresh or launch main.py."
    exit 1
}
Write-Log "Using Python: $PythonExe"

Write-Log "=== Morning routine starting ==="

# --- 1. Git sync -------------------------------------------------------------

Push-Location $RepoRoot
try {
    $fetchOutput = git fetch origin 2>&1
    $fetchExitCode = $LASTEXITCODE
    Write-Log "git fetch origin (exit code $fetchExitCode): $fetchOutput"
    if ($fetchExitCode -ne 0) {
        throw "git fetch origin exited with code $fetchExitCode; leaving the current checkout untouched."
    }

    $resetOutput = git reset --hard origin/master 2>&1
    $resetExitCode = $LASTEXITCODE
    Write-Log "git reset --hard origin/master (exit code $resetExitCode): $resetOutput"
    if ($resetExitCode -ne 0) {
        throw "git reset --hard origin/master exited with code $resetExitCode."
    }

    $headCommit = (git rev-parse --short HEAD 2>&1)
    $headExitCode = $LASTEXITCODE
    if ($headExitCode -ne 0) {
        throw "git rev-parse --short HEAD exited with code $headExitCode."
    }
    Write-Log "Now at commit $headCommit"
} catch {
    Write-Log "WARN: git sync failed ($($_.Exception.Message)) -- continuing with whatever code is already on disk."
} finally {
    Pop-Location
}

# --- 1.25. Environment schema sync -----------------------------------------
# .env and .env.pc remain gitignored.  Only the non-secret schema/defaults in
# .env.example arrive through Git; this step preserves every configured local
# .env value and adds newly documented settings before any Python process
# reads configuration.

try {
    $envSyncOutput = & $PythonExe (Join-Path $RepoRoot "scripts\sync_env_files.py") 2>&1
    $envSyncExitCode = $LASTEXITCODE
    Write-Log "Environment sync (exit code $envSyncExitCode): $envSyncOutput"
    if ($envSyncExitCode -ne 0) {
        throw "Environment synchronization failed with exit code $envSyncExitCode."
    }
} catch {
    Write-Log "ERROR: environment sync failed ($($_.Exception.Message)); aborting before refresh or app launch."
    exit 1
}

# --- 1.5. Keep the venv's packages on the tested dependency graph ----------
# Cheap/idempotent when nothing changed; catches cases like this one where a
# dependency (e.g. tzdata) got added on the laptop after the venv here was
# first created, so a code-only git sync would otherwise leave it missing.

try {
    $requirementsFile = Join-Path $RepoRoot "requirements.lock"
    $pipOutput = & $PythonExe -m pip install -q --require-hashes -r $requirementsFile 2>&1
    $pipExitCode = $LASTEXITCODE
    Write-Log "pip install -r requirements.lock: exit code $pipExitCode"
    if ($pipExitCode -ne 0) {
        Write-Log "pip output: $pipOutput"
        throw "Locked dependency install failed with exit code $pipExitCode."
    }
} catch {
    Write-Log "ERROR: dependency sync failed ($($_.Exception.Message)); aborting before data refresh."
    exit 1
}

# --- 2. Data refresh (trading-day gated inside the script) -----------------

# This machine hosts the authoritative MySQL database. If MySQL is down, fail
# visibly instead of creating/refreshing the laptop-only SQLite fallback here.
# Start-Process below inherits this setting, so main.py follows the same rule.
$env:QUANT_LOCAL_MIRROR_ENABLED = "0"

try {
    & $PythonExe (Join-Path $RepoRoot "scripts\run_daily_refresh.py") 2>&1 | ForEach-Object { Write-Log "[refresh] $_" }
    Write-Log "Data refresh step finished (exit code $LASTEXITCODE)."
} catch {
    Write-Log "ERROR: data refresh step threw: $($_.Exception.Message)"
}

# --- 3. Launch main.py (detached, so this task can finish while the GUI stays open) ---
# stdout/stderr are captured to their own file (not this log) since main.py
# keeps running after this script exits -- if it crashes on startup (e.g. a
# missing dependency), that's the only place the error will show up.

$MainPyOutLog = Join-Path $LogDir "main_py_stdout.log"
$MainPyErrLog = Join-Path $LogDir "main_py_stderr.log"

try {
    $proc = Start-Process -FilePath $PythonExe -ArgumentList (Join-Path $RepoRoot "main.py") -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $MainPyOutLog -RedirectStandardError $MainPyErrLog -PassThru
    Start-Sleep -Seconds 5
    if ($proc.HasExited) {
        Write-Log "ERROR: main.py exited almost immediately (code $($proc.ExitCode)) -- see $MainPyErrLog"
    } else {
        Write-Log "main.py launched (PID $($proc.Id)) and is still running after 5s."
    }
} catch {
    Write-Log "ERROR: could not launch main.py: $($_.Exception.Message)"
}

# --- 4. Launch the remote-control listener (detached) -----------------------
# Lets the laptop check "is the PC on" and send an authenticated remote
# shutdown signal over Tailscale (see pc_remote_control_listener.py). Started
# fresh on every logon, same as main.py -- if a prior instance is somehow
# still around, the new one will fail to bind the port and exit; that's
# surfaced in its own log rather than blocking anything here.

$ListenerOutLog = Join-Path $LogDir "pc_remote_control_listener_stdout.log"
$ListenerErrLog = Join-Path $LogDir "pc_remote_control_listener_stderr.log"

try {
    $listenerProc = Start-Process -FilePath $PythonExe -ArgumentList (Join-Path $RepoRoot "scripts\pc_remote_control_listener.py") `
        -WorkingDirectory $RepoRoot -RedirectStandardOutput $ListenerOutLog -RedirectStandardError $ListenerErrLog -PassThru
    Start-Sleep -Seconds 2
    if ($listenerProc.HasExited) {
        Write-Log "ERROR: remote control listener exited almost immediately (code $($listenerProc.ExitCode)) -- see $ListenerErrLog"
    } else {
        Write-Log "Remote control listener launched (PID $($listenerProc.Id))."
    }
} catch {
    Write-Log "ERROR: could not launch remote control listener: $($_.Exception.Message)"
}

Write-Log "=== Morning routine finished ==="
