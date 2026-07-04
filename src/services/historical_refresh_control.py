"""Launch/monitor/terminate the standalone historical.py refresh process.

The 1D and 1H data refreshes run as detached OS subprocesses (historical.py)
instead of in-process QThread workers, so closing the main window has no
effect on an in-flight refresh. This module is the only place that knows how
to talk to that subprocess: it owns the status-file schema, PID liveness
checks, and launch/terminate mechanics, so the UI mixins only ever call these
functions and never touch subprocess/taskkill/status-file details directly.

Status file lifecycle: idle -> starting -> running -> completed | error | terminated.
"starting" is written by launch_refresh() itself (with the real PID) the
moment Popen() returns, so a stale terminal status from a previous run_id is
never the freshest thing on disk once a new launch has happened. historical.py
overwrites the same run_id's record to "running" shortly after.
"""
from __future__ import annotations

import csv
import io
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.utils.storage import load_json, save_json

MODE_1D = "1d"
MODE_1H = "1h"
REFRESH_MODES = (MODE_1D, MODE_1H)

# Phases each mode must complete before its derived data (indicators/scanner
# metrics for 1D; the hourly bars themselves for 1H) is considered consistent
# with the freshly-saved price history.
REQUIRED_PHASES: Dict[str, Tuple[str, ...]] = {
    MODE_1D: ("daily_history", "chart_indicators", "scanner_metrics"),
    MODE_1H: ("hourly_history",),
}

# Retained for diagnostics/logging only. "starting" liveness is now decided
# solely by whether the PID is alive (see is_refresh_running()) — a live
# child that is merely slow to flip its own status to "running" must not be
# treated as inactive just because this much time has passed.
STARTING_STATUS_MAX_AGE_SECONDS = 30.0

REPO_ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_SCRIPT_PATH = REPO_ROOT / "historical.py"
DATA_DIR = REPO_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"


@dataclass
class LaunchResult:
    process: subprocess.Popen
    run_id: str


def status_path(mode: str) -> Path:
    return DATA_DIR / f"refresh_status_{mode}.json"


def lock_path(mode: str) -> Path:
    return DATA_DIR / f"refresh_lock_{mode}.lock"


def log_path(mode: str) -> Path:
    return LOG_DIR / f"historical_{mode}.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_derived_data_complete(mode: str, completed_phases: Optional[List[str]]) -> bool:
    required = set(REQUIRED_PHASES.get(mode, ()))
    return required.issubset(set(completed_phases or []))


def _default_status(mode: str) -> Dict[str, Any]:
    return {
        "schema_version": 2,
        "mode": mode,
        "status": "idle",
        "run_id": None,
        "pid": None,
        "started_at": None,
        "updated_at": None,
        "finished_at": None,
        "backfill": False,
        "phase": "",
        "progress": {},
        "recent_log": [],
        "completed_phases": [],
        "derived_data_complete": False,
        "result": {},
    }


def read_status(mode: str) -> Dict[str, Any]:
    """Tolerant status read; an absent/corrupt file reads back as idle."""
    return load_json(status_path(mode), _default_status(mode))


# --- Windows process liveness -----------------------------------------------
# Isolated here so the rest of the module stays OS-agnostic in intent, even
# though this app currently only targets Windows.

def _parse_tasklist_csv(output: str, pid: int) -> bool:
    """Parse `tasklist /FO CSV /NH` output for an exact PID column match.

    Guards against substring false positives (e.g. PID 123 matching within
    "1234") by parsing the PID as its own CSV field and comparing as an int,
    and against malformed/no-match output (tasklist prints a plain
    "INFO: No tasks..." line, which is not valid CSV).
    """
    output = output.strip()
    if not output or output.startswith("INFO:"):
        return False
    try:
        rows = list(csv.reader(io.StringIO(output)))
    except csv.Error:
        return False
    for row in rows:
        if len(row) < 2:
            continue
        pid_field = row[1].strip()
        if pid_field.isdigit() and int(pid_field) == pid:
            return True
    return False


def is_process_alive(pid: Optional[int]) -> bool:
    """Windows PID liveness check via tasklist (no psutil dependency)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return _parse_tasklist_csv(result.stdout, pid)


def _taskkill(pid: int) -> None:
    """Best-effort forced kill. Never raises — actual success is verified by the caller via is_process_alive."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# --- Status queries ----------------------------------------------------------

def is_refresh_running(mode: str) -> Tuple[bool, Dict[str, Any]]:
    """Returns (running, status_dict).

    'running' requires status=='running' and a live PID. 'starting' counts as
    running for as long as its PID is alive — the parent process is alive and
    working, it just hasn't (yet) overwritten its own transient 'starting'
    record with 'running'. Only a dead PID makes a 'starting' record inactive.
    """
    status = read_status(mode)
    state = status.get("status")
    if state == "running":
        return is_process_alive(status.get("pid")), status
    if state == "starting":
        return is_process_alive(status.get("pid")), status
    return False, status


