"""Launch/monitor/terminate the standalone historical.py refresh process.

The 1D and 1H data refreshes run as detached OS subprocesses (historical.py)
instead of in-process QThread workers, so closing the main window has no
effect on an in-flight refresh. This module is the only place that knows how
to talk to that subprocess: it owns the status-file schema, PID liveness
checks, and launch/terminate mechanics, so the UI mixins only ever call these
functions and never touch subprocess/taskkill/status-file details directly.
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.utils.storage import load_json, save_json

MODE_1D = "1d"
MODE_1H = "1h"
REFRESH_MODES = (MODE_1D, MODE_1H)

REPO_ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_SCRIPT_PATH = REPO_ROOT / "historical.py"
DATA_DIR = REPO_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"


def status_path(mode: str) -> Path:
    return DATA_DIR / f"refresh_status_{mode}.json"


def lock_path(mode: str) -> Path:
    return DATA_DIR / f"refresh_lock_{mode}.lock"


def log_path(mode: str) -> Path:
    return LOG_DIR / f"historical_{mode}.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_status(mode: str) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": mode,
        "status": "idle",
        "run_id": None,
        "pid": None,
        "started_at": None,
        "updated_at": None,
        "finished_at": None,
        "phase": "",
        "progress": {},
        "recent_log": [],
        "result": {},
    }


def read_status(mode: str) -> Dict[str, Any]:
    """Tolerant status read; an absent/corrupt file reads back as idle."""
    return load_json(status_path(mode), _default_status(mode))


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
    output = result.stdout.strip()
    return bool(output) and str(pid) in output and "No tasks" not in output


def is_refresh_running(mode: str) -> Tuple[bool, Dict[str, Any]]:
    """Returns (running, status_dict). running requires status=='running' AND a live PID."""
    status = read_status(mode)
    if status.get("status") != "running":
        return False, status
    if not is_process_alive(status.get("pid")):
        return False, status
    return True, status


def reconcile_stale_status(mode: str) -> None:
    """Self-heal a 'running' status whose PID is actually dead (crash / manual kill)."""
    status = read_status(mode)
    if status.get("status") != "running" or is_process_alive(status.get("pid")):
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


def launch_refresh(
    mode: str,
    backfill: bool = False,
    universe_limit: Optional[int] = None,
) -> subprocess.Popen:
    """Launch historical.py as a detached subprocess for the given mode.

    Does not pre-write the status file — historical.py writes status='running'
    itself moments after starting, avoiding a race where main.py would read a
    'running' record for a PID that doesn't exist yet.
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
    return process


def terminate_refresh(mode: str, wait_seconds: float = 3.0) -> bool:
    """Force-terminate the running refresh for a mode. Returns False if nothing was running.

    Force-kill is safe here because durability comes from per-batch DB upserts
    (see docs/historical_refactor_plan.md), not from a graceful in-process
    shutdown handler — no benefit to a softer signal-based stop first.
    """
    running, status = is_refresh_running(mode)
    if not running:
        return False

    pid = status["pid"]
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline and is_process_alive(pid):
        time.sleep(0.25)

    now = _now_iso()
    status["status"] = "terminated"
    status["updated_at"] = now
    status["finished_at"] = now
    result = status.get("result") or {}
    result["error_message"] = None
    status["result"] = result
    save_json(status_path(mode), status)
    return True
