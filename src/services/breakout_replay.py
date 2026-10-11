"""Scan, replay, and persist a historical learning database."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from io import BytesIO
import json
from pathlib import Path
import threading
import uuid
import time
from zoneinfo import ZoneInfo

import yfinance as yf
import pandas as pd

from PIL import Image
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from src.core.breakout_replay import (REPLAY_CLOSURES, STUDY_START, clean_json, daily_chart, metrics_as_of,
    next_session, normalize_daily, opening_moves, orb_check, rank_and_scan, regular_session, scanner_rules, scanner_setups)
from src.infrastructure.database.repositories.breakout_replay import ReplayRepository, encode, now
from src.services.replay_intraday import ReplayIntradayProvider
from src.services.replay_paper import ReplayPaperService
from src.services.replay_daily import ReplayDailyCache
from src.utils.config import DATA_DIR, ROOT_DIR, get_env_value, resolve_repo_path
from src.utils.data_loader import _extract_symbol_history
from src.utils.market_calendar import expected_latest_market_data_date, is_nyse_trading_day
from src.infrastructure.database.repositories.market_bars import save_universe_history_batch_to_db
from src.infrastructure.database.sql_helpers import _execute_bulk_upsert
from src.core.replay_precompute import rules_key


class ReplayService:
    def __init__(self, repository: ReplayRepository, snapshot_dir: Path | None = None, provider=None):
        self.repo = repository
        self.provider = provider or ReplayIntradayProvider()
        self.snapshot_dir = snapshot_dir or resolve_repo_path(get_env_value("REPLAY_SNAPSHOT_DIR", "data/breakout_replay/snapshots"))
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="breakout-replay")
        self.lock = threading.RLock()
        self._intraday_locks = {}
        self.daily_cache = ReplayDailyCache(self.repo)
        self.paper = ReplayPaperService(self.repo, self.load_intraday_archive, self.lock)
        self._bounds_cache = None
        self._bounds_cached_at = 0.0

    def settings(self):
        path = DATA_DIR / "settings.json"
        settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        return settings.get("orb_settings", {}), float(settings.get("orb_buffer_percent", 0.1))

    def status(self):
        published = self.repo.prepared.job("published")
        if self._bounds_cache is None:
            self._bounds_cache = published["bounds"] if published else self.repo.bounds()
            self._bounds_cached_at = time.monotonic()
        elif time.monotonic() - self._bounds_cached_at >= 60:
            try:
                self._bounds_cache.update(self.repo.date_bounds())
            except SQLAlchemyError:
                pass  # Preserve useful metadata while a bulk import is busy.
            self._bounds_cached_at = time.monotonic()
        bounds = self._bounds_cache
        settings, buffer = self.settings()
        return clean_json({"database": "MySQL" if self.repo.engine.dialect.name == "mysql" else "Test database",
            "bounds": bounds, "study_start": STUDY_START, "rules": scanner_rules(DATA_DIR / "scanner_setups.json", "Setup 2"),
            "default_setup": "Setup 2", "preparation": self.repo.prepared.job("progress"),
            "prepared_through": published.get("end_day") if published else None,
            "scanner_setups": {name: {"rules": setup["rules"]}
                for name, setup in scanner_setups(DATA_DIR / "scanner_setups.json").items()},
            "providers": self.provider.configuration(), "snapshot_dir": str(self.snapshot_dir),
            "alpaca_plan_rpm": 200, "alpaca_client_rpm": getattr(getattr(self.provider, "alpaca_limiter", None), "limit", 180),
            "latest_completed_session": expected_latest_market_data_date(),
            "orb_settings": settings, "orb_buffer_pct": buffer,
            "scans": self.repo.rows(select(self.repo.scans.c.id, self.repo.scans.c.requested_day,
                self.repo.scans.c.scan_day, self.repo.scans.c.setup_name, self.repo.scans.c.status, self.repo.scans.c.created_at)
                .order_by(self.repo.scans.c.created_at.desc()).limit(30))})

    def start_scan(self, requested: dt.date, setup_name: str = "Setup 2"):
        with self.lock:
            return self._start_scan(requested, setup_name)

    def _start_scan(self, requested: dt.date, setup_name: str = "Setup 2"):
        rules = scanner_rules(DATA_DIR / "scanner_setups.json", setup_name)
        if requested < STUDY_START:
            raise ValueError(f"Choose a scan date on or after {STUDY_START}")
        day = requested
        while not is_nyse_trading_day(day) or day in REPLAY_CLOSURES:
            day += dt.timedelta(days=1)
        if day > expected_latest_market_data_date():
            raise ValueError("Select a completed New York trading session")
        prepared = self.repo.prepared.scan(day, setup_name, rules_key(rules))
        if prepared:
            scan_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"breakout-study:{requested}:{day}:{setup_name}:{rules_key(rules)}:{prepared['source_key']}"))
            existing = self.repo.rows(select(self.repo.scans.c.id).where(self.repo.scans.c.id == scan_id,
                self.repo.scans.c.status == "complete"))
            if not existing:
                payload = {"processed": prepared["total"], "total": prepared["total"], "eligible": prepared["eligible"],
                    "matches": len(prepared["results"]), "rules": rules, "prepared": True,
                    "message": "Loaded the prepared historical scan from local storage."}
                with self.repo.engine.begin() as conn:
                    _execute_bulk_upsert(conn, self.repo.scans, [{"id": scan_id, "requested_day": requested,
                        "scan_day": day, "setup_name": setup_name, "status": "complete", "created_at": now(),
                        "payload": encode(payload)}], ("id",), self.repo.engine.dialect.name)
                    _execute_bulk_upsert(conn, self.repo.results, [{"scan_id": scan_id, "symbol": row["symbol"],
                        "metrics": encode(row)} for row in prepared["results"]], ("scan_id", "symbol"), self.repo.engine.dialect.name)
            return {"id": scan_id, "prepared": True}
        preparation = self.repo.prepared.job("progress") if setup_name == "Setup 2" else None
        if preparation:
            raise ValueError("This scan date is still being prepared in the background. The preparation status shows when saved dates are ready.")
        running = self.repo.rows(select(self.repo.scans).where(self.repo.scans.c.status == "running"))
        if running:
            if running[0]["requested_day"] == requested and running[0]["setup_name"] == setup_name:
                return {"id": running[0]["id"]}
            raise ValueError("A scan is already running. Wait for its historical import and scan to finish.")
        scan_id = str(uuid.uuid4())
        self.repo.upsert(self.repo.scans, [{"id": scan_id, "requested_day": requested, "scan_day": day,
            "setup_name": setup_name,
            "status": "running", "created_at": now(), "payload": encode({"processed": 0, "total": 0, "rules": rules, "message": "Loading daily cache"})}], ("id",))
        self.pool.submit(self._scan, scan_id, day, rules)
        return {"id": scan_id}

    def _scan(self, scan_id, day, rules):
        try:
            scanner_label = self.repo.get_scan(scan_id)["setup_name"].replace("Setup", "Scanner")
            self._prepare_daily(scan_id, day, rules)
            # Resolve exceptional closures from SPY bars, within a short window.
            spy_dates = self.repo.daily_dates("SPY", day)
            if not spy_dates or (spy_dates[0] - day).days > 7:
                raise ValueError("Historical SPY data for the selected session is unavailable")
            day = spy_dates[0]
            self.repo.update_scan(scan_id, scan_day=day)
            symbols = sorted(set(self.repo.symbols()) - {"SPY"})
            # Every locally cached symbol, not a present-day market-cap subset.
            spy = self.repo.histories(["SPY"], day - dt.timedelta(days=520), day).get("SPY")
            if spy is None:
                raise ValueError("SPY history is missing")
            metrics = []
            for offset in range(0, len(symbols), 150):
                batch = symbols[offset:offset + 150]
                histories = self.repo.histories(batch, day - dt.timedelta(days=520), day)
                for symbol in batch:
                    history = histories.get(symbol)
                    if history is not None:
                        result = metrics_as_of(symbol, history, spy, day)
                        if result:
                            metrics.append(result)
                self.repo.update_scan(scan_id, payload={"processed": min(offset + len(batch), len(symbols)),
                    "total": len(symbols), "eligible": len(metrics), "rules": rules, "message": f"Computing historical {scanner_label} metrics"})
            results = rank_and_scan(metrics, rules)
            self.repo.upsert(self.repo.results, [{"scan_id": scan_id, "symbol": row["symbol"], "metrics": encode(row)} for row in results], ("scan_id", "symbol"))
            self.repo.update_scan(scan_id, status="complete", payload={"processed": len(symbols), "total": len(symbols),
                "eligible": len(metrics), "missing": len(symbols) - len(metrics), "matches": len(results), "rules": rules,
                "message": f"{scanner_label} complete. Ranking uses only symbols with a bar on the scan session.",
                "universe_note": "All symbols in the local daily cache. Historical delisted stocks absent from the cache cannot be included."})
            self.repo.event("scan_complete", {"id": scan_id, "matches": len(results)}, day=day)
        except Exception:
            self.repo.update_scan(scan_id, status="failed", payload={"message": "Scan failed while reading/calculating cached bars. Check the local database and retry."})

    @staticmethod
    def _daily_fallback_ranges(day, latest, bounds, imports, coverage):
        start = min(dt.date(2024, 1, 1), day - dt.timedelta(days=365))
        missing = []
        for symbol in ["SPY", *sorted(set(bounds) - {"SPY"})]:
            record, manifest = bounds.get(symbol), imports.get(symbol)
            local = coverage.get(symbol, {})
            enough_history = local.get("count", 0) >= 65
            if enough_history and local["last"].date() == day:
                continue
            known_history = bool(record and manifest and manifest["status"] == "complete" and manifest["start_day"] <= start)
            if known_history and manifest["end_day"] >= day:
                continue
            if not record:
                missing.append((symbol, start, latest))
                continue
            first, last = record["first"].date(), record["last"].date()
            needs_warmup = not (enough_history or known_history or first <= start + dt.timedelta(days=3)) and first > start
            if needs_warmup:
                missing.append((symbol, start, min(latest, first - dt.timedelta(days=1))))
            if last < day or (needs_warmup and last < latest):
                covered_end = max(last, manifest["end_day"]) if known_history else last
                missing.append((symbol, max(start, covered_end + dt.timedelta(days=1)), latest))
            elif first <= day and local.get("last", dt.datetime.min).date() != day:
                missing.append((symbol, day, day))
        return [(symbol, first, last) for symbol, first, last in missing if first <= last]

    def _prepare_daily(self, scan_id, day, rules):
        start = min(dt.date(2024, 1, 1), day - dt.timedelta(days=365))
        latest = expected_latest_market_data_date()
        while not is_nyse_trading_day(day) or day in REPLAY_CLOSURES:
            day += dt.timedelta(days=1)
        bounds = self.repo.symbol_bounds()
        self._bounds_cache = {"first": min((r["first"] for r in bounds.values()), default=None),
            "last": max((r["last"] for r in bounds.values()), default=None), "symbols": len(bounds)}
        self._bounds_cached_at = time.monotonic()
        imports = {r["symbol"]: r for r in self.repo.rows(select(self.repo.daily_imports))}
        known = {symbol for symbol in bounds if symbol in imports and imports[symbol]["status"] == "complete"
            and imports[symbol]["start_day"] <= start and imports[symbol]["end_day"] >= day}
        coverage = self.repo.daily_coverage(start, day, sorted((set(bounds) | {"SPY"}) - known))
        missing = self._daily_fallback_ranges(day, latest, bounds, imports, coverage)
        retry_after = now() - dt.timedelta(hours=6)
        negative = self.repo.rows(select(self.repo.daily_requests).where(
            self.repo.daily_requests.c.status != "complete", self.repo.daily_requests.c.updated_at > retry_after))
        negative.extend(row for row in imports.values() if row["status"] == "unavailable" and row["updated_at"] > retry_after)
        negative_by_symbol = {}
        for row in negative:
            negative_by_symbol.setdefault(row["symbol"], []).append(row)
        missing = [(symbol, first, last) for symbol, first, last in missing if not any(
            row["start_day"] <= first and row["end_day"] >= last for row in negative_by_symbol.get(symbol, []))]
        if not missing:
            return
        ranges = {}
        for symbol, fetch_start, fetch_end in missing:
            ranges.setdefault((fetch_start, fetch_end), []).append(symbol)
        jobs = [(symbols[offset:offset+40], fetch_start, fetch_end) for (fetch_start, fetch_end), symbols in ranges.items()
            for offset in range(0, len(symbols), 40)]
        imported = failed = 0
        processed = 0
        for batch, fetch_start, fetch_end in jobs:
            self.repo.update_scan(scan_id, payload={"phase": "backfill", "processed": processed, "total": len(missing),
                "rules": rules, "imported": imported, "failed": failed,
                "message": f"Yahoo fallback · missing local dates {fetch_start}–{fetch_end}"})
            # The production daily cache is already a yfinance pipeline. Use its
            # adjusted-OHLC contract and existing bulk persistence helper.
            try:
                frame = yf.download(batch, start=fetch_start.isoformat(), end=(fetch_end+dt.timedelta(days=1)).isoformat(),
                    interval="1d", group_by="ticker", auto_adjust=True, progress=False, threads=8, timeout=15)
                histories = {}
                for symbol in batch:
                    data = _extract_symbol_history(frame, symbol) if not frame.empty else None
                    if data is not None and not data.empty:
                        data = normalize_daily(data).loc[pd.Timestamp(fetch_start):pd.Timestamp(fetch_end)]
                        if not data.empty:
                            histories[symbol] = data
                if histories and not save_universe_history_batch_to_db(pd.concat(histories, axis=1), list(histories), self.repo.engine):
                    raise ValueError("Daily fallback could not be persisted")
                records = []
                requests = []
                for symbol in batch:
                    data = histories.get(symbol)
                    usable = data is not None
                    imported += int(usable)
                    failed += int(not usable)
                    requests.append({"symbol": symbol, "start_day": fetch_start, "end_day": fetch_end,
                        "status": "complete" if usable else "empty", "row_count": len(data) if usable else 0, "updated_at": now()})
                    prior = imports.get(symbol)
                    if not usable and prior and prior["status"] == "complete":
                        continue
                    first, last = fetch_start, fetch_end
                    row_count = len(data) if usable else 0
                    if usable and prior and prior["status"] == "complete" and first <= prior["end_day"] + dt.timedelta(days=1) and last >= prior["start_day"] - dt.timedelta(days=1):
                        first, last = min(first, prior["start_day"]), max(last, prior["end_day"])
                        row_count += prior["row_count"]
                    record = {"symbol": symbol, "start_day": first, "end_day": last,
                        "status": "complete" if usable else "unavailable", "row_count": row_count, "updated_at": now()}
                    records.append(record)
                    imports[symbol] = record
                if records:
                    self.repo.upsert(self.repo.daily_imports, records, ("symbol",))
                self.repo.upsert(self.repo.daily_requests, requests, ("symbol", "start_day", "end_day"))
            except Exception:
                failed += len(batch)
                self.repo.upsert(self.repo.daily_requests, [{"symbol": symbol, "start_day": fetch_start,
                    "end_day": fetch_end, "status": "failed", "row_count": 0, "updated_at": now()} for symbol in batch],
                    ("symbol", "start_day", "end_day"))
            processed += len(batch)
        self.repo.event("daily_backfill", {"symbols": len(missing), "imported": imported, "unavailable": failed}, day=day)
        self.daily_cache.clear()

    def scan_results(self, scan_id):
        scan = self.repo.get_scan(scan_id)
        results = self.repo.rows(select(self.repo.results).where(self.repo.results.c.scan_id == scan_id))
        if results:
            symbols = [r["symbol"] for r in results]
            day = scan["scan_day"]
            daily, reviews = self.repo.daily, self.repo.reviews
            prepared = self.repo.prepared
            calendars = prepared.calendars(symbols)
            review_rows = self.repo.rows(select(reviews).where(reviews.c.symbol.in_(symbols), reviews.c.day >= day))
            review_days = {}
            for row in review_rows:
                review_days.setdefault(row["symbol"], set()).add(row["day"].isoformat())
            coverage = {}
            for symbol, calendar in calendars.items():
                dates = {stamp for stamp in calendar if stamp >= day.isoformat()}
                coverage[symbol] = {"symbol": symbol, "total": len(dates),
                    "reviewed": len(dates & review_days.get(symbol, set()))}
            missing = set(symbols) - set(coverage)
            if missing:
                statement = select(daily.c.symbol, func.count().label("total"), func.count(reviews.c.day).label("reviewed")) \
                    .select_from(daily.outerjoin(reviews, (daily.c.symbol == reviews.c.symbol) & (func.date(daily.c.date) == reviews.c.day))) \
                    .where(daily.c.symbol.in_(missing), daily.c.interval == "1d", daily.c.date >= dt.datetime.combine(day, dt.time.min)) \
                    .group_by(daily.c.symbol)
                statement = statement.with_hint(daily, "USE INDEX (ix_price_history_symbol_interval_date)", dialect_name="mysql")
                coverage.update({row["symbol"]: row for row in self.repo.rows(statement)})
            reviewed = {r["symbol"]: r for r in review_rows if r["day"] == day}
            for row in results:
                row["metrics"] = json.loads(row["metrics"])
                row["coverage"] = coverage.get(row["symbol"], {"total": 0, "reviewed": 0})
                review = reviewed.get(row["symbol"])
                row["reviewed"] = bool(review)
                row["decision"] = review["decision"] if review else None
                row["outcome"] = review["outcome"] if review else None
                row["complete"] = bool(row["coverage"]["total"] and row["coverage"]["reviewed"] == row["coverage"]["total"])
            results.sort(key=lambda row: row["metrics"].get("return_1m") or 0, reverse=True)
        return clean_json({"scan": scan, "results": results})

    def open_session(self, scan_id, symbol, mode="backtesting"):
        if mode not in ("backtesting", "historical"):
            raise ValueError("Choose Backtesting or Historical study mode")
        scan = self.repo.get_scan(scan_id)
        matches = self.repo.rows(select(self.repo.results).where(self.repo.results.c.scan_id == scan_id, self.repo.results.c.symbol == symbol))
        if scan["status"] != "complete" or not matches:
            raise ValueError("Select a stock from a completed scan")
        archive = self.daily_cache.get({"symbol": symbol, "start_day": scan["scan_day"]}, refresh=True)
        dates = archive.dates
        if not dates:
            raise ValueError("Daily replay history is not available for this scan date")
        reviewed = {r["day"] for r in self.repo.review_rows(symbol)}
        cursor = next((day for day in dates if day not in reviewed), dates[-1])
        session_id = str(uuid.uuid4())
        self.repo.upsert(self.repo.sessions, [{"id": session_id, "scan_id": scan_id, "symbol": symbol,
            "start_day": scan["scan_day"], "cursor": cursor, "created_at": now()}], ("id",))
        self.repo.set_cursor(session_id, symbol, cursor)
        self.repo.event("replay_opened", {"session_id": session_id, "scan_id": scan_id}, symbol, cursor)
        archive.warm(cursor)
        return self.set_mode(session_id, mode)

    def set_mode(self, session_id, mode):
        if mode not in ("backtesting", "historical"):
            raise ValueError("Choose Backtesting or Historical study mode")
        with self.lock:
            session = self.repo.get_session(session_id)
            self.repo.upsert(self.repo.session_views, [{"session_id": session_id, "mode": mode,
                "updated_at": now()}], ("session_id",))
            if mode == "historical":
                self.repo.mark_revealed(session["symbol"], self.daily_cache.get(session).dates[-1])
            self.repo.event("replay_mode_changed", {"session_id": session_id, "mode": mode},
                session["symbol"], session["cursor"])
            return self.session_payload(session_id)

    def select_day(self, session_id, day):
        with self.lock:
            session = self.repo.get_session(session_id)
            if session["mode"] != "historical":
                raise ValueError("Use Historical study mode to select any daily session")
            if day not in self.daily_cache.get(session).dates:
                raise ValueError("Choose a daily session from this scan's available history")
            self.repo.set_cursor(session_id, session["symbol"], day)
            self.repo.event("study_day_selected", {"session_id": session_id}, session["symbol"], day)
            return self.session_payload(session_id)

    def session_payload(self, session_id):
        session = self.repo.get_session(session_id)
        symbol, day = session["symbol"], session["cursor"]
        archive = self.daily_cache.get(session)
        historical = session["mode"] == "historical"
        chart_day = archive.dates[-1] if historical else day
        metrics = archive.metrics_at(day)
        reviews = self.repo.review_rows(symbol, None if historical else day)
        dates = archive.dates
        all_reviews = self.repo.review_rows(symbol)
        reviewed_dates = {r["day"] for r in all_reviews}
        prior = self.repo.rows(select(self.repo.progress).where(self.repo.progress.c.symbol == symbol))
        current_review = next((r for r in reviews if r["day"] == day), None)
        preview_day = current_review["expected_day"] if historical and current_review and current_review["decision"] == "expected" and current_review["expected_day"] in dates and current_review["expected_day"] > day else None
        annotation_cutoff = chart_day
        drawings = self.repo.rows(select(self.repo.drawings).where(self.repo.drawings.c.symbol == symbol, self.repo.drawings.c.day <= annotation_cutoff).order_by(self.repo.drawings.c.created_at))
        for drawing in drawings:
            drawing["payload"] = json.loads(drawing["payload"])
        drawings = [d for d in drawings if historical or d["day"] <= day]
        snapshots = self.repo.rows(select(self.repo.snapshots.c.id, self.repo.snapshots.c.day, self.repo.snapshots.c.frame,
            self.repo.snapshots.c.path).where(self.repo.snapshots.c.symbol == symbol,
                self.repo.snapshots.c.replay_asof <= chart_day))
        paper = self.paper.reconcile(symbol, day, cached_only=True)
        if historical:
            saved_fills = [json.loads(row["payload"]) for row in self.repo.rows(select(self.repo.paper_fills)
                .where(self.repo.paper_fills.c.symbol == symbol, self.repo.paper_fills.c.day <= chart_day))]
            paper["chart_fills"] = sorted({fill["id"]: fill for fill in [*saved_fills, *paper["chart_fills"]]}.values(),
                key=lambda fill: fill["time"])
        return clean_json({"session": session, **archive.visible(chart_day, full_history=historical), "metrics": metrics,
            "reviews": reviews, "drawings": drawings, "snapshots": snapshots,
            "can_predict": not historical and (not prior or prior[0]["max_revealed"] <= day),
            "next_expected_day": next_session(day), "entry_preview_day": preview_day, "index": dates.index(day), "total": len(dates),
            "reviewed_count": sum(d in reviewed_dates for d in dates),
            "has_previous": day != dates[0], "has_next": day != dates[-1],
            "latest_day": dates[-1], "current_review": current_review,
            "scan_review": next((r for r in all_reviews if r["day"] == session["start_day"]), None),
            "daily_preload": {"ahead": len(dates) - dates.index(day) - 1, "through": dates[-1]},
            "intraday_days": [stamp for stamp in archive.days if historical or stamp <= day],
            "daily_days": dates, "chart_through": chart_day,
            "projection_days": self.projection_days(chart_day),
            "paper": paper})

    @staticmethod
    def projection_days(day):
        dates = []
        for _ in range(120):
            day = next_session(day)
            dates.append(day.isoformat())
        return dates

    def save_review(self, session_id, decision, pattern="", notes="", outcome=None, return_payload=True, day=None, expected_day=None):
        with self.lock:
            payload = self.session_payload(session_id)
            session = payload["session"]
            cursor = dt.date.fromisoformat(session["cursor"])
            day = day or cursor
            archive = self.daily_cache.get(self.repo.get_session(session_id))
            cutoff = archive.dates[-1] if session["mode"] == "historical" else cursor
            if day not in archive.days or day > cutoff:
                raise ValueError("Choose a revealed trading session for the observation")
            symbol = session["symbol"]
            prior = next((r for r in self.repo.review_rows(symbol) if r["day"] == day), None)
            if decision not in ("pass", "expected") or outcome not in (None, "actual", "failed"):
                raise ValueError("Unknown review decision or outcome")
            if decision == "expected":
                expected_day = expected_day or ((prior or {}).get("expected_day") if (prior or {}).get("decision") == "expected" else None) or next_session(day)
                if expected_day not in archive.days and expected_day <= archive.days[-1]:
                    raise ValueError("Choose an available trading session as the expected breakout day")
            progress = self.repo.rows(select(self.repo.progress).where(self.repo.progress.c.symbol == symbol))
            asof = progress[0]["max_revealed"] if progress else day
            blind = session["mode"] == "backtesting" and asof <= day and (decision != "expected" or expected_day > asof)
            changed = not prior or any((prior.get(k) or "") != (v or "") for k, v in
                {"decision": decision, "notes": notes, "pattern": pattern, "expected_day": expected_day if decision == "expected" else None}.items())
            metrics = dict(prior["metrics"]) if prior and not changed else {**archive.metrics_at(day),
                "observation_asof": asof, "blind_observation": blind, "observation_mode": session["mode"]}
            if changed and not blind:
                metrics.update(retrospective_revision=True, revision_asof=asof,
                    original_decision=(prior or {}).get("decision"), original_observation_asof=(prior or {}).get("metrics", {}).get("observation_asof"))
            if changed and prior:
                self.repo.event("review_revised", {"before": prior, "decision": decision,
                    "expected_day": expected_day, "pattern": pattern, "notes": notes, "retrospective": not blind,
                    "replay_asof": asof}, symbol, day)
            outcome_asof = asof if outcome is not None else (prior or {}).get("outcome_asof")
            outcome = outcome if outcome is not None else (prior or {}).get("outcome")
            self.repo.save_review(symbol, day, {"decision": decision,
                "expected_day": expected_day if decision == "expected" else None,
                "outcome": outcome, "outcome_asof": outcome_asof, "pattern": pattern[:40], "notes": notes[:3000],
                "metrics": encode(metrics)})
            return self.session_payload(session_id) if return_payload else None

    def mark_expected_day(self, session_id, day):
        session = self.repo.get_session(session_id)
        if day not in self.daily_cache.get(session).days:
            raise ValueError("Choose an available trading session")
        observation_day = day if session["mode"] == "historical" else min(day, session["cursor"])
        prior = next((r for r in self.repo.review_rows(session["symbol"]) if r["day"] == observation_day), {})
        return self.save_review(session_id, "expected", prior.get("pattern") or "", prior.get("notes") or "",
            day=observation_day, expected_day=day)

    def step(self, session_id, direction):
        with self.lock:
            session = self.repo.get_session(session_id)
            dates = self.daily_cache.get(session).dates
            index = dates.index(session["cursor"])
            if direction not in (-1, 1):
                raise ValueError("Step must be left or right")
            if session["mode"] == "backtesting" and direction == 1 and not any(r["day"] == session["cursor"] for r in self.repo.review_rows(session["symbol"], session["cursor"])):
                self.save_review(session_id, "pass", return_payload=False)
            target = max(0, min(len(dates) - 1, index + direction))
            self.repo.set_cursor(session_id, session["symbol"], dates[target])
            self.repo.event("replay_step", {"direction": direction, "session_id": session_id}, session["symbol"], dates[target])
            return self.session_payload(session_id)

    def _intraday_access(self, session, day):
        if day < STUDY_START or day not in self.daily_cache.get(session).days:
            raise ValueError(f"Choose an available completed trading session on or after {STUDY_START}")
        if day <= session["cursor"]:
            return False
        if session["mode"] == "backtesting":
            raise ValueError("Future intraday sessions are hidden in Backtesting mode. Advance the replay or switch to Historical study.")
        return False

    def load_intraday_archive(self, symbol, day, source="auto"):
        with self.lock:
            fetch_lock = self._intraday_locks.setdefault((symbol, day), threading.Lock())
        with fetch_lock:
            cached_source = self.repo.cached_intraday_source(symbol, day, source)
            cached = cached_source is not None
            if cached:
                source = cached_source
                frame = self.repo.intraday(symbol, day, source)
            else:
                source = self.provider.resolve(source)
                frame, source = self.provider.fetch(symbol, day, source)
                self.repo.archive_intraday(symbol, frame, source)
        return frame, source, cached

    def intraday(self, session_id, day, source="auto", visible_bars=100, interval_minutes=5, study_id=None):
        if interval_minutes not in (5, 30):
            raise ValueError("Choose 5- or 30-minute intraday candles")
        session = self.repo.get_session(session_id)
        preview = self._intraday_access(session, day)
        frame, source, cached = self.load_intraday_archive(session["symbol"], day, source)
        frame = regular_session(frame, day)
        total_bars = len(frame)
        if visible_bars < 1 or visible_bars > 100:
            raise ValueError("Reveal between 1 and 100 intraday candles")
        frame = frame.iloc[:visible_bars]
        archive = self.daily_cache.get(session)
        prior_end = pd.Timestamp(day - dt.timedelta(days=1))
        prior = archive.history.loc[pd.Timestamp(day - dt.timedelta(days=70)):prior_end]
        adr = None
        if prior is not None and not prior.empty:
            metrics = metrics_as_of(session["symbol"], prior, archive.spy.loc[:prior_end], prior.index[-1].date())
            adr_field = "adr_20_high_low" if session["setup_name"] == "Setup 2" else "adr_20"
            adr = metrics.get(adr_field) if metrics else None
        settings, buffer = self.settings()
        result = {"day": day, "source": source, "cached": cached, "adr": adr, "total_bars": total_bars,
            "interval_minutes": interval_minutes, "execution_minutes": 5,
            "opening_moves": opening_moves(frame, day),
            "visible_bars": len(frame), "entry_preview": preview, "daily_asof": session["cursor"],
            "mode": session["mode"],
            "hindsight": session["mode"] == "historical" or (day != session["cursor"] and not preview),
            "review": next((r for r in self.repo.review_rows(session["symbol"]) if r["day"] == day), None),
            "drawings": self.intraday_drawings(session["symbol"], day),
            "bars": [{"time": int(stamp.timestamp()), **{name.lower(): float(row[name]) for name in ("Open", "High", "Low", "Close", "Volume")}}
                for stamp, row in frame.iterrows()], "orb": orb_check(frame, day, adr, settings, buffer, interval_minutes),
            "paper": self.paper.reconcile(session["symbol"], day, source, study_id=study_id)}
        if day > session["cursor"]:
            self.repo.mark_revealed(session["symbol"], day)
        self.repo.event("intraday_opened", {"source": source, "cached": cached, "orb": result["orb"],
            "hindsight": result["hindsight"], "daily_asof": session["cursor"], "interval_minutes": interval_minutes,
            "opening_moves": result["opening_moves"]}, session["symbol"], day)
        return clean_json(result)

    def intraday_drawings(self, symbol, day):
        rows = self.repo.rows(select(self.repo.drawings).where(self.repo.drawings.c.symbol == symbol,
            self.repo.drawings.c.frame == "5m").order_by(self.repo.drawings.c.created_at))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return [row for row in rows if row["payload"].get("session_day", row["day"].isoformat()) == day.isoformat()]

    def paper_state(self, session_id, day, source="auto", time=None, study_id=None):
        session = self.repo.get_session(session_id)
        self._intraday_access(session, day)
        if time is not None:
            bars, _, _ = self.load_intraday_archive(session["symbol"], day, source)
            if time not in {int(stamp.timestamp()) for stamp in regular_session(bars, day).index}:
                raise ValueError("Choose an available five-minute candle")
        return clean_json(self.paper.reconcile(session["symbol"], day, source, time, study_id))

    def create_paper_study(self, session_id, day, request_id):
        session = self.repo.get_session(session_id)
        self._intraday_access(session, day)
        study_id = self.paper.create_study(session["symbol"], day, str(request_id))
        return {"study_id": study_id}

    def paper_trade(self, session_id, day, time, action, price, stop_pct, percent, source, request_id, interval_minutes=5, study_id=None):
        session = self.repo.get_session(session_id)
        self._intraday_access(session, day)
        if interval_minutes not in (5, 30):
            raise ValueError("Choose a 5- or 30-minute entry window")
        opening_end = pd.Timestamp(dt.datetime.combine(day, dt.time(9, 30)), tz="America/New_York") + pd.Timedelta(minutes=interval_minutes)
        if action == "buy" and time + 300 < opening_end.timestamp():
            raise ValueError(f"Wait until the {interval_minutes}-minute opening window is complete before buying")
        result = self.paper.trade(session["symbol"], day, time, action, price, stop_pct, percent, source, str(request_id), interval_minutes, study_id)
        return self.intraday(session_id, day, source, interval_minutes=interval_minutes, study_id=result["study_id"])

    def add_drawing(self, session_id, frame, points, kind, day=None, visible_bars=100, source="auto", interval_minutes=5):
        session = self.repo.get_session(session_id)
        day = day or session["cursor"]
        if frame == "5m":
            self._intraday_access(session, day)
        progress = self.repo.rows(select(self.repo.progress).where(self.repo.progress.c.symbol == session["symbol"]))
        drawing_asof = max(session["cursor"], day) if frame == "5m" else session["cursor"]
        if frame == "daily" and session["mode"] == "historical":
            drawing_asof = self.daily_cache.get(session).dates[-1]
        if frame == "5m" and progress:
            drawing_asof = max(drawing_asof, progress[0]["max_revealed"])
        if frame not in ("daily", "5m") or kind not in ("line", "horizontal", "measure") or len(points) not in (1, 2):
            raise ValueError("Invalid drawing")
        if frame == "daily" and day > session["cursor"]:
            raise ValueError("Cannot draw on unrevealed data")
        if frame == "daily":
            known = {d.isoformat() for d in self.repo.daily_dates(session["symbol"])}
            for point in points:
                if not isinstance(point["time"], str):
                    raise ValueError("Daily drawing points require dates")
                stamp = dt.date.fromisoformat(point["time"])
                if stamp <= session["cursor"] and point["time"] not in known:
                    raise ValueError("Drawing contains an unknown past session")
        else:
            if any(not isinstance(p["time"], int) or dt.datetime.fromtimestamp(p["time"], dt.timezone.utc).astimezone(ZoneInfo('America/New_York')).date() != day for p in points):
                raise ValueError("Intraday drawing must belong to its selected session")
        intraday_asof = None
        if frame == "5m":
            visible = self.intraday(session_id, day, source, visible_bars, interval_minutes)
            known = {b["time"] for b in visible["bars"]}
            if any(p["time"] not in known for p in points):
                raise ValueError("Draw only on revealed intraday candles")
            intraday_asof = visible["bars"][-1]["time"]
        drawing_id = str(uuid.uuid4())
        self.repo.upsert(self.repo.drawings, [{"id": drawing_id, "symbol": session["symbol"], "day": drawing_asof, "frame": frame,
            "payload": encode({"points": points, "kind": kind, "session_day": day, "intraday_asof": intraday_asof,
                "chart_interval_minutes": interval_minutes if frame == "5m" else None,
                "mode": session["mode"],
                "observation_asof": progress[0]["max_revealed"] if progress else session["cursor"]}), "created_at": now()}], ("id",))
        self.repo.event("drawing_added", {"id": drawing_id, "kind": kind}, session["symbol"], day)
        result = self.session_payload(session_id)
        if frame == "5m":
            result["study_drawings"] = clean_json(self.intraday_drawings(session["symbol"], day))
        return result

    def delete_drawing(self, session_id, drawing_id):
        session = self.repo.get_session(session_id)
        with self.repo.engine.begin() as conn:
            row = conn.execute(select(self.repo.drawings).where(self.repo.drawings.c.id == drawing_id,
                self.repo.drawings.c.symbol == session["symbol"])).mappings().first()
            if not row:
                raise ValueError("Drawing not found")
            cutoff = self.daily_cache.get(session).dates[-1] if session["mode"] == "historical" else session["cursor"]
            if row["frame"] == "5m":
                chart_day = dt.date.fromisoformat(json.loads(row["payload"]).get("session_day", str(row["day"])))
                self._intraday_access(session, chart_day)
                cutoff = max(cutoff, chart_day)
            if row["frame"] == "daily" and row["day"] > cutoff:
                raise ValueError("Drawing is from a later replay session")
            conn.execute(self.repo.drawings.delete().where(self.repo.drawings.c.id == drawing_id))
        self.repo.event("drawing_removed", dict(row), session["symbol"], session["cursor"])
        result = self.session_payload(session_id)
        if row["frame"] == "5m":
            result["study_drawings"] = clean_json(self.intraday_drawings(session["symbol"], chart_day))
        return result

    def save_snapshot(self, session_id, data_url, frame="daily", day=None, visible_bars=100, source="auto", interval_minutes=5, study_id=None):
        payload = self.session_payload(session_id)
        session = self.repo.get_session(session_id)
        day = day or session["cursor"]
        if (frame not in ("daily", "5m")
                or (frame == "daily" and day != session["cursor"])
                or not data_url.startswith("data:image/png;base64,")):
            raise ValueError("Invalid snapshot")
        preview = self._intraday_access(session, day) if frame == "5m" else False
        try:
            raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
            if len(raw) > 15_000_000:
                raise ValueError("Snapshot exceeds 15 MB")
            with Image.open(BytesIO(raw)) as img:
                if img.format != "PNG" or img.width > 12000 or img.height > 12000:
                    raise ValueError("Invalid PNG dimensions")
                img.verify()
        except Exception:
            raise ValueError("Send a valid PNG snapshot up to 15 MB") from None
        snapshot_id = str(uuid.uuid4())
        captured_review = payload["current_review"] if preview else clean_json(next(
            (row for row in self.repo.review_rows(session["symbol"]) if row["day"] == day), None))
        label = (captured_review or {}).get("decision", "unreviewed")
        captured_frame = f"{interval_minutes}m" if frame == "5m" else frame
        mode_label = "_historical" if session["mode"] == "historical" else ""
        relative = Path(session["symbol"]) / day.isoformat() / f"{session['symbol']}_{day}_{captured_frame}_{label}{mode_label}_{snapshot_id[:8]}.png"
        snapshot_metrics = payload["metrics"]
        intraday = None
        if frame == "5m":
            intraday = self.intraday(session_id, day, source, visible_bars, interval_minutes, study_id)
            paper = intraday["paper"]
            intraday["paper"] = {**paper, "fills": [f for f in paper["fills"] if f["day"] == day.isoformat()],
                "equity_curve": paper["equity_curve"][-1:]}
            if not preview:
                histories = self.repo.histories([session["symbol"], "SPY"], day-dt.timedelta(days=520), day)
                snapshot_metrics = metrics_as_of(session["symbol"], histories[session["symbol"]], histories["SPY"], day)
        path = self.snapshot_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        progress = self.repo.rows(select(self.repo.progress).where(self.repo.progress.c.symbol == session["symbol"]))
        observation_asof = max(day, session["cursor"], progress[0]["max_revealed"] if progress else day)
        self.repo.upsert(self.repo.snapshots, [{"id": snapshot_id, "symbol": session["symbol"], "day": day, "frame": captured_frame,
            "path": relative.as_posix(), "replay_asof": observation_asof, "metadata": encode({"metrics": snapshot_metrics,
                "review": captured_review, "replay_cursor": session["cursor"], "intraday": intraday,
                "mode": session["mode"], "chart_through": payload["chart_through"]}), "created_at": now()}], ("id",))
        self.repo.event("snapshot_saved", {"id": snapshot_id, "path": str(relative)}, session["symbol"], day)
        return {"id": snapshot_id, "path": str(path)}
