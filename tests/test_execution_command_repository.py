"""Tests for src.services.execution_command_repository.

docs/kanban_production_readiness.md, Workstream 2, A5, revision 3.1.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.services import execution_command_repository as repo


def _make_engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'commands.db'}", future=True, poolclass=NullPool
    )


def _command(**overrides) -> repo.ExecutionCommand:
    fields = dict(
        idempotency_key="IDEMP-1",
        command_type="submit",
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        lease_epoch=1,
    )
    fields.update(overrides)
    return repo.ExecutionCommand(**fields)


# --- Construction / validation ------------------------------------------


def test_command_requires_a_non_blank_idempotency_key():
    with pytest.raises(ValueError):
        _command(idempotency_key="")


def test_command_rejects_an_unknown_command_type():
    with pytest.raises(ValueError):
        _command(command_type="not-a-real-type")


# --- record_command / idempotency (A5) -----------------------------------


def test_record_command_round_trips(tmp_path):
    engine = _make_engine(tmp_path)
    recorded = repo.record_command(engine, _command())
    assert recorded.command_id is not None

    fetched = repo.get_command_by_idempotency_key(engine, "IDEMP-1")
    assert fetched is not None
    assert fetched.command_type == "submit"
    assert fetched.environment == "PROD"
    assert fetched.status == "REQUESTED"


def test_duplicate_submit_command_after_restart_is_rejected_by_idempotency_key(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_command(engine, _command())
    with pytest.raises(repo.DuplicateCommandError):
        # Simulates a restart re-attempting the exact same command --
        # same idempotency_key, a fresh ExecutionCommand object.
        repo.record_command(engine, _command())


def test_duplicate_cancel_command_after_lease_handoff_is_rejected(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_command(
        engine, _command(idempotency_key="CANCEL-1", command_type="cancel", target_broker_order_id="B-1", lease_epoch=1)
    )
    # A new device with a *different* lease_epoch still must not be able
    # to replay the same idempotency_key.
    with pytest.raises(repo.DuplicateCommandError):
        repo.record_command(
            engine,
            _command(idempotency_key="CANCEL-1", command_type="cancel", target_broker_order_id="B-1", lease_epoch=2),
        )


def test_get_command_by_idempotency_key_returns_none_when_absent(tmp_path):
    engine = _make_engine(tmp_path)
    assert repo.get_command_by_idempotency_key(engine, "does-not-exist") is None


# --- update_command_response (B4b's post-call persist) --------------------


def test_update_command_response_round_trips(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_command(engine, _command())

    updated = repo.update_command_response(
        engine, "IDEMP-1", status="ACKNOWLEDGED", broker_response={"broker_order_id": "B-1"}
    )
    assert updated.status == "ACKNOWLEDGED"
    assert updated.broker_response == {"broker_order_id": "B-1"}

    fetched = repo.get_command_by_idempotency_key(engine, "IDEMP-1")
    assert fetched.status == "ACKNOWLEDGED"
    assert fetched.broker_response == {"broker_order_id": "B-1"}


def test_update_command_response_for_a_missing_command_raises(tmp_path):
    engine = _make_engine(tmp_path)
    with pytest.raises(repo.CommandNotFoundError):
        repo.update_command_response(
            engine, "does-not-exist", status="FAILED", broker_response={}
        )


def test_distinct_commands_for_the_same_symbol_do_not_collide(tmp_path):
    """Different idempotency keys for the same account+symbol (e.g. a
    submit followed by a later cancel) are independent rows."""
    engine = _make_engine(tmp_path)
    repo.record_command(engine, _command(idempotency_key="SUBMIT-1", command_type="submit"))
    repo.record_command(
        engine, _command(idempotency_key="CANCEL-1", command_type="cancel", target_broker_order_id="B-1")
    )
    assert repo.get_command_by_idempotency_key(engine, "SUBMIT-1").command_type == "submit"
    assert repo.get_command_by_idempotency_key(engine, "CANCEL-1").command_type == "cancel"
