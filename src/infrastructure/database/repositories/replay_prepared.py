"""Persist prepared studies separately from observations and trading state."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import zlib

from sqlalchemy import Column, Date, DateTime, Integer, LargeBinary, MetaData, String, Table, Text, func, select
from sqlalchemy.dialects.mysql import LONGBLOB

from src.core.breakout_replay import clean_json
from src.core.replay_precompute import PRECOMPUTE_VERSION
from src.infrastructure.database.sql_helpers import _execute_bulk_upsert


def pack(value):
    return zlib.compress(json.dumps(clean_json(value), allow_nan=False, separators=(",", ":")).encode(), 3)


def unpack(value):
    return json.loads(zlib.decompress(value))


class PreparedReplayRepository:
    def __init__(self, engine, cache_dir=None):
        self.engine, self.cache_dir = engine, Path(cache_dir) if cache_dir else None
        metadata = MetaData()
        blob = LargeBinary().with_variant(LONGBLOB(), "mysql")
        self.archives = Table("breakout_replay_prepared_daily", metadata,
            Column("symbol", String(20), primary_key=True), Column("source_key", String(64), nullable=False),
            Column("version", Integer, nullable=False), Column("first_day", Date, nullable=False),
            Column("last_day", Date, nullable=False), Column("days", Text, nullable=False),
            Column("payload", blob, nullable=False), Column("updated_at", DateTime, nullable=False))
        self.scans = Table("breakout_replay_prepared_scans", metadata,
            Column("day", Date, primary_key=True), Column("setup_name", String(80), primary_key=True),
            Column("rules_key", String(64), nullable=False), Column("version", Integer, nullable=False),
            Column("source_key", String(64), nullable=False), Column("payload", blob, nullable=False),
            Column("updated_at", DateTime, nullable=False))
        self.jobs = Table("breakout_replay_preparation", metadata,
            Column("name", String(80), primary_key=True), Column("payload", blob, nullable=False),
            Column("updated_at", DateTime, nullable=False))
        metadata.create_all(engine)

    def rows(self, stmt):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def save(self, table, records, keys):
        with self.engine.begin() as conn:
            _execute_bulk_upsert(conn, table, records, keys, self.engine.dialect.name)

    def job(self, name):
        rows = self.rows(select(self.jobs.c.payload).where(self.jobs.c.name == name))
        return unpack(rows[0]["payload"]) if rows else None

    def save_job(self, name, payload):
        self.save(self.jobs, [{"name": name, "payload": pack(payload), "updated_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)}], ("name",))

    def _disk_path(self, symbol):
        key = hashlib.sha256(symbol.encode()).hexdigest()
        return self.cache_dir / f"{key}.zlib" if self.cache_dir else None

    def _write_disk(self, symbol, payload):
        path = self._disk_path(symbol)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)

    def archive_revision(self, symbol):
        path = self._disk_path(symbol)
        return path.stat().st_mtime_ns if path and path.exists() else None

    def calendars(self, symbols):
        if not symbols:
            return {}
        compressed = self.engine.dialect.name == "mysql"
        column = func.compress(self.archives.c.days) if compressed else self.archives.c.days
        rows = self.rows(select(self.archives.c.symbol, column.label("days"))
            .where(self.archives.c.symbol.in_(symbols), self.archives.c.version == PRECOMPUTE_VERSION))
        # MySQL COMPRESS adds a four-byte length before the zlib stream.
        return {row["symbol"]: json.loads(zlib.decompress(row["days"][4:]) if compressed else row["days"])
            for row in rows}

    def archive(self, symbol, use_disk=True):
        path = self._disk_path(symbol)
        if use_disk and path and path.exists():
            try:
                value = unpack(path.read_bytes())
                if value["version"] == PRECOMPUTE_VERSION:
                    return value
            except (ValueError, KeyError, zlib.error):
                pass
        rows = self.rows(select(self.archives.c.payload).where(self.archives.c.symbol == symbol,
            self.archives.c.version == PRECOMPUTE_VERSION))
        if not rows:
            return None
        value = unpack(rows[0]["payload"])
        self._write_disk(symbol, rows[0]["payload"])
        return value

    def save_archive(self, symbol, source_key, artifact):
        self.save_archive_records([self.archive_record(symbol, source_key, artifact)])

    def archive_record(self, symbol, source_key, artifact):
        artifact["source_key"] = source_key
        payload = pack(artifact)
        days = [row["time"] for row in artifact["chart"]["bars"]]
        return {"symbol": symbol, "source_key": source_key, "version": PRECOMPUTE_VERSION,
            "first_day": dt.date.fromisoformat(days[0]), "last_day": dt.date.fromisoformat(days[-1]),
            "days": json.dumps(days), "payload": payload,
            "updated_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)}

    def save_archive_records(self, records):
        for offset in range(0, len(records), 20):
            batch = records[offset:offset + 20]
            self.save(self.archives, batch, ("symbol",))
            for record in batch:
                self._write_disk(record["symbol"], record["payload"])

    def scan(self, day, setup_name, rules_key):
        rows = self.rows(select(self.scans.c.payload, self.scans.c.source_key).where(self.scans.c.day == day,
            self.scans.c.setup_name == setup_name, self.scans.c.rules_key == rules_key,
            self.scans.c.version == PRECOMPUTE_VERSION))
        if not rows:
            return None
        value = unpack(rows[0]["payload"])
        value["source_key"] = rows[0]["source_key"]
        return value

    def save_scans(self, setup_name, rules_key, source_key, scans):
        self.save(self.scans, [{"day": day, "setup_name": setup_name, "rules_key": rules_key,
            "version": PRECOMPUTE_VERSION, "source_key": source_key, "payload": pack(value),
            "updated_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)} for day, value in scans.items()], ("day", "setup_name"))
