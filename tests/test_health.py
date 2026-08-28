import datetime as dt
import json
from types import SimpleNamespace

from sqlalchemy import create_engine

from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus
from src.services import health
from src.services.event_journal import EventJournalStatus


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
        kis_last_success_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        orders=[order],
    )

    snapshot = health.collect_health_snapshot(context)
    checks = {check.component: check for check in snapshot.checks}

    assert checks["KIS API"].level == health.HealthLevel.HEALTHY
    assert checks["MySQL"].level == health.HealthLevel.WARNING
    assert checks["Data mirror"].level == health.HealthLevel.WARNING
    assert checks["Reconciliation"].level == health.HealthLevel.CRITICAL
    assert "Event journal" in checks
    assert "Portfolio entry limits" in checks
    assert "NVDA" in checks["Reconciliation"].detail
    assert snapshot.overall_level == health.HealthLevel.CRITICAL


def test_mirror_health_uses_separate_daily_and_hourly_scopes(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        health, "expected_latest_market_data_date", lambda: dt.date(2026, 8, 21)
    )
    monkeypatch.setattr(
        health,
        "local_mirror_is_stale",
        lambda _engine, _expected, *, tickers: calls.setdefault(
            "daily", list(tickers)
        )
        and False,
    )
    monkeypatch.setattr(
        health,
        "local_mirror_hourly_is_stale",
        lambda _engine, _expected, *, tickers: calls.setdefault(
            "hourly", list(tickers)
        )
        and False,
    )
    context = health.HealthContext(
        mirror_engine=object(),
        mirror_tickers=["AAPL", "MSFT", "NVDA"],
        mirror_hourly_tickers=["SPY", "AAPL"],
    )

    check = health._mirror_check(context)

    assert check.level == health.HealthLevel.HEALTHY
    assert calls["daily"] == ["AAPL", "MSFT", "NVDA"]
    assert calls["hourly"] == ["SPY", "AAPL"]


def test_health_displays_effective_runtime_portfolio_limits(monkeypatch):
    monkeypatch.setattr(
        health.execution_config,
        "PORTFOLIO_MAX_SIMULTANEOUS_POSITIONS",
        30,
    )
    monkeypatch.setattr(
        health.execution_config,
        "PORTFOLIO_MAX_TOTAL_OPEN_RISK_FRACTION",
        0.20,
    )
    monkeypatch.setattr(
        health.execution_config,
        "PORTFOLIO_MAX_GROSS_NOTIONAL_FRACTION",
        10.0,
    )

    check = health._portfolio_risk_configuration_check()

    assert check.level == health.HealthLevel.HEALTHY
    assert check.summary == (
        "Effective: 30 positions, 20% open risk, 1000% gross notional"
    )
    assert "runtime configuration overrides" in check.detail


def test_health_surfaces_invalid_entry_configuration_as_critical(monkeypatch):
    monkeypatch.setattr(
        health.execution_config,
        "configuration_issues",
        lambda: ("PORTFOLIO_MAX_GROSS_NOTIONAL_FRACTION: must be finite",),
    )
    monkeypatch.setattr(
        health.execution_config,
        "entry_configuration_issues",
        lambda: ("PORTFOLIO_MAX_GROSS_NOTIONAL_FRACTION: must be finite",),
    )

    check = health._execution_configuration_check()

    assert check.level == health.HealthLevel.CRITICAL
    assert "BUY entries fail closed" in check.detail
    assert "SELL/cancel/reconciliation remain available" in check.detail


def test_unreadable_order_ledger_is_critical():
    check = health._reconciliation_check(
        health.HealthContext(order_ledger_error="orders.json is malformed")
    )

    assert check.level == health.HealthLevel.CRITICAL
    assert "unreadable" in check.summary.lower()


def test_health_separates_kanban_store_from_offline_history_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'kanban.sqlite3'}")
    context = health.HealthContext(
        db_source="none",
        operational_store_configured=True,
        operational_store_engine=engine,
    )

    operational = health._operational_store_check(context)
    historical = health._mysql_check(context)

    assert operational.level == health.HealthLevel.HEALTHY
    assert "execution state is available" in operational.summary
    assert historical.level == health.HealthLevel.WARNING
    assert "historical" in historical.summary.lower()
    assert "Kanban execution is independent" in historical.detail


