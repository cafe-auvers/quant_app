from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from scripts import check_controlled_live_readiness as preflight
from scripts.check_controlled_live_readiness import (
    _missing_external_delivery_configuration,
)


def test_external_delivery_requires_both_alert_and_heartbeat_urls(monkeypatch):
    monkeypatch.delenv("EXTERNAL_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_HEARTBEAT_WEBHOOK_URL", raising=False)

    assert _missing_external_delivery_configuration() == (
        "EXTERNAL_ALERT_WEBHOOK_URL",
        "EXTERNAL_HEARTBEAT_WEBHOOK_URL",
    )

    monkeypatch.setenv("EXTERNAL_ALERT_WEBHOOK_URL", "https://alerts.example.test")
    assert _missing_external_delivery_configuration() == (
        "EXTERNAL_HEARTBEAT_WEBHOOK_URL",
    )

    monkeypatch.setenv(
        "EXTERNAL_HEARTBEAT_WEBHOOK_URL", "https://heartbeat.example.test"
    )
    assert _missing_external_delivery_configuration() == ()


@pytest.fixture
def configured_preflight(monkeypatch):
    commit = "a" * 40
    monkeypatch.setattr(preflight, "_git", lambda *args: commit if args[0] == "rev-parse" else "")
    monkeypatch.setenv("KIS_RUNTIME_COMMIT_SHA", commit)
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setattr(preflight, "is_trading_locked_disabled", lambda: not preflight._truthy(preflight.os.getenv("TRADING_ENABLED", "")))
    monkeypatch.setattr(preflight, "current_release_identity", lambda: SimpleNamespace(issues=()))
    monkeypatch.setattr(preflight, "_configured", lambda name: True)
    config = preflight.execution_config
    monkeypatch.setattr(config, "is_buyboard_engine_enabled", lambda: True)
    for name, value in {
        "KIS_LIVE_EXECUTION_MODE": "CONTROLLED_LIVE",
        "KIS_WS_ENABLED": True,
        "KIS_WS_PROTOCOL_VERIFIED": True,
        "KIS_MARKET_DATA_MODE": "WEBSOCKET",
        "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY": 41,
        "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL": 100,
        "KIS_CONTROLLED_LIVE_MAX_ENTRY_EQUITY_FRACTION": 0,
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(preflight, "controlled_live_symbols", lambda: ())
    monkeypatch.setattr(preflight, "KisWsSymbolKeyStore", lambda: SimpleNamespace(
        snapshot=lambda: SimpleNamespace(last_error=None, keys={}),
    ))
    monkeypatch.setattr(preflight, "require_controlled_live_configuration", lambda **kwargs: None)
    monkeypatch.setattr(preflight, "build_kis_realtime_market_data_from_environment", lambda **kwargs: SimpleNamespace(
        configure_desired_channels=lambda **channels: None,
        subscription_capacity_snapshot=lambda: SimpleNamespace(reconnect_replay_count=0, total_capacity=41),
    ))
    monkeypatch.setattr(preflight, "init_coordination_engine", lambda **kwargs: SimpleNamespace(
        connect=lambda: nullcontext(SimpleNamespace(execute=lambda query: None)),
        dispose=lambda: None,
    ))


def _set_read_only(monkeypatch):
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setattr(preflight.execution_config, "KIS_LIVE_EXECUTION_MODE", "DISABLED")
    monkeypatch.setattr(preflight.execution_config, "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL", 0)


def test_default_preflight_still_blocks_disabled_live_configuration(configured_preflight, monkeypatch, capsys):
    _set_read_only(monkeypatch)

    assert preflight.main([]) == 1

    output = capsys.readouterr().out
    for check in ("administrative trading permission", "controlled-live mode", "entry notional cap"):
        assert f"[FAIL] {check}" in output


def test_startup_reports_read_only_without_claiming_live_readiness(configured_preflight, monkeypatch, tmp_path, capsys):
    _set_read_only(monkeypatch)
    report_path = tmp_path / "preflight.json"

    assert preflight.main(["--startup", "--json-output", str(report_path)]) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "READ_ONLY_STARTUP_READY"
    assert report["startup_ready"] is True
    assert report["ready"] is False
    assert report["controlled_live_ready"] is False
    assert report["failures"] == []
    output = capsys.readouterr().out
    assert "[FAIL]" not in output
    assert "[INFO] Read-only startup" in output
    assert "This does not certify Gate 2 or controlled-live readiness" in output


def test_read_only_startup_retains_release_and_database_failures(configured_preflight, monkeypatch, tmp_path, capsys):
    _set_read_only(monkeypatch)
    monkeypatch.setenv("KIS_RUNTIME_COMMIT_SHA", "b" * 40)
    monkeypatch.setattr(preflight, "current_release_identity", lambda: SimpleNamespace(
        issues=("KIS_RUNTIME_COMMIT_SHA does not match repository HEAD",),
    ))
    monkeypatch.setattr(preflight, "init_coordination_engine", lambda **kwargs: None)
    report_path = tmp_path / "preflight.json"

    assert preflight.main(["--startup", "--json-output", str(report_path)]) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "BLOCKED"
    assert report["startup_ready"] is False
    assert report["controlled_live_ready"] is False
    assert len(report["failures"]) == 3
    output = capsys.readouterr().out
    for check in ("runtime SHA pin", "approved exact-release identity", "canonical MySQL connectivity"):
        assert f"[FAIL] {check}" in output
    assert "[FAIL] administrative trading permission" not in output


@pytest.mark.parametrize("permission", ["", "typo", "true"])
def test_startup_requires_an_explicit_administrative_lock_for_read_only_mode(configured_preflight, monkeypatch, permission, capsys):
    _set_read_only(monkeypatch)
    monkeypatch.setenv("TRADING_ENABLED", permission)

    assert preflight.main(["--startup"]) == 1

    output = capsys.readouterr().out
    assert "[INFO] Read-only startup" not in output
    assert "[FAIL] controlled-live mode" in output


def test_startup_preserves_controlled_live_readiness_checks(configured_preflight, tmp_path):
    report_path = tmp_path / "preflight.json"

    assert preflight.main(["--startup", "--json-output", str(report_path)]) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "CONTROLLED_LIVE_CONFIGURATION_READY"
    assert report["ready"] is True
    assert report["startup_ready"] is True
    assert report["controlled_live_ready"] is True
