from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from src.services import live_trading_control as lc, state_sync as ss


@pytest.fixture
def setup_control(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    ss.ensure_state_sync_tables(engine)
    monkeypatch.setattr(ss, "DATA_DIR", tmp_path)
    monkeypatch.setattr(lc, "is_trading_locked_disabled", lambda: False)
    role = ss.LocalDeviceRole("pc", "PC", True)
    monkeypatch.setattr(lc, "require_approved_release_identity", lambda: SimpleNamespace(repository_head="a" * 40))
    monkeypatch.setattr(lc, "_approved_owner", lambda *a, **kw: (role, "a" * 40))
    yield engine, role
    engine.dispose()


def test_default_on_is_durable_and_survives_next_session(setup_control):
    engine, role = setup_control
    result = lc.initialize_default_live_trading_control(engine, role)
    assert result.success and result.control.enabled
    assert result.control.persist_across_sessions
    assert ss.live_trading_control_is_effective(result.control,
        now=datetime(2027, 1, 20, 15, tzinfo=timezone.utc), runtime_commit_sha="a" * 40)
    again = lc.initialize_default_live_trading_control(engine, role)
    assert again.control.revision == result.control.revision == 1


def test_saved_off_survives_restart_and_default_initialization(setup_control):
    engine, role = setup_control
    off = ss.set_live_trading_control(engine, role, False)
    result = lc.initialize_default_live_trading_control(engine, role)
    assert result.success and not result.control.enabled
    assert result.control.revision == off.control.revision


def test_default_cannot_overwrite_concurrent_operator_off(setup_control, monkeypatch):
    engine, role = setup_control
    original = ss.set_live_trading_control

    def concurrent_off(*args, **kwargs):
        assert kwargs["expected_revision"] == 0
        assert original(engine, role, False).success
        return original(*args, **kwargs)

    monkeypatch.setattr(lc, "set_live_trading_control", concurrent_off)
    result = lc.initialize_default_live_trading_control(engine, role)
    assert result.success and not result.control.enabled
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM live_trading_control_audit")) == 1


@pytest.mark.parametrize("case", ["lock", "different_owner", "different_release", "unapproved", "unready"])
def test_default_requires_approved_ready_owner(setup_control, monkeypatch, case):
    engine, role = setup_control
    if case == "lock":
        monkeypatch.setattr(lc, "is_trading_locked_disabled", lambda: True)
    elif case == "different_owner":
        monkeypatch.setattr(lc, "_approved_owner", lambda *a, **kw: (ss.LocalDeviceRole("other", "OTHER"), "a" * 40))
    elif case == "different_release":
        monkeypatch.setattr(lc, "require_approved_release_identity", lambda: SimpleNamespace(repository_head="b" * 40))
    else:
        def blocked(*args, **kwargs):
            raise ValueError(case)
        monkeypatch.setattr(lc, "require_approved_release_identity" if case == "unapproved" else "_approved_owner", blocked)
    result = lc.initialize_default_live_trading_control(engine, role)
    assert not result.success
    assert ss.get_live_trading_control(engine).control.revision == 0


def test_observer_on_uses_execution_owner_release(setup_control, monkeypatch):
    engine, _role = setup_control
    monkeypatch.setattr(ss, "get_env_value", lambda *args: "b" * 40)
    result = lc.set_operator_live_trading_control(engine, ss.LocalDeviceRole("laptop", "LAPTOP"), True)
    assert result.success and result.control.runtime_commit_sha == "a" * 40
    assert result.control.persist_across_sessions


def test_off_does_not_require_available_executor(setup_control, monkeypatch):
    engine, role = setup_control
    monkeypatch.setattr(lc, "_approved_owner", lambda *a, **kw: pytest.fail("OFF must remain available"))
    assert not lc.set_operator_live_trading_control(engine, role, False).control.enabled


@pytest.mark.parametrize("case", ["valid", "stale", "unapproved", "unready"])
def test_owner_identity_and_readiness_are_verified(monkeypatch, case):
    role = ss.LocalDeviceRole("pc", "PC", True)
    monkeypatch.setattr(lc, "get_main_device", lambda _engine: SimpleNamespace(success=True, main_device=role))
    record = SimpleNamespace(updated_at=datetime.now(timezone.utc), state=SimpleNamespace(value="ACTIVE"), details=dict(
        repository_commit_sha="a" * 40, release_id="a" * 40 + ":" + "c" * 64,
        main_py_alive=True, db_connected=True, kis_ready=True, executor_ready=True,
        order_reconciliation_ready=True, command_consumer_ready=True))
    if case == "stale":
        record.updated_at -= timedelta(hours=1)
    elif case == "unapproved":
        record.details["release_id"] = ""
    elif case == "unready":
        record.details["order_reconciliation_ready"] = False
    monkeypatch.setattr(lc, "get_runtime_device_state", lambda *a, **kw: record)
    if case == "valid":
        assert lc._approved_owner(object(), require_ready=True) == (role, "a" * 40)
    else:
        with pytest.raises(ValueError):
            lc._approved_owner(object(), require_ready=True)


def test_control_write_notifies_other_process_after_commit(setup_control, monkeypatch):
    engine, role = setup_control
    from src.services import coordination_change_pulse as pulse
    observed = []

    def notification(directory, event_id, *, tables):
        observed.append((ss.get_live_trading_control(engine).control.enabled, tables))
        return True

    monkeypatch.setattr(pulse, "record_external_change_pulses", notification)
    assert ss.set_live_trading_control(engine, role, True).success
    assert observed == [(True, ("app_state_sync",))]
