"""TLS-only engine for the small shared trading-coordination database.

Historical bars remain on the PC/local mirror. MySQL/TiDB or PostgreSQL/Supabase
is used only for leases, control state, commands, cards, and execution journals.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Dict, Mapping, Optional

from sqlalchemy import create_engine, event, func, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from src.infrastructure.database.engine import (
    validate_mysql_identifier,
    validate_mysql_port,
)
from src.utils.config import get_env_value, resolve_repo_path

logger = logging.getLogger(__name__)


_READ_ENGINE_ATTRIBUTE = "_quant_coordination_read_engine"


def get_coordination_database_config() -> Dict[str, object]:
    """Return normalized settings without ever rendering the password."""

    keys = ("BACKEND", "HOST", "PORT", "USER", "PASSWORD", "NAME", "SSL_CA", "SCHEMA")
    return normalize_coordination_database_config(
        {f"COORD_DB_{key}": get_env_value(f"COORD_DB_{key}") for key in keys}
    )


def normalize_coordination_database_config(values: Mapping[str, object]) -> Dict[str, object]:
    """Validate either supported store without changing the default MySQL path."""

    backend = str(values.get("COORD_DB_BACKEND") or "mysql").strip().lower()
    if backend not in {"mysql", "postgresql"}:
        raise ValueError("COORD_DB_BACKEND must be mysql or postgresql")

    host = str(values.get("COORD_DB_HOST") or "").strip()
    user = str(values.get("COORD_DB_USER") or "").strip()
    password = str(values.get("COORD_DB_PASSWORD") or "")
    database = validate_mysql_identifier(
        str(values.get("COORD_DB_NAME") or ("postgres" if backend == "postgresql" else "quant_coordination")),
        label="coordination database name",
    )
    if host and (len(host) > 255 or any(ch.isspace() for ch in host)):
        raise ValueError("Invalid COORD_DB_HOST")
    if user and (len(user) > 128 or any(ord(ch) < 32 for ch in user)):
        raise ValueError("Invalid COORD_DB_USER")
    schema = str(values.get("COORD_DB_SCHEMA") or "").strip()
    if backend == "postgresql":
        schema = validate_private_coordination_schema(schema or "quant_coordination")
    return {
        "backend": backend,
        "host": host,
        "port": validate_mysql_port(values.get("COORD_DB_PORT") or ("5432" if backend == "postgresql" else "4000")),
        "user": user,
        "password": password,
        "database": database,
        "ssl_ca": str(values.get("COORD_DB_SSL_CA") or "").strip(),
        "schema": schema,
    }


def validate_private_coordination_schema(schema: str) -> str:
    schema = validate_mysql_identifier(schema, label="coordination schema")
    reserved = {"public", "auth", "storage", "extensions", "information_schema"}
    if len(schema) > 63 or schema.lower() in reserved or schema.lower().startswith("pg_"):
        raise ValueError("PostgreSQL coordination requires a private schema")
    return schema


def coordination_database_configured() -> bool:
    """A partial configuration is unavailable, never silently downgraded."""

    try:
        config = get_coordination_database_config()
    except (TypeError, ValueError):
        return False
    return all(
        str(config.get(key) or "").strip()
        for key in ("host", "user", "password", "database")
    )


def get_coordination_connection_url() -> URL:
    config = get_coordination_database_config()
    if not coordination_database_configured():
        raise ValueError(
            "Set COORD_DB_HOST, COORD_DB_USER, COORD_DB_PASSWORD, and COORD_DB_NAME"
        )
    return coordination_connection_url(config)


def coordination_connection_url(config: Mapping[str, object]) -> URL:
    postgres = config.get("backend") == "postgresql"
    return URL.create(
        drivername="postgresql+psycopg" if postgres else "mysql+pymysql",
        username=str(config["user"]),
        password=str(config["password"]),
        host=str(config["host"]),
        port=int(config["port"]),
        database=str(config["database"]),
        query={} if postgres else {"charset": "utf8mb4"},
    )


def coordination_store_id() -> str:
    """Non-secret stable identity used in diagnostics and state metadata."""

    config = get_coordination_database_config()
    public = f"{config['host']}:{config['port']}/{config['database']}"
    if config.get("backend") == "postgresql":
        public += f"/{config['schema']}"
    return hashlib.sha256(public.encode("utf-8")).hexdigest()[:16]


def _coordination_connect_args(config: Dict[str, object]) -> Dict[str, object]:
    if config.get("backend") == "postgresql":
        import certifi

        raw_ca = str(config.get("ssl_ca") or "").strip()
        ca_path = resolve_repo_path(raw_ca) if raw_ca else Path(certifi.where())
        if not ca_path.is_file():
            raise ValueError(f"COORD_DB_SSL_CA does not exist: {ca_path}")
        return {
            "connect_timeout": 5,
            "sslmode": "verify-full",
            "sslrootcert": str(ca_path.resolve()),
        }
    args: Dict[str, object] = {
        "connect_timeout": 5,
        "read_timeout": 10,
        "write_timeout": 10,
        # TiDB Cloud public endpoints require an authenticated TLS session.
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    }
    raw_ca = str(config.get("ssl_ca") or "").strip()
    if raw_ca:
        ca_path = resolve_repo_path(raw_ca)
        if not ca_path.is_file():
            raise ValueError(f"COORD_DB_SSL_CA does not exist: {ca_path}")
        args["ssl_ca"] = str(Path(ca_path).resolve())
    return args


def configure_postgresql_session(engine: Engine, config: Mapping[str, object]) -> None:
    """Keep unqualified repositories inside a private schema and use UTC."""
    if config.get("backend") != "postgresql":
        return
    schema = validate_private_coordination_schema(str(config["schema"]))

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection, _record):
        previous = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f'SET SESSION search_path TO "{schema}"')
                cursor.execute("SET SESSION TIME ZONE 'UTC'")
                cursor.execute("SET SESSION statement_timeout = '10s'")
                cursor.execute("SET SESSION lock_timeout = '3s'")
        finally:
            dbapi_connection.autocommit = previous


def coordination_server_now(engine: Engine):
    """Actual server wall clock, including after a PostgreSQL lock wait."""
    if engine.dialect.name == "mysql":
        return func.utc_timestamp(6)
    if engine.dialect.name == "postgresql":
        return func.timezone("UTC", func.clock_timestamp())
    return func.current_timestamp()


def create_coordination_connection_engine(
    config: Dict[str, object], *, read_only: bool, pool_size: int = 2, max_overflow: int = 0
) -> Engine:
    options = {
        "future": True, "pool_pre_ping": not read_only, "pool_recycle": 240,
        "pool_size": pool_size, "max_overflow": max_overflow, "pool_timeout": 3,
        "connect_args": _coordination_connect_args(config),
    }
    if read_only:
        options.update(isolation_level="AUTOCOMMIT", skip_autocommit_rollback=True)
    engine = create_engine(coordination_connection_url(config), **options)
    configure_postgresql_session(engine, config)
    return engine


def coordination_autocommit_connection(engine: Engine) -> Connection:
    """Return a one-statement, rollback-free coordination connection.

    The production coordination engine owns a separate autocommit reader
    pool.  Keeping that pool separate matters: changing isolation level on a
    transactional pool checkout sends two additional ``SET autocommit``
    requests.  The reader also omits ``pool_pre_ping`` because recycling at
    240 seconds is already below TiDB Cloud's public-endpoint idle timeout.
    Consequently a routine read emits the SELECT itself -- no ping, COMMIT,
    ROLLBACK, or isolation-toggle request.  Non-coordination engines retain
    their normal connection behavior for unit tests and local stores.
    """

    read_engine = getattr(engine, _READ_ENGINE_ATTRIBUTE, None)
    if read_engine is not None:
        return read_engine.connect()
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def coordination_read_connection(engine: Engine) -> Connection:
    """Return the low-request autocommit connection for a read operation."""

    read_engine = getattr(engine, _READ_ENGINE_ATTRIBUTE, None)
    return (read_engine or engine).connect()


def init_coordination_engine(
    *, ensure_schema: bool = True, raise_on_error: bool = False
) -> Optional[Engine]:
    """Connect to the shared coordination store with a small, short-lived pool."""

    if not coordination_database_configured():
        return None
    engine: Optional[Engine] = None
    try:
        config = get_coordination_database_config()
        connection_url = get_coordination_connection_url()
        connect_args = _coordination_connect_args(config)
        engine = create_engine(
            connection_url,
            future=True,
            pool_pre_ping=True,
            # AWS public load balancers can close an idle connection after
            # 340 seconds. Recycling first prevents surprise half-open pools.
            pool_recycle=240,
            pool_size=3,
            max_overflow=1,
            pool_timeout=3,
            connect_args=connect_args,
        )
        configure_postgresql_session(engine, config)
        # SQLAlchemy 2.0.43 added ``skip_autocommit_rollback`` specifically
        # for this case.  Without it, closing a read-only AUTOCOMMIT
        # connection still called DBAPI.rollback(), which TiDB billed as a
        # standalone request.  A dedicated pool avoids per-checkout
        # isolation toggles and makes every ordinary read exactly one SQL
        # request.
        read_engine = create_engine(
            connection_url,
            future=True,
            isolation_level="AUTOCOMMIT",
            skip_autocommit_rollback=True,
            pool_pre_ping=False,
            pool_recycle=240,
            pool_size=2,
            max_overflow=0,
            pool_timeout=3,
            connect_args=connect_args,
        )
        configure_postgresql_session(read_engine, config)
        setattr(engine, _READ_ENGINE_ATTRIBUTE, read_engine)
        event.listen(
            engine,
            "engine_disposed",
            lambda _engine: read_engine.dispose(),
        )
        from src.services.coordination_change_pulse import (
            install_coordination_change_tracking,
        )

        install_coordination_change_tracking(engine)
        # The dedicated AUTOCOMMIT pool also carries one-statement readiness
        # refreshes. Track its semantic writes against the primary engine's
        # pulse state while continuing to ignore timestamp-only heartbeats.
        install_coordination_change_tracking(
            read_engine,
            pulse_engine=engine,
            autocommit=True,
        )
        with coordination_read_connection(engine) as conn:
            conn.execute(text("SELECT 1"))
        if ensure_schema:
            from src.services.coordination_schema import ensure_coordination_schema

            ensure_coordination_schema(engine)
        return engine
    except (ImportError, OSError, SQLAlchemyError, ValueError, TypeError) as exc:
        if engine is not None:
            engine.dispose()
        logger.debug(
            "Shared coordination database unavailable (%s): %s",
            coordination_store_id() if coordination_database_configured() else "unconfigured",
            type(exc).__name__,
        )
        if raise_on_error:
            raise
        return None
