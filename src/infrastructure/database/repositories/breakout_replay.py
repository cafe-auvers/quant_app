"""Dedicated, durable replay records; never touches execution tables."""
from __future__ import annotations

import datetime as dt
import json
import uuid

import pandas as pd
from sqlalchemy import Column, Date, DateTime, Float, Integer, MetaData, String, Table, Text, func, select, update, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.dialects.mysql import LONGTEXT

from src.core.breakout_replay import clean_json
from src.infrastructure.database.schema import _get_price_history_table
from src.infrastructure.database.sql_helpers import _execute_bulk_upsert


def now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def encode(value):
    return json.dumps(clean_json(value), ensure_ascii=False, allow_nan=False)


class ReplayRepository:
    def __init__(self, engine: Engine):
        self.engine = engine
        metadata = MetaData()
        self.scans = Table("breakout_replay_scans", metadata,
            Column("id", String(36), primary_key=True), Column("requested_day", Date, nullable=False),
            Column("setup_name", String(80), nullable=False, server_default="Setup 1"),
            Column("scan_day", Date), Column("status", String(20), nullable=False),
            Column("payload", Text, nullable=False), Column("created_at", DateTime, nullable=False))
        self.results = Table("breakout_replay_results", metadata,
            Column("scan_id", String(36), primary_key=True), Column("symbol", String(20), primary_key=True),
            Column("metrics", Text, nullable=False))
        self.reviews = Table("breakout_replay_reviews", metadata,
            Column("symbol", String(20), primary_key=True), Column("day", Date, primary_key=True),
            Column("decision", String(20), nullable=False), Column("expected_day", Date),
            Column("outcome", String(20)), Column("outcome_asof", Date), Column("pattern", String(40)), Column("notes", Text),
            Column("metrics", Text, nullable=False), Column("created_at", DateTime, nullable=False),
            Column("updated_at", DateTime, nullable=False))
        self.sessions = Table("breakout_replay_sessions", metadata,
            Column("id", String(36), primary_key=True), Column("scan_id", String(36), nullable=False),
            Column("symbol", String(20), nullable=False), Column("start_day", Date, nullable=False),
            Column("cursor", Date, nullable=False), Column("created_at", DateTime, nullable=False))
        self.session_views = Table("breakout_replay_session_views", metadata,
            Column("session_id", String(36), primary_key=True), Column("mode", String(20), nullable=False),
            Column("updated_at", DateTime, nullable=False))
        self.progress = Table("breakout_replay_progress", metadata,
            Column("symbol", String(20), primary_key=True), Column("max_revealed", Date, nullable=False))
        self.drawings = Table("breakout_replay_drawings", metadata,
            Column("id", String(36), primary_key=True), Column("symbol", String(20), nullable=False),
            Column("day", Date, nullable=False), Column("frame", String(10), nullable=False),
            Column("payload", Text, nullable=False), Column("created_at", DateTime, nullable=False))
        self.snapshots = Table("breakout_replay_snapshots", metadata,
            Column("id", String(36), primary_key=True), Column("symbol", String(20), nullable=False),
            Column("day", Date, nullable=False), Column("frame", String(10), nullable=False),
            Column("path", String(500), nullable=False), Column("replay_asof", Date, nullable=False), Column("metadata", Text, nullable=False),
            Column("created_at", DateTime, nullable=False))
        self.events = Table("breakout_replay_events", metadata,
            Column("id", String(36), primary_key=True), Column("symbol", String(20)), Column("day", Date),
            Column("kind", String(40), nullable=False), Column("payload", Text, nullable=False),
            Column("created_at", DateTime, nullable=False))
        self.daily_imports = Table("breakout_replay_daily_imports", metadata,
            Column("symbol", String(20), primary_key=True), Column("start_day", Date, nullable=False),
            Column("end_day", Date, nullable=False), Column("status", String(20), nullable=False),
            Column("row_count", Integer, nullable=False), Column("updated_at", DateTime, nullable=False))
        self.daily_requests = Table("breakout_replay_daily_requests", metadata,
            Column("symbol", String(20), primary_key=True), Column("start_day", Date, primary_key=True),
            Column("end_day", Date, primary_key=True), Column("status", String(20), nullable=False),
            Column("row_count", Integer, nullable=False), Column("updated_at", DateTime, nullable=False))
        self.paper_accounts = Table("breakout_replay_paper_accounts", metadata,
            Column("symbol", String(20), primary_key=True), Column("initial_cash", Float, nullable=False),
            Column("risk_pct", Float, nullable=False), Column("payload", Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
            Column("created_at", DateTime, nullable=False), Column("updated_at", DateTime, nullable=False))
        self.paper_studies = Table("breakout_replay_paper_studies", metadata,
            Column("id", String(36), primary_key=True), Column("symbol", String(20), nullable=False, index=True),
            Column("start_day", Date, nullable=False), Column("label", String(80), nullable=False),
            Column("payload", Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
            Column("created_at", DateTime, nullable=False), Column("updated_at", DateTime, nullable=False))
        self.paper_actions = Table("breakout_replay_paper_actions", metadata,
            Column("id", String(36), primary_key=True), Column("symbol", String(20), nullable=False, index=True),
            Column("day", Date, nullable=False), Column("timestamp", DateTime, nullable=False),
            Column("payload", Text, nullable=False), Column("created_at", DateTime, nullable=False))
        self.paper_fills = Table("breakout_replay_paper_fills", metadata,
            Column("symbol", String(20), primary_key=True), Column("id", String(100), primary_key=True),
            Column("day", Date, nullable=False), Column("timestamp", DateTime, nullable=False),
            Column("payload", Text, nullable=False), Column("updated_at", DateTime, nullable=False))
        # This archive is intentionally separate from the live cache's retention policy.
        self.bars = Table("breakout_replay_intraday", metadata,
            Column("symbol", String(20), primary_key=True), Column("timestamp", DateTime, primary_key=True),
            Column("source", String(30), primary_key=True), Column("open", Float), Column("high", Float),
            Column("low", Float), Column("close", Float), Column("volume", Float),
            Column("created_at", DateTime, nullable=False))
        metadata.create_all(engine)
        if "setup_name" not in {c["name"] for c in inspect(engine).get_columns(self.scans.name)}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE breakout_replay_scans ADD COLUMN setup_name VARCHAR(80) NOT NULL DEFAULT 'Setup 1'"))
        if "replay_asof" not in {c["name"] for c in inspect(engine).get_columns(self.snapshots.name)}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE breakout_replay_snapshots ADD COLUMN replay_asof DATE"))
                rows = conn.execute(text("SELECT id,day,metadata FROM breakout_replay_snapshots")).mappings().all()
                for row in rows:
                    cutoff = json.loads(row["metadata"]).get("replay_cursor") or str(row["day"])
                    conn.execute(update(self.snapshots).where(self.snapshots.c.id == row["id"]).values(replay_asof=dt.date.fromisoformat(cutoff[:10])))
        self.daily = _get_price_history_table(MetaData())
        from src.infrastructure.database.repositories.replay_prepared import PreparedReplayRepository
        from src.utils.config import DATA_DIR

        self.prepared = PreparedReplayRepository(engine,
            DATA_DIR / "breakout_replay" / "prepared" if engine.dialect.name == "mysql" else None)

    def upsert(self, table, values, keys):
        with self.engine.begin() as conn:
            _execute_bulk_upsert(conn, table, values, keys, self.engine.dialect.name)

    def rows(self, stmt):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def event(self, kind, payload, symbol=None, day=None):
        with self.engine.begin() as conn:
            conn.execute(self.events.insert().values(id=str(uuid.uuid4()), kind=kind,
                payload=encode(payload), symbol=symbol, day=day, created_at=now()))

    def paper_action_rows(self, symbol):
        rows = self.rows(select(self.paper_actions).where(self.paper_actions.c.symbol == symbol)
            .order_by(self.paper_actions.c.timestamp, self.paper_actions.c.created_at))
        actions = []
        for sequence, row in enumerate(rows):
            action = json.loads(row["payload"])
            action["day"] = row["day"]
            action.setdefault("sequence", sequence)
            actions.append(action)
        return sorted(actions, key=lambda a: (a["time"], a["sequence"], a["id"]))

    def paper_study_rows(self, symbol):
        rows = self.rows(select(self.paper_studies).where(self.paper_studies.c.symbol == symbol)
            .order_by(self.paper_studies.c.created_at.desc()))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def save_study_projection(self, symbol, study, projection, action=None):
        with self.engine.begin() as conn:
            existing = {row[0] for row in conn.execute(select(self.paper_fills.c.id).where(self.paper_fills.c.symbol == symbol))}
            if action:
                conn.execute(self.paper_actions.insert().values(id=action["id"], symbol=symbol,
                    day=action["day"], timestamp=dt.datetime.fromtimestamp(action["time"], dt.timezone.utc).replace(tzinfo=None),
                    payload=encode(action), created_at=now()))
                # Rebuild only this study's derived fills; other studies remain intact.
                stale = [row["id"] for row in conn.execute(select(self.paper_fills)
                    .where(self.paper_fills.c.symbol == symbol)).mappings()
                    if json.loads(row["payload"]).get("study_id") == study["id"]]
                if stale:
                    conn.execute(self.paper_fills.delete().where(self.paper_fills.c.symbol == symbol,
                        self.paper_fills.c.id.in_(stale)))
                conn.execute(self.events.insert().values(id=str(uuid.uuid4()), symbol=symbol, day=action["day"],
                    kind="paper_trade", payload=encode({"action": action, "study_id": study["id"],
                        "account": {k: v for k, v in projection.items() if k not in ("fills", "equity_curve", "warnings")}}), created_at=now()))
            _execute_bulk_upsert(conn, self.paper_studies, [{"id": study["id"], "symbol": symbol,
                "start_day": study["start_day"], "label": study["label"], "payload": encode(projection),
                "created_at": study["created_at"], "updated_at": now()}], ("id",), self.engine.dialect.name)
            records = [{"symbol": symbol, "id": fill["id"], "day": dt.date.fromisoformat(fill["day"]),
                "timestamp": dt.datetime.fromtimestamp(fill["time"], dt.timezone.utc).replace(tzinfo=None),
                "payload": encode(fill), "updated_at": now()} for fill in projection["fills"]]
            if records:
                _execute_bulk_upsert(conn, self.paper_fills, records, ("symbol", "id"), self.engine.dialect.name)
            for fill in projection["fills"]:
                if fill["id"] not in existing and fill["reason"].startswith("stop"):
                    conn.execute(self.events.insert().values(id=str(uuid.uuid4()), symbol=symbol,
                        day=dt.date.fromisoformat(fill["day"]), kind="paper_stop_fill", payload=encode(fill), created_at=now()))

    def save_paper_projection(self, symbol, projection, action=None):
        with self.engine.begin() as conn:
            account = conn.execute(select(self.paper_accounts).where(self.paper_accounts.c.symbol == symbol)
                .with_for_update()).mappings().first()
            if action:
                conn.execute(self.paper_actions.insert().values(id=action["id"], symbol=symbol,
                    day=action["day"], timestamp=dt.datetime.fromtimestamp(action["time"], dt.timezone.utc).replace(tzinfo=None),
                    payload=encode(action), created_at=now()))
                # Later automatic fills are projections; new earlier partial exits can change them.
                conn.execute(self.paper_fills.delete().where(self.paper_fills.c.symbol == symbol,
                    self.paper_fills.c.timestamp >= dt.datetime.fromtimestamp(action["time"], dt.timezone.utc).replace(tzinfo=None)))
                conn.execute(self.events.insert().values(id=str(uuid.uuid4()), symbol=symbol, day=action["day"],
                    kind="paper_trade", payload=encode({"action": action,
                        "account": {k: v for k, v in projection.items() if k not in ("fills", "equity_curve", "warnings")}}), created_at=now()))
            if not account and not action:
                return
            _execute_bulk_upsert(conn, self.paper_accounts, [{"symbol": symbol, "initial_cash": projection["initial_cash"],
                "risk_pct": projection["risk_pct"], "payload": encode(projection),
                "created_at": account["created_at"] if account else now(), "updated_at": now()}], ("symbol",), self.engine.dialect.name)
            records = [{"symbol": symbol, "id": fill["id"], "day": dt.date.fromisoformat(fill["day"]),
                "timestamp": dt.datetime.fromtimestamp(fill["time"], dt.timezone.utc).replace(tzinfo=None),
                "payload": encode(fill), "updated_at": now()} for fill in projection["fills"]]
            existing = {row[0] for row in conn.execute(select(self.paper_fills.c.id).where(self.paper_fills.c.symbol == symbol))}
            if records:
                _execute_bulk_upsert(conn, self.paper_fills, records, ("symbol", "id"), self.engine.dialect.name)
            for fill in projection["fills"]:
                if fill["id"] not in existing and fill["reason"].startswith("stop"):
                    conn.execute(self.events.insert().values(id=str(uuid.uuid4()), symbol=symbol,
                        day=dt.date.fromisoformat(fill["day"]), kind="paper_stop_fill", payload=encode(fill), created_at=now()))

    def symbols(self):
        statement = select(self.daily.c.symbol).where(self.daily.c.interval == "1d").distinct()
        statement = statement.with_hint(self.daily,
            "FORCE INDEX (ix_price_history_symbol_interval_date)", dialect_name="mysql")
        return [r["symbol"] for r in self.rows(statement)]

    def daily_dates(self, symbol, start=None):
        stmt = select(self.daily.c.date).where(self.daily.c.symbol == symbol, self.daily.c.interval == "1d",
            self.daily.c.open.is_not(None), self.daily.c.high.is_not(None), self.daily.c.low.is_not(None),
            self.daily.c.close > 0, self.daily.c.volume.is_not(None))
        if start:
            stmt = stmt.where(self.daily.c.date >= dt.datetime.combine(start, dt.time.min))
        return sorted({r["date"].date() for r in self.rows(stmt)})

    def bounds(self):
        # Per-symbol min/max uses the existing symbol/interval/date index. A
        # combined count(distinct)+min/max scans millions of bars during import.
        bounds = self.symbol_bounds()
        return {"first": min((r["first"] for r in bounds.values()), default=None),
            "last": max((r["last"] for r in bounds.values()), default=None), "symbols": len(bounds)}

    def symbol_bounds(self):
        statement = select(self.daily.c.symbol,
            func.min(self.daily.c.date).label("first"), func.max(self.daily.c.date).label("last"))
        statement = statement.where(self.daily.c.interval == "1d").group_by(self.daily.c.symbol)
        statement = statement.with_hint(self.daily,
            "FORCE INDEX (ix_price_history_symbol_interval_date)", dialect_name="mysql")
        return {row["symbol"]: row for row in self.rows(statement)}

    def date_bounds(self):
        return self.rows(select(func.min(self.daily.c.date).label("first"), func.max(self.daily.c.date).label("last"))
            .where(self.daily.c.interval == "1d"))[0]

    def daily_coverage(self, start, end, symbols=None):
        if symbols == []:
            return {}
        stmt = (select(self.daily.c.symbol,
            func.count().label("count"), func.max(self.daily.c.date).label("last"))
            .where(self.daily.c.interval == "1d", self.daily.c.open.is_not(None),
                self.daily.c.high.is_not(None), self.daily.c.low.is_not(None),
                self.daily.c.close > 0, self.daily.c.volume.is_not(None),
                self.daily.c.date >= dt.datetime.combine(start, dt.time.min),
                self.daily.c.date < dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min))
            .group_by(self.daily.c.symbol))
        if symbols is not None:
            stmt = stmt.where(self.daily.c.symbol.in_(symbols))
        return {row["symbol"]: row for row in self.rows(stmt)}

    def histories(self, symbols, start, end):
        stmt = select(self.daily.c.symbol, self.daily.c.date, self.daily.c.open, self.daily.c.high,
            self.daily.c.low, self.daily.c.close, self.daily.c.volume).where(self.daily.c.symbol.in_(symbols), self.daily.c.interval == "1d",
            self.daily.c.date >= dt.datetime.combine(start, dt.time.min),
            self.daily.c.date < dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min)).order_by(self.daily.c.symbol, self.daily.c.date)
        stmt = stmt.with_hint(self.daily, "USE INDEX (PRIMARY)", dialect_name="mysql")
        rows = self.rows(stmt)
        if not rows:
            return {}
        frame = pd.DataFrame(rows).set_index("date")
        return {s: group.rename(columns={n: n.title() for n in ("open", "high", "low", "close", "volume")})
            [["Open", "High", "Low", "Close", "Volume"]] for s, group in frame.groupby("symbol")}

    def get_scan(self, scan_id):
        rows = self.rows(select(self.scans).where(self.scans.c.id == scan_id))
        if not rows:
            raise ValueError("Scan not found")
        row = rows[0]
        row["payload"] = json.loads(row["payload"])
        return row

    def update_scan(self, scan_id, **values):
        if "payload" in values:
            values["payload"] = encode(values["payload"])
        with self.engine.begin() as conn:
            conn.execute(update(self.scans).where(self.scans.c.id == scan_id).values(**values))

    def review_rows(self, symbol=None, through=None):
        stmt = select(self.reviews)
        if symbol:
            stmt = stmt.where(self.reviews.c.symbol == symbol)
        if through:
            stmt = stmt.where(self.reviews.c.day <= through)
        rows = self.rows(stmt.order_by(self.reviews.c.day))
        for row in rows:
            row["metrics"] = json.loads(row["metrics"])
            if through and row["outcome_asof"] and row["outcome_asof"] > through:
                row["outcome"] = None
        return rows

    def get_session(self, session_id):
        rows = self.rows(select(self.sessions, self.scans.c.setup_name).select_from(
            self.sessions.join(self.scans, self.sessions.c.scan_id == self.scans.c.id))
            .where(self.sessions.c.id == session_id))
        if not rows:
            raise ValueError("Replay session not found")
        views = self.rows(select(self.session_views.c.mode).where(self.session_views.c.session_id == session_id))
        return {**rows[0], "mode": views[0]["mode"] if views else "backtesting"}

    def set_cursor(self, session_id, symbol, day):
        with self.engine.begin() as conn:
            conn.execute(update(self.sessions).where(self.sessions.c.id == session_id).values(cursor=day))
            self._mark_revealed(conn, symbol, day)

    def mark_revealed(self, symbol, day):
        with self.engine.begin() as conn:
            self._mark_revealed(conn, symbol, day)

    def _mark_revealed(self, conn, symbol, day):
        prior = conn.execute(select(self.progress).where(self.progress.c.symbol == symbol).with_for_update()).mappings().first()
        _execute_bulk_upsert(conn, self.progress, [{"symbol": symbol, "max_revealed": max(day, prior["max_revealed"]) if prior else day}],
            ("symbol",), self.engine.dialect.name)

    def save_review(self, symbol, day, values):
        record = {"symbol": symbol, "day": day, **values, "updated_at": now()}
        with self.engine.begin() as conn:
            prior = conn.execute(select(self.reviews).where(self.reviews.c.symbol == symbol, self.reviews.c.day == day)).mappings().first()
            record["created_at"] = prior["created_at"] if prior else now()
            _execute_bulk_upsert(conn, self.reviews, [record], ("symbol", "day"), self.engine.dialect.name)
            conn.execute(self.events.insert().values(id=str(uuid.uuid4()), symbol=symbol, day=day,
                kind="review_saved", payload=encode({"before": dict(prior) if prior else None, "after": record}), created_at=now()))

    def intraday(self, symbol, day, source):
        start = dt.datetime.combine(day, dt.time.min)
        rows = self.rows(select(self.bars).where(self.bars.c.symbol == symbol, self.bars.c.source == source,
            self.bars.c.timestamp >= start, self.bars.c.timestamp < start + dt.timedelta(days=1)).order_by(self.bars.c.timestamp))
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).set_index("timestamp").rename(columns={n: n.title() for n in ("open", "high", "low", "close", "volume")})

    def cached_intraday_source(self, symbol, day, source="auto"):
        start = dt.datetime.combine(day, dt.time.min)
        stmt = select(self.bars.c.source, func.count().label("count")).where(
            self.bars.c.symbol == symbol, self.bars.c.timestamp >= start,
            self.bars.c.timestamp < start + dt.timedelta(days=1))
        if source != "auto":
            stmt = stmt.where(self.bars.c.source == source)
        rows = self.rows(stmt.group_by(self.bars.c.source))
        preference = {"alpaca_sip": 0, "massive": 1, "yfinance": 2}
        rows.sort(key=lambda row: (-row["count"], preference.get(row["source"], 3), row["source"]))
        return rows[0]["source"] if rows else None

    def archive_intraday(self, symbol, history, source):
        records = []
        for stamp, row in history.iterrows():
            stamp = pd.Timestamp(stamp).tz_convert("UTC").tz_localize(None).to_pydatetime()
            records.append({"symbol": symbol, "timestamp": stamp, "source": source, "created_at": now(),
                **{n.lower(): float(row[n]) for n in ("Open", "High", "Low", "Close", "Volume")}})
        if records:
            self.upsert(self.bars, records, ("symbol", "timestamp", "source"))
