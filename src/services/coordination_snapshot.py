"""Fresh, differential reads of versioned coordination rows.

Every call executes one statement against the canonical database. Known row
identities, versions and timestamps travel *into* the database; only changed
rows and deletion markers travel out. A statement snapshot avoids timestamp
watermarks that can miss a transaction which commits late.
"""
from __future__ import annotations

import json
import threading
import weakref
from dataclasses import dataclass, field

from sqlalchemy import (
    DateTime, Text, and_, bindparam, case, cast, column, exists, func, null,
    select, union_all, values,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from src.infrastructure.database.coordination_engine import coordination_read_connection


@dataclass
class _Snapshot:
    lock: threading.RLock = field(default_factory=threading.RLock)
    rows: dict = field(default_factory=dict)


_snapshots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_snapshots_lock = threading.Lock()


def invalidate_versioned_rows(engine: Engine) -> None:
    """Forget snapshots after a schema/store restore."""
    with _snapshots_lock:
        _snapshots.pop(engine, None)


def read_versioned_rows(
    engine: Engine,
    table,
    *,
    cache_key: tuple,
    key_columns: tuple[str, ...],
    revision_column: str,
    conditions=(),
    comparison_columns: tuple[str, ...] = (),
):
    """Return immutable raw rows, freshly verified in one database snapshot.

    The cache is scoped to an engine and query scope. Callers must decode/copy
    rows into their own mutable models. Failed reads never return cached success.
    """
    with _snapshots_lock:
        snapshot = _snapshots.setdefault(engine, {}).setdefault(cache_key, _Snapshot())
    with snapshot.lock:
        revision = table.c[revision_column]
        stamp = table.c.updated_at
        statement = select(table).where(*conditions)
        if snapshot.rows:
            columns = tuple(dict.fromkeys((
                *key_columns, revision_column, "updated_at", *comparison_columns,
            )))
            typed_columns = tuple(column(name, table.c[name].type) for name in columns)
            if engine.dialect.name == "postgresql":
                # One bind and stable SQL as history grows; VALUES otherwise
                # eventually exceeds PostgreSQL's bind-parameter limit.
                document = json.dumps([
                    {name: row._mapping[name] for name in columns}
                    for row in snapshot.rows.values()
                ], default=str, separators=(",", ":"))
                known = func.jsonb_to_recordset(cast(
                    bindparam("known_document", document, type_=Text()), JSONB
                )).table_valued(
                    *typed_columns
                ).render_derived(name="known_rows", with_types=True)
            else:
                known = values(*typed_columns).data([
                    tuple(row._mapping[name] for name in columns)
                    for row in snapshot.rows.values()
                ]).cte("known_rows")
            identity_matches = and_(
                *(known.c[name] == table.c[name] for name in key_columns)
            )
            known_stamp, stored_stamp = known.c.updated_at, stamp
            comparisons = []
            if engine.dialect.name == "sqlite":
                # CURRENT_TIMESTAMP has no fractional suffix; SQLAlchemy's
                # datetime binds do. Compare the same exact representation.
                def normalized_stamp(value):
                    raw = func.replace(cast(value, Text()), "T", " ")
                    padded = case((func.instr(raw, ".") == 0, raw + ".000000"), else_=raw)
                    return func.rtrim(func.rtrim(padded, "0"), ".")

                known_stamp, stored_stamp = normalized_stamp(known_stamp), normalized_stamp(stored_stamp)
            for name in comparison_columns:
                known_value, stored_value = known.c[name], table.c[name]
                if engine.dialect.name == "sqlite" and isinstance(table.c[name].type, DateTime):
                    known_value = normalized_stamp(known_value)
                    stored_value = normalized_stamp(stored_value)
                comparisons.append(known_value.is_not_distinct_from(stored_value))
            unchanged = exists(
                select(1).select_from(known).where(
                    identity_matches,
                    known.c[revision_column] == revision,
                    known_stamp == stored_stamp,
                    *comparisons,
                )
            )
            changed = statement.where(~unchanged)
            present = exists(
                select(1).select_from(table).where(identity_matches, *conditions)
            )
            removed = select(*(
                known.c[item.name] if item.name in key_columns
                else cast(null(), item.type).label(item.name)
                for item in table.c
            )).select_from(known).where(~present)
            statement = union_all(changed, removed)
        with coordination_read_connection(engine) as connection:
            rows = connection.execute(statement).fetchall()
        refreshed = dict(snapshot.rows)
        for row in rows:
            identity = tuple(row._mapping[name] for name in key_columns)
            if row._mapping[revision_column] is None:
                refreshed.pop(identity, None)
            else:
                refreshed[identity] = row
        snapshot.rows = refreshed
        return [refreshed[key] for key in sorted(refreshed)]
