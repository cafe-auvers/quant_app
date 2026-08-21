"""TLS-only engine for the small shared trading-coordination database.

Historical bars remain on the PC/local mirror.  This engine is deliberately
separate so an Internet-hosted MySQL-compatible service such as TiDB Cloud is
used only for leases, control state, commands, cards, and execution journals.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Dict, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError

from src.infrastructure.database.engine import (
    validate_mysql_identifier,
    validate_mysql_port,
)
from src.utils.config import get_env_value, resolve_repo_path

logger = logging.getLogger(__name__)


def get_coordination_database_config() -> Dict[str, object]:
    """Return normalized settings without ever rendering the password."""

    host = str(get_env_value("COORD_DB_HOST", "") or "").strip()
    user = str(get_env_value("COORD_DB_USER", "") or "").strip()
    password = str(get_env_value("COORD_DB_PASSWORD", "") or "")
    database = validate_mysql_identifier(
        str(get_env_value("COORD_DB_NAME", "quant_coordination") or ""),
        label="coordination database name",
    )
    if host and (len(host) > 255 or any(ch.isspace() for ch in host)):
        raise ValueError("Invalid COORD_DB_HOST")
    if user and (len(user) > 128 or any(ord(ch) < 32 for ch in user)):
        raise ValueError("Invalid COORD_DB_USER")
    return {
        "host": host,
        "port": validate_mysql_port(get_env_value("COORD_DB_PORT", "4000")),
        "user": user,
        "password": password,
        "database": database,
        "ssl_ca": str(get_env_value("COORD_DB_SSL_CA", "") or "").strip(),
    }


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
    return URL.create(
        drivername="mysql+pymysql",
        username=str(config["user"]),
        password=str(config["password"]),
        host=str(config["host"]),
        port=int(config["port"]),
        database=str(config["database"]),
        query={"charset": "utf8mb4"},
    )


def coordination_store_id() -> str:
    """Non-secret stable identity used in diagnostics and state metadata."""

    config = get_coordination_database_config()
    public = f"{config['host']}:{config['port']}/{config['database']}"
    return hashlib.sha256(public.encode("utf-8")).hexdigest()[:16]


def _coordination_connect_args(config: Dict[str, object]) -> Dict[str, object]:
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


def init_coordination_engine(
    *, ensure_schema: bool = True, raise_on_error: bool = False
) -> Optional[Engine]:
    """Connect to the shared coordination store with a small, short-lived pool."""

    if not coordination_database_configured():
        return None
    engine: Optional[Engine] = None
    try:
        config = get_coordination_database_config()
        engine = create_engine(
            get_coordination_connection_url(),
            future=True,
            pool_pre_ping=True,
            # AWS public load balancers can close an idle connection after
            # 340 seconds. Recycling first prevents surprise half-open pools.
            pool_recycle=240,
            pool_size=3,
            max_overflow=1,
            pool_timeout=3,
            connect_args=_coordination_connect_args(config),
        )
        with engine.connect() as conn:
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
