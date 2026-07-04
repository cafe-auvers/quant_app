"""Focused tests for the historical.py refresh process control layer.

None of these tests spawn a real historical.py subprocess or touch MySQL/
yfinance -- subprocess.run/Popen and is_process_alive are monkeypatched
throughout, and all status/lock/log paths are redirected into a tmp_path.
"""
from __future__ import annotations

import subprocess
import uuid

import pytest

import src.services.historical_refresh_control as hrc


@pytest.fixture(autouse=True)
def _isolated_runtime_dirs(tmp_path, monkeypatch):
    """Redirect all status/lock/log paths into a throwaway tmp directory."""
    monkeypatch.setattr(hrc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hrc, "LOG_DIR", tmp_path / "logs")


def _write_status(mode, **overrides):
    status = hrc._default_status(mode)
    status.update(overrides)
    hrc.save_json(hrc.status_path(mode), status)
    return status


# --- read_status -------------------------------------------------------

def test_read_status_missing_file_returns_idle_default():
    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "idle"
    assert status["run_id"] is None
    assert status["pid"] is None


def test_read_status_corrupt_file_returns_idle_default():
    path = hrc.status_path(hrc.MODE_1D)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    status = hrc.read_status(hrc.MODE_1D)

    assert status["status"] == "idle"


# --- reconcile_stale_status ----------------------------------------------

def test_reconcile_stale_status_dead_pid_becomes_error(monkeypatch):
    _write_status(hrc.MODE_1D, status="running", pid=999999, run_id="r1")
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: False)

    hrc.reconcile_stale_status(hrc.MODE_1D)

    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "error"
    assert status["finished_at"] is not None
    assert "unexpectedly" in status["result"]["error_message"]


def test_reconcile_stale_status_live_pid_unchanged(monkeypatch):
    _write_status(hrc.MODE_1D, status="running", pid=123, run_id="r1")
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: True)

    hrc.reconcile_stale_status(hrc.MODE_1D)

    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "running"


def test_reconcile_stale_status_starting_dead_pid_becomes_error(monkeypatch):
    _write_status(hrc.MODE_1D, status="starting", pid=999999, run_id="r1")
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: False)

    hrc.reconcile_stale_status(hrc.MODE_1D)

    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "error"


# --- terminate_refresh -----------------------------------------------------

def test_terminate_refresh_nothing_running_returns_false():
    assert hrc.terminate_refresh(hrc.MODE_1D) is False


def test_terminate_refresh_still_alive_returns_false_and_keeps_running(monkeypatch):
    _write_status(hrc.MODE_1D, status="running", pid=123, run_id="r1")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(hrc.time, "sleep", lambda s: None)
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: True)

    result = hrc.terminate_refresh(hrc.MODE_1D, wait_seconds=0.05)

    assert result is False
    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "running"
    assert status["result"]["error_message"] == "Termination requested but process is still running."


def test_terminate_refresh_confirmed_dead_returns_true_and_marks_terminated(monkeypatch):
    _write_status(hrc.MODE_1D, status="running", pid=123, run_id="r1")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    calls = {"n": 0}

    def fake_alive(pid):
        calls["n"] += 1
        return calls["n"] == 1  # alive for the initial is_refresh_running check, dead afterwards

    monkeypatch.setattr(hrc, "is_process_alive", fake_alive)

    result = hrc.terminate_refresh(hrc.MODE_1D, wait_seconds=0.05)

    assert result is True
    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "terminated"
    assert status["finished_at"] is not None
    assert status["result"]["error_message"] is None


def test_terminate_refresh_does_not_relabel_a_run_that_finished_on_its_own(monkeypatch):
    _write_status(hrc.MODE_1D, status="running", pid=123, run_id="r1")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    calls = {"n": 0}

    def fake_alive(pid):
        calls["n"] += 1
        if calls["n"] == 1:
            return True
        if calls["n"] == 2:
            # Simulate the child legitimately completing on its own right as
            # we're about to confirm the kill took effect.
            _write_status(hrc.MODE_1D, status="completed", pid=123, run_id="r1", finished_at="done-ts")
        return False

    monkeypatch.setattr(hrc, "is_process_alive", fake_alive)

    result = hrc.terminate_refresh(hrc.MODE_1D, wait_seconds=0.05)

    assert result is False
    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "completed"  # not clobbered with "terminated"


