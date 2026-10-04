from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_background_launcher_logs_startup_failures_and_preserves_previous_log(tmp_path):
    log = tmp_path / "logs" / "web.log"
    log.parent.mkdir()
    log.write_text("previous startup\n", encoding="utf-8")
    script = """
import runpy
import sys

module = runpy.run_path('scripts/run_web.py')
main = module['main']
def fail_config(_path):
    raise RuntimeError('configuration unavailable')
main.__globals__['load_web_config'] = fail_config
sys.argv = ['run_web.py', '--log-file', sys.argv[1]]
raise SystemExit(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(log)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    saved = log.read_text(encoding="utf-8")
    assert saved.startswith("previous startup\n")
    assert "Starting web dashboard" in saved
    assert "Traceback" in saved
    assert "RuntimeError: configuration unavailable" in saved


def test_background_launcher_captures_server_output_and_creates_log_directory(tmp_path):
    log = tmp_path / "new" / "logs" / "web.log"
    script = """
import runpy
import sys

module = runpy.run_path('scripts/run_web.py')
main = module['main']
main.__globals__['load_web_config'] = lambda path: path
main.__globals__['with_runtime_overrides'] = lambda config, **kwargs: config
def fake_server(_config):
    print('server ready')
    print('server diagnostic', file=sys.stderr)
main.__globals__['run'] = fake_server
sys.argv = ['run_web.py', '--log-file', sys.argv[1]]
raise SystemExit(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(log)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    saved = log.read_text(encoding="utf-8")
    assert "server ready" in saved
    assert "server diagnostic" in saved


def test_supervisor_restarts_after_both_failure_and_clean_exit(tmp_path):
    supervise = runpy.run_path(str(ROOT / "scripts" / "supervise_web.py"))["supervise"]
    marker = tmp_path / "launches.txt"
    log = tmp_path / "supervisor.log"
    command = [sys.executable, "-c", r"""
from pathlib import Path
import sys
marker = Path(sys.argv[1])
previous = marker.read_text() if marker.exists() else ''
marker.write_text(previous + 'launched\n')
print('child output', flush=True)
raise SystemExit(3 if not previous else 0)
""", str(marker)]
    delays = []

    def stop_after_two_restarts(seconds):
        delays.append(seconds)
        if len(delays) == 2:
            raise InterruptedError("test complete")

    with pytest.raises(InterruptedError, match="test complete"):
        supervise(command, log_path=log, wait=stop_after_two_restarts)
    assert marker.read_text().splitlines() == ["launched", "launched"]
    assert delays == [60, 60]
    saved = log.read_text(encoding="utf-8")
    assert "exited with code 3" in saved
    assert "exited with code 0" in saved
    assert saved.count("child output") == 2


def test_supervisor_backs_off_when_interpreter_is_unavailable(tmp_path):
    supervise = runpy.run_path(str(ROOT / "scripts" / "supervise_web.py"))["supervise"]
    log = tmp_path / "supervisor.log"
    delays = []

    def stop_after_backoff(seconds):
        delays.append(seconds)
        raise InterruptedError("test complete")

    with pytest.raises(InterruptedError, match="test complete"):
        supervise([str(tmp_path / "missing-python")], log_path=log, wait=stop_after_backoff)
    assert delays == [60]
    assert "could not start (FileNotFoundError)" in log.read_text(encoding="utf-8")
