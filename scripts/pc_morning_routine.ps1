<#
Runs once per PC wake, launched by an "at logon" / daily WakeToRun Task
Scheduler task (see setup_pc_morning_task.ps1).

On a cold boot/logon, when main.py is not already running, it chains:

  1. git fetch + hard reset to origin/master -- so this PC always runs
     whatever's actually on GitHub, never a stale local clone. This is a
     deployment target, not a dev workspace: nobody should be editing code
     here, so discarding local state is intentional and safe (see
     docs/pc_sync_data_pipeline.md).
  2. Start the restricted remote-control listener so an environment or
     dependency failure remains diagnosable from the laptop.
  3. Synchronize the credential-only .env schema, migrate legacy non-secret
     settings to config/runtime.local.json, then regenerate .env.pc.
  4. scripts/run_daily_refresh.py -- gates on whether the database's actual
     latest stored date is behind what's expected (same check the dashboard
     itself shows as "Needs refresh"), and if so, runs historical.py
     --mode 1d then --mode 1h. This self-heals multi-day gaps, not just
     "yesterday."
  5. Launches main.py (detached) so the dashboard is visible if you check
     in during the PC's short on-window.

On a normal S3 resume, main.py is already running. In that case this routine
uses refresh-only resume mode: it does not mutate the checkout or venv and it
does not launch a duplicate dashboard. It still runs the gated historical
refresh. A Git update requires a normal app shutdown/reboot so one process can
load one coherent checkout; changing files underneath a live trading process
would create a mixed-version runtime.

Each step's outcome is logged. Git and data-refresh failures retain the last
known-good checkout/data and continue toward the dashboard. Environment
migration or dependency failures stop before main.py because running a
partially migrated or untested dependency set would be unsafe; the restricted
listener is already running so that failure remains remotely visible.
#>

$ErrorActionPreference = "Continue"

$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$RoutineScriptPath = (Resolve-Path -LiteralPath $MyInvocation.MyCommand.Path).Path
$RoutineHashBeforeSync = (Get-FileHash -LiteralPath $RoutineScriptPath -Algorithm SHA256).Hash
$LogDir = Join-Path $RepoRoot "data\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogPath = Join-Path $LogDir "pc_morning_routine.log"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

function Get-QuantMainProcesses {
    param([string]$MainScriptPath)

    $resolvedMainScript = (Resolve-Path -LiteralPath $MainScriptPath).Path
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.Name -match '^python(w)?\.exe$' -and
            $_.CommandLine -and
            (
                $_.CommandLine.IndexOf(
                    $resolvedMainScript,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -ge 0 -or
                # Defensive compatibility for a dashboard started manually
                # from RepoRoot as `python main.py` instead of with the
                # absolute path used by the scheduled scripts.
                $_.CommandLine -match '(?i)(?:^|[\s"])(?:\.\\)?main\.py(?:$|[\s"])'
            )
        }
    )
}

