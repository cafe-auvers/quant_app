from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine, event, text

from src.core.execution_config import COORDINATION_RU_PROFILE
from src.web.executor_status import executor_status


@pytest.fixture
def heartbeat_engine():
    engine = create_engine("sqlite://")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE runtime_device_state (device_id TEXT, hostname TEXT, state TEXT, updated_at DATETIME, details_json TEXT)"))
        connection.execute(text("CREATE TABLE app_runtime_status (hostname TEXT, process_name TEXT, active BOOLEAN, heartbeat_at DATETIME)"))
        connection.execute(
            text("INSERT INTO app_runtime_status VALUES ('pc', 'main.py', 1, :stamp)"),
            {"stamp": now - dt.timedelta(hours=12)},
        )
    yield engine
    engine.dispose()


def put_runtime(engine, *, device="owner", state="ACTIVE", age=100, alive=True, ready=True, profile=COORDINATION_RU_PROFILE):
    details = {"main_py_alive": alive, "executor_ready": ready, "coordination_ru_profile": profile, "executor_not_ready_reason": "Account reconciliation pending" if not ready else ""}
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM runtime_device_state WHERE device_id=:id"), {"id": device})
        connection.execute(
            text("INSERT INTO runtime_device_state VALUES (:id, 'pc', :state, :stamp, :details)"),
            {"id": device, "state": state, "stamp": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(seconds=age), "details": json.dumps(details)},
        )


def status(engine):
    with engine.connect() as connection:
        return executor_status(connection, {"device_id": "owner", "hostname": "PC"})


def test_canonical_heartbeat_uses_current_cadence_and_no_legacy_query(heartbeat_engine):
    put_runtime(heartbeat_engine, age=240)
    statements = []
    event.listen(heartbeat_engine, "before_cursor_execute", lambda _conn, _cursor, statement, *_args: statements.append(statement))
    result = status(heartbeat_engine)
    assert result["executor"] == "HEALTHY"
    assert result["executor_state"] == "ACTIVE"
    assert 238 <= result["executor_heartbeat_age_seconds"] <= 241
    assert not any("app_runtime_status" in sql for sql in statements)
    assert all(sql.startswith("SELECT") for sql in statements)


@pytest.mark.parametrize("state,age,alive,ready,expected", [
    ("ACTIVE", 400, True, True, "STALE"),
    ("FAILED", 0, True, True, "STALE"),
    ("STOPPED", 0, False, True, "STALE"),
    ("ACTIVE", 0, False, True, "STALE"),
    ("ACTIVE", 0, True, False, "BLOCKED"),
    ("STARTING", 0, True, False, "STARTING"),
    ("STANDBY_READY", 0, True, True, "STANDBY_READY"),
    ("UNRECOGNIZED", 0, True, True, "UNKNOWN"),
])
def test_unhealthy_or_idle_owner_is_not_green(heartbeat_engine, state, age, alive, ready, expected):
    put_runtime(heartbeat_engine, state=state, age=age, alive=alive, ready=ready)
    assert status(heartbeat_engine)["executor"] == expected


def test_fresh_other_device_cannot_hide_stale_owner(heartbeat_engine):
    put_runtime(heartbeat_engine, age=400)
    put_runtime(heartbeat_engine, device="other", age=0)
    assert status(heartbeat_engine)["executor"] == "STALE"


@pytest.mark.parametrize("canonical_exists", [True, False])
def test_legacy_heartbeat_fallback_for_older_deployment(heartbeat_engine, canonical_exists):
    with heartbeat_engine.begin() as connection:
        if canonical_exists:
            put_runtime(heartbeat_engine, profile="older-profile")
        else:
            connection.execute(text("DROP TABLE runtime_device_state"))
        connection.execute(text("UPDATE app_runtime_status SET heartbeat_at=CURRENT_TIMESTAMP"))
    result = status(heartbeat_engine)
    assert result["executor"] == "HEALTHY"
    assert result["executor_state"] == "LEGACY"
