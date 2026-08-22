import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from src.services import health
from src.services.repository_sync import (
    RepositoryStatus,
    _process_is_running,
    inspect_repository,
    synchronize_repository,
)
from src.ui.mixins.dashboard_mixin import DashboardMixin


def _git(repository: Path, *arguments: str) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Quant App Tests",
            "GIT_AUTHOR_EMAIL": "quant-app@example.invalid",
            "GIT_COMMITTER_NAME": "Quant App Tests",
            "GIT_COMMITTER_EMAIL": "quant-app@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(repository),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    pc = tmp_path / "pc"
    remote.mkdir()
    source.mkdir()
    _git(remote, "init", "--bare")
    _git(source, "init")
    _git(source, "checkout", "-b", "main")
    (source / "version.txt").write_text("version 1\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "Add version one")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", "--branch", "main", str(remote), str(pc))
    return source, pc, remote


def _publish_version_two(source: Path) -> None:
    (source / "version.txt").write_text("version 2\n", encoding="utf-8")
    _git(source, "add", "version.txt")
    _git(source, "commit", "-m", "Add version two")
    _git(source, "push")


def test_repository_status_detects_remote_commit_and_fast_forwards(tmp_path):
    source, pc, _remote = _repositories(tmp_path)
    _publish_version_two(source)

    before = inspect_repository(pc, fetch=True)

    assert before.error == ""
    assert before.ahead_count == 0
    assert before.behind_count == 1
    assert before.dirty is False
    assert before.can_fast_forward is True

    result = synchronize_repository(pc)

    assert result.success is True
    assert result.status.is_exactly_current is True
    assert (pc / "version.txt").read_text(encoding="utf-8") == "version 2\n"


def test_repository_sync_refuses_to_overwrite_local_changes(tmp_path):
    source, pc, _remote = _repositories(tmp_path)
    _publish_version_two(source)
    (pc / "version.txt").write_text("local PC edit\n", encoding="utf-8")

    result = synchronize_repository(pc)

    assert result.success is False
    assert result.status.behind_count == 1
    assert result.status.dirty is True
    assert "local changes" in result.message.lower()
    assert (pc / "version.txt").read_text(encoding="utf-8") == "local PC edit\n"


class _Button:
    def __init__(self) -> None:
        self.text = ""
        self.enabled = None
        self.tooltip = ""

    def setText(self, value: str) -> None:
        self.text = value

    def setEnabled(self, value: bool) -> None:
        self.enabled = value

    def setToolTip(self, value: str) -> None:
        self.tooltip = value


def test_dashboard_git_button_is_enabled_only_when_remote_is_behind():
    button = _Button()
    window = SimpleNamespace(git_sync_button=button)
    behind = RepositoryStatus(
        Path("repo"),
        branch="main",
        upstream="origin/main",
        behind_count=2,
    )

    DashboardMixin._apply_repository_status(window, behind)

    assert button.text == "Recent Git Not Synced"
    assert button.enabled is True

    current = RepositoryStatus(
        Path("repo"),
        branch="main",
        upstream="origin/main",
        local_revision="abc123",
        remote_revision="abc123",
    )
    DashboardMixin._apply_repository_status(window, current)

    assert button.text == "Synced with Most Recent Git"
    assert button.enabled is False


def test_health_reports_whether_most_recent_git_is_synced():
    behind = RepositoryStatus(
        Path("repo"),
        branch="main",
        upstream="origin/main",
        local_revision="abc123",
        remote_revision="def456",
        behind_count=1,
    )
    stale_check = health._repository_check(behind)

    assert stale_check.level == health.HealthLevel.WARNING
    assert stale_check.summary == "Recent Git is not synced"

    current = RepositoryStatus(
        Path("repo"),
        branch="main",
        upstream="origin/main",
        local_revision="def456",
        remote_revision="def456",
    )
    current_check = health._repository_check(current)

    assert current_check.level == health.HealthLevel.HEALTHY
    assert current_check.summary == "Synced with most recent Git"


def test_sync_helper_can_detect_its_live_parent_process():
    assert _process_is_running(os.getpid()) is True
    assert _process_is_running(0) is False
