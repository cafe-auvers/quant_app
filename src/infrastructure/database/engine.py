"""P2 engine extraction from the legacy database loader."""
from ._shared import *  # noqa: F401,F403

def validate_mysql_identifier(value: str, *, label: str = "database name") -> str:
    """Accept a deliberately narrow set of safe MySQL identifier characters."""
    identifier = str(value or "").strip()
    if (
        not identifier
        or len(identifier) > 64
        or _MYSQL_IDENTIFIER_PATTERN.fullmatch(identifier) is None
    ):
        raise ValueError(
            f"Invalid {label} {value!r}; use 1-64 ASCII letters, digits, or underscores"
        )
    return identifier


def validate_mysql_port(value: object) -> int:
    """Validate a TCP port before passing it to the database driver."""
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid MySQL port {value!r}; use an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid MySQL port {value!r}; use an integer from 1 to 65535")
    return port


def _validate_mysql_host(value: object) -> str:
    host = str(value or "").strip()
    if (
        not host
        or len(host) > 255
        or any(character.isspace() or character in _MYSQL_HOST_FORBIDDEN_CHARACTERS for character in host)
    ):
        raise ValueError("Invalid MySQL host; set MYSQL_HOST to a hostname or IP address")
    return host


def _validate_mysql_user(value: object) -> str:
    user = str(value or "").strip()
    if not user or len(user) > 128 or any(character.isspace() or ord(character) < 32 for character in user):
        raise ValueError("Invalid MySQL user; set MYSQL_USER to a non-empty database account")
    return user


def validate_mysql_config(config: Optional[Dict[str, str]] = None) -> Dict[str, object]:
    """Validate optional-cache settings and return normalized connection values.

    The app intentionally uses a pre-provisioned database.  Schema creation
    needs only rights within that database; it never needs a global CREATE
    DATABASE privilege.
    """
    raw = config or get_mysql_config()
    return {
        "host": _validate_mysql_host(raw.get("host")),
        "port": validate_mysql_port(raw.get("port")),
        "user": _validate_mysql_user(raw.get("user")),
        "password": str(raw.get("password") or ""),
        "database": validate_mysql_identifier(raw.get("database"), label="database name"),
    }

def _utcnow_naive() -> dt.datetime:
    """Return a naive UTC timestamp for existing DB columns and comparisons."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def get_mysql_connection_url(db_name: Optional[str] = None) -> URL:
    # Validate an explicit identifier before inspecting environment settings so
    # callers get the actionable injection-safety error they asked for.
    if db_name is not None:
        db_name = validate_mysql_identifier(db_name)
    config = validate_mysql_config()
    if db_name is None:
        db_name = config["database"]
    db_name = validate_mysql_identifier(str(db_name))

    host = str(config["host"])
    port = int(config["port"])
    user = str(config["user"])
    password = str(config["password"])

    return URL.create(
        drivername="mysql+pymysql",
        username=user or None,
        password=password or None,
        host=host,
        port=port,
        database=db_name,
        query={"charset": "utf8mb4"},
    )


def init_mysql_engine(
    db_name: Optional[str] = None,
    *,
    log_unavailable: bool = True,
    ensure_schema: bool = True,
) -> Optional[Engine]:
    """Open the optional MySQL cache, returning ``None`` when unavailable.

    Periodic connectivity probes can set ``log_unavailable=False`` to keep an
    expected offline PC from producing the same INFO message on every poll.
    The failure remains available at DEBUG level for diagnostics. Routing
    probes use ``ensure_schema=False`` because the refresh workflow provisions
    the PC database and table inspection must not delay the dashboard.
    """
    engine: Optional[Engine] = None
    try:
        if db_name is None:
            db_name = str(validate_mysql_config()["database"])
        db_name = validate_mysql_identifier(db_name)
        engine = create_engine(
            get_mysql_connection_url(db_name=db_name),
            future=True,
            pool_pre_ping=True,
            pool_recycle=MYSQL_POOL_RECYCLE_SECONDS,
            connect_args={
                "connect_timeout": MYSQL_CONNECT_TIMEOUT_SECONDS,
                "read_timeout": MYSQL_READ_WRITE_TIMEOUT_SECONDS,
                "write_timeout": MYSQL_READ_WRITE_TIMEOUT_SECONDS,
            },
        )
        # Connect before doing schema work so a disabled/unreachable optional
        # cache fails quickly and cleanly instead of surfacing later in a UI
        # action.  The configured account must already have access to DB_NAME.
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        if ensure_schema:
            _ensure_price_history_table(engine)
            _ensure_hourly_price_history_table(engine)
            _ensure_chart_indicators_table(engine)
            _ensure_chart_indicator_manifests_table(engine)
            _ensure_intraday_price_history_table(engine)
            _ensure_scanner_metrics_table(engine)
            _ensure_scanner_metric_snapshots_table(engine)
        return engine
    except (ImportError, OSError, SQLAlchemyError, ValueError, TypeError) as exc:
        if engine is not None:
            engine.dispose()
        log = logger.info if log_unavailable else logger.debug
        log("MySQL cache disabled: %s", exc)
        return None
