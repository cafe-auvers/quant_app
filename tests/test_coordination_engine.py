from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL

from src.infrastructure.database import coordination_engine as coordination
from src.services.coordination_schema import ensure_coordination_schema


def _values(**overrides):
    values = {
        "COORD_DB_HOST": "example.tidbcloud.com",
        "COORD_DB_PORT": "4000",
        "COORD_DB_USER": "app.user",
        "COORD_DB_PASSWORD": "secret-value",
        "COORD_DB_NAME": "quant_coordination",
        "COORD_DB_SSL_CA": "",
    }
    values.update(overrides)
    return values


def test_coordination_config_requires_complete_sql_credentials(monkeypatch):
    values = _values(COORD_DB_PASSWORD="")
    monkeypatch.setattr(
        coordination, "get_env_value", lambda key, default=None: values.get(key, default)
    )

    assert coordination.coordination_database_configured() is False


def test_coordination_url_does_not_expose_password(monkeypatch):
    values = _values()
    monkeypatch.setattr(
        coordination, "get_env_value", lambda key, default=None: values.get(key, default)
    )

    url = coordination.get_coordination_connection_url()

    assert url.drivername == "mysql+pymysql"
    assert url.port == 4000
    assert "secret-value" not in str(url)


def test_coordination_engine_uses_small_tls_pool(monkeypatch):
    captured = {}

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return None

    class _Engine:
        def connect(self):
            return _Connection()

        def dispose(self):
            return None

    monkeypatch.setattr(coordination, "coordination_database_configured", lambda: True)
    monkeypatch.setattr(
        coordination,
        "get_coordination_database_config",
        lambda: {
            "host": "example.tidbcloud.com",
            "port": 4000,
            "user": "user",
            "password": "password",
            "database": "quant_coordination",
            "ssl_ca": "",
        },
    )
    monkeypatch.setattr(
        coordination,
        "get_coordination_connection_url",
        lambda: URL.create("mysql+pymysql", host="example.tidbcloud.com"),
    )

    def _create_engine(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return _Engine()

    monkeypatch.setattr(coordination, "create_engine", _create_engine)

    assert coordination.init_coordination_engine(ensure_schema=False) is not None
    assert captured["pool_size"] == 3
    assert captured["max_overflow"] == 1
    assert captured["pool_recycle"] == 240
    assert captured["connect_args"]["ssl_verify_cert"] is True
    assert captured["connect_args"]["ssl_verify_identity"] is True


def test_coordination_schema_excludes_historical_market_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    ensure_coordination_schema(engine)
    tables = set(inspect(engine).get_table_names())

    assert {"trade_cards", "execution_orders", "operator_commands"} <= tables
    assert "price_history" not in tables
    assert "hourly_price_history" not in tables
    assert "scanner_metrics" not in tables
