"""Non-destructive Git status checks and fast-forward application updates."""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence


GIT_COMMAND_TIMEOUT_SECONDS = 30
PARENT_EXIT_TIMEOUT_SECONDS = 120
SYNC_RESULT_ENV = "QUANT_APP_GIT_SYNC_RESULT"

_GIT_OPERATION_LOCK = threading.Lock()


@dataclass(frozen=True)
class RepositoryStatus:
    """Comparison between the checked-out commit and its tracked branch."""

    repository: Path
    branch: str = ""
    upstream: str = ""
    local_revision: str = ""
    remote_revision: str = ""
    ahead_count: int = 0
    behind_count: int = 0
    dirty: bool = False
    error: str = ""

    @property
    def is_exactly_current(self) -> bool:
        return bool(
            not self.error
            and not self.dirty
            and self.ahead_count == 0
            and self.behind_count == 0
        )

    @property
    def can_fast_forward(self) -> bool:
        return bool(
            not self.error
            and not self.dirty
            and self.ahead_count == 0
            and self.behind_count > 0
        )


@dataclass(frozen=True)
class RepositorySyncResult:
    success: bool
    message: str
    status: RepositoryStatus


class GitCommandError(RuntimeError):
    pass


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    # A background health check must never wait indefinitely for terminal input.
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _clean_git_error(value: object) -> str:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    message = " ".join(lines[:3]) or "Git command failed without an error message."
    message = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", message)
    return message[:800]


def _run_git(
    repository: Path,
    arguments: Sequence[str],
    *,
    timeout: int = GIT_COMMAND_TIMEOUT_SECONDS,
    environment: Optional[Mapping[str, str]] = None,
) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=str(repository),
            env=dict(environment or _git_environment()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitCommandError("Git is not installed or is not available on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(
            f"Git did not respond within {timeout} seconds."
        ) from exc
    except OSError as exc:
        raise GitCommandError(_clean_git_error(exc)) from exc
    if completed.returncode != 0:
        raise GitCommandError(_clean_git_error(completed.stderr or completed.stdout))
    return completed.stdout.strip()


def _status_error(repository: Path, exc: object, **values: object) -> RepositoryStatus:
    return RepositoryStatus(
        repository=repository,
        error=_clean_git_error(exc),
        **values,
    )


def inspect_repository(
    repository: str | Path,
    *,
    fetch: bool = True,
) -> RepositoryStatus:
    """Fetch and compare HEAD with its upstream without changing working files."""

    repo = Path(repository).resolve()
    branch = ""
    upstream = ""
    with _GIT_OPERATION_LOCK:
        try:
            if _run_git(repo, ["rev-parse", "--is-inside-work-tree"]) != "true":
                raise GitCommandError("The application directory is not a Git repository.")
            branch = _run_git(repo, ["branch", "--show-current"])
            if not branch:
                raise GitCommandError("Git HEAD is detached; no tracked branch can be synced.")
            upstream = _run_git(
                repo,
                ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            )
            if fetch:
                remote = _run_git(repo, ["config", "--get", f"branch.{branch}.remote"])
                if remote and remote != ".":
                    _run_git(repo, ["fetch", "--quiet", "--prune", remote])

            local_revision = _run_git(repo, ["rev-parse", "--short", "HEAD"])
            remote_revision = _run_git(repo, ["rev-parse", "--short", "@{u}"])
            counts = _run_git(
                repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"]
            ).split()
            if len(counts) != 2:
                raise GitCommandError("Git returned an invalid branch comparison.")
            ahead_count, behind_count = (int(value) for value in counts)
            dirty = bool(_run_git(repo, ["status", "--porcelain"]))
            return RepositoryStatus(
                repository=repo,
                branch=branch,
                upstream=upstream,
                local_revision=local_revision,
                remote_revision=remote_revision,
                ahead_count=ahead_count,
                behind_count=behind_count,
                dirty=dirty,
            )
        except (GitCommandError, TypeError, ValueError) as exc:
            return _status_error(repo, exc, branch=branch, upstream=upstream)


def synchronize_repository(repository: str | Path) -> RepositorySyncResult:
    """Update a clean checkout using a fast-forward merge only."""

    repo = Path(repository).resolve()
    before = inspect_repository(repo, fetch=True)
    if before.error:
        return RepositorySyncResult(False, before.error, before)
    if before.dirty:
        return RepositorySyncResult(
            False,
            "Automatic Git sync was blocked because the repository has local changes.",
            before,
        )
    if before.ahead_count:
        return RepositorySyncResult(
            False,
            "Automatic Git sync was blocked because the local branch has commits "
            "that are not on its tracked remote branch.",
            before,
        )
    if before.behind_count == 0:
        return RepositorySyncResult(
            True,
            f"Already synced with {before.upstream} at {before.local_revision}.",
            before,
        )

    try:
        with _GIT_OPERATION_LOCK:
            _run_git(repo, ["merge", "--ff-only", "@{u}"])
    except GitCommandError as exc:
        failed = inspect_repository(repo, fetch=False)
        return RepositorySyncResult(False, _clean_git_error(exc), failed)

    after = inspect_repository(repo, fetch=False)
    if not after.is_exactly_current:
        message = after.error or "Git sync finished, but the checkout is not exactly current."
        return RepositorySyncResult(False, message, after)
    return RepositorySyncResult(
        True,
        f"Synced {after.branch} with {after.upstream} at {after.local_revision}.",
        after,
    )


def _process_is_running(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(synchronize, False, process_id)
        if not handle:
            # If Windows refuses inspection, fail closed and keep waiting
            # rather than changing application files under a possibly-live PID.
            return ctypes.get_last_error() == access_denied
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_exit(
    process_id: int,
    *,
    timeout: float = PARENT_EXIT_TIMEOUT_SECONDS,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while _process_is_running(process_id):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


def launch_sync_helper(
    repository: str | Path,
    *,
    parent_process_id: int,
    python_executable: str | Path,
    main_script: str | Path,
) -> subprocess.Popen:
    """Start the updater that waits for this application process to exit."""

    repo = Path(repository).resolve()
    command = [
        str(python_executable),
        "-m",
        "src.services.repository_sync",
        "--wait-pid",
        str(parent_process_id),
        "--repository",
        str(repo),
        "--python",
        str(python_executable),
        "--main",
        str(Path(main_script).resolve()),
    ]
    kwargs: dict[str, object] = {"cwd": str(repo), "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, **kwargs)


def _relaunch(
    python_executable: str | Path,
    main_script: str | Path,
    repository: str | Path,
    result: RepositorySyncResult,
) -> None:
    environment = dict(os.environ)
    prefix = "success" if result.success else "failure"
    environment[SYNC_RESULT_ENV] = f"{prefix}:{result.message}"
    kwargs: dict[str, object] = {
        "cwd": str(Path(repository).resolve()),
        "env": environment,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [str(python_executable), str(Path(main_script).resolve())],
        **kwargs,
    )


def _parse_arguments(arguments: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update quant_app and relaunch it.")
    parser.add_argument("--wait-pid", required=True, type=int)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--main", required=True)
    return parser.parse_args(arguments)


def main(arguments: Optional[Sequence[str]] = None) -> int:
    options = _parse_arguments(arguments)
    if not wait_for_process_exit(options.wait_pid):
        return 2
    result = synchronize_repository(options.repository)
    try:
        _relaunch(options.python, options.main, options.repository, result)
    except OSError:
        return 3
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
