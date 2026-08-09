import json
import threading
import time
from datetime import datetime, timezone

from src.services import app_state
from src.services.app_state import SaveResult, StateSaveManager
from src.core.watchlist import BuylistItem
import src.ui.main_window as main_window_module
from src.ui.main_window import MainWindow
from src.utils.storage import load_json, save_json


def _patch_app_state_paths(monkeypatch, tmp_path):
    paths = {
        "WATCHLIST_FILE": tmp_path / "watchlist.json",
        "BUYLIST_FILE": tmp_path / "buylist.json",
        "TRADE_PLANS_FILE": tmp_path / "trade_plans.json",
        "SCANNER_SETUPS_FILE": tmp_path / "scanner_setups.json",
        "CHART_DRAWINGS_FILE": tmp_path / "chart_drawings.json",
        "TAB_OPTIONS_FILE": tmp_path / "tab_options.json",
        "STATE_METADATA_FILE": tmp_path / "state_metadata.json",
        "LEGACY_NON_PRODUCTION_BUYLIST_FILE": tmp_path
        / "legacy_non_prod_buylist.json",
        "LEGACY_NON_PRODUCTION_EXECUTION_QUEUE_FILE": tmp_path
        / "legacy_non_prod_execution_queue.json",
    }
    for name, path in paths.items():
        monkeypatch.setattr(app_state, name, path)
    return paths


def _sample_payload():
    return (
        {"name": "Default", "items": [{"symbol": "AAPL"}]},
        {"items": [{"symbol": "MSFT"}]},
        {"plans": [{"symbol": "TSLA"}]},
        {"Setup 1": {"rules": []}},
        {"AAPL": []},
        {"dashboard": True, "scanner": False},
    )


