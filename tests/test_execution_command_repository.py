"""Tests for src.services.execution_command_repository.

docs/kanban_production_readiness.md, Workstream 2, A5, revisions 3.1 and
3.2.
"""
from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateTable

from src.services import execution_command_repository as repo


def _make_engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'commands.db'}", future=True, poolclass=NullPool
    )


def _command(**overrides) -> repo.ExecutionCommand:
    # Realistic account-number shape (matching the redaction utility's own
    # _ACCOUNT_RE pattern) deliberately, not the "1" placeholder used
    # elsewhere in this test suite -- a single-digit account number can
    # incidentally collide with unrelated substrings (e.g. "B-1") once run
    # through the shared account-masking pass, which a real 8+2-digit
    # account number never does.
    fields = dict(
        idempotency_key="IDEMP-1",
        command_type="submit",
        environment="PROD",
        account_no="12345678-01",
        symbol="AAPL",
        lease_epoch=1,
        owner_device_id="device-1",
        lease_token="lease-token-1",
    )
    fields.update(overrides)
    return repo.ExecutionCommand(**fields)


def test_mysql_command_journal_text_column_has_no_server_default():
    table = repo._get_execution_commands_table(MetaData())
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
    response_line = next(
        line for line in ddl.splitlines() if "redacted_response" in line
    )

    assert "DEFAULT" not in response_line.upper()


# --- Construction / validation ------------------------------------------


def test_command_requires_a_non_blank_idempotency_key():
    with pytest.raises(ValueError):
        _command(idempotency_key="")


def test_command_rejects_an_unknown_command_type():
    with pytest.raises(ValueError):
        _command(command_type="not-a-real-type")


def test_command_carries_owner_device_and_lease_token_a6():
    cmd = _command()
    assert cmd.owner_device_id == "device-1"
    assert cmd.lease_token == "lease-token-1"


# --- Redaction (revision 3.2) --------------------------------------------


def test_command_redacts_a_non_dict_redacted_response_field_at_construction():
    cmd = _command(redacted_response="not-a-dict-should-be-wrapped")
    assert isinstance(cmd.redacted_response, dict)


def test_command_redacts_sensitive_keys_at_construction():
    cmd = _command(redacted_response={"access_token": "SECRET", "symbol": "AAPL"})
    assert cmd.redacted_response["access_token"] == "[REDACTED]"
    assert cmd.redacted_response["symbol"] == "AAPL"


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
    assert fetched.owner_device_id == "device-1"
    assert fetched.lease_token == "lease-token-1"


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


