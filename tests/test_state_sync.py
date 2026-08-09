"""Conflict-safe cross-machine state synchronization tests."""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from src.services import app_state
from src.services import state_sync as ss
import src.ui.main_window as main_window_module
from src.ui.main_window import MainWindow


def _make_engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'shared.db'}",
        future=True,
        poolclass=NullPool,
    )


def _use_machine(monkeypatch, root):
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "WATCHLIST_FILE": root / "watchlist.json",
        "BUYLIST_FILE": root / "buylist.json",
        "TRADE_PLANS_FILE": root / "trade_plans.json",
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
    assert pulled.updated_keys == {"watchlist", "buylist", "trade_plans"}
    assert app_state.load_json(pc_paths["WATCHLIST_FILE"], {}) == current


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


def test_main_device_button_reflects_exclusive_role():
    class Button:
        def __init__(self):
            self.text = ""
            self.enabled = None
            self.tooltip = ""

        def setEnabled(self, value):
            self.enabled = value

        def setText(self, value):
            self.text = value

        def setStyleSheet(self, value):
            pass

        def setToolTip(self, value):
            self.tooltip = value

    window = MainWindow.__new__(MainWindow)
    window.main_device_button = Button()
    window.pc_db_engine = object()
    window.db_initializing = False
    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", True)

    window._update_main_device_button()
    assert window.main_device_button.text == "Main Device: ON"
    assert window.main_device_button.enabled is True

    window.state_sync_role = ss.LocalDeviceRole("laptop-id", "LAPTOP", False)
    window._update_main_device_button(main_hostname="PC")
    assert window.main_device_button.text == "Use This Device as Main"
    assert "PC" in window.main_device_button.tooltip