def test_save_json_writes_atomically_and_creates_backup_on_overwrite(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")

    save_json(path, {"version": 2})

    assert load_json(path, {}) == {"version": 2}
    assert load_json(path.with_suffix(path.suffix + ".bak"), {}) == {"version": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_load_json_falls_back_to_backup_when_main_is_malformed(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("{bad json", encoding="utf-8")
    path.with_suffix(path.suffix + ".bak").write_text(
        json.dumps({"items": [{"symbol": "AAPL"}]}),
        encoding="utf-8",
    )

    assert load_json(path, {"items": []}) == {"items": [{"symbol": "AAPL"}]}
    assert len(list(tmp_path.glob("watchlist.json.corrupt-*"))) == 1


def test_load_buylist_state_quarantines_rejected_rows(tmp_path, monkeypatch):
    paths = _patch_app_state_paths(monkeypatch, tmp_path)
    valid = BuylistItem(
        symbol="AAPL",
        name="Apple",
        entry_price=100.0,
        target_price=0.0,
        stop_loss=95.0,
        total_score=80.0,
        status="WATCHING",
        technical_score=80.0,
        setup_score=80.0,
        risk_score=80.0,
        news_score=80.0,
        timing_score=80.0,
        rr=0.0,
        stop_adr=20.0,
        position_percent=10.0,
        ai_summary="",
        warnings=[],
    ).to_dict()
    invalid = dict(valid)
    invalid["symbol"] = "BROKEN"
    invalid["entry_price"] = "not-a-number"
    save_json(paths["BUYLIST_FILE"], {"items": [valid, invalid]})

    manager = app_state.load_buylist_state()

    assert [item.symbol for item in manager.items] == ["AAPL"]
    quarantine_path = tmp_path / "rejected" / "buylist.rejected.json"
    quarantined = load_json(quarantine_path, {})
    assert quarantined["records"][0]["identity"] == "BROKEN"
    assert quarantined["records"][0]["record"]["entry_price"] == "not-a-number"


def test_load_buylist_state_archives_non_production_rows_before_filtering(
    tmp_path, monkeypatch
):
    paths = _patch_app_state_paths(monkeypatch, tmp_path)
    save_json(
        paths["BUYLIST_FILE"],
        {"items": [{"symbol": "AAPL", "environment": "SIM"}]},
    )

    manager = app_state.load_buylist_state()

    assert manager.items == []
    assert load_json(paths["LEGACY_NON_PRODUCTION_BUYLIST_FILE"], {}) == {
        "items": [{"symbol": "AAPL", "environment": "SIM"}]
    }


def test_execution_queue_archive_keeps_only_non_production_rows(tmp_path):
    archive_path = tmp_path / "legacy_non_prod_execution_queue.json"
    data = {
        "upgrade_margin": 5.0,
        "items": {
            "SIM:AAPL": {"symbol": "AAPL", "environment": "SIM"},
            "PROD:MSFT": {"symbol": "MSFT", "environment": "PROD"},
        },
    }

    archived_count = app_state.archive_non_production_execution_queue_state(
        data,
        archive_path=archive_path,
    )

    assert archived_count == 1
    archived = load_json(archive_path, {})
    assert list(archived["items"]) == ["SIM:AAPL"]
    assert archived["items"]["SIM:AAPL"]["environment"] == "SIM"


def test_state_save_manager_save_now_writes_all_expected_files(tmp_path, monkeypatch):
    paths = _patch_app_state_paths(monkeypatch, tmp_path)
    manager = StateSaveManager()

    result = manager.save_now(*_sample_payload())

    assert result.success is True
    assert result.error == ""
    assert manager.last_save_status == "success"
    assert set(result.files_written) == {
        str(paths["WATCHLIST_FILE"]),
        str(paths["BUYLIST_FILE"]),
        str(paths["TRADE_PLANS_FILE"]),
        str(paths["SCANNER_SETUPS_FILE"]),
        str(paths["CHART_DRAWINGS_FILE"]),
        str(paths["TAB_OPTIONS_FILE"]),
    }
    assert load_json(paths["WATCHLIST_FILE"], {})["items"][0]["symbol"] == "AAPL"
    assert load_json(paths["BUYLIST_FILE"], {})["items"][0]["symbol"] == "MSFT"
    assert load_json(paths["TRADE_PLANS_FILE"], {})["plans"][0]["symbol"] == "TSLA"
    assert load_json(paths["SCANNER_SETUPS_FILE"], {}) == {"setups": {"Setup 1": {"rules": []}}}
    assert load_json(paths["CHART_DRAWINGS_FILE"], {}) == {"AAPL": []}
    assert load_json(paths["TAB_OPTIONS_FILE"], {}) == {"tabs": {"dashboard": True, "scanner": False}}

    metadata = load_json(paths["STATE_METADATA_FILE"], {})
    assert metadata["last_successful_save_at"]
    assert metadata["last_error"] == ""
    assert metadata["files_written"] == result.files_written


def test_state_save_manager_captures_save_exceptions(tmp_path, monkeypatch):
    _patch_app_state_paths(monkeypatch, tmp_path)
    manager = StateSaveManager()
    messages = []

    def failing_save_json(path, data):
        raise RuntimeError(f"cannot write {path.name}")

    monkeypatch.setattr(app_state, "save_json", failing_save_json)

    result = manager.save_now(*_sample_payload(), append_log=messages.append)

    assert result.success is False
    assert "cannot write watchlist.json" in result.error
    assert manager.last_save_status == "failed"
    assert manager.last_save_error == result.error
    assert any("Local app-state save failed" in message for message in messages)


def test_wait_for_pending_saves_waits_for_scheduled_save_completion(tmp_path, monkeypatch):
    paths = _patch_app_state_paths(monkeypatch, tmp_path)
    manager = StateSaveManager()
    original_save_json = app_state.save_json
    writes_started = threading.Event()

    def slow_save_json(path, data):
        writes_started.set()
        time.sleep(0.02)
        original_save_json(path, data)

    monkeypatch.setattr(app_state, "save_json", slow_save_json)

    thread = manager.schedule_save(*_sample_payload())

    assert thread.daemon is False
    assert writes_started.wait(timeout=1)
    assert manager.wait_for_pending_saves(timeout=2) is True
    assert thread.is_alive() is False
    assert load_json(paths["WATCHLIST_FILE"], {})["items"][0]["symbol"] == "AAPL"


def test_cloud_backup_retries_destination_discovery_after_throttle(tmp_path, monkeypatch):
    manager = StateSaveManager()
    clock = {"now": 100.0}
    resolutions = iter([None, tmp_path / "drive"])
    resolve_calls = []
    backup_calls = []

    def resolve():
        resolve_calls.append(clock["now"])
        return next(resolutions)

    def backup(files, root):
        backup_calls.append(root)
        return app_state.BackupResult(success=True, root=root / "quant_app_backup")

    monkeypatch.setattr(app_state.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(app_state, "CLOUD_BACKUP_THROTTLE_SECONDS", 10)
    monkeypatch.setattr(app_state, "resolve_backup_root", resolve)
    monkeypatch.setattr(app_state, "backup_state_files", backup)

    assert manager._backup_to_cloud() is None
    clock["now"] = 105.0
    assert manager._backup_to_cloud() is None
    assert resolve_calls == [100.0]

    clock["now"] = 110.0
    result = manager._backup_to_cloud()

    assert result is not None and result.success
    assert resolve_calls == [100.0, 110.0]
    assert backup_calls == [tmp_path / "drive"]


def test_superseded_scheduled_save_does_not_leave_status_running(tmp_path, monkeypatch):
    _patch_app_state_paths(monkeypatch, tmp_path)
    manager = StateSaveManager()

    with manager._pending_lock:
        manager._generation = 2

    result = manager.save_now(*_sample_payload(), _scheduled_generation=1)

    assert result.success is True
    assert result.error == "Skipped superseded app-state save."
    assert manager.last_save_status == "idle"
    assert manager.last_result is None


def test_shutdown_flush_uses_bounded_wait_and_sync_save():
    class Obj:
        def __init__(self, data):
            self.data = data

        def to_dict(self):
            return self.data

    class FakeManager:
        def __init__(self):
            self.wait_timeout = None
            self.save_timeout = None
            self.supersede_pending = None

        def wait_for_pending_saves(self, timeout=None):
            self.wait_timeout = timeout
            return True

        def save_now(self, *args, save_lock=None, append_log=None, lock_timeout=None, supersede_pending=False):
            self.save_timeout = lock_timeout
            self.supersede_pending = supersede_pending
            return SaveResult(
                success=True,
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                files_written=["watchlist.json"],
            )

    window = MainWindow.__new__(MainWindow)
    window.state_save_manager = FakeManager()
    window.watchlist = Obj({"name": "Default", "items": []})
    window.buylist_manager = Obj({"items": []})
    window.trade_manager = Obj({"plans": []})
    window.scanner_setups = {"Setup 1": {"rules": []}}
    window.chart_drawings = {}
    window.tab_options = {"dashboard": True}
    window.append_log = lambda message: None

    result = window._flush_state_saves_for_shutdown(timeout=0.5)

    assert result.success is True
    assert window.state_save_manager.wait_timeout == 0.5
    assert 0 <= window.state_save_manager.save_timeout <= 0.5
    assert window.state_save_manager.supersede_pending is True


def test_worker_shutdown_uses_one_total_timeout_budget(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(main_window_module.time, "monotonic", lambda: clock["now"])

    class Worker:
        def __init__(self, elapsed_seconds):
            self.elapsed_seconds = elapsed_seconds
            self.interrupted = False
            self.quit_called = False
            self.wait_calls = []

        def requestInterruption(self):
            self.interrupted = True

        def quit(self):
            self.quit_called = True

        def wait(self, timeout_ms):
            self.wait_calls.append(timeout_ms)
            clock["now"] += self.elapsed_seconds
            return True

    workers = [Worker(0.6), Worker(0.0)]

    assert MainWindow._stop_workers_for_shutdown(workers, timeout_ms=1000) is True

    assert all(worker.interrupted for worker in workers)
    assert all(worker.quit_called for worker in workers)
    assert workers[0].wait_calls == [1000]
    assert 0 <= workers[1].wait_calls[0] <= 400
