from __future__ import annotations

from datetime import datetime, timezone
import json

from scripts import capture_kis_ws_notice_evidence as collector
from src.api.kis_websocket import KisWsDataFrame, KisWsSystemFrame


def test_notice_capture_persists_structure_without_decrypted_values(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(collector, "_git_snapshot", lambda: "d" * 40)
    for name, value in {
        "KIS_PROD_BASE_URL": "configured",
        "KIS_PROD_APP_KEY": "configured",
        "KIS_PROD_APP_SECRET": "configured",
        "KIS_PROD_WS_URL": "configured",
        "KIS_WS_HTS_ID": "configured",
        "KIS_PROD_ACCOUNT_NO": "12345678",
    }.items():
        monkeypatch.setenv(name, value)

    private_values = [
        f"VALUE-{index}" for index in range(len(collector.NOTICE_COLUMNS))
    ]
    private_values[1] = "12345678"

    class FakeClient:
        def __init__(self, **_kwargs):
            self._connection = lambda *_args: None
            self._ack = lambda *_args: None
            self._data = lambda *_args: None

        def on_connection(self, callback):
            self._connection = callback

        def on_ack(self, callback):
            self._ack = callback

        def on_data(self, callback):
            self._data = callback

        def subscribe(self, _subscriptions):
            return None

        def start(self):
            now = datetime.now(timezone.utc)
            self._connection(True, "", 1)
            self._ack(
                KisWsSystemFrame(
                    tr_id=collector.NOTICE_TR_ID,
                    accepted=True,
                    encrypt="Y",
                    encryption_key="K" * 32,
                    encryption_iv="I" * 16,
                )
            )
            self._data(
                KisWsDataFrame(
                    tr_id=collector.NOTICE_TR_ID,
                    record_count=1,
                    payload="^".join(private_values),
                    encrypted=True,
                    received_at=now,
                    payload_fingerprint="ciphertext-fingerprint",
                )
            )

        def stop(self):
            return None

    monkeypatch.setattr(
        collector, "KisWsApprovalKeyProvider", lambda **_kwargs: object()
    )
    monkeypatch.setattr(collector, "KisWebSocketClient", FakeClient)

    output = tmp_path / "notice.json"
    status_output = tmp_path / "notice-status.json"
    evidence = collector.capture_notice(
        output=output,
        timeout_seconds=5,
        status_output=status_output,
        status_seconds=1,
    )
    persisted = output.read_text(encoding="utf-8")
    status = status_output.read_text(encoding="utf-8")

    assert evidence["errors"] == []
    assert evidence["broker_mutations"] == 0
    assert evidence["subscription_acknowledgement"]["encryption_key_present"] is True
    assert evidence["notice_observation"]["field_count"] == len(
        collector.NOTICE_COLUMNS
    )
    assert evidence["notice_observation"]["configured_account_matches_field_2"] is True
    assert evidence["notice_observation"]["decrypted_values_persisted"] is False
    assert not any(value in persisted for value in private_values)
    assert json.loads(status)["state"] == "CAPTURED"
    assert not any(value in status for value in private_values)


def test_non_fill_order_event_accepts_observed_missing_trailing_fill_price():
    values = [f"VALUE-{index}" for index in range(len(collector.NOTICE_COLUMNS) - 1)]
    values[collector.NOTICE_FILL_FLAG_POSITION] = "1"

    schema = collector._notice_schema(values)

    assert schema["notification_kind"] == "ORDER_EVENT"
    assert schema["schema_variant"] == "ORDER_EVENT_WITHOUT_TRAILING_FILL_PRICE"
    assert schema["schema_validation_passed"] is True
    assert schema["wire_field_count"] == 24
    assert schema["normalized_field_count"] == 25
    assert schema["field_count_matches_official_sample"] is False
    assert schema["missing_trailing_columns"] == ["CNTG_UNPR12"]
    assert schema["normalized_values"][-1] == ""


def test_fill_notice_does_not_accept_missing_trailing_fill_price():
    values = [f"VALUE-{index}" for index in range(len(collector.NOTICE_COLUMNS) - 1)]
    values[collector.NOTICE_FILL_FLAG_POSITION] = collector.NOTICE_FILL_FLAG

    schema = collector._notice_schema(values)

    assert schema["notification_kind"] == "FILL"
    assert schema["schema_variant"] == "UNRECOGNIZED"
    assert schema["schema_validation_passed"] is False


def test_main_persists_safe_preflight_failure(monkeypatch, tmp_path):
    output = tmp_path / "notice.json"
    status_output = tmp_path / "status.json"
    monkeypatch.setattr(collector, "install_repository_configuration", lambda: None)

    def fail_capture(**_kwargs):
        raise RuntimeError("missing configuration: KIS_WS_HTS_ID")

    monkeypatch.setattr(collector, "capture_notice", fail_capture)

    exit_code = collector.main(
        [
            "--confirm-read-only",
            "--output",
            str(output),
            "--status-output",
            str(status_output),
        ]
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))
    status = json.loads(status_output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert evidence["errors"] == ["missing configuration: KIS_WS_HTS_ID"]
    assert evidence["broker_mutations"] == 0
    assert status["state"] == "PREFLIGHT_FAILED"
    assert status["error"]["message"] == "missing configuration: KIS_WS_HTS_ID"
