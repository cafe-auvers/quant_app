from __future__ import annotations

import math

import pytest

from src.core import execution_config
from src.ui.buylist.orders import BuylistOrdersMixin


def _clear_issue(name: str) -> None:
    execution_config._configuration_issues.pop(name, None)
    execution_config._entry_configuration_keys.discard(name)


def test_nonfinite_float_override_uses_safe_default_and_is_reported(monkeypatch):
    name = "TEST_NONFINITE_EXECUTION_FLOAT"
    monkeypatch.setenv(name, "nan")
    try:
        value = execution_config._env_float(
            name,
            0.25,
            minimum=0.0,
            maximum=1.0,
            entry_boundary=True,
        )

        assert value == pytest.approx(0.25)
        assert math.isfinite(value)
        assert any(
            item.startswith(f"{name}:")
            for item in execution_config.configuration_issues()
        )
        assert any(
            item.startswith(f"{name}:")
            for item in execution_config.entry_configuration_issues()
        )
    finally:
        _clear_issue(name)


@pytest.mark.parametrize("raw", ["thirty", "0", "31"])
def test_bounded_integer_override_rejects_malformed_or_out_of_range(
    monkeypatch, raw
):
    name = "TEST_POSITION_LIMIT_OVERRIDE"
    monkeypatch.setenv(name, raw)
    try:
        assert execution_config._env_int(
            name,
            30,
            minimum=1,
            maximum=30,
            entry_boundary=True,
        ) == 30
        assert any(
            item.startswith(f"{name}:")
            for item in execution_config.configuration_issues()
        )
    finally:
        _clear_issue(name)


def test_invalid_engine_boolean_fails_closed_and_is_reported(monkeypatch):
    name = "TEST_ENGINE_BOOLEAN"
    monkeypatch.setenv(name, "tru")
    try:
        assert execution_config._env_bool(name, True, fail_closed=True) is False
        assert any(item.startswith(f"{name}:") for item in execution_config.configuration_issues())
    finally:
        _clear_issue(name)


def test_buylist_save_persists_queue_before_remote_state_snapshot():
    calls = []

    class Window:
        def _save_execution_queue_state(self):
            calls.append("queue")

        def _save_state(self):
            calls.append("state")

    BuylistOrdersMixin._save_buylist_state(Window())

    assert calls == ["queue", "state"]
