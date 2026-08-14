import json
from types import SimpleNamespace

from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
from src.services import health


def test_kis_token_health_uses_only_cache_metadata(monkeypatch, tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text(
        json.dumps({"access_token": "secret-value", "expires_at": 2_000}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        health,
        "load_config",
        lambda _environment: SimpleNamespace(token_cache_path=token_path),
    )

    check, configured = health.inspect_kis_token(now_epoch=1_000)

    assert configured is True
    assert check.level == health.HealthLevel.HEALTHY
    assert "secret-value" not in check.detail


def test_health_snapshot_surfaces_stale_data_and_unknown_orders(monkeypatch):
    monkeypatch.setattr(
        health,
        "inspect_kis_token",
        lambda: (
            health.HealthCheck(
                "KIS token", health.HealthLevel.HEALTHY, "Token is valid"
            ),
            True,
        ),
    )
    monkeypatch.setattr(health, "local_mirror_is_stale", lambda *_a, **_k: True)
    monkeypatch.setattr(health, "local_mirror_hourly_is_stale", lambda *_a, **_k: False)
    order = BrokerOrder.create(
        environment="PROD",
        account_no="12345678",
        symbol="NVDA",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=147,
        limit_price=100.0,
        status=OrderStatus.UNKNOWN_SUBMISSION_STATE,
    )
    context = health.HealthContext(
        db_source="local_mirror",
        mirror_engine=object(),
        mirror_tickers=["NVDA"],
        kis_snapshot_count=1,
        orders=[order],
    )

    snapshot = health.collect_health_snapshot(context)
    checks = {check.component: check for check in snapshot.checks}

    assert checks["KIS API"].level == health.HealthLevel.HEALTHY
    assert checks["MySQL"].level == health.HealthLevel.WARNING
    assert checks["Data mirror"].level == health.HealthLevel.WARNING
    assert checks["Reconciliation"].level == health.HealthLevel.CRITICAL
    assert "NVDA" in checks["Reconciliation"].detail
    assert snapshot.overall_level == health.HealthLevel.CRITICAL


def test_unreadable_order_ledger_is_critical():
    check = health._reconciliation_check(
        health.HealthContext(order_ledger_error="orders.json is malformed")
    )

    assert check.level == health.HealthLevel.CRITICAL
    assert "unreadable" in check.summary.lower()
