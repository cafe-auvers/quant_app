import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.services import state_sync as ss
from src.services.runtime_device_state_repository import ensure_runtime_device_state_table

spec = importlib.util.spec_from_file_location(
    'resume_live_trading', Path(__file__).resolve().parents[1] / 'scripts/resume_live_trading.py')
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)
NOW = dt.datetime(2026, 10, 28, 13, 0, tzinfo=dt.timezone.utc)
RELEASE = 'a' * 40


@pytest.fixture
def engine():
    value = create_engine('sqlite:///:memory:', future=True)
    ss.ensure_state_sync_tables(value)
    ensure_runtime_device_state_table(value)
    role = ss.LocalDeviceRole('pc-id', 'HOME-PC', True)
    ss.set_live_trading_control(value, role, True, session_date=dt.date(2026, 10, 27),
                               runtime_commit_sha=RELEASE, persist_across_sessions=False)
    with value.begin() as conn:
        conn.execute(text(
            "INSERT INTO app_state_sync (state_key,payload,revision,updated_at) "
            "VALUES ('__main_device__',:payload,1,:now)"),
            dict(payload=json.dumps(dict(hostname='HOME-PC', device_id='pc-id')), now=NOW))
        details = dict(repository_commit_sha=RELEASE, main_py_alive=True, db_connected=True,
                       kis_ready=True, executor_ready=True, order_reconciliation_ready=True,
                       command_consumer_ready=True)
        conn.execute(text(
            "INSERT INTO runtime_device_state (device_id,hostname,state,details_json,updated_at) "
            "VALUES ('pc-id','HOME-PC','ACTIVE',:details,:now)"),
            dict(details=json.dumps(details), now=NOW.replace(tzinfo=None)))
    yield value
    value.dispose()


def renew(engine, **kwargs):
    return resume.renew_enabled_session(engine, release=RELEASE, hostname='HOME-PC',
                                       session=NOW.date(), now=NOW, **kwargs)


def test_renewal_is_durable_audited_and_idempotent(engine):
    assert renew(engine) == 'SAVED_ON_CHOICE_RENEWED'
    control = ss.get_live_trading_control(engine).control
    assert control.enabled and control.session_date == NOW.date()
    assert control.runtime_commit_sha == RELEASE
    assert renew(engine) == 'ALREADY_CURRENT'
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM live_trading_control_audit')) == 2


def test_dry_run_does_not_change_authorization(engine):
    assert renew(engine, dry_run=True) == 'WOULD_RENEW_SAVED_ON_CHOICE'
    assert ss.get_live_trading_control(engine).control.session_date == dt.date(2026, 10, 27)


def test_explicit_off_survives_subsequent_automatic_runs(engine):
    ss.set_live_trading_control(engine, ss.LocalDeviceRole('phone', 'PHONE', False), False)
    assert renew(engine) == 'OFF_PRESERVED'
    assert not ss.get_live_trading_control(engine).control.enabled


@pytest.mark.parametrize('change,expected', [
    ("DELETE FROM app_state_sync WHERE state_key='__live_trading_control__'", 'NO_SAVED_ON_CHOICE'),
    ("UPDATE runtime_device_state SET state='FAILED'", 'WAITING_FOR_READY_EXECUTOR'),
    ("UPDATE runtime_device_state SET updated_at='2026-10-27 12:00:00'", 'WAITING_FOR_READY_EXECUTOR'),
    ("DELETE FROM runtime_device_state", 'WAITING_FOR_EXECUTOR'),
    ("DELETE FROM app_state_sync WHERE state_key='__main_device__'", 'WAITING_FOR_PC_OWNERSHIP'),
])
def test_unready_or_missing_state_is_never_armed(engine, change, expected):
    with engine.begin() as conn:
        conn.execute(text(change))
    assert renew(engine) == expected


def test_different_release_never_automatically_arms(engine):
    assert resume.renew_enabled_session(engine, release='b' * 40, hostname='HOME-PC',
                                       session=NOW.date(), now=NOW) == 'RELEASE_CHANGE_REQUIRES_OPERATOR'


def test_audit_failure_rolls_back_switch_change(engine):
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE live_trading_control_audit'))
    with pytest.raises(Exception):
        renew(engine)
    assert ss.get_live_trading_control(engine).control.session_date == dt.date(2026, 10, 27)
