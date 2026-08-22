import pytest
from sqlalchemy.engine import URL

import main as app_main
import src.utils.config as config_module
import src.utils.db_loader as db_loader
from src.infrastructure.database import engine as engine_module
from src.infrastructure.database import schema as schema_module
from src.utils.db_loader import (get_mysql_connection_url,
                                 validate_mysql_config,
                                 validate_mysql_identifier)


@pytest.mark.parametrize(
    "value",
    ["quant_app", "db2", "_local", "A" * 64],
)
def test_validate_mysql_identifier_accepts_allowlisted_names(value):
    assert validate_mysql_identifier(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "quant-app", "quant app", "db` ; DROP DATABASE mysql; --", "A" * 65],
)
def test_validate_mysql_identifier_rejects_unsafe_names(value):
    with pytest.raises(ValueError, match="Invalid database name"):
        validate_mysql_identifier(value)


def test_connection_url_rejects_unsafe_database_before_engine_creation():
    with pytest.raises(ValueError, match="Invalid database name"):
        get_mysql_connection_url("bad`name")


def test_mysql_config_requires_explicit_host_and_user(monkeypatch):
    monkeypatch.setattr(
        engine_module,
        "get_mysql_config",
        lambda: {"host": "", "port": "3306", "user": "", "password": "", "database": "quant_app"},
    )

    with pytest.raises(ValueError, match="MySQL host"):
        validate_mysql_config()


def test_init_mysql_uses_preprovisioned_database_with_bounded_timeouts(monkeypatch):
    calls = []
    queries = []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            queries.append(str(statement))

    class _Engine:
        dialect = type("Dialect", (), {"name": "mysql"})()

        def connect(self):
            return _Connection()

        def dispose(self):
            return None

    engine = _Engine()
    monkeypatch.setattr(
        engine_module,
        "get_mysql_config",
        lambda: {
            "host": "127.0.0.1",
            "port": "3306",
            "user": "quant_user",
            "password": "secret",
            "database": "quant_app",
        },
    )
    monkeypatch.setattr(
        engine_module,
        "create_engine",
        lambda url, **kwargs: calls.append((url, kwargs)) or engine,
    )
    for name in (
        "_ensure_price_history_table",
        "_ensure_hourly_price_history_table",
        "_ensure_chart_indicators_table",
        "_ensure_chart_indicator_manifests_table",
        "_ensure_intraday_price_history_table",
            "_ensure_scanner_metrics_table",
            "_ensure_scanner_metric_snapshots_table",
            "_ensure_stock_profiles_table",
            "_ensure_earnings_events_table",
            "_ensure_fundamental_sync_state_table",
        ):
        monkeypatch.setattr(schema_module, name, lambda _engine: None)

    result = db_loader.init_mysql_engine()

    assert result is engine
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert isinstance(url, URL)
    assert url.database == "quant_app"
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["connect_args"]["connect_timeout"] == db_loader.MYSQL_CONNECT_TIMEOUT_SECONDS
    assert queries == ["SELECT 1"]


def test_main_loads_repository_env_before_runtime_imports(monkeypatch):
    """The early loader fills values before runtime modules snapshot config."""

    key = "QUANT_TEST_EARLY_ENV_LOAD"
    monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        config_module,
        "load_env_file",
        lambda: {key: "loaded-before-runtime-import"},
    )

    app_main._load_repository_environment()

    assert app_main.os.environ[key] == "loaded-before-runtime-import"


def test_main_does_not_override_explicit_machine_environment(monkeypatch):
    key = "QUANT_TEST_EARLY_ENV_OVERRIDE"
    monkeypatch.setenv(key, "machine-value")
    monkeypatch.setattr(
        config_module,
        "load_env_file",
        lambda: {key: "file-value"},
    )

    app_main._load_repository_environment()

    assert app_main.os.environ[key] == "machine-value"
