"""Small SQL helpers shared by focused database modules."""

from typing import List, Tuple

from sqlalchemy import Table, insert
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


def _clean_symbols(symbols: List[str]) -> List[str]:
    cleaned = [
        str(symbol).strip().upper()
        for symbol in symbols
        if str(symbol).strip()
    ]
    return list(dict.fromkeys(cleaned))


def _record_chunks(records: List[dict], chunk_size: int) -> List[List[dict]]:
    size = max(1, int(chunk_size or 1))
    return [
        records[index : index + size]
        for index in range(0, len(records), size)
    ]


def _execute_bulk_upsert(
    conn,
    table: Table,
    records: List[dict],
    key_columns: Tuple[str, ...],
    dialect_name: str,
) -> int:
    if not records:
        return 0

    chunk_size = 5000 if dialect_name == "mysql" else 500
    rows_written = 0
    for chunk in _record_chunks(records, chunk_size):
        if dialect_name == "mysql":
            stmt = mysql_insert(table).values(chunk)
            update_cols = {
                col.name: stmt.inserted[col.name]
                for col in table.columns
                if col.name not in key_columns
            }
            conn.execute(stmt.on_duplicate_key_update(**update_cols))
        elif dialect_name == "sqlite":
            stmt = sqlite_insert(table).values(chunk)
            update_cols = {
                col.name: getattr(stmt.excluded, col.name)
                for col in table.columns
                if col.name not in key_columns
            }
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=list(key_columns),
                    set_=update_cols,
                )
            )
        else:
            conn.execute(insert(table), chunk)
        rows_written += len(chunk)
    return rows_written