function Start-RemoteControlListener {
    param(
        [string]$PythonPath,
        [string]$RepositoryRoot,
        [string]$LogsDirectory
    )

    $listenerScript = Join-Path $RepositoryRoot "scripts\pc_remote_control_listener.py"
    if (-not (Test-Path -LiteralPath $listenerScript)) {
        Write-Log "ERROR: remote-control listener script is missing: $listenerScript"
        return $false
    }
    $resolvedListener = (Resolve-Path -LiteralPath $listenerScript).Path
    try {
        $existing = @(
            Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                $_.Name -match '^python(w)?\.exe$' -and
                $_.CommandLine -and
                $_.CommandLine.Contains($resolvedListener)
            }
        )
    } catch {
        Write-Log "WARN: could not inspect the remote-control listener process: $($_.Exception.Message)"
        $existing = @()
    }
    if ($existing.Count -gt 0) {
        Write-Log "Remote-control listener is already running; keeping the existing process."
        return $true
    }

    $listenerOutLog = Join-Path $LogsDirectory "pc_remote_control_listener_stdout.log"
    $listenerErrLog = Join-Path $LogsDirectory "pc_remote_control_listener_stderr.log"
    try {
        $listenerProc = Start-Process -FilePath $PythonPath -ArgumentList "`"$resolvedListener`"" `
            -WorkingDirectory $RepositoryRoot -RedirectStandardOutput $listenerOutLog `
            -RedirectStandardError $listenerErrLog -WindowStyle Hidden -PassThru
        Start-Sleep -Seconds 2
        if ($listenerProc.HasExited) {
            Write-Log "ERROR: remote-control listener exited immediately (code $($listenerProc.ExitCode)) -- see $listenerErrLog"
            return $false
        }
        Write-Log "Remote-control listener launched (PID $($listenerProc.Id))."
        return $true
    } catch {
        Write-Log "ERROR: could not launch remote-control listener: $($_.Exception.Message)"
        return $false
    }
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

# The daily WakeToRun trigger normally resumes an existing Windows session,
# including its Python processes. Inspect before Git or pip: updating either
# underneath a live dashboard would leave imported modules old while later
# imports/subprocesses read new files. If inspection itself fails, fail closed
# instead of risking a second trading process.
$MainScriptPath = Join-Path $RepoRoot "main.py"
try {
    $MainProcessesAtStart = @(Get-QuantMainProcesses -MainScriptPath $MainScriptPath)
} catch {
    Write-Log "ERROR: could not inspect existing main.py processes ($($_.Exception.Message)); refusing maintenance or a possible duplicate launch."
    exit 1
}
$ResumeMode = $MainProcessesAtStart.Count -gt 0
if ($ResumeMode) {
    $existingMainPids = ($MainProcessesAtStart | ForEach-Object { $_.ProcessId }) -join ", "
    Write-Log "main.py is already running (PID(s): $existingMainPids); entering refresh-only resume mode."
}

# --- 1. Git sync -------------------------------------------------------------

if ($ResumeMode) {
    Write-Log "Resume mode: skipping Git sync so the live dashboard keeps one coherent code version. Reboot or close main.py normally before a maintenance run to apply newer commits."
} else {
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

    # PowerShell parses this file before executing it. If Git replaced the routine
    # itself, continuing here would run the old in-memory instructions against the
    # new Python files. Relaunch exactly once so this same scheduled invocation
    # uses the newly deployed routine and its matching migration/order semantics.
    try {
        $routineHashAfterSync = (Get-FileHash -LiteralPath $RoutineScriptPath -Algorithm SHA256).Hash
        if ($routineHashAfterSync -ne $RoutineHashBeforeSync) {
            if ($env:QUANT_MORNING_ROUTINE_RELAUNCHED -eq "1") {
                Write-Log "WARN: morning routine changed again after its guarded relaunch; continuing without a loop."
            } else {
                Write-Log "Morning routine was updated by Git; relaunching the new version in this same task run."
                $env:QUANT_MORNING_ROUTINE_RELAUNCHED = "1"
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $RoutineScriptPath
                $childExitCode = $LASTEXITCODE
                Write-Log "Updated morning routine completed with exit code $childExitCode."
                exit $childExitCode
            }
        }
    } catch {
        Write-Log "WARN: could not verify/relaunch the updated morning routine: $($_.Exception.Message)"
    }
}

# --- 1.25. Keep diagnostics reachable before abort-prone maintenance -------

$listenerReady = Start-RemoteControlListener -PythonPath $PythonExe `
    -RepositoryRoot $RepoRoot -LogsDirectory $LogDir
if (-not $listenerReady) {
    Write-Log "WARN: remote diagnostics are unavailable; continuing the local routine."
}

# --- 1.5. Environment schema sync ------------------------------------------
# .env, .env.pc, and config/runtime.local.json remain gitignored. Tracked
# .env.example contains credential names only; config/runtime.json contains
# non-secret defaults. This step preserves existing effective values and
# migrates legacy runtime keys before any Python process reads configuration.

if ($ResumeMode) {
    Write-Log "Resume mode: skipping environment migration; the live dashboard has already loaded its credential/runtime configuration."
} else {
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
}

# --- 1.75. Keep the venv's packages on the tested dependency graph ---------
# Cheap/idempotent when nothing changed; catches cases like this one where a
# dependency (e.g. tzdata) got added on the laptop after the venv here was
# first created, so a code-only git sync would otherwise leave it missing.

if ($ResumeMode) {
    Write-Log "Resume mode: skipping dependency sync so the live dashboard's environment is not changed underneath it."
} else {
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

if ($ResumeMode) {
    Write-Log "Resume mode: existing main.py retained; no duplicate process was launched."
} else {
    try {
        $proc = Start-Process -FilePath $PythonExe -ArgumentList $MainScriptPath -WorkingDirectory $RepoRoot `
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
}

Write-Log "=== Morning routine finished ==="