def reconcile_stale_status(mode: str) -> None:
    """Self-heal a 'running'/'starting' status whose process is actually dead or stuck."""
    status = read_status(mode)
    if status.get("status") not in ("running", "starting"):
        return
    running, _ = is_refresh_running(mode)
    if running:
        return
    now = _now_iso()
    status["status"] = "error"
    result = status.get("result") or {}
    result["error_message"] = result.get("error_message") or (
        "Process terminated unexpectedly (no clean shutdown detected)."
    )
    status["result"] = result
    status["finished_at"] = now
    status["updated_at"] = now
    save_json(status_path(mode), status)


# --- Launch / terminate --------------------------------------------------

def launch_refresh(
    mode: str,
    backfill: bool = False,
    universe_limit: Optional[int] = None,
) -> LaunchResult:
    """Launch historical.py as a detached subprocess for the given mode.

    Writes a 'starting' status (with the real PID and a fresh run_id)
    immediately after Popen() returns, so any stale terminal status left by a
    previous run_id is no longer the freshest thing on disk by the time the
    caller's next status poll runs. historical.py overwrites this same
    run_id's record to 'running' shortly after it starts.

    The child can start and write its own status (running/completed/error/
    terminated) before this function gets to write 'starting' -- Popen()
    returning control to the parent is not synchronized with the child's own
    execution. So the current status is re-read right before writing, and if
    the child has already recorded a status for this same run_id, that record
    is left untouched rather than being clobbered with a stale 'starting'.
    """
    reconcile_stale_status(mode)
    running, _ = is_refresh_running(mode)
    if running:
        raise RuntimeError(f"A {mode} refresh is already running.")

    run_id = uuid.uuid4().hex
    cmd = [sys.executable, str(HISTORICAL_SCRIPT_PATH), "--mode", mode, "--run-id", run_id]
    if backfill:
        cmd.append("--backfill")
    if universe_limit:
        cmd.extend(["--universe-limit", str(universe_limit)])

    log_file = log_path(mode)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NO_WINDOW
    )
    with log_file.open("w", encoding="utf-8") as log_fh:
        process = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            creationflags=creationflags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )

    current = read_status(mode)
    if current.get("run_id") == run_id and current.get("status") in (
        "running", "completed", "error", "terminated",
    ):
        # The child already recorded its own status for this run_id -- do not
        # clobber a real (and possibly already-terminal) status with 'starting'.
        return LaunchResult(process=process, run_id=run_id)

    now = _now_iso()
    starting_status = _default_status(mode)
    starting_status.update({
        "status": "starting",
        "run_id": run_id,
        "pid": process.pid,
        "started_at": now,
        "updated_at": now,
        "backfill": backfill,
    })
    save_json(status_path(mode), starting_status)

    return LaunchResult(process=process, run_id=run_id)


def terminate_refresh(mode: str, wait_seconds: float = 3.0) -> bool:
    """Force-terminate the running refresh for a mode.

    Returns True only when the PID is confirmed dead *and* that death can be
    attributed to this termination request. Returns False if nothing was
    running, if the process is still alive after waiting (status is kept as
    'running' with an explanatory result.error_message in that case), or if
    the child turned out to have already written its own completed/error
    status before the kill could take effect (that authentic status is left
    untouched rather than being relabeled 'terminated').

    Force-kill is safe here because durability comes from per-batch DB upserts
    (see docs/historical_refactor_plan.md), not from a graceful in-process
    shutdown handler — no benefit to a softer signal-based stop first.
    """
    running, status = is_refresh_running(mode)
    if not running:
        return False

    pid = status["pid"]
    run_id = status.get("run_id")
    _taskkill(pid)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline and is_process_alive(pid):
        time.sleep(0.25)

    now = _now_iso()
    current = read_status(mode)

    if is_process_alive(pid):
        result = current.get("result") or {}
        result["error_message"] = "Termination requested but process is still running."
        current["status"] = "running"
        current["result"] = result
        current["updated_at"] = now
        save_json(status_path(mode), current)
        return False

    if current.get("run_id") == run_id and current.get("status") not in ("completed", "error", "terminated"):
        # Died without writing its own terminal status -- our kill caused this.
        current["status"] = "terminated"
        current["updated_at"] = now
        current["finished_at"] = now
        result = current.get("result") or {}
        result["error_message"] = None
        current["result"] = result
        save_json(status_path(mode), current)
        return True

    # Either the child already wrote its own completed/error status before
    # dying, or a newer run has since taken over this mode's status file --
    # in both cases it would be inaccurate to relabel it "terminated".
    return False
