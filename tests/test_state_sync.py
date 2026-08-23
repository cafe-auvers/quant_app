"""Conflict-safe cross-machine state synchronization tests."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from src.services import app_state
from src.services import runtime_status
from src.services import state_sync as ss
from src.core.execution_config import COORDINATION_RU_PROFILE
from src.core.runtime_readiness import RuntimeDeviceState
from src.services.runtime_device_state_repository import save_runtime_device_state
import src.ui.main_window as main_window_module
from src.ui.main_window import MainWindow


def _make_heartbeat_stale(engine, hostname: str, *, minutes: float = 6) -> None:
    """Force a previously-recorded heartbeat to look old, like test_pc_runtime_status.py."""
    stale_time = (
        dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        - dt.timedelta(minutes=minutes)
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE app_runtime_status "
                "SET heartbeat_at = :heartbeat_at "
                "WHERE hostname = :hostname AND process_name = 'main.py'"
            ),
            {"heartbeat_at": stale_time, "hostname": hostname.lower()},
        )


def _make_engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'shared.db'}",
        future=True,
        poolclass=NullPool,
    )


def _lease_kwargs(ownership):
    lease = ownership.main_device
    assert lease is not None
    return {
        "expected_lease_token": lease.lease_token,
        "expected_lease_epoch": lease.lease_epoch,
    }


def _current_lease_kwargs(engine):
    return _lease_kwargs(ss.get_main_device(engine))


def _use_machine(monkeypatch, root):
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "WATCHLIST_FILE": root / "watchlist.json",
        "BUYLIST_FILE": root / "buylist.json",
        "TRADE_PLANS_FILE": root / "trade_plans.json",
        "EXECUTION_QUEUE_FILE": root / "execution_queue.json",
        "STATE_METADATA_FILE": root / "state_metadata.json",
        "SCANNER_SETUPS_FILE": root / "scanner_setups.json",
        "CHART_DRAWINGS_FILE": root / "chart_drawings.json",
        "TAB_OPTIONS_FILE": root / "tab_options.json",
    }
    for name, path in paths.items():
        monkeypatch.setattr(app_state, name, path)
    monkeypatch.setattr(ss, "LOCAL_DEVICE_ROLE_FILE", root / "device_role.json")
    return paths


def _save_local_state(paths, watchlist, buylist=None, trade_plans=None):
    app_state.save_json(paths["WATCHLIST_FILE"], watchlist)
    app_state.save_json(paths["BUYLIST_FILE"], buylist or {"items": []})
    app_state.save_json(
        paths["TRADE_PLANS_FILE"], trade_plans or {"plans": []}
    )


def _remote(engine, state_key):
    pulled = ss.pull_state(engine, state_key)
    assert pulled.status == ss.PULL_OK
    assert pulled.state is not None
    return pulled.state


def test_new_device_role_defaults_to_pull_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ss.platform, "node", lambda: "PC")

    role = ss.load_local_device_role(tmp_path / "device_role.json")

    assert role.hostname == "PC"
    assert role.is_main is False
    assert role.device_id


def test_copied_role_file_resets_identity_and_main_flag(tmp_path, monkeypatch):
    path = tmp_path / "device_role.json"
    ss.save_local_device_role(
        ss.LocalDeviceRole("laptop-id", "LAPTOP", True),
        path=path,
    )
    monkeypatch.setattr(ss.platform, "node", lambda: "PC")

    role = ss.load_local_device_role(path)

    assert role.device_id != "laptop-id"
    assert role.hostname == "PC"
    assert role.is_main is False


def test_local_operational_metadata_does_not_reuse_shared_db_revisions(
    tmp_path, monkeypatch
):
    paths = _use_machine(monkeypatch, tmp_path / "laptop")
    local_payload = {"name": "Local", "items": [{"symbol": "WEX"}]}
    _save_local_state(paths, local_payload)
    app_state.save_json(
        paths["STATE_METADATA_FILE"],
        {
            "state_sync": {
                app_state.WATCHLIST_KEY: {
                    "revision": 99,
                    "content_hash": "old-shared-database-hash",
                    "updated_at": "",
                }
            }
        },
    )
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", False)
    local_metadata = tmp_path / "kanban_state_metadata.json"

    result = app_state.activate_device_as_main(
        engine,
        role,
        metadata_path=local_metadata,
    )

    assert result.errors == []
    assert result.conflict_keys == set()
    assert result.is_main_device is True
    assert _remote(engine, app_state.WATCHLIST_KEY).payload == local_payload
    assert local_metadata.exists()


def test_main_device_claim_is_exclusive(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)

    assert ss.claim_main_device(engine, laptop).success
    assert ss.get_main_device(engine).main_device.device_id == "laptop-id"

    assert ss.claim_main_device(engine, pc).success
    owner = ss.get_main_device(engine).main_device
    assert owner.device_id == "pc-id"
    assert owner.hostname == "PC"


def test_push_then_pull_uses_server_revision(tmp_path):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    assert ss.claim_main_device(engine, role).success

    first = ss.push_state(
        engine,
        ss.WATCHLIST_KEY,
        {"items": [{"symbol": "AAPL"}]},
        device_id=role.device_id,
        expected_revision=0,
    )
    second = ss.push_state(
        engine,
        ss.WATCHLIST_KEY,
        {"items": [{"symbol": "MSFT"}]},
        device_id=role.device_id,
        expected_revision=first.revision,
    )

    assert first.status == ss.PUSH_WRITTEN
    assert second.status == ss.PUSH_WRITTEN
    assert second.revision == first.revision + 1
    remote = _remote(engine, ss.WATCHLIST_KEY)
    assert remote.payload == {"items": [{"symbol": "MSFT"}]}
    assert remote.revision == second.revision


def test_pull_distinguishes_missing_from_database_error(tmp_path):
    engine = _make_engine(tmp_path)

    assert ss.pull_state(engine, ss.BUYLIST_KEY).status == ss.PULL_MISSING
    unavailable = ss.pull_state(None, ss.BUYLIST_KEY)
    assert unavailable.status == ss.PULL_ERROR
    assert unavailable.error


def test_live_trading_control_is_shared_and_independent_of_main_owner(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", False)
    pc = ss.LocalDeviceRole("pc-id", "PC", True)
    assert ss.claim_main_device(engine, pc).success

    missing = ss.get_live_trading_control(engine)
    assert missing.success is True
    assert missing.control is not None
    assert missing.control.enabled is False
    assert missing.control.revision == 0

    enabled = ss.set_live_trading_control(engine, laptop, True)
    assert enabled.success is True
    assert enabled.control is not None
    assert enabled.control.enabled is True
    assert enabled.control.revision == 1
    assert ss.get_main_device(engine).main_device.device_id == "pc-id"

    observed_on_pc = ss.get_live_trading_control(engine)
    assert observed_on_pc.control.enabled is True
    disabled = ss.set_live_trading_control(engine, pc, False)
    assert disabled.control.enabled is False
    assert disabled.control.revision == 2
    assert ss.get_live_trading_control(engine).control.enabled is False


def test_live_trading_control_survives_restart_and_main_handoff(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)

    assert ss.set_live_trading_control(engine, laptop, True).success
    assert ss.claim_main_device(engine, laptop).success
    assert ss.claim_main_device(engine, pc).success

    restarted_reader = ss.get_live_trading_control(engine)
    assert restarted_reader.success is True
    assert restarted_reader.control.enabled is True
    assert restarted_reader.control.revision == 1
    assert ss.get_main_device(engine).main_device.device_id == "pc-id"


def test_live_trading_control_database_failure_is_not_treated_as_off(tmp_path):
    result = ss.get_live_trading_control(None)

    assert result.success is False
    assert result.control is None
    assert result.error


def test_coordination_status_snapshot_combines_control_and_revision_reads(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    assert ss.claim_main_device(engine, laptop).success
    assert ss.set_live_trading_control(engine, laptop, True).success
    assert ss.set_operator_control(engine, laptop, laptop).success
    expected_revisions = {}
    for state_key in ss.SYNCED_STATE_KEYS:
        pushed = ss.push_state(
            engine,
            state_key,
            {"items": []},
            device_id=laptop.device_id,
            expected_revision=0,
        )
        assert pushed.status == ss.PUSH_WRITTEN
        expected_revisions[state_key] = pushed.revision

    app_state_selects = []

    def record_statement(_conn, _cursor, statement, _params, _context, _many):
        normalized = statement.lower().lstrip()
        if normalized.startswith("select") and "app_state_sync" in normalized:
            app_state_selects.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        snapshot = ss.get_coordination_status_snapshot(engine)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert len(app_state_selects) == 1
    assert snapshot.live_trading.success is True
    assert snapshot.live_trading.control is not None
    assert snapshot.live_trading.control.enabled is True
    assert snapshot.operator_control.success is True
    assert snapshot.operator_control.control is not None
    assert snapshot.operator_control.control.device_id == laptop.device_id
    assert snapshot.state_revisions == expected_revisions


def test_pull_only_pc_cannot_seed_or_overwrite_first_sync(monkeypatch, tmp_path):
    engine = _make_engine(tmp_path)
    pc_paths = _use_machine(monkeypatch, tmp_path / "pc")
    _save_local_state(pc_paths, {"items": [{"symbol": "OLD_PC"}]})
    pc = ss.LocalDeviceRole("pc-id", "PC", False)

    pc_result = app_state.reconcile_state_with_remote(engine, pc)

    assert pc_result.is_main_device is False
    assert ss.pull_state(engine, ss.WATCHLIST_KEY).status == ss.PULL_MISSING

    laptop_paths = _use_machine(monkeypatch, tmp_path / "laptop")
    current = {"items": [{"symbol": "CURRENT_LAPTOP"}]}
    _save_local_state(laptop_paths, current)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    laptop_result = app_state.reconcile_state_with_remote(engine, laptop)

    assert laptop_result.is_main_device is True
    assert _remote(engine, ss.WATCHLIST_KEY).payload == current

    _use_machine(monkeypatch, tmp_path / "pc")
    pulled = app_state.reconcile_state_with_remote(engine, pc)
    assert pulled.updated_keys == {
        "watchlist",
        "buylist",
        "trade_plans",
        "execution_queue",
    }
    assert app_state.load_json(pc_paths["WATCHLIST_FILE"], {}) == current


def test_unchanged_pull_only_sync_uses_revisions_without_payload_downloads(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    laptop_paths = _use_machine(monkeypatch, tmp_path / "laptop")
    _save_local_state(laptop_paths, {"items": [{"symbol": "AAPL"}]})
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    assert not app_state.reconcile_state_with_remote(engine, laptop).errors

    _use_machine(monkeypatch, tmp_path / "pc")
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    assert not app_state.reconcile_state_with_remote(engine, pc).errors

    payload_reads = []
    real_pull = app_state.pull_state

    def counted_pull(*args, **kwargs):
        payload_reads.append(True)
        return real_pull(*args, **kwargs)

    monkeypatch.setattr(app_state, "pull_state", counted_pull)
    result = app_state.reconcile_state_with_remote(engine, pc)

    assert not result.errors
    assert payload_reads == []


def test_activating_pc_deactivates_laptop_and_rejects_old_writer(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    laptop_paths = _use_machine(monkeypatch, tmp_path / "laptop")
    original = {"items": [{"symbol": "AAPL"}]}
    _save_local_state(laptop_paths, original)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    app_state.reconcile_state_with_remote(engine, laptop)
    base_revision = _remote(engine, ss.WATCHLIST_KEY).revision

    pc_paths = _use_machine(monkeypatch, tmp_path / "pc")
    _save_local_state(pc_paths, {"items": [{"symbol": "OLD_PC"}]})
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    app_state.reconcile_state_with_remote(engine, pc)
    activated = app_state.activate_device_as_main(engine, pc)

    assert activated.is_main_device is True
    assert ss.get_main_device(engine).main_device.device_id == "pc-id"

    stale_push = ss.push_state(
        engine,
        ss.WATCHLIST_KEY,
        {"items": [{"symbol": "STALE_LAPTOP"}]},
        device_id=laptop.device_id,
        expected_revision=base_revision,
    )
    assert stale_push.status == ss.PUSH_NOT_MAIN
    assert _remote(engine, ss.WATCHLIST_KEY).payload == original

    _use_machine(monkeypatch, tmp_path / "laptop")
    demoted = app_state.reconcile_state_with_remote(engine, laptop)
    assert demoted.is_main_device is False
    assert demoted.local_role.is_main is False


def test_stale_revision_cannot_overwrite_newer_remote_state(tmp_path):
    engine = _make_engine(tmp_path)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    ss.claim_main_device(engine, role)
    first = ss.push_state(
        engine,
        ss.WATCHLIST_KEY,
        {"items": [{"symbol": "BASE"}]},
        device_id=role.device_id,
        expected_revision=0,
    )
    newer = ss.push_state(
        engine,
        ss.WATCHLIST_KEY,
        {"items": [{"symbol": "NEWER"}]},
        device_id=role.device_id,
        expected_revision=first.revision,
    )

    stale = ss.push_state(
        engine,
        ss.WATCHLIST_KEY,
        {"items": [{"symbol": "STALE"}]},
        device_id=role.device_id,
        expected_revision=first.revision,
    )

    assert stale.status == ss.PUSH_CONFLICT
    assert stale.revision == newer.revision
    assert _remote(engine, ss.WATCHLIST_KEY).payload["items"][0]["symbol"] == "NEWER"


def test_offline_changes_on_both_devices_are_preserved_as_conflict(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    laptop_paths = _use_machine(monkeypatch, tmp_path / "laptop")
    base = {"items": [{"symbol": "BASE"}]}
    _save_local_state(laptop_paths, base)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    app_state.reconcile_state_with_remote(engine, laptop)

    pc_paths = _use_machine(monkeypatch, tmp_path / "pc")
    _save_local_state(pc_paths, {"items": []})
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    app_state.reconcile_state_with_remote(engine, pc)
    app_state.activate_device_as_main(engine, pc)
    pc_changed = {"items": [{"symbol": "PC_EDIT"}]}
    app_state.save_json(pc_paths["WATCHLIST_FILE"], pc_changed)
    pc_reconciled = app_state.reconcile_state_with_remote(
        engine, ss.LocalDeviceRole("pc-id", "PC", True)
    )
    assert not pc_reconciled.conflict_keys

    laptop_changed = {"items": [{"symbol": "LAPTOP_EDIT"}]}
    app_state.save_json(laptop_paths["WATCHLIST_FILE"], laptop_changed)
    _use_machine(monkeypatch, tmp_path / "laptop")
    activated = app_state.activate_device_as_main(engine, laptop)

    assert activated.is_main_device is False
    assert "watchlist" in activated.conflict_keys
    assert app_state.load_json(laptop_paths["WATCHLIST_FILE"], {}) == laptop_changed
    assert _remote(engine, ss.WATCHLIST_KEY).payload == pc_changed
    assert ss.get_main_device(engine).main_device.device_id == "pc-id"


def test_save_manager_pushes_only_changed_synced_key(monkeypatch, tmp_path):
    engine = _make_engine(tmp_path)
    paths = _use_machine(monkeypatch, tmp_path / "laptop")
    watchlist = {"items": [{"symbol": "AAPL"}]}
    buylist = {"items": []}
    plans = {"plans": []}
    _save_local_state(paths, watchlist, buylist, plans)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    app_state.reconcile_state_with_remote(engine, role)
    before = {
        key: _remote(engine, key).revision for key in ss.SYNCED_STATE_KEYS
    }

    manager = app_state.StateSaveManager()
    manager.set_engine(
        engine,
        device_id=role.device_id,
        is_main_device=True,
    )
    same_result = manager.save_now(
        watchlist,
        buylist,
        plans,
        [],
        {"AAPL": []},
        {},
    )
    assert same_result.success
    assert {
        key: _remote(engine, key).revision for key in ss.SYNCED_STATE_KEYS
    } == before

    changed_watchlist = {"items": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]}
    changed_result = manager.save_now(
        changed_watchlist,
        buylist,
        plans,
        [],
        {"AAPL": []},
        {},
    )

    assert changed_result.success
    after = {key: _remote(engine, key).revision for key in ss.SYNCED_STATE_KEYS}
    assert after[ss.WATCHLIST_KEY] == before[ss.WATCHLIST_KEY] + 1
    assert after[ss.BUYLIST_KEY] == before[ss.BUYLIST_KEY]
    assert after[ss.TRADE_PLANS_KEY] == before[ss.TRADE_PLANS_KEY]


def test_save_manager_can_save_locally_without_partial_remote_push(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    paths = _use_machine(monkeypatch, tmp_path / "laptop")
    original = {"items": [{"symbol": "AAPL"}]}
    buylist = {"items": []}
    plans = {"plans": []}
    _save_local_state(paths, original, buylist, plans)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    app_state.reconcile_state_with_remote(engine, role)
    remote_revision = _remote(engine, ss.WATCHLIST_KEY).revision

    manager = app_state.StateSaveManager()
    manager.set_engine(
        engine,
        device_id=role.device_id,
        is_main_device=True,
    )
    local_update = {"items": [{"symbol": "MSFT"}]}
    result = manager.save_now(
        local_update,
        buylist,
        plans,
        [],
        {},
        {},
        push_remote=False,
    )

    assert result.success
    assert app_state.load_json(paths["WATCHLIST_FILE"], {}) == local_update
    assert _remote(engine, ss.WATCHLIST_KEY).payload == original
    assert _remote(engine, ss.WATCHLIST_KEY).revision == remote_revision


def test_publish_plan_click_saves_locally_without_per_document_remote_push(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(main_window_module, "is_regular_session_open", lambda: False)
    monkeypatch.setattr(
        main_window_module,
        "load_json",
        lambda *_args, **_kwargs: {"items": [{"symbol": "AAPL"}]},
    )
    workers = []

    class Signal:
        def connect(self, callback):
            self.callback = callback

    class Worker:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.completed = Signal()
            self.started = False
            workers.append(self)

        def isRunning(self):
            return False

        def start(self):
            self.started = True

    monkeypatch.setattr(main_window_module, "PlanPublishWorker", Worker)
    window = MainWindow.__new__(MainWindow)
    window.plan_publish_worker = None
    window.execution_queue_manager = None
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", False)
    window.publish_trading_plan_button = SimpleNamespace(
        setEnabled=lambda _enabled: None,
        setText=lambda _text: None,
    )
    window._execution_state_ready = lambda: True
    window._execution_state_engine = lambda: object()
    window._execution_state_metadata_path = lambda: tmp_path / "metadata.json"
    window._state_save_payload = lambda: (
        {"items": [{"symbol": "AAPL"}]},
        {"items": []},
        {"plans": []},
    )
    save_calls = []

    def save_state_now(**kwargs):
        save_calls.append(kwargs)
        now = dt.datetime.now(dt.timezone.utc)
        return app_state.SaveResult(True, now, now)

    window._save_state_now = save_state_now
    window._track_worker = lambda *_args: None

    MainWindow._on_publish_trading_plan_clicked(window)

    assert save_calls == [
        {
            "timeout": 5.0,
            "supersede_pending": True,
            "push_remote": False,
        }
    ]
    assert len(workers) == 1
    assert workers[0].started is True


def test_main_device_periodic_poll_checks_ownership_without_touching_state(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    paths = _use_machine(monkeypatch, tmp_path / "laptop")
    original = {"items": [{"symbol": "AAPL"}]}
    _save_local_state(paths, original)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    app_state.reconcile_state_with_remote(engine, role)
    revision = _remote(engine, ss.WATCHLIST_KEY).revision

    local_edit = {"items": [{"symbol": "LOCAL_UNSAVED_REMOTE"}]}
    app_state.save_json(paths["WATCHLIST_FILE"], local_edit)
    result = app_state.reconcile_state_with_remote(
        engine,
        role,
        ownership_only_when_main=True,
    )

    assert result.is_main_device is True
    assert result.updated_keys == set()
    assert result.conflict_keys == set()
    assert app_state.load_json(paths["WATCHLIST_FILE"], {}) == local_edit
    assert _remote(engine, ss.WATCHLIST_KEY).revision == revision
    assert _remote(engine, ss.WATCHLIST_KEY).payload == original


def test_pull_only_save_manager_never_pushes(monkeypatch, tmp_path):
    engine = _make_engine(tmp_path)
    paths = _use_machine(monkeypatch, tmp_path / "laptop")
    original = {"items": [{"symbol": "AAPL"}]}
    _save_local_state(paths, original)
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    app_state.reconcile_state_with_remote(engine, role)

    manager = app_state.StateSaveManager()
    manager.set_engine(
        engine,
        device_id="pc-id",
        is_main_device=False,
    )
    result = manager.save_now(
        {"items": [{"symbol": "STALE_PC"}]},
        {"items": []},
        {"plans": []},
        [],
        {},
        {},
    )

    assert result.success
    assert _remote(engine, ss.WATCHLIST_KEY).payload == original


def test_read_error_never_falls_through_to_push(monkeypatch, tmp_path):
    engine = _make_engine(tmp_path)
    paths = _use_machine(monkeypatch, tmp_path / "laptop")
    _save_local_state(paths, {"items": [{"symbol": "LOCAL"}]})
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    ss.claim_main_device(engine, role)
    pushes = []

    monkeypatch.setattr(
        app_state,
        "pull_state",
        lambda engine, key: ss.PullResult(ss.PULL_ERROR, error="read failed"),
    )
    monkeypatch.setattr(
        app_state,
        "push_state",
        lambda *args, **kwargs: pushes.append((args, kwargs)),
    )

    result = app_state.reconcile_state_with_remote(engine, role)

    assert result.errors
    assert pushes == []
    assert ss.pull_state(engine, ss.WATCHLIST_KEY).status == ss.PULL_MISSING


def test_legacy_sync_table_is_migrated_with_revision_columns(tmp_path):
    engine = _make_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE app_state_sync ("
                "state_key VARCHAR(40) PRIMARY KEY, payload TEXT, "
                "updated_at DATETIME NOT NULL, updated_by_host VARCHAR(128))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO app_state_sync "
                "(state_key, payload, updated_at, updated_by_host) "
                "VALUES ('watchlist', '{\"items\":[]}', CURRENT_TIMESTAMP, 'OLD')"
            )
        )

    pulled = ss.pull_state(engine, ss.WATCHLIST_KEY)

    assert pulled.status == ss.PULL_OK
    assert pulled.state.revision == 1
    assert pulled.state.updated_by_device == ""


def test_mysql_schema_and_timestamp_expression_include_safety_fields():
    ddl = str(
        CreateTable(ss._get_state_sync_table(MetaData())).compile(
            dialect=mysql.dialect()
        )
    )
    server_now = str(
        ss._server_now(SimpleNamespace(dialect=SimpleNamespace(name="mysql"))).compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "revision BIGINT" in ddl
    assert "updated_by_device VARCHAR(64)" in ddl
    assert "utc_timestamp(6)" in server_now.lower()


def test_state_save_manager_without_engine_still_saves_locally(
    monkeypatch, tmp_path
):
    paths = _use_machine(monkeypatch, tmp_path / "local")
    manager = app_state.StateSaveManager()

    result = manager.save_now(
        {"items": [{"symbol": "AAPL"}]},
        {"items": []},
        {"plans": []},
        [],
        {},
        {},
    )

    assert result.success
    assert app_state.load_json(paths["WATCHLIST_FILE"], {}) == {
        "items": [{"symbol": "AAPL"}]
    }


def test_pull_only_device_blocks_low_level_order_submission(monkeypatch):
    window = MainWindow.__new__(MainWindow)
    window.state_sync_role = ss.LocalDeviceRole("pc-id", "PC", False)
    window.state_save_manager = SimpleNamespace(_is_main_device=False)
    messages = []
    window.append_log = messages.append
    warnings = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    item = SimpleNamespace(symbol="AAPL", _buy_order_pending=True)

    MainWindow._submit_kis_buy_order(
        window,
        item,
        quantity=1,
        order_price=100.0,
    )

    assert item._buy_order_pending is False
    assert warnings
    assert any("pull-only" in message.lower() for message in messages)


# --- Fenced ownership primitives (automatic cross-machine handoff) --------


def test_release_main_device_clears_ownership_row_when_owned_by_role(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    claimed = ss.claim_main_device(engine, laptop)
    assert claimed.success

    result = ss.release_main_device(engine, laptop, **_lease_kwargs(claimed))

    assert result.success
    assert ss.get_main_device(engine).main_device is None


def test_clean_release_and_reclaim_advances_lease_epoch(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    first = ss.claim_main_device(engine, laptop)
    assert first.success
    assert ss.release_main_device(engine, laptop, **_lease_kwargs(first)).success

    second = ss.claim_main_device_if_unclaimed(engine, pc)

    assert second.success
    assert second.main_device.lease_epoch > first.main_device.lease_epoch


def test_stale_same_device_lease_cannot_release_newer_lease(tmp_path):
    engine = _make_engine(tmp_path)
    pc = ss.LocalDeviceRole("pc-id", "PC", True)
    first = ss.claim_main_device(engine, pc)
    assert first.success
    assert ss.release_main_device(engine, pc, **_lease_kwargs(first)).success
    second = ss.claim_main_device_if_unclaimed(engine, pc)
    assert second.success

    stale_release = ss.release_main_device(engine, pc, **_lease_kwargs(first))

    assert stale_release.success is False
    current = ss.get_main_device(engine).main_device
    assert current is not None
    assert current.lease_token == second.main_device.lease_token
    assert current.lease_epoch == second.main_device.lease_epoch


def test_exact_lease_release_preserves_epoch_in_tombstone(tmp_path):
    engine = _make_engine(tmp_path)
    pc = ss.LocalDeviceRole("pc-id", "PC", True)
    claimed = ss.claim_main_device(engine, pc)
    assert claimed.success

    released = ss.release_main_device(engine, pc, **_lease_kwargs(claimed))

    assert released.success is True
    assert ss.get_main_device(engine).main_device is None
    tombstone = ss.pull_state(engine, ss.MAIN_DEVICE_KEY)
    assert tombstone.status == ss.PULL_OK
    assert tombstone.state.payload["device_id"] == ""
    assert tombstone.state.payload["lease_epoch"] == claimed.main_device.lease_epoch


def test_release_main_device_is_noop_when_not_owner(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    claimed = ss.claim_main_device(engine, laptop)
    assert claimed.success

    result = ss.release_main_device(engine, pc, **_lease_kwargs(claimed))

    assert result.success
    assert ss.get_main_device(engine).main_device.device_id == "laptop-id"


def test_release_main_device_noop_when_already_unclaimed(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)

    result = ss.release_main_device(
        engine,
        laptop,
        expected_lease_token="already-released",
        expected_lease_epoch=1,
    )

    assert result.success
    assert ss.get_main_device(engine).main_device is None


def test_claim_main_device_if_stale_succeeds_when_owner_and_heartbeat_match(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    assert ss.claim_main_device(engine, laptop).success
    runtime_status.record_runtime_heartbeat(engine, hostname="LAPTOP", pid=1)
    _make_heartbeat_stale(engine, "LAPTOP")

    result = ss.claim_main_device_if_stale(
        engine,
        pc,
        expected_owner_device_id="laptop-id",
        heartbeat_cutoff_seconds=60,
    )

    assert result.success
    owner = ss.get_main_device(engine).main_device
    assert owner.device_id == "pc-id"
    assert owner.lease_token
    # A fresh lease token every claim -- the whole point of fencing.
    assert owner.lease_token != ""


def test_claim_main_device_if_stale_fails_when_owner_changed(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    desktop = ss.LocalDeviceRole("desktop-id", "DESKTOP", False)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    assert ss.claim_main_device(engine, laptop).success
    # Ownership moved on (e.g. someone else already claimed) since the
    # caller last observed "laptop-id" as the owner.
    assert ss.claim_main_device(engine, desktop).success

    result = ss.claim_main_device_if_stale(
        engine,
        pc,
        expected_owner_device_id="laptop-id",
        heartbeat_cutoff_seconds=60,
    )

    assert not result.success
    assert ss.get_main_device(engine).main_device.device_id == "desktop-id"


def test_claim_main_device_if_stale_fails_when_heartbeat_fresh_again(tmp_path):
    """The core TOCTOU race this primitive exists to close.

    The caller observed a stale heartbeat earlier, but by the time it
    actually claims (inside the same atomic transaction), the owner has
    reconnected and resumed its heartbeat -- the claim must be rejected.
    """
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    assert ss.claim_main_device(engine, laptop).success
    runtime_status.record_runtime_heartbeat(engine, hostname="LAPTOP", pid=1)
    # No staleness applied -- heartbeat is fresh "again" (or still).

    result = ss.claim_main_device_if_stale(
        engine,
        pc,
        expected_owner_device_id="laptop-id",
        heartbeat_cutoff_seconds=60,
    )

    assert not result.success
    assert ss.get_main_device(engine).main_device.device_id == "laptop-id"


def test_current_profile_runtime_row_fences_takeover_without_duplicate_heartbeat(
    tmp_path,
):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    assert ss.claim_main_device(engine, laptop).success
    save_runtime_device_state(
        engine,
        device_id=laptop.device_id,
        hostname=laptop.hostname,
        state=RuntimeDeviceState.ACTIVE,
        details={
            "main_py_alive": True,
            "coordination_ru_profile": COORDINATION_RU_PROFILE,
        },
    )

    fresh = ss.claim_main_device_if_stale(
        engine,
        pc,
        expected_owner_device_id=laptop.device_id,
        heartbeat_cutoff_seconds=60,
    )
    assert fresh.success is False

    stale_at = dt.datetime.now(dt.timezone.utc).replace(
        tzinfo=None
    ) - dt.timedelta(minutes=5)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runtime_device_state SET updated_at = :stale_at "
                "WHERE device_id = :device_id"
            ),
            {"stale_at": stale_at, "device_id": laptop.device_id},
        )
    stale = ss.claim_main_device_if_stale(
        engine,
        pc,
        expected_owner_device_id=laptop.device_id,
        heartbeat_cutoff_seconds=60,
    )

    assert stale.success is True
    assert ss.get_main_device(engine).main_device.device_id == pc.device_id


def test_claim_main_device_if_stale_fails_when_ownership_released(tmp_path):
    engine = _make_engine(tmp_path)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)

    result = ss.claim_main_device_if_stale(
        engine,
        pc,
        expected_owner_device_id="laptop-id",
        heartbeat_cutoff_seconds=60,
    )

    assert not result.success


# --- should_auto_claim_main -------------------------------------------


def test_should_auto_claim_main_true_when_ownership_unclaimed(tmp_path):
    engine = _make_engine(tmp_path)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)

    should_claim, expected_owner, reason = app_state.should_auto_claim_main(
        engine, pc
    )

    assert should_claim is True
    assert expected_owner == ""
    assert "released" in reason


def test_should_auto_claim_main_false_when_already_main(tmp_path):
    engine = _make_engine(tmp_path)
    pc = ss.LocalDeviceRole("pc-id", "PC", True)

    should_claim, expected_owner, reason = app_state.should_auto_claim_main(
        engine, pc
    )

    assert should_claim is False
    assert expected_owner == ""
    assert reason == ""


def test_should_auto_claim_main_false_when_owner_heartbeat_fresh(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    assert ss.claim_main_device(engine, laptop).success
    runtime_status.record_runtime_heartbeat(engine, hostname="LAPTOP", pid=1)

    should_claim, expected_owner, reason = app_state.should_auto_claim_main(
        engine, pc, other_hostname="LAPTOP"
    )

    assert should_claim is False
    assert expected_owner == ""


def test_should_auto_claim_main_true_when_owner_heartbeat_stale(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    assert ss.claim_main_device(engine, laptop).success
    runtime_status.record_runtime_heartbeat(engine, hostname="LAPTOP", pid=1)
    _make_heartbeat_stale(engine, "LAPTOP")

    should_claim, expected_owner, reason = app_state.should_auto_claim_main(
        engine, pc, other_hostname="LAPTOP"
    )

    assert should_claim is True
    assert expected_owner == "laptop-id"
    assert "stale heartbeat" in reason


def test_should_auto_claim_main_false_when_owner_never_reported_heartbeat(tmp_path):
    """No heartbeat row at all (e.g. an old/pre-heartbeat device) -- do not act on unknown state."""
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    assert ss.claim_main_device(engine, laptop).success

    should_claim, expected_owner, reason = app_state.should_auto_claim_main(
        engine, pc, other_hostname="LAPTOP"
    )

    assert should_claim is False


# --- End-to-end automatic handoff sequence ------------------------------


def test_full_handoff_release_then_pc_auto_claims_via_stale_primitive(
    monkeypatch, tmp_path
):
    engine = _make_engine(tmp_path)
    laptop_paths = _use_machine(monkeypatch, tmp_path / "laptop")
    _save_local_state(laptop_paths, {"items": [{"symbol": "AAPL"}]})
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    app_state.reconcile_state_with_remote(engine, laptop)
    assert ss.get_main_device(engine).main_device.device_id == "laptop-id"
    lease_kwargs = _current_lease_kwargs(engine)

    # Clean release, as MainWindow._release_main_device_ownership_for_shutdown does.
    released, demoted_role, error = app_state.release_main_device_and_demote(
        engine, laptop, **lease_kwargs
    )
    assert released
    assert not error
    assert demoted_role.is_main is False
    assert ss.get_main_device(engine).main_device is None

    _use_machine(monkeypatch, tmp_path / "pc")
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    should_claim, expected_owner, reason = app_state.should_auto_claim_main(engine, pc)
    assert should_claim is True
    assert "released" in reason

    claimed = app_state.auto_claim_main_device_if_stale(
        engine, pc, expected_owner_device_id=expected_owner
    )

    assert claimed.is_main_device is True
    assert claimed.lease_token
    assert app_state.load_json(
        _use_machine(monkeypatch, tmp_path / "pc")["WATCHLIST_FILE"], {}
    ) == {"items": [{"symbol": "AAPL"}]}


def test_released_laptop_does_not_self_reclaim_on_next_reconcile(monkeypatch, tmp_path):
    """Covers the gap the first design draft missed: demote must be persisted.

    Without persisting is_main=False locally, reconcile_state_with_remote's
    own bootstrap branch ("owner row missing + local role.is_main == True"
    -> self-claim) would let a released laptop silently re-claim its own
    just-vacated row on its very next tick.
    """
    engine = _make_engine(tmp_path)
    laptop_paths = _use_machine(monkeypatch, tmp_path / "laptop")
    _save_local_state(laptop_paths, {"items": []})
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    app_state.reconcile_state_with_remote(engine, laptop)
    lease_kwargs = _current_lease_kwargs(engine)

    released, demoted_role, _error = app_state.release_main_device_and_demote(
        engine, laptop, **lease_kwargs
    )
    assert released
    assert demoted_role.is_main is False

    # Simulate the laptop staying open and its 15s reconcile timer firing
    # again with the (correctly) demoted local role.
    again = app_state.reconcile_state_with_remote(engine, demoted_role)

    assert again.is_main_device is False
    assert ss.get_main_device(engine).main_device is None


def test_release_demotes_and_disables_writer_before_deleting_ownership(monkeypatch):
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    calls = []

    def fake_demote(current, is_main):
        calls.append("demote")
        return ss.LocalDeviceRole(current.device_id, current.hostname, is_main)

    def fake_release(_engine, current, **kwargs):
        calls.append("release")
        assert current.is_main is True
        assert kwargs == {
            "expected_lease_token": "tok-1",
            "expected_lease_epoch": 10,
        }
        return SimpleNamespace(success=True, error="")

    monkeypatch.setattr(app_state, "set_local_device_main", fake_demote)
    monkeypatch.setattr(app_state, "release_main_device", fake_release)

    released, demoted, error = app_state.release_main_device_and_demote(
        object(),
        role,
        expected_lease_token="tok-1",
        expected_lease_epoch=10,
        disable_remote_writer=lambda: calls.append("disable_writer"),
    )

    assert released is True
    assert demoted.is_main is False
    assert error == ""
    assert calls == ["demote", "disable_writer", "release"]


def test_release_retains_ownership_when_local_demotion_fails(monkeypatch):
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    release_calls = []
    monkeypatch.setattr(
        app_state,
        "set_local_device_main",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        app_state,
        "release_main_device",
        lambda *a, **k: release_calls.append(True),
    )

    released, unchanged, error = app_state.release_main_device_and_demote(
        object(),
        role,
        expected_lease_token="tok-1",
        expected_lease_epoch=10,
    )

    assert released is False
    assert unchanged == role
    assert "disk full" in error
    assert release_calls == []


def test_claim_atomically_requires_the_exact_fresh_standby_generation(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    claimed = ss.claim_main_device(engine, laptop)
    assert claimed.success
    ready = save_runtime_device_state(
        engine,
        device_id=pc.device_id,
        hostname=pc.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
    )

    save_runtime_device_state(
        engine,
        device_id=pc.device_id,
        hostname=pc.hostname,
        state=RuntimeDeviceState.STANDBY,
    )
    rejected = ss.claim_main_device(
        engine,
        pc,
        expected_standby_generation=ready.readiness_generation,
    )

    assert rejected.success is False
    assert "not STANDBY_READY" in rejected.error
    assert ss.get_main_device(engine).main_device.device_id == laptop.device_id


def test_clean_handoff_claim_uses_the_persisted_standby_generation(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    pc = ss.LocalDeviceRole("pc-id", "PC", False)
    claimed = ss.claim_main_device(engine, laptop)
    assert claimed.success
    ready = save_runtime_device_state(
        engine,
        device_id=pc.device_id,
        hostname=pc.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
    )
    assert ss.release_main_device(engine, laptop, **_lease_kwargs(claimed)).success

    claimed = ss.claim_main_device_if_unclaimed(
        engine,
        pc,
        expected_standby_generation=ready.readiness_generation,
    )

    assert claimed.success is True
    assert claimed.main_device.device_id == pc.device_id


def test_release_retains_ownership_when_writer_cannot_be_disabled(monkeypatch):
    role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    release_calls = []
    monkeypatch.setattr(
        app_state,
        "set_local_device_main",
        lambda current, is_main: ss.LocalDeviceRole(
            current.device_id, current.hostname, is_main
        ),
    )
    monkeypatch.setattr(
        app_state,
        "release_main_device",
        lambda *a, **k: release_calls.append(True),
    )

    released, demoted, error = app_state.release_main_device_and_demote(
        object(),
        role,
        expected_lease_token="tok-1",
        expected_lease_epoch=10,
        disable_remote_writer=lambda: (_ for _ in ()).throw(
            RuntimeError("writer still bound")
        ),
    )

    assert released is False
    assert demoted.is_main is True
    assert "writer still bound" in error
    assert release_calls == []


def test_publish_handoff_snapshot_requires_both_pushes_to_land(tmp_path):
    engine = _make_engine(tmp_path)
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)
    assert ss.claim_main_device(engine, laptop).success

    published = app_state.publish_handoff_snapshot(
        engine,
        laptop,
        {"items": [{"symbol": "AAPL"}]},
        {"items": {}},
        metadata_path=tmp_path / "state_metadata.json",
    )

    assert published is True
    buylist_remote = ss.pull_state(engine, ss.BUYLIST_KEY)
    assert buylist_remote.status == ss.PULL_OK
    assert buylist_remote.state.payload == {"items": [{"symbol": "AAPL"}]}
    queue_remote = ss.pull_state(engine, ss.EXECUTION_QUEUE_KEY)
    assert queue_remote.status == ss.PULL_OK


def test_publish_handoff_snapshot_fails_when_database_unavailable(tmp_path):
    laptop = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)

    published = app_state.publish_handoff_snapshot(
        None,
        laptop,
        {"items": []},
        {"items": {}},
        metadata_path=tmp_path / "state_metadata.json",
    )

    assert published is False


def test_explicit_plan_publish_recovers_rows_missing_from_new_remote_store(tmp_path):
    engine = _make_engine(tmp_path)
    pc = ss.LocalDeviceRole("pc-id", "TRADING-PC", True)
    laptop = ss.LocalDeviceRole("laptop-id", "TRADING-LAPTOP", False)
    assert ss.claim_main_device(engine, pc).success
    assert ss.set_operator_control(engine, pc, laptop).success
    metadata_path = tmp_path / "state_metadata.json"
    app_state.save_json(
        metadata_path,
        {
            "state_sync": {
                key: {
                    "revision": 108,
                    "content_hash": f"old-store-{key}",
                    "updated_at": "2026-08-20T00:00:00",
                }
                for key in ss.SYNCED_STATE_KEYS
            }
        },
    )
    payloads = {
        ss.WATCHLIST_KEY: {"items": [{"symbol": "AAPL"}]},
        ss.BUYLIST_KEY: {"items": [{"symbol": "AAPL"}]},
        ss.TRADE_PLANS_KEY: {"plans": [{"symbol": "AAPL"}]},
        ss.EXECUTION_QUEUE_KEY: {"items": [{"symbol": "AAPL"}]},
    }

    result = app_state.publish_trading_plan(
        engine,
        laptop,
        payloads[ss.WATCHLIST_KEY],
        payloads[ss.BUYLIST_KEY],
        payloads[ss.TRADE_PLANS_KEY],
        payloads[ss.EXECUTION_QUEUE_KEY],
        market_is_open=False,
        metadata_path=metadata_path,
    )

    assert result.success is True
    assert result.revisions == {key: 1 for key in ss.SYNCED_STATE_KEYS}
    for key, payload in payloads.items():
        assert _remote(engine, key).payload == payload
    entries = app_state._read_sync_entries(metadata_path)
    assert {key: entry["revision"] for key, entry in entries.items()} == {
        key: 1 for key in ss.SYNCED_STATE_KEYS
    }


def test_explicit_plan_publish_still_rejects_existing_newer_remote_revision(tmp_path):
    engine = _make_engine(tmp_path)
    pc = ss.LocalDeviceRole("pc-id", "TRADING-PC", True)
    laptop = ss.LocalDeviceRole("laptop-id", "TRADING-LAPTOP", False)
    assert ss.claim_main_device(engine, pc).success
    assert ss.set_operator_control(engine, pc, laptop).success
    initial = {
        key: {"items": []} if key != ss.TRADE_PLANS_KEY else {"plans": []}
        for key in ss.SYNCED_STATE_KEYS
    }
    first = ss.publish_planning_snapshot(
        engine,
        laptop,
        initial,
        expected_revisions={key: 0 for key in ss.SYNCED_STATE_KEYS},
        market_is_open=False,
    )
    assert first.success
    newer = ss.push_state(
        engine,
        ss.WATCHLIST_KEY,
        {"items": [{"symbol": "REMOTE"}]},
        device_id=pc.device_id,
        expected_revision=1,
    )
    assert newer.status == ss.PUSH_WRITTEN
    metadata_path = tmp_path / "state_metadata.json"
    app_state.save_json(
        metadata_path,
        {
            "state_sync": {
                key: {
                    "revision": 1,
                    "content_hash": f"base-{key}",
                    "updated_at": "2026-08-20T00:00:00",
                }
                for key in ss.SYNCED_STATE_KEYS
            }
        },
    )

    result = app_state.publish_trading_plan(
        engine,
        laptop,
        {"items": [{"symbol": "LOCAL"}]},
        {"items": []},
        {"plans": []},
        {"items": []},
        market_is_open=False,
        metadata_path=metadata_path,
    )

    assert result.success is False
    assert "watchlist revision changed from 1 to 2" in result.error
    assert _remote(engine, ss.WATCHLIST_KEY).payload == {
        "items": [{"symbol": "REMOTE"}]
    }
    assert _remote(engine, ss.BUYLIST_KEY).revision == 1
