<#
Runs once per PC wake, launched by an "at logon" Task Scheduler task (see
setup_pc_morning_task.ps1). Chains, in order:

  1. git fetch + hard reset to origin/master -- so this PC always runs
     whatever's actually on GitHub, never a stale local clone. This is a
     deployment target, not a dev workspace: nobody should be editing code
     here, so discarding local state is intentional and safe (see
     docs/pc_sync_data_pipeline.md).
  2. scripts/run_daily_refresh.py -- gates on whether the database's actual
     latest stored date is behind what's expected (same check the dashboard
     itself shows as "Needs refresh"), and if so, runs historical.py
     --mode 1d then --mode 1h. This self-heals multi-day gaps, not just
     "yesterday."
  3. Launches main.py (detached) so the dashboard is visible if you check
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
    Write-Log "git fetch origin: $fetchOutput"

    $resetOutput = git reset --hard origin/master 2>&1
    Write-Log "git reset --hard origin/master: $resetOutput"

    $headCommit = (git rev-parse --short HEAD 2>&1)
    Write-Log "Now at commit $headCommit"
} catch {
    Write-Log "WARN: git sync failed ($($_.Exception.Message)) -- continuing with whatever code is already on disk."
} finally {
    Pop-Location
}

# --- 1.5. Keep the venv's packages in sync with requirements.txt -----------
# Cheap/idempotent when nothing changed; catches cases like this one where a
# dependency (e.g. tzdata) got added on the laptop after the venv here was
# first created, so a code-only git sync would otherwise leave it missing.

try {
    $pipOutput = & $PythonExe -m pip install -q -r (Join-Path $RepoRoot "requirements.txt") 2>&1
    Write-Log "pip install -r requirements.txt: exit code $LASTEXITCODE"
    if ($LASTEXITCODE -ne 0) { Write-Log "pip output: $pipOutput" }
} catch {
    Write-Log "WARN: pip sync failed ($($_.Exception.Message)) -- continuing with whatever packages are already installed."
}

# --- 2. Data refresh (trading-day gated inside the script) -----------------

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

Write-Log "=== Morning routine finished ==="