def test_terminate_refresh_taskkill_exception_does_not_assume_success(monkeypatch):
    _write_status(hrc.MODE_1D, status="running", pid=123, run_id="r1")

    def raising_run(*args, **kwargs):
        raise OSError("taskkill not found")

    monkeypatch.setattr(subprocess, "run", raising_run)
    monkeypatch.setattr(hrc.time, "sleep", lambda s: None)
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: True)

    result = hrc.terminate_refresh(hrc.MODE_1D, wait_seconds=0.05)

    assert result is False
    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "running"


# --- launch_refresh --------------------------------------------------------

class _FakePopen:
    def __init__(self, cmd, **kwargs):
        self.args = cmd
        self.pid = 424242


def test_launch_refresh_writes_starting_status_with_run_id(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: _FakePopen(cmd, **kwargs))
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: False)

    result = hrc.launch_refresh(hrc.MODE_1D, universe_limit=5)

    assert result.run_id
    assert result.process.pid == 424242
    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "starting"
    assert status["run_id"] == result.run_id
    assert status["pid"] == 424242


def test_launch_refresh_rejects_duplicate_when_already_running(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: _FakePopen(cmd, **kwargs))
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: True)

    hrc.launch_refresh(hrc.MODE_1D, universe_limit=5)

    with pytest.raises(RuntimeError):
        hrc.launch_refresh(hrc.MODE_1D, universe_limit=5)


class _FakeUUID:
    def __init__(self, hex_value: str):
        self.hex = hex_value


def test_launch_refresh_does_not_overwrite_child_running_status(monkeypatch):
    """Regression: historical.py can write 'running' before Popen() returns to the parent."""
    monkeypatch.setattr(uuid, "uuid4", lambda: _FakeUUID("run123"))
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: False)

    def racing_popen(cmd, **kwargs):
        # Simulate the child starting extremely fast and already recording its
        # own real status for this run_id before Popen() returns control here.
        _write_status(hrc.MODE_1D, status="running", pid=555555, run_id="run123")
        return _FakePopen(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", racing_popen)

    result = hrc.launch_refresh(hrc.MODE_1D, universe_limit=5)

    assert result.run_id == "run123"
    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "running"
    assert status["pid"] == 555555


def test_launch_refresh_does_not_overwrite_child_error_status(monkeypatch):
    """Regression: a child that fails almost instantly must not be masked by 'starting'."""
    monkeypatch.setattr(uuid, "uuid4", lambda: _FakeUUID("run456"))
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: False)

    def racing_popen(cmd, **kwargs):
        _write_status(
            hrc.MODE_1D,
            status="error",
            pid=555555,
            run_id="run456",
            finished_at="2026-01-01T00:00:00+00:00",
            result={"updated_count": 0, "error_message": "boom"},
        )
        return _FakePopen(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", racing_popen)

    result = hrc.launch_refresh(hrc.MODE_1D, universe_limit=5)

    assert result.run_id == "run456"
    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "error"
    assert status["result"]["error_message"] == "boom"


def test_launch_refresh_writes_starting_when_child_has_not_reported_yet(monkeypatch):
    """No same-run_id status exists yet -- ordinary launch behavior is unchanged."""
    monkeypatch.setattr(uuid, "uuid4", lambda: _FakeUUID("run789"))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: _FakePopen(cmd, **kwargs))
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: False)

    result = hrc.launch_refresh(hrc.MODE_1D, universe_limit=5)

    assert result.run_id == "run789"
    status = hrc.read_status(hrc.MODE_1D)
    assert status["status"] == "starting"
    assert status["run_id"] == "run789"


