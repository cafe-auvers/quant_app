<#
Runs once per PC wake, launched by an "at logon" Task Scheduler task (see
setup_pc_morning_task.ps1). Chains, in order:

  1. git fetch + hard reset to origin/master -- so this PC always runs
     whatever's actually on GitHub, never a stale local clone. This is a
     deployment target, not a dev workspace: nobody should be editing code
     here, so discarding local state is intentional and safe (see
     docs/pc_sync_data_pipeline.md).
  2. scripts/run_daily_refresh.py -- gates on "was there a new US trading
     session" and, if so, runs historical.py --mode 1d then --mode 1h.
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

$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    Write-Log "ERROR: python.exe not found on PATH -- cannot run refresh or launch main.py. Fix PATH/venv activation, then re-run this script or Start-ScheduledTask."
    exit 1
}

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

# --- 2. Data refresh (trading-day gated inside the script) -----------------

try {
    & $PythonExe (Join-Path $RepoRoot "scripts\run_daily_refresh.py") 2>&1 | ForEach-Object { Write-Log "[refresh] $_" }
    Write-Log "Data refresh step finished (exit code $LASTEXITCODE)."
} catch {
    Write-Log "ERROR: data refresh step threw: $($_.Exception.Message)"
}

# --- 3. Launch main.py (detached, so this task can finish while the GUI stays open) ---

try {
    Start-Process -FilePath $PythonExe -ArgumentList (Join-Path $RepoRoot "main.py") -WorkingDirectory $RepoRoot
    Write-Log "main.py launched."
} catch {
    Write-Log "ERROR: could not launch main.py: $($_.Exception.Message)"
}

Write-Log "=== Morning routine finished ==="
