"""Complete, verified coordination backups and empty-store restoration.

Callers must stop every desktop and web writer before exporting for cutover.
This module never changes credentials, leases, trading controls, or broker state.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, MetaData, create_engine, func, inspect, select, text
from sqlalchemy.engine import Connection, Engine

from src.infrastructure.database.engine import validate_mysql_identifier
from src.infrastructure.database.coordination_engine import validate_private_coordination_schema
from src.services.coordination_schema import ensure_coordination_schema
from src.services.schema_migration import _decode_backup_value, _encode_backup_value


class CoordinationStoreMigrationError(RuntimeError):
    pass


def coordination_metadata() -> MetaData:
    """Build the current coordination schema without contacting a cloud store."""
    model = create_engine("sqlite+pysqlite:///:memory:")
    try:
        ensure_coordination_schema(model)
        metadata = MetaData()
        metadata.reflect(model)
        return metadata
    finally:
        model.dispose()


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _table_rows(connection: Connection, table) -> list[dict]:
    rows = []
    for row in connection.execute(select(table)).mappings():
        normalized = {}
        for column in table.columns:
            value = row[column.name]
            if value is not None and isinstance(column.type, Boolean):
                if value not in (True, False, 0, 1):
                    raise CoordinationStoreMigrationError("Invalid boolean in backup")
                value = bool(value)
            normalized[column.name] = _encode_backup_value(value)
        rows.append(normalized)
    return sorted(rows, key=_canonical_json)


def export_coordination_snapshot(engine: Engine, *, writers_stopped: bool) -> dict:
    if not writers_stopped:
        raise CoordinationStoreMigrationError("Stop all source writers before exporting")
    return backup_coordination_snapshot(engine)


def backup_coordination_snapshot(engine: Engine) -> dict:
    """Read a coherent recovery backup without stopping application writers.

    A live backup is not sufficient evidence for a cutover: the source can
    change after its transaction completes.
    """
    metadata = coordination_metadata()
    with engine.connect() as connection:
        if engine.dialect.name != "sqlite":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin():
            actual = set(inspect(connection).get_table_names())
            expected = set(metadata.tables)
            if actual != expected:
                raise CoordinationStoreMigrationError(
                    "Source table set differs from current coordination schema; investigate before cutover"
                )
            tables = {}
            for name, table in sorted(metadata.tables.items()):
                actual_columns = {c["name"] for c in inspect(connection).get_columns(name)}
                if actual_columns != set(table.c.keys()):
                    raise CoordinationStoreMigrationError(f"Source columns differ for {name}")
                tables[name] = _table_rows(connection, table)
    payload = {"format": "quant-coordination-backup-v1", "tables": tables}
    return {
        "payload": payload, "sha256": _digest(payload),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def write_coordination_snapshot(path: Path, snapshot: dict) -> None:
    validate_coordination_snapshot(snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite the only pre-cutover backup.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical_json(snapshot) + "\n")


def validate_coordination_snapshot(snapshot: dict) -> None:
    payload = snapshot.get("payload") or {}
    if payload.get("format") != "quant-coordination-backup-v1" or snapshot.get("sha256") != _digest(payload):
        raise CoordinationStoreMigrationError("Backup checksum or format is invalid")
    metadata = coordination_metadata()
    tables = payload.get("tables") or {}
    if set(tables) != set(metadata.tables):
        raise CoordinationStoreMigrationError("Backup does not contain every coordination table")
    for name, rows in tables.items():
        if not isinstance(rows, list) or any(set(row) != set(metadata.tables[name].c.keys()) for row in rows):
            raise CoordinationStoreMigrationError(f"Backup columns differ for {name}")


def provision_private_coordination_schema(engine: Engine, schema: str) -> None:
    if engine.dialect.name != "postgresql":
        raise CoordinationStoreMigrationError("Private schema provisioning requires PostgreSQL")
    schema = validate_private_coordination_schema(schema)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.execute(text(f'REVOKE ALL ON SCHEMA "{schema}" FROM PUBLIC, anon, authenticated'))
        connection.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated'))
        connection.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated'))
    ensure_coordination_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema}" FROM PUBLIC, anon, authenticated'))
        connection.execute(text(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA "{schema}" FROM PUBLIC, anon, authenticated'))


def provision_coordination_application_role(engine: Engine, schema: str, role: str, password: str) -> None:
    """Create a fresh login with access only to the prepared application schema.

    The administrator credential stays out of the desktop and web runtime.
    Raw DBAPI execution keeps the password out of SQLAlchemy error messages.
    """
    from psycopg import sql

    if engine.dialect.name != "postgresql":
        raise CoordinationStoreMigrationError("Application roles require PostgreSQL")
    schema = validate_private_coordination_schema(schema)
    role = validate_mysql_identifier(role, label="coordination application role")
    if not role.startswith("quant_") or len(role) > 63 or len(password) < 32:
        raise CoordinationStoreMigrationError("Use a fresh quant_ application role and strong password")
    with engine.begin() as connection:
        if connection.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}).first():
            raise CoordinationStoreMigrationError("Application role already exists; refusing to replace credentials")
        with connection.connection.driver_connection.cursor() as cursor:
            cursor.execute(sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS CONNECTION LIMIT 24"
            ).format(sql.Identifier(role), sql.Literal(password)))
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        connection.execute(text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"'))
        connection.execute(text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{role}"'))
        connection.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{role}"'))
        connection.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT USAGE, SELECT ON SEQUENCES TO "{role}"'))


def restore_coordination_snapshot(engine: Engine, snapshot: dict) -> dict:
    """Insert into an empty prepared store, compare every row, then commit."""
    validate_coordination_snapshot(snapshot)
    metadata = coordination_metadata()
    tables = snapshot["payload"]["tables"]
    with engine.begin() as connection:
        if set(inspect(connection).get_table_names()) != set(metadata.tables):
            raise CoordinationStoreMigrationError("Target schema is incomplete or contains unrelated tables")
        if engine.dialect.name == "postgresql":
            # Block concurrent target writes throughout emptiness checking and restoration.
            names = ", ".join(f'"{name}"' for name in sorted(metadata.tables))
            connection.execute(text(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE"))
        for table in metadata.tables.values():
            if connection.execute(select(func.count()).select_from(table)).scalar_one():
                raise CoordinationStoreMigrationError("Target contains records; refusing to overwrite")
        for name, table in sorted(metadata.tables.items()):
            rows = [{key: _decode_backup_value(value) for key, value in row.items()} for row in tables[name]]
            for offset in range(0, len(rows), 500):
                connection.execute(table.insert(), rows[offset:offset + 500])
        restored = {name: _table_rows(connection, table) for name, table in sorted(metadata.tables.items())}
        if restored != tables:
            raise CoordinationStoreMigrationError("Target row verification failed; transaction rolled back")
        if engine.dialect.name == "postgresql":
            for table in metadata.tables.values():
                column = table.autoincrement_column
                if column is None:
                    continue
                sequence = connection.execute(
                    text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                    {"table_name": table.name, "column_name": column.name},
                ).scalar_one()
                if sequence:
                    maximum = connection.execute(select(func.max(column))).scalar_one()
                    connection.execute(
                        text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
                        {"sequence": sequence, "value": maximum or 1, "called": maximum is not None},
                    )
    from src.services.coordination_snapshot import invalidate_versioned_rows

    invalidate_versioned_rows(engine)
    return {"verified": True, "sha256": snapshot["sha256"], "rows": {name: len(rows) for name, rows in tables.items()}}
