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