def test_reconciliation_health_ignores_non_production_open_orders():
    simulation_order = BrokerOrder.create(
        environment="SIM",
        account_no="simulation",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=5,
        limit_price=100.0,
        status=OrderStatus.UNKNOWN_SUBMISSION_STATE,
    )

    check = health._reconciliation_check(
        health.HealthContext(orders=[simulation_order])
    )

    assert check.level == health.HealthLevel.HEALTHY
    assert check.summary == "No unresolved production broker orders"
    assert "Ignored 1 non-production open ledger order(s)." in check.detail


def test_reconciliation_health_counts_only_production_open_orders():
    production_order = BrokerOrder.create(
        environment="PROD",
        account_no="production",
        symbol="NVDA",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=2,
        limit_price=100.0,
        status=OrderStatus.ACCEPTED,
    )
    simulation_order = BrokerOrder.create(
        environment="SIM",
        account_no="simulation",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity_requested=5,
        limit_price=100.0,
        status=OrderStatus.UNKNOWN_SUBMISSION_STATE,
    )

    check = health._reconciliation_check(
        health.HealthContext(orders=[production_order, simulation_order])
    )

    assert check.level == health.HealthLevel.WARNING
    assert check.summary == "1 open order(s) await final state"
    assert "Ignored 1 non-production open ledger order(s)." in check.detail


def test_main_device_handoff_check_healthy_when_main():
    check = health._main_device_handoff_check(
        health.HealthContext(is_main_device=True, lease_age_seconds=12.0)
    )

    assert check.level == health.HealthLevel.HEALTHY
    assert "exclusive main device" in check.summary.lower()
    assert "12" in check.detail


def test_main_device_handoff_check_healthy_pull_only():
    check = health._main_device_handoff_check(
        health.HealthContext(is_main_device=False, main_device_hostname="LAPTOP")
    )

    assert check.level == health.HealthLevel.HEALTHY
    assert "LAPTOP" in check.summary


def test_main_device_handoff_check_unknown_while_reconciling():
    check = health._main_device_handoff_check(
        health.HealthContext(handoff_reconciliation_running=True)
    )

    assert check.level == health.HealthLevel.UNKNOWN


def test_main_device_handoff_check_critical_when_symbols_blocked():
    check = health._main_device_handoff_check(
        health.HealthContext(handoff_blocked_symbols=("AAPL", "MSFT"))
    )

    assert check.level == health.HealthLevel.CRITICAL
    assert "AAPL" in check.detail
    assert "MSFT" in check.detail


def test_main_device_handoff_check_critical_when_execution_fence_remains():
    check = health._main_device_handoff_check(
        health.HealthContext(handoff_reconciliation_required=True)
    )

    assert check.level == health.HealthLevel.CRITICAL
    assert "fenced" in check.summary.lower()


def test_kis_api_health_requires_recent_verified_timestamp():
    now = dt.datetime(2026, 8, 14, 0, 0, tzinfo=dt.timezone.utc)

    recent = health._kis_api_check(
        health.HealthContext(
            kis_snapshot_count=1,
            kis_last_success_at=(now - dt.timedelta(minutes=5)).isoformat(),
        ),
        True,
        now=now,
    )
    stale = health._kis_api_check(
        health.HealthContext(
            kis_snapshot_count=1,
            kis_last_success_at=(now - dt.timedelta(hours=2)).isoformat(),
        ),
        True,
        now=now,
    )
    unknown = health._kis_api_check(
        health.HealthContext(kis_snapshot_count=1),
        True,
        now=now,
    )

    assert recent.level == health.HealthLevel.HEALTHY
    assert stale.level == health.HealthLevel.WARNING
    assert unknown.level == health.HealthLevel.UNKNOWN


def test_mysql_health_executes_current_read_only_probe():
    executed = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            executed.append(str(statement))

    class Engine:
        def connect(self):
            return Connection()

    check = health._mysql_check(
        health.HealthContext(
            db_source="pc",
            pc_database_ready=True,
            pc_database_engine=Engine(),
        )
    )

    assert check.level == health.HealthLevel.HEALTHY
    assert executed == ["SELECT 1"]
    assert "verified now" in check.summary.lower()


def test_mysql_health_does_not_report_cached_state_as_current_success():
    cached = health._mysql_check(
        health.HealthContext(db_source="pc", pc_database_ready=True)
    )

    class BrokenEngine:
        def connect(self):
            raise OSError("database offline")

    failed = health._mysql_check(
        health.HealthContext(
            db_source="pc",
            pc_database_ready=True,
            pc_database_engine=BrokenEngine(),
        )
    )

    assert cached.level == health.HealthLevel.WARNING
    assert "last known" in cached.summary.lower()
    assert failed.level == health.HealthLevel.CRITICAL
    assert "database offline" in failed.detail


