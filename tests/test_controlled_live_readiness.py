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