# --- "starting" liveness: PID alive always wins over age --------------------

def test_starting_with_live_pid_is_running_regardless_of_age(monkeypatch):
    old_timestamp = "2020-01-01T00:00:00+00:00"
    _write_status(
        hrc.MODE_1D,
        status="starting",
        pid=123,
        run_id="r1",
        started_at=old_timestamp,
        updated_at=old_timestamp,
    )
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: True)

    running, status = hrc.is_refresh_running(hrc.MODE_1D)
    assert running is True

    hrc.reconcile_stale_status(hrc.MODE_1D)
    status_after = hrc.read_status(hrc.MODE_1D)
    assert status_after["status"] == "starting"


def test_starting_with_dead_pid_is_not_running(monkeypatch):
    _write_status(hrc.MODE_1D, status="starting", pid=999999, run_id="r1")
    monkeypatch.setattr(hrc, "is_process_alive", lambda pid: False)

    running, _ = hrc.is_refresh_running(hrc.MODE_1D)

    assert running is False


# --- is_process_alive / tasklist CSV parsing --------------------------------

def test_parse_tasklist_csv_exact_match():
    output = '"python.exe","1912","Console","1","23,456 K"'
    assert hrc._parse_tasklist_csv(output, 1912) is True


def test_parse_tasklist_csv_no_task():
    output = "INFO: No tasks are running which match the specified criteria."
    assert hrc._parse_tasklist_csv(output, 1912) is False


def test_parse_tasklist_csv_malformed_output():
    assert hrc._parse_tasklist_csv("not,valid,,,", 1912) is False
    assert hrc._parse_tasklist_csv("", 1912) is False


def test_parse_tasklist_csv_substring_false_positive():
    # 1912 must not match a PID field of 19123 or 191.
    assert hrc._parse_tasklist_csv('"python.exe","19123","Console","1","1 K"', 1912) is False
    assert hrc._parse_tasklist_csv('"python.exe","191","Console","1","1 K"', 1912) is False


def test_is_process_alive_uses_tasklist(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class _Result:
            returncode = 0
            stdout = '"python.exe","1912","Console","1","23,456 K"'

        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert hrc.is_process_alive(1912) is True
    assert "tasklist" in captured["cmd"]


def test_is_process_alive_invalid_pid_short_circuits():
    assert hrc.is_process_alive(None) is False
    assert hrc.is_process_alive(0) is False
    assert hrc.is_process_alive(-5) is False


# --- UI terminal-event de-duplication (no PyQt widgets touched) -------------

class _FakeUiState:
    def __init__(self):
        self._refresh_last_finished_at = {}
        self._refresh_active_run_id = {}


def test_ui_terminal_event_same_finished_at_not_retriggered():
    from src.ui.mixins.scanner_mixin import ScannerMixin

    ui = _FakeUiState()
    status = {"finished_at": "t1", "run_id": "r1"}

    assert ScannerMixin._is_new_terminal_refresh_event(ui, hrc.MODE_1D, status) is True
    assert ScannerMixin._is_new_terminal_refresh_event(ui, hrc.MODE_1D, status) is False


def test_ui_terminal_event_ignores_older_run_id_when_newer_launch_pending():
    from src.ui.mixins.scanner_mixin import ScannerMixin

    ui = _FakeUiState()
    ui._refresh_active_run_id[hrc.MODE_1D] = "new_run"
    stale_status = {"finished_at": "t1", "run_id": "old_run"}

    assert ScannerMixin._is_new_terminal_refresh_event(ui, hrc.MODE_1D, stale_status) is False


def test_ui_terminal_event_fires_for_matching_active_run_id():
    from src.ui.mixins.scanner_mixin import ScannerMixin

    ui = _FakeUiState()
    ui._refresh_active_run_id[hrc.MODE_1D] = "run_a"
    status = {"finished_at": "t1", "run_id": "run_a"}

    assert ScannerMixin._is_new_terminal_refresh_event(ui, hrc.MODE_1D, status) is True
