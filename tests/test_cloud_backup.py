from pathlib import Path

import src.services.cloud_backup as cloud_backup
from src.services.cloud_backup import (
    BACKUP_SUBDIR_NAME,
    backup_state_files,
    list_daily_snapshots,
    resolve_backup_root,
    restore_state_files,
)


def test_resolve_backup_root_prefers_explicit_path(tmp_path):
    explicit = tmp_path / "some_drive_folder"
    resolved = resolve_backup_root(str(explicit))
    assert resolved == explicit


def test_resolve_backup_root_returns_none_when_nothing_found(monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.cloud_backup.get_env_value", lambda key: None)
    monkeypatch.setattr("src.services.cloud_backup._DRIVE_LETTER_CANDIDATES", [])
    monkeypatch.setattr("src.services.cloud_backup.Path.home", lambda: tmp_path / "nonexistent_home")

    assert resolve_backup_root() is None


def test_backup_state_files_writes_current_and_daily_snapshot(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    watchlist = source_dir / "watchlist.json"
    watchlist.write_text('{"name": "Default", "items": []}', encoding="utf-8")
    buylist = source_dir / "buylist.json"
    buylist.write_text('{"items": []}', encoding="utf-8")

    backup_root = tmp_path / "drive"
    result = backup_state_files([watchlist, buylist], backup_root)

    assert result.success
    assert set(result.backed_up) == {"watchlist.json", "buylist.json"}
    assert result.daily_snapshot_created

    current_dir = backup_root / BACKUP_SUBDIR_NAME / "current"
    assert (current_dir / "watchlist.json").read_text(encoding="utf-8") == watchlist.read_text(encoding="utf-8")
    assert (current_dir / "buylist.json").exists()

    daily_dirs = list((backup_root / BACKUP_SUBDIR_NAME / "daily").iterdir())
    assert len(daily_dirs) == 1
    assert (daily_dirs[0] / "watchlist.json").exists()


def test_backup_state_files_skips_missing_sources(tmp_path):
    backup_root = tmp_path / "drive"
    missing = tmp_path / "does_not_exist.json"

    result = backup_state_files([missing], backup_root)

    assert result.success
    assert result.backed_up == []


def test_backup_state_files_second_call_same_day_does_not_recreate_daily_snapshot(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    watchlist = source_dir / "watchlist.json"
    watchlist.write_text('{"items": []}', encoding="utf-8")
    backup_root = tmp_path / "drive"

    first = backup_state_files([watchlist], backup_root)
    assert first.daily_snapshot_created

    watchlist.write_text('{"items": [{"symbol": "AAPL"}]}', encoding="utf-8")
    second = backup_state_files([watchlist], backup_root)

    assert not second.daily_snapshot_created
    daily_dirs = list((backup_root / BACKUP_SUBDIR_NAME / "daily").iterdir())
    assert len(daily_dirs) == 1
    # current/ still updates every call regardless of the daily snapshot.
    current_file = backup_root / BACKUP_SUBDIR_NAME / "current" / "watchlist.json"
    assert "AAPL" in current_file.read_text(encoding="utf-8")


def test_backup_state_files_repairs_incomplete_daily_snapshot(tmp_path, monkeypatch):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    watchlist = source_dir / "watchlist.json"
    watchlist.write_text('{"items": []}', encoding="utf-8")
    buylist = source_dir / "buylist.json"
    buylist.write_text('{"items": []}', encoding="utf-8")
    backup_root = tmp_path / "drive"

    original_atomic_copy = cloud_backup._atomic_copy
    failed_once = False

    def fail_one_daily_copy(src, dest):
        nonlocal failed_once
        if (
            not failed_once
            and src.name == "buylist.json"
            and dest.parent.parent.name == "daily"
        ):
            failed_once = True
            raise OSError("simulated interrupted Drive copy")
        original_atomic_copy(src, dest)

    monkeypatch.setattr(cloud_backup, "_atomic_copy", fail_one_daily_copy)
    first = backup_state_files([watchlist, buylist], backup_root)
    assert not first.success

    monkeypatch.setattr(cloud_backup, "_atomic_copy", original_atomic_copy)
    second = backup_state_files([watchlist, buylist], backup_root)

    assert second.success
    daily_dir = next((backup_root / BACKUP_SUBDIR_NAME / "daily").iterdir())
    assert (daily_dir / "watchlist.json").is_file()
    assert (daily_dir / "buylist.json").is_file()


def test_backup_state_files_does_not_replace_good_copy_with_invalid_json(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    watchlist = source_dir / "watchlist.json"
    watchlist.write_text('{"items": ["GOOD"]}', encoding="utf-8")
    backup_root = tmp_path / "drive"
    assert backup_state_files([watchlist], backup_root).success

    watchlist.write_text('{"items":', encoding="utf-8")
    result = backup_state_files([watchlist], backup_root)

    assert not result.success
    assert result.failed
    current = backup_root / BACKUP_SUBDIR_NAME / "current" / "watchlist.json"
    assert "GOOD" in current.read_text(encoding="utf-8")


def test_backup_state_files_prunes_old_daily_snapshots(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    watchlist = source_dir / "watchlist.json"
    watchlist.write_text("{}", encoding="utf-8")
    backup_root = tmp_path / "drive"
    daily_root = backup_root / BACKUP_SUBDIR_NAME / "daily"

    for day in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        (daily_root / day).mkdir(parents=True)

    backup_state_files([watchlist], backup_root, keep_daily_snapshots=2)

    remaining = sorted(entry.name for entry in daily_root.iterdir())
    assert "2026-01-01" not in remaining
    assert len(remaining) == 2


def test_list_daily_snapshots_returns_sorted_dates(tmp_path):
    backup_root = tmp_path / "drive"
    daily_root = backup_root / BACKUP_SUBDIR_NAME / "daily"
    for day in ["2026-02-01", "2026-01-15", "2026-01-20"]:
        (daily_root / day).mkdir(parents=True)

    assert list_daily_snapshots(backup_root) == ["2026-01-15", "2026-01-20", "2026-02-01"]


def test_snapshot_listing_and_pruning_ignore_non_date_directories(tmp_path):
    backup_root = tmp_path / "drive"
    daily_root = backup_root / BACKUP_SUBDIR_NAME / "daily"
    unexpected = daily_root / "do_not_delete"
    unexpected.mkdir(parents=True)
    for day in ["2026-01-01", "2026-01-02"]:
        (daily_root / day).mkdir()

    source = tmp_path / "watchlist.json"
    source.write_text("{}", encoding="utf-8")
    backup_state_files([source], backup_root, keep_daily_snapshots=1)

    assert unexpected.is_dir()
    assert "do_not_delete" not in list_daily_snapshots(backup_root)


def test_list_daily_snapshots_returns_empty_when_no_backup_exists(tmp_path):
    assert list_daily_snapshots(tmp_path / "drive") == []


def test_restore_state_files_from_current(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    watchlist = source_dir / "watchlist.json"
    watchlist.write_text('{"items": ["AAPL"]}', encoding="utf-8")
    backup_root = tmp_path / "drive"
    backup_state_files([watchlist], backup_root)

    target_dir = tmp_path / "restored_data"
    result = restore_state_files(backup_root, target_dir)

    assert result.success
    assert result.restored == ["watchlist.json"]
    assert result.preserved_originals_dir is None
    restored_file = target_dir / "watchlist.json"
    assert restored_file.read_text(encoding="utf-8") == watchlist.read_text(encoding="utf-8")


def test_restore_state_files_preserves_existing_local_file_first(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    watchlist = source_dir / "watchlist.json"
    watchlist.write_text('{"items": ["BACKED_UP"]}', encoding="utf-8")
    backup_root = tmp_path / "drive"
    backup_state_files([watchlist], backup_root)

    target_dir = tmp_path / "restored_data"
    target_dir.mkdir()
    existing = target_dir / "watchlist.json"
    existing.write_text('{"items": ["LOCAL_UNSAVED"]}', encoding="utf-8")

    result = restore_state_files(backup_root, target_dir)

    assert result.success
    assert result.preserved_originals_dir is not None
    preserved_file = result.preserved_originals_dir / "watchlist.json"
    assert "LOCAL_UNSAVED" in preserved_file.read_text(encoding="utf-8")
    assert "BACKED_UP" in existing.read_text(encoding="utf-8")


def test_restore_state_files_from_specific_daily_snapshot(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    watchlist = source_dir / "watchlist.json"
    watchlist.write_text('{"items": ["DAY_ONE"]}', encoding="utf-8")
    backup_root = tmp_path / "drive"

    daily_dir = backup_root / BACKUP_SUBDIR_NAME / "daily" / "2026-01-01"
    daily_dir.mkdir(parents=True)
    (daily_dir / "watchlist.json").write_text('{"items": ["DAY_ONE"]}', encoding="utf-8")

    target_dir = tmp_path / "restored_data"
    result = restore_state_files(backup_root, target_dir, snapshot="2026-01-01")

    assert result.success
    assert "DAY_ONE" in (target_dir / "watchlist.json").read_text(encoding="utf-8")


def test_restore_state_files_missing_snapshot_fails_cleanly(tmp_path):
    backup_root = tmp_path / "drive"
    target_dir = tmp_path / "restored_data"

    result = restore_state_files(backup_root, target_dir, snapshot="2020-01-01")

    assert not result.success
    assert result.restored == []
    assert "No backup found" in result.error


def test_restore_state_files_rejects_invalid_snapshot_path(tmp_path):
    result = restore_state_files(
        tmp_path / "drive",
        tmp_path / "restored_data",
        snapshot="../../current",
    )

    assert not result.success
    assert "valid YYYY-MM-DD" in result.error


def test_restore_state_files_ignores_unexpected_cloud_files(tmp_path):
    backup_root = tmp_path / "drive"
    current = backup_root / BACKUP_SUBDIR_NAME / "current"
    current.mkdir(parents=True)
    (current / "watchlist.json").write_text("{}", encoding="utf-8")
    (current / "unexpected.json").write_text('{"do": "not restore"}', encoding="utf-8")

    target = tmp_path / "restored_data"
    result = restore_state_files(backup_root, target)

    assert result.success
    assert result.restored == ["watchlist.json"]
    assert not (target / "unexpected.json").exists()


def test_restore_state_files_validates_all_files_before_overwriting(tmp_path):
    backup_root = tmp_path / "drive"
    current = backup_root / BACKUP_SUBDIR_NAME / "current"
    current.mkdir(parents=True)
    (current / "watchlist.json").write_text('{"items": ["CLOUD"]}', encoding="utf-8")
    (current / "buylist.json").write_text('{"items":', encoding="utf-8")

    target = tmp_path / "restored_data"
    target.mkdir()
    local = target / "watchlist.json"
    local.write_text('{"items": ["LOCAL"]}', encoding="utf-8")

    result = restore_state_files(backup_root, target)

    assert not result.success
    assert "LOCAL" in local.read_text(encoding="utf-8")
