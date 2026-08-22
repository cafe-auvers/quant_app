from pathlib import Path

from scripts.check_repository_hygiene import forbidden_tracked_paths
from scripts.check_requirements_lock import requirement_lock_errors


ROOT = Path(__file__).resolve().parents[1]


def test_repository_requirements_and_lock_are_consistent():
    assert requirement_lock_errors(
        ROOT / "requirements.txt", ROOT / "requirements.lock"
    ) == ()


def test_requirement_lock_check_reports_a_mismatched_direct_pin(tmp_path):
    requirements = tmp_path / "requirements.txt"
    lock = tmp_path / "requirements.lock"
    requirements.write_text("websockets==15.0.1\n", encoding="utf-8")
    lock.write_text("websockets==17.0.1 \\\n+    --hash=sha256:abc\n", encoding="utf-8")

    assert requirement_lock_errors(requirements, lock) == (
        "websockets==15.0.1 does not allow locked version 17.0.1",
    )


def test_repository_hygiene_rejects_runtime_backups_and_credentials():
    assert forbidden_tracked_paths(
        [
            "src/core/scanner.py",
            ".env.example",
            ".env.pc",
            "data/pre_restore_backup_20260809/orders.json",
            "config/client.key",
        ]
    ) == (
        ".env.pc",
        "config/client.key",
        "data/pre_restore_backup_20260809/orders.json",
    )
