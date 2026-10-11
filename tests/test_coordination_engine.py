from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL

from src.infrastructure.database import coordination_engine as coordination
from src.services.coordination_schema import ensure_coordination_schema
import pytest
from sqlalchemy.dialects import postgresql


def _values(**overrides):
    values = {
        "COORD_DB_HOST": "example.supabase.com",
        "COORD_DB_PORT": "5432",
        "COORD_DB_USER": "app.user",
        "COORD_DB_PASSWORD": "secret-value",
        "COORD_DB_NAME": "postgres",
        "COORD_DB_SSL_CA": "",
    }
    values.update(overrides)
    return values


def test_partial_coordination_config_stays_on_shared_route(monkeypatch):
    values = _values(COORD_DB_PASSWORD="")
    monkeypatch.setattr(
        coordination, "get_env_value", lambda key, default=None: values.get(key, default)
    )

    assert coordination.coordination_database_configured() is True
    with pytest.raises(ValueError, match="Set COORD_DB_HOST"):
        coordination.get_coordination_connection_url()
    assert coordination.init_coordination_engine(ensure_schema=False) is None


def test_empty_coordination_config_allows_local_mode(monkeypatch):
    monkeypatch.setattr(coordination, "get_env_value", lambda *_args: None)
    assert coordination.coordination_database_configured() is False


def test_default_coordination_backend_is_postgresql():
    config = coordination.normalize_coordination_database_config({})
    assert config["backend"] == "postgresql"
    assert config["port"] == 5432
    assert config["database"] == "postgres"
    assert config["schema"] == "quant_coordination"


@pytest.mark.parametrize("backend", ["mysql", "tidb", "sqlite"])
def test_retired_coordination_backend_fails_closed(monkeypatch, backend):
    values = _values(COORD_DB_BACKEND=backend)
    monkeypatch.setattr(
        coordination, "get_env_value", lambda key, default=None: values.get(key, default)
    )
    monkeypatch.setattr(
        coordination, "create_engine",
        lambda *_args, **_kwargs: pytest.fail("Rejected backend must not connect"),
    )
    assert coordination.coordination_database_configured() is True
    assert coordination.init_coordination_engine(ensure_schema=False) is None
    with pytest.raises(ValueError, match="requires COORD_DB_BACKEND=postgresql"):
        coordination.init_coordination_engine(ensure_schema=False, raise_on_error=True)


def test_connection_helpers_cannot_bypass_retired_backend():
    config = {"backend": "mysql"}
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        coordination.coordination_connection_url(config)
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        coordination.create_coordination_connection_engine(config, read_only=False)


def test_coordination_url_does_not_expose_password(monkeypatch):
    values = _values()
    monkeypatch.setattr(
        coordination, "get_env_value", lambda key, default=None: values.get(key, default)
    )

    url = coordination.get_coordination_connection_url()

    assert url.drivername == "postgresql+psycopg"
    assert url.port == 5432
    assert "secret-value" not in str(url)


def test_coordination_engine_uses_small_tls_pool(monkeypatch):
    captured = []
    engines = []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return None

    class _Engine:
        def __init__(self):
            self.connect_count = 0

        def connect(self):
            self.connect_count += 1
            return _Connection()

        def dispose(self):
            return None

    monkeypatch.setattr(coordination, "coordination_database_configured", lambda: True)
    monkeypatch.setattr(
        coordination,
        "get_coordination_database_config",
        lambda: {
            "backend": "postgresql",
            "schema": "quant_coordination",
            "host": "example.supabase.com",
            "port": 5432,
            "user": "user",
            "password": "password",
            "database": "quant_coordination",
            "ssl_ca": "",
        },
    )
    monkeypatch.setattr(
        coordination,
        "get_coordination_connection_url",
        lambda: URL.create("postgresql+psycopg", host="example.supabase.com"),
    )

    def _create_engine(url, **kwargs):
        captured.append({**kwargs, "url": url})
        engine = _Engine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(coordination, "create_engine", _create_engine)
    monkeypatch.setattr(coordination.event, "listen", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(coordination, "configure_postgresql_session", lambda *_args: None)

    assert coordination.init_coordination_engine(ensure_schema=False) is not None
    assert len(captured) == 2
    writer, reader = captured
    assert writer["pool_size"] == 3
    assert writer["max_overflow"] == 1
    assert writer["pool_pre_ping"] is True
    assert writer["pool_recycle"] == 240
    assert writer["connect_args"]["sslmode"] == "verify-full"
    assert writer["connect_args"]["sslrootcert"]
    assert reader["isolation_level"] == "AUTOCOMMIT"
    assert reader["skip_autocommit_rollback"] is True
    assert reader["pool_pre_ping"] is False
    assert reader["pool_size"] == 2
    assert reader["max_overflow"] == 0
    assert engines[0].connect_count == 0
    assert engines[1].connect_count == 1


def test_coordination_schema_excludes_historical_market_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    ensure_coordination_schema(engine)
    tables = set(inspect(engine).get_table_names())

    assert {"trade_cards", "execution_orders", "operator_commands"} <= tables
    assert "price_history" not in tables
    assert "hourly_price_history" not in tables
    assert "scanner_metrics" not in tables


def test_postgresql_connection_requires_verified_tls_and_private_schema(tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("test certificate")
    config = coordination.normalize_coordination_database_config(
        _values(COORD_DB_BACKEND="postgresql", COORD_DB_PORT="5432",
                COORD_DB_NAME="postgres", COORD_DB_SCHEMA="quant_coordination",
                COORD_DB_SSL_CA=str(ca))
    )
    url = coordination.coordination_connection_url(config)
    args = coordination._coordination_connect_args(config)
    assert url.drivername == "postgresql+psycopg"
    assert "secret-value" not in str(url)
    assert args["sslmode"] == "verify-full"
    assert args["sslrootcert"] == str(ca.resolve())


@pytest.mark.parametrize("schema", ["public", "auth", "storage", "pg_catalog", "bad;schema", "x" * 64])
def test_postgresql_rejects_exposed_or_invalid_schemas(schema):
    with pytest.raises(ValueError):
        coordination.normalize_coordination_database_config(
            _values(COORD_DB_BACKEND="postgresql", COORD_DB_SCHEMA=schema)
        )


def test_postgresql_lease_clock_advances_within_transactions():
    class Engine:
        dialect = postgresql.dialect()

    sql = str(coordination.coordination_server_now(Engine()).compile(dialect=Engine.dialect))
    assert "clock_timestamp()" in sql
    assert "CURRENT_TIMESTAMP" not in sql