def test_record_command_response_round_trips(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_command(engine, _command())

    updated = repo.record_command_response(
        engine, "IDEMP-1", status="ACKNOWLEDGED", broker_response={"broker_order_id": "B-1"},
    )
    assert updated.status == "ACKNOWLEDGED"
    assert updated.redacted_response == {"broker_order_id": "B-1"}
    assert updated.response_hash

    fetched = repo.get_command_by_idempotency_key(engine, "IDEMP-1")
    assert fetched.status == "ACKNOWLEDGED"
    assert fetched.redacted_response == {"broker_order_id": "B-1"}


def test_record_command_response_redacts_the_broker_response(tmp_path):
    engine = _make_engine(tmp_path)
    repo.record_command(engine, _command())

    updated = repo.record_command_response(
        engine,
        "IDEMP-1",
        status="ACKNOWLEDGED",
        broker_response={"account_no": "12345678-01", "access_token": "SECRET", "order_note": "no digits here"},
    )
    assert updated.redacted_response["access_token"] == "[REDACTED]"
    assert "12345678-01" not in str(updated.redacted_response)
    assert updated.redacted_response["order_note"] == "no digits here"


def test_update_command_response_for_a_missing_command_raises(tmp_path):
    engine = _make_engine(tmp_path)
    repo.ensure_execution_commands_table(engine)
    with pytest.raises(repo.CommandNotFoundError):
        repo.record_command_response(
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


# --- shared-transaction primitives (revision 3.2) --------------------------


def test_insert_command_composes_into_a_caller_owned_transaction(tmp_path):
    """The whole point of the primitive split: a caller (the future
    execution gateway) can insert a command and roll the whole
    transaction back without ever committing it -- proving this
    genuinely shares the caller's transaction, not its own."""
    engine = _make_engine(tmp_path)
    repo.ensure_execution_commands_table(engine)

    with engine.begin() as conn:
        repo.insert_command(conn, _command(idempotency_key="ROLLBACK-ME"))
        conn.rollback()

    assert repo.get_command_by_idempotency_key(engine, "ROLLBACK-ME") is None


def test_insert_command_and_update_command_response_compose_in_one_transaction(tmp_path):
    engine = _make_engine(tmp_path)
    repo.ensure_execution_commands_table(engine)

    with engine.begin() as conn:
        repo.insert_command(conn, _command(idempotency_key="ONE-TXN"))
        repo.update_command_response(
            conn, "ONE-TXN", status="ACKNOWLEDGED", broker_response={"broker_order_id": "B-1"}
        )

    fetched = repo.get_command_by_idempotency_key(engine, "ONE-TXN")
    assert fetched.status == "ACKNOWLEDGED"


# --- account_no lookup / compare-and-set (revision 3.2 hardening) ----------


def test_update_command_response_redacts_using_the_commands_own_stored_account_no(tmp_path):
    """No caller-supplied account_no parameter exists any more -- redaction
    must still work, using the account_no the command itself was recorded
    with."""
    engine = _make_engine(tmp_path)
    repo.record_command(engine, _command(account_no="12345678-01"))

    updated = repo.record_command_response(
        engine,
        "IDEMP-1",
        status="ACKNOWLEDGED",
        broker_response={"account_no": "12345678-01", "note": "plain"},
    )
    assert "12345678-01" not in str(updated.redacted_response)
    assert updated.redacted_response["note"] == "plain"


def test_update_command_response_rejects_a_second_write_to_an_already_acknowledged_command(tmp_path):
    """Compare-and-set guard (revision 3.2): once a response has been
    recorded, a second write -- stale retry, race, or bug -- must fail
    loudly rather than silently overwrite the real, already-recorded
    outcome."""
    engine = _make_engine(tmp_path)
    repo.record_command(engine, _command())
    repo.record_command_response(
        engine, "IDEMP-1", status="ACKNOWLEDGED", broker_response={"broker_order_id": "B-1"}
    )

    with pytest.raises(repo.CommandResponseConflictError):
        repo.record_command_response(
            engine, "IDEMP-1", status="FAILED", broker_response={"error": "late retry"}
        )

    # The original, real outcome must survive untouched.
    fetched = repo.get_command_by_idempotency_key(engine, "IDEMP-1")
    assert fetched.status == "ACKNOWLEDGED"
    assert fetched.redacted_response == {"broker_order_id": "B-1"}


def test_update_command_response_rejects_a_stale_expected_version(tmp_path):
    engine = _make_engine(tmp_path)
    recorded = repo.record_command(engine, _command())
    assert recorded.version == 1

    with pytest.raises(repo.CommandResponseConflictError):
        repo.record_command_response(
            engine,
            "IDEMP-1",
            status="ACKNOWLEDGED",
            broker_response={"broker_order_id": "B-1"},
            expected_version=99,
        )
    # A rejected write must not have applied.
    assert repo.get_command_by_idempotency_key(engine, "IDEMP-1").status == "REQUESTED"


# --- _parse_dt / requested_at integrity (revision 3.2) ----------------------


def test_insert_command_rejects_a_blank_requested_at(tmp_path):
    engine = _make_engine(tmp_path)
    with pytest.raises(ValueError):
        repo.record_command(engine, _command(requested_at=""))


def test_insert_command_rejects_a_malformed_requested_at(tmp_path):
    engine = _make_engine(tmp_path)
    with pytest.raises(ValueError):
        repo.record_command(engine, _command(requested_at="not-a-timestamp"))
