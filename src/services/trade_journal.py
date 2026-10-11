"""Private, durable trade evidence and manually edited publishing drafts.

This service only reads existing orders and plan events. Market-data and disk
failures never participate in broker execution or change a trading decision.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from uuid import uuid4

import pandas as pd

from src.core.journal_charts import render_chart_svg, snapshot_bars
from src.core.trade_journal import (
    NOTE_FIELDS, NY_ZONE, estimated_gross_results, fill_facts, parse_time,
    select_entry_plan,
)
from src.services.daily_trading_summary import load_trade_journal_events
from src.services.execution_order_repository import list_execution_orders
from src.utils.config import DATA_DIR, get_env_value, resolve_repo_path
from src.utils.market_calendar import nyse_regular_session_close_time
from src.utils.redaction import scrub_sensitive_text

logger = logging.getLogger(__name__)
JOURNAL_ROOT = DATA_DIR / "trade_journal"
RESULT_NOTE = ("Estimated gross realized P/L: FIFO using cumulative order averages "
               "and submission sequence; excludes fees, taxes and FX. Missing entry "
               "history is unknown. Confirm against broker statements.")


def journal_root() -> Path:
    configured = str(get_env_value("TRADE_JOURNAL_DIR", "") or "").strip()
    return resolve_repo_path(configured) if configured else JOURNAL_ROOT


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_chart_png(path: Path, svg: str) -> None:
    """QImage painting is thread-safe; a GUI application supplies font metrics."""
    from PyQt5.QtCore import QByteArray
    from PyQt5.QtGui import QGuiApplication, QImage, QPainter
    from PyQt5.QtSvg import QSvgRenderer

    if QGuiApplication.instance() is None or path.exists():
        return
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    image = QImage(1200, 680, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        if not image.save(str(temporary), "PNG"):
            raise OSError("Could not save chart PNG")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class TradeJournalStore:
    """Separate local SQLite archive, with no pruning or execution credentials."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or journal_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "journal.sqlite3"
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    journal_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '{}', notes_updated_at TEXT,
                    last_chart_attempt_at TEXT);
                CREATE TABLE IF NOT EXISTS observations (
                    journal_id TEXT NOT NULL, signature TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (journal_id, signature));
                CREATE TABLE IF NOT EXISTS snapshots (
                    journal_id TEXT NOT NULL, stage TEXT NOT NULL, interval TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (journal_id, stage, interval));
                CREATE TABLE IF NOT EXISTS daily_journals (
                    session_date TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS daily_nav (
                    account_key TEXT NOT NULL, session_date TEXT NOT NULL,
                    stage TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (account_key, session_date, stage));
                CREATE TABLE IF NOT EXISTS journal_cash_flows (
                    account_key TEXT NOT NULL, session_date TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY (account_key, session_date));
                CREATE TABLE IF NOT EXISTS journal_market_days (
                    session_date TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def list_trades(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM trades ORDER BY journal_id").fetchall()
        result = []
        for row in rows:
            trade = json.loads(row["payload"])
            trade["notes"] = json.loads(row["notes"])
            trade["notes_updated_at"] = row["notes_updated_at"]
            trade["last_chart_attempt_at"] = row["last_chart_attempt_at"]
            result.append(trade)
        return sorted(result, key=lambda row: (row["session_date"], row.get("submitted_at") or "", row["journal_id"]), reverse=True)

    def record_fill(self, facts: dict, plan: dict, now: dt.datetime) -> bool:
        signature = hashlib.sha256(_json({key: facts[key] for key in (
            "quantity", "average_fill_price", "status",
        )}).encode()).hexdigest()
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM trades WHERE journal_id=?", (facts["journal_id"],)).fetchone()
            if row:
                previous = json.loads(row["payload"])
                if facts["quantity"] < previous["quantity"]:
                    raise ValueError("Journal fill quantity decreased; manual reconciliation required")
                facts = {**facts, "entry_plan": previous["entry_plan"],
                         "schema_version": previous["schema_version"],
                         "first_observed_at": previous["first_observed_at"],
                         "first_fill_observed_at": previous["first_fill_observed_at"]}
            else:
                facts = {**facts, "entry_plan": plan, "schema_version": 1,
                         "first_observed_at": now.isoformat(),
                         "first_fill_observed_at": facts["fill_observed_at"]}
            inserted = conn.execute(
                "INSERT OR IGNORE INTO observations VALUES (?, ?, ?)",
                (facts["journal_id"], signature, _json({**facts, "archived_at": now.isoformat()})),
            ).rowcount
            if inserted:
                conn.execute(
                    "INSERT INTO trades (journal_id,payload) VALUES (?,?) "
                    "ON CONFLICT(journal_id) DO UPDATE SET payload=excluded.payload",
                    (facts["journal_id"], _json(facts)),
                )
        return bool(inserted)

    def trade_directory(self, trade: dict) -> Path:
        session = dt.date.fromisoformat(trade["session_date"]).isoformat()
        symbol = re.sub(r"[^A-Z0-9.-]", "_", trade["symbol"].upper())[:20] or "UNKNOWN"
        identity = trade["journal_id"]
        if not re.fullmatch(r"[a-f0-9]{24}", identity):
            raise ValueError("Invalid journal identity")
        directory = self.root / "trades" / session / f"{symbol}-{identity}"
        if not directory.resolve().is_relative_to(self.root):
            raise ValueError("Journal path escapes archive")
        return directory

    def snapshots(self, identity: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM snapshots WHERE journal_id=? ORDER BY stage,interval", (identity,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_snapshot(self, trade: dict, snapshot: dict) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)", (
                trade["journal_id"], snapshot["stage"], snapshot["interval"], _json(snapshot),
            ))

    def save_notes(self, identity: str, notes: dict, now: dt.datetime | None = None) -> None:
        safe = {key: scrub_sensitive_text(str(notes.get(key, ""))) for key in NOTE_FIELDS}
        with self.connect() as conn:
            changed = conn.execute("UPDATE trades SET notes=?,notes_updated_at=? WHERE journal_id=?", (
                _json(safe), (now or dt.datetime.now(dt.timezone.utc)).isoformat(), identity,
            )).rowcount
            if not changed:
                raise KeyError(identity)
        trade = next(row for row in self.list_trades() if row["journal_id"] == identity)
        self.materialize(trade)

    def materialize(self, trade: dict) -> Path:
        directory = self.trade_directory(trade)
        snapshots = self.snapshots(trade["journal_id"])
        export = {key: value for key, value in trade.items() if key != "last_chart_attempt_at"}
        _atomic_text(directory / "trade.json", _json({**export, "chart_snapshots": snapshots}))
        with self.connect() as conn:
            observations = conn.execute("SELECT signature,payload FROM observations WHERE journal_id=?", (trade["journal_id"],)).fetchall()
        for row in observations:
            path = directory / "observations" / f"{row['signature']}.json"
            if not path.exists():
                _atomic_text(path, row["payload"])
        for snapshot in snapshots:
            name = f"{snapshot['stage']}-{snapshot['interval']}"
            _atomic_text(directory / f"{name}.json", _json(snapshot))
            svg = render_chart_svg(
                snapshot["bars"], title=f"{trade['symbol']} | {trade['session_date']} | {snapshot['interval']}",
                subtitle=f"{snapshot['stage']} | {snapshot['source']} | {snapshot['capture_basis']}",
                plan=trade["entry_plan"], fill_price=snapshot["fill_price_at_capture"],
                interval=snapshot["interval"], marker_time=trade["first_fill_observed_at"],
            )
            _atomic_text(directory / f"{name}.svg", svg)
            _write_chart_png(directory / f"{name}.png", svg)
        return directory


def load_journal_market_data(symbol: str, session: dt.date, now: dt.datetime,
                             market_engine=None, allow_network: bool = True) -> dict:
    """Use the existing cache, then bounded Yahoo chart requests; never poll orders."""
    from src.infrastructure.database.repositories.market_bars import (
        load_intraday_history_from_db, load_symbol_history_from_db,
    )
    from src.services.intraday_provider import resample_ohlcv_bars
    from src.utils.data_loader import _extract_symbol_history, download_price_history

    frames = {"1d": (pd.DataFrame(), "missing"), "5m": (pd.DataFrame(), "missing")}
    if market_engine is not None:
        daily = load_symbol_history_from_db(symbol, market_engine,
                                           end=dt.datetime.combine(session, dt.time(23, 59)), max_rows=180)
        frames["1d"] = (daily, "daily cache; freshness unverified")
        for source in ("kis", "yfinance"):
            minute = load_intraday_history_from_db(symbol, market_engine, interval="1m", source=source,
                                                   since=dt.datetime.combine(session, dt.time()))
            five = resample_ohlcv_bars(minute, "5m") if not minute.empty else pd.DataFrame()
            if five.empty:
                five = load_intraday_history_from_db(symbol, market_engine, interval="5m", source=source,
                                                     since=dt.datetime.combine(session, dt.time()))
            if snapshot_bars(five, session, "5m", now):
                frames["5m"] = (five, f"{source} cache")
                break
    for interval, period in (("1d", "6mo"), ("5m", "60d")):
        frame, _ = frames[interval]
        bars = snapshot_bars(frame, session, interval, now)
        current = bars and (interval == "5m" or bars[-1]["time"] == session.isoformat())
        close_at = dt.datetime.combine(session, nyse_regular_session_close_time(session), NY_ZONE)
        if interval == "5m" and bars:
            completed_until = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
            horizon = min(completed_until, close_at)
            current = parse_time(bars[-1]["time"]) + dt.timedelta(minutes=5) >= horizon
        if interval == "1d" and now >= close_at:
            current = False
        if not allow_network or current or (now.astimezone(NY_ZONE).date() - session).days > (60 if interval == "5m" else 180):
            continue
        try:
            history = download_price_history(
                [symbol], period=period, interval=interval, max_symbols=1,
                threads=False, batch_sleep=0, max_retries=0,
                fallback_to_single=False, chart_fallback=False, timeout_seconds=8,
            )
            downloaded = _extract_symbol_history(history, symbol)
            if snapshot_bars(downloaded, session, interval, now):
                frames[interval] = (downloaded, "yfinance; chart evidence only")
        except Exception:
            logger.exception("Journal chart download failed for %s %s", symbol, interval)
    return frames


def sync_trade_journal(engine, *, store: TradeJournalStore | None = None,
                       market_loader: Callable | None = None, market_engine=None,
                       now: dt.datetime | None = None, force_charts: bool = False,
                       context_session: str | None = None,
                       interrupted: Callable[[], bool] = lambda: False) -> dict:
    store = store or TradeJournalStore()
    now = now or dt.datetime.now(dt.timezone.utc)
    events, errors, changed = {}, [], 0
    existing = {row["journal_id"]: row for row in store.list_trades()}
    changed_ids = set()
    for order in list_execution_orders(engine) if engine is not None else []:
        if interrupted():
            break
        try:
            facts = fill_facts(order, now)
            if facts is None:
                continue
            session = dt.date.fromisoformat(facts["session_date"])
            plan = {"provenance": "sell order; see entry trade", "fields": {}}
            if order.side.value == "BUY" and facts["journal_id"] not in existing:
                try:
                    if session not in events:
                        events[session] = load_trade_journal_events(engine, session)
                    plan = select_entry_plan(order, events[session])
                except Exception as exc:
                    plan = {"provenance": "missing; plan history could not be loaded", "fields": {}}
                    errors.append(scrub_sensitive_text(str(exc), account_no=order.account_no))
            updated = store.record_fill(facts, plan, now)
            changed += int(updated)
            if updated:
                changed_ids.add(facts["journal_id"])
        except Exception as exc:
            logger.exception("Could not archive a journal fill")
            errors.append(scrub_sensitive_text(str(exc), account_no=order.account_no))
    loader = market_loader or (lambda symbol, session, stamp: load_journal_market_data(
        symbol, session, stamp, market_engine,
        str(get_env_value("TRADE_JOURNAL_CHART_NETWORK_ENABLED", "true")).lower() in {"true", "1", "yes"},
    ))
    chart_cache = {}
    for trade in store.list_trades():
        if interrupted():
            break
        try:
            session = dt.date.fromisoformat(trade["session_date"])
            close_at = dt.datetime.combine(session, nyse_regular_session_close_time(session), NY_ZONE)
            stages = ["first_observed"] + (["end_of_day"] if now >= close_at + dt.timedelta(minutes=15) else [])
            captured = {(row["stage"], row["interval"]) for row in store.snapshots(trade["journal_id"])}
            materialize = trade["journal_id"] in changed_ids or not (store.trade_directory(trade) / "trade.json").exists()
            if materialize:
                store.materialize(trade)
                materialize = False
            pending = [(stage, interval) for stage in stages for interval in ("1d", "5m") if (stage, interval) not in captured]
            attempted = parse_time(trade.get("last_chart_attempt_at"))
            if pending and (force_charts or not attempted or now - attempted >= dt.timedelta(minutes=15)):
                with store.connect() as conn:
                    conn.execute("UPDATE trades SET last_chart_attempt_at=? WHERE journal_id=?", (now.isoformat(), trade["journal_id"]))
                key = (trade["symbol"], session)
                if key not in chart_cache:
                    chart_cache[key] = loader(trade["symbol"], session, now)
                frames = chart_cache[key]
                for stage, interval in pending:
                    frame, source = frames.get(interval, (pd.DataFrame(), "missing"))
                    bars = snapshot_bars(frame, session, interval, now)
                    if not bars or (stage == "end_of_day" and interval == "1d" and bars[-1]["time"] != session.isoformat()):
                        continue
                    if stage == "end_of_day" and interval == "5m":
                        last_end = parse_time(bars[-1]["time"]) + dt.timedelta(minutes=5)
                        if last_end < close_at:
                            continue
                    first_seen = parse_time(trade["first_observed_at"])
                    fill_seen = parse_time(trade["first_fill_observed_at"])
                    recovered = (not fill_seen or now - fill_seen > dt.timedelta(minutes=10)
                                 or not first_seen or now - first_seen > dt.timedelta(minutes=10))
                    basis = "end-of-day review" if stage == "end_of_day" else (
                        "recovered view; not evidence of what was visible at entry" if recovered
                        else "chart captured when fill was observed; not exact execution")
                    store.save_snapshot(trade, {
                        "stage": stage, "interval": interval, "source": source,
                        "captured_at": now.isoformat(), "capture_basis": basis,
                        "fill_price_at_capture": trade["average_fill_price"], "bars": bars,
                        "bars_sha256": hashlib.sha256(_json(bars).encode()).hexdigest(),
                        "coverage_note": "Available bars only; gaps are not filled. Daily current-session candle may be partial.",
                    })
                    materialize = True
            if materialize:
                store.materialize(trade)
        except Exception as exc:
            logger.exception("Journal chart/archive update failed")
            errors.append(scrub_sensitive_text(str(exc)))
    if not interrupted():
        try:
            from src.services.journal_context import sync_journal_market_day
            from src.utils.market_calendar import previous_nyse_trading_day

            local = now.astimezone(NY_ZONE)
            close = dt.datetime.combine(local.date(), nyse_regular_session_close_time(local.date()), NY_ZONE)
            latest = previous_nyse_trading_day(local.date() if local >= close + dt.timedelta(minutes=15)
                                               else local.date() - dt.timedelta(days=1)).isoformat()
            for session in dict.fromkeys([latest, context_session or latest]):
                if interrupted():
                    break
                sync_journal_market_day(store, session, now=now, force=force_charts)
        except Exception as exc:
            logger.exception("Journal market context could not be collected")
            errors.append(scrub_sensitive_text(str(exc)))
    trades = store.list_trades()
    return {"trades": trades, "changed": changed, "errors": errors,
            "gross_results": estimated_gross_results(trades), "root": str(store.root)}



def export_journal_html(store: TradeJournalStore, destination: Path,
                        session_date: str | None = None) -> Path:
    """Standalone printable export with embedded charts; works offline."""
    trades = [row for row in store.list_trades() if not session_date or row["session_date"] == session_date]
    results = estimated_gross_results(store.list_trades())
    body = ["<!doctype html><html><head><meta charset='utf-8'><title>My trading journey</title>",
            "<style>body{font:16px Arial;max-width:1100px;margin:32px auto;color:#182235}article{break-before:page}article:first-of-type{break-before:auto}svg{width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}h1,h2,h3{break-after:avoid}@media print{body{margin:0}svg{max-height:160mm}pre{font-size:11pt}}</style></head><body>",
            "<h1>My trading journey</h1><p>Private journal. All archived buys and sells are included in the selected period; missing history remains unknown.</p>",
            f"<p>{html.escape(RESULT_NOTE)}</p>"]
    for trade in trades:
        estimate = results.get(trade["journal_id"])
        body.append(f"<article><h2>{html.escape(trade['session_date'])} — {html.escape(trade['symbol'])} {trade['side']}</h2>")
        body.append(f"<p>{trade['quantity']} shares at cumulative average ${trade['average_fill_price']:.4f}; status {html.escape(trade['status'])}.</p>")
        body.append(f"<p>First fill observed: {html.escape(trade['first_fill_observed_at'])}. Exact broker execution time unavailable.</p>")
        if trade["side"] == "SELL":
            body.append(f"<p>Estimated gross realized P/L: {'unknown (missing cost basis)' if estimate is None else f'${estimate:+.2f}'}</p>")
        body.append(f"<h3>Recorded entry plan</h3><pre>{html.escape(_json(trade['entry_plan']))}</pre>")
        for key in NOTE_FIELDS:
            body.append(f"<h3>{key.replace('_', ' ').title()}</h3><pre>{html.escape(trade.get('notes', {}).get(key) or '[Not yet written]')}</pre>")
        body.append(f"<p>Notes last edited: {html.escape(str(trade.get('notes_updated_at') or 'not written'))}</p>")
        snapshots = store.snapshots(trade["journal_id"])
        for snapshot in snapshots:
            body.append(f"<h3>{snapshot['stage'].replace('_', ' ').title()} — {snapshot['interval']}</h3><p>{html.escape(snapshot['capture_basis'])}; source: {html.escape(snapshot['source'])}; captured {html.escape(snapshot['captured_at'])}. {html.escape(snapshot['coverage_note'])}</p>")
            body.append(render_chart_svg(snapshot["bars"], title=f"{trade['symbol']} {trade['session_date']} {snapshot['interval']}",
                                         subtitle=snapshot["capture_basis"], plan=trade["entry_plan"],
                                         fill_price=snapshot["fill_price_at_capture"], interval=snapshot["interval"],
                                         marker_time=trade["first_fill_observed_at"]))
        if not snapshots:
            body.append("<p>Charts unavailable. Fill evidence is preserved; chart capture will retry.</p>")
        body.append("</article>")
    body.append("</body></html>")
    _atomic_text(Path(destination), "".join(body))
    return Path(destination)
