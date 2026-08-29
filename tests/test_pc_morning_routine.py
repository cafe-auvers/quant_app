from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTINE = ROOT / "scripts" / "pc_morning_routine.ps1"


def _routine_text() -> str:
    return ROUTINE.read_text(encoding="utf-8")


def test_remote_listener_starts_before_abort_prone_environment_sync():
    text = _routine_text()

    listener_call = text.index(
        "$listenerReady = Start-RemoteControlListener -PythonPath $PythonExe"
    )
    environment_sync = text.index('"scripts\\sync_env_files.py"')

    assert listener_call < environment_sync
    assert "-WindowStyle Hidden -PassThru" in text


def test_git_updated_routine_relaunches_once_before_maintenance():
    text = _routine_text()

    relaunch = text.index(
        "Morning routine was updated by Git; relaunching the new version"
    )
    listener_call = text.index(
        "$listenerReady = Start-RemoteControlListener -PythonPath $PythonExe"
    )

    assert relaunch < listener_call
    assert '$env:QUANT_MORNING_ROUTINE_RELAUNCHED -eq "1"' in text
    assert "exit $childExitCode" in text


def test_existing_main_is_detected_before_git_or_dependency_mutation():
    text = _routine_text()

    inspection = text.index(
        "$MainProcessesAtStart = @(Get-QuantMainProcesses -MainScriptPath $MainScriptPath)"
    )
    git_sync = text.index("$fetchOutput = git fetch origin")
    dependency_sync = text.index("-m pip install -q --require-hashes")

    assert inspection < git_sync < dependency_sync
    assert "refusing maintenance or a possible duplicate launch" in text
    assert "(?:\\.\\\\)?main\\.py" in text


def test_resume_mode_refreshes_without_git_env_pip_or_duplicate_main():
    text = _routine_text()

    assert '$ResumeMode = $MainProcessesAtStart.Count -gt 0' in text
    assert "entering refresh-only resume mode" in text
    assert "Resume mode: skipping Git sync" in text
    assert "Resume mode: skipping environment migration" in text
    assert "Resume mode: skipping dependency sync" in text
    assert "Resume mode: existing main.py retained; no duplicate process was launched" in text

    refresh = text.index('"scripts\\run_daily_refresh.py"')
    duplicate_guard = text.index(
        "Resume mode: existing main.py retained; no duplicate process was launched"
    )
    launch = text.index("$proc = Start-Process -FilePath $PythonExe")

    assert refresh < duplicate_guard < launch


def test_cold_start_records_preflight_before_refresh_and_dashboard_launch():
    text = _routine_text()

    preflight = text.index('"scripts\\check_controlled_live_readiness.py"')
    refresh = text.index('"scripts\\run_daily_refresh.py"')
    launch = text.index("$proc = Start-Process -FilePath $PythonExe")

    assert preflight < refresh < launch
    assert '"controlled_live_preflight.json"' in text
    assert "production broker mutations remain fail-closed" in text