def test_market_data_health_metrics_are_projected_to_health_tab():
    metrics = SimpleNamespace(
        ws_connected=True,
        trade_channels_desired=2,
        trade_channels_acked=2,
        quote_channels_desired=2,
        quote_channels_acked=2,
        critical_trade_channels_missing=(),
        critical_quote_channels_missing=(),
        stale_symbols=(),
        receive_lag_p50_ms=1.0,
        receive_lag_p95_ms=2.0,
        receive_lag_p99_ms=3.0,
        reconnect_count=1,
        nack_count=0,
        malformed_frame_count=0,
        queue_depth=0,
        dropped_event_count=4,
    )

    check = health._market_data_check(metrics)

    assert check.level == health.HealthLevel.HEALTHY
    assert "2/2" in check.detail
    assert "exact duplicates" in check.detail


def test_missing_critical_market_data_channel_is_critical():
    metrics = SimpleNamespace(
        ws_connected=True,
        trade_channels_desired=1,
        trade_channels_acked=0,
        quote_channels_desired=1,
        quote_channels_acked=1,
        critical_trade_channels_missing=("AAPL",),
        critical_quote_channels_missing=(),
        stale_symbols=(),
        receive_lag_p50_ms=0.0,
        receive_lag_p95_ms=0.0,
        receive_lag_p99_ms=0.0,
        reconnect_count=0,
        nack_count=1,
        malformed_frame_count=0,
        queue_depth=0,
        dropped_event_count=0,
    )

    check = health._market_data_check(metrics)

    assert check.level == health.HealthLevel.CRITICAL
    assert "AAPL" in check.detail


def test_quiet_symbol_is_warning_while_exact_order_gate_remains_closed():
    metrics = SimpleNamespace(
        ws_connected=True,
        trade_channels_desired=1,
        trade_channels_acked=1,
        quote_channels_desired=1,
        quote_channels_acked=1,
        critical_trade_channels_missing=(),
        critical_quote_channels_missing=(),
        stale_symbols=("STIM",),
        receive_lag_p50_ms=0.0,
        receive_lag_p95_ms=0.0,
        receive_lag_p99_ms=0.0,
        reconnect_count=0,
        nack_count=0,
        malformed_frame_count=0,
        queue_depth=0,
        dropped_event_count=0,
    )

    check = health._market_data_check(metrics)

    assert check.level == health.HealthLevel.WARNING
    assert "STIM" in check.detail
    assert "orders" in check.detail.lower()
    assert "fail closed" in check.detail.lower()


def test_request_scheduler_metrics_are_exposed_in_health():
    metrics = SimpleNamespace(
        queued_requests=2,
        budget_rejections=1,
        uncertain_entry_rejections=1,
        read_retries=3,
        confirmed_mutation_retries=1,
        known_mutation_budget_buckets=0,
        uncertain_mutation_budget_buckets=1,
    )

    check = health._request_scheduler_check(metrics)

    assert check.level == health.HealthLevel.WARNING
    assert "queue=2" in check.detail
    assert "uncertain_entry_blocks=1" in check.detail
    assert "WS0 mutation budgets are unverified" in check.summary


def _journal_status(**overrides):
    values = {
        "path": SimpleNamespace(),
        "directory_writable": True,
        "lock_available": True,
        "lock_stale": False,
        "last_write_at": "2026-08-14T00:00:00+00:00",
        "last_error": "",
        "last_error_at": "",
        "latest_event_at": "2026-08-14T00:00:00+00:00",
        "active_file_size": 1024,
        "archive_count": 2,
        "archive_bytes": 2048,
        "available_disk_space": 10 * 1024 * 1024 * 1024,
        "inspection_error": "",
    }
    values.update(overrides)
    return EventJournalStatus(**values)


def test_event_journal_health_surfaces_write_failure_and_storage_metrics():
    failed = health._event_journal_check(
        _journal_status(last_error="disk unavailable", last_error_at="now")
    )
    healthy = health._event_journal_check(_journal_status())

    assert failed.level == health.HealthLevel.CRITICAL
    assert "disk unavailable" in failed.detail
    assert "active:" in failed.detail
    assert "archives: 2" in failed.detail
    assert "disk free:" in failed.detail
    assert healthy.level == health.HealthLevel.HEALTHY
