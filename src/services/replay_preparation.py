"""Prepare all historical scan dates and daily charts outside interactive requests."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time

import pandas as pd
from sqlalchemy import func, select, tuple_

from src.core.breakout_replay import REPLAY_CLOSURES, STUDY_START, scanner_rules
from src.core.replay_precompute import PRECOMPUTE_VERSION, daily_study_artifact, rank_daily_universe, rules_key
from src.utils.config import DATA_DIR
from src.utils.market_calendar import expected_latest_market_data_date


class ReplayPreparation:
    def __init__(self, repository, log=print):
        self.repo, self.cache, self.log = repository, repository.prepared, log

    def signatures(self):
        table = self.repo.daily
        symbols = self.repo.symbols()
        rows = []
        for offset in range(0, len(symbols), 40):
            bounds = select(table.c.symbol, func.min(table.c.date).label("first"),
                func.max(table.c.date).label("last"), func.count().label("count")).where(table.c.interval == "1d",
                    table.c.symbol.in_(symbols[offset:offset + 40])).group_by(table.c.symbol)
            bounds = bounds.with_hint(table, "USE INDEX (ix_price_history_symbol_interval_date)", dialect_name="mysql").subquery()
            statement = select(bounds.c.symbol, bounds.c.first, bounds.c.last, bounds.c.count,
                table.c.updated_at.label("revision")).select_from(bounds.join(table,
                    (table.c.symbol == bounds.c.symbol) & (table.c.date == bounds.c.last) & (table.c.interval == "1d")))
            rows.extend(self.repo.rows(statement))
        return {row.pop("symbol"): {key: str(value) for key, value in row.items()} for row in rows}

    def progress(self, phase, **values):
        self.cache.save_job("progress", {"phase": phase, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), **values})
        self.log(f"{phase}: " + ", ".join(f"{key}={value}" for key, value in values.items()))

    def run(self, start=STUDY_START, end=None, setup_name="Setup 2", force=False):
        end = end or expected_latest_market_data_date()
        rules = scanner_rules(DATA_DIR / "scanner_setups.json", setup_name)
        rule_key = rules_key(rules)
        self.progress("checking_daily_inputs")
        signatures = self.signatures()
        if "SPY" not in signatures:
            raise ValueError("SPY daily history is required before preparation")
        source_key = hashlib.sha256(json.dumps(signatures, sort_keys=True).encode()).hexdigest()
        inputs_key = source_key
        previous = self.cache.job("published") or {}
        if not force and previous.get("inputs_key", previous.get("source_key")) == inputs_key and previous.get("rules_key") == rule_key \
                and previous.get("version") == PRECOMPUTE_VERSION and previous.get("start_day", "9999") <= str(start) \
                and previous.get("end_day", "") >= str(end):
            self.progress("ready", **{key: previous[key] for key in ("start_day", "end_day", "symbols", "scan_dates")})
            return previous
        if force:
            # An older corrected bar may leave the endpoint signature unchanged.
            # New scan identities must still replace its previous prepared values.
            source_key = hashlib.sha256(f"{inputs_key}:{time.time_ns()}".encode()).hexdigest()
        dirty_start = start
        prior_signatures = previous.get("signatures", {})
        if not force and prior_signatures and previous.get("rules_key") == rule_key \
                and previous.get("version") == PRECOMPUTE_VERSION and previous.get("start_day", "9999") <= str(start):
            dirty_start = dt.date.fromisoformat(previous["end_day"]) + dt.timedelta(days=1)
            for symbol in set(signatures) | set(prior_signatures):
                old, new = prior_signatures.get(symbol), signatures.get(symbol)
                if old == new:
                    continue
                if not old or not new or old["first"] != new["first"] or old["last"] == new["last"]:
                    dirty_start = start
                    break
                # New suffix bars do not alter earlier ranks or returns.
                dirty_start = min(dirty_start, dt.date.fromisoformat(old["last"][:10]) + dt.timedelta(days=1))
            if dirty_start > start:
                # The original rolling refresh can revise existing bars as well
                # as append them. Check the prior endpoint before reusing dates.
                changed = [s for s in signatures if s in prior_signatures and signatures[s] != prior_signatures[s]]
                for offset in range(0, len(changed), 100):
                    batch = changed[offset:offset + 100]
                    keys = [(s, dt.datetime.fromisoformat(prior_signatures[s]["last"])) for s in batch]
                    table = self.repo.daily
                    rows = self.repo.rows(select(table.c.symbol, table.c.updated_at).where(table.c.interval == "1d",
                        tuple_(table.c.symbol, table.c.date).in_(keys)))
                    if any(str(row["updated_at"]) != prior_signatures[row["symbol"]]["revision"] for row in rows):
                        dirty_start = start
                        break
        self.progress("daily_archives", processed=0, total=len(signatures), start_day=str(start), end_day=str(end))
        first = min(dt.date.fromisoformat(row["first"][:10]) for row in signatures.values())
        spy = self.repo.histories(["SPY"], first, end)["SPY"]
        frames = {}
        symbols = ["SPY", *sorted(set(signatures) - {"SPY"})]
        spy_key = signatures["SPY"]
        tokens = {symbol: hashlib.sha256(json.dumps([PRECOMPUTE_VERSION, signatures[symbol], spy_key, str(end)],
            sort_keys=True).encode()).hexdigest() for symbol in symbols}
        metadata = {row["symbol"]: row for row in self.cache.rows(select(self.cache.archives.c.symbol,
            self.cache.archives.c.source_key, self.cache.archives.c.version))}
        started = time.monotonic()
        for offset in range(0, len(symbols), 80):
            batch = symbols[offset:offset + 80]
            archive_records = []
            changed = [symbol for symbol in batch if force or metadata.get(symbol, {}).get("source_key") != tokens[symbol]
                       or metadata.get(symbol, {}).get("version") != PRECOMPUTE_VERSION]
            own_changes = [symbol for symbol in changed if force or signatures[symbol] != prior_signatures.get(symbol)
                or (previous.get("end_day", "") < str(end) and signatures[symbol]["last"][:10] > previous.get("end_day", ""))]
            histories = self.repo.histories(own_changes, first, end) if own_changes else {}
            for symbol in batch:
                artifact = None
                if symbol in changed:
                    history = histories.get(symbol)
                    if history is None:
                        cached = self.cache.archive(symbol)
                        history = self.history_from_artifact(cached) if cached else self.repo.histories([symbol], first, end).get(symbol)
                    if history is None or history.empty:
                        continue
                    artifact, frame = daily_study_artifact(symbol, history, spy)
                    if not artifact["chart"]["bars"]:
                        continue
                    archive_records.append(self.cache.archive_record(symbol, tokens[symbol], artifact))
                else:
                    artifact = self.cache.archive(symbol)
                    if artifact["source_key"] != tokens[symbol]:
                        artifact = self.cache.archive(symbol, use_disk=False)
                    frame = pd.DataFrame.from_dict(artifact["metrics"], orient="index")
                    frame.index = pd.to_datetime(frame.index)
                frames[symbol] = frame.drop(columns=["symbol", "name"], errors="ignore")
            self.cache.save_archive_records(archive_records)
            self.progress("daily_archives", processed=min(offset + len(batch), len(symbols)), total=len(symbols),
                elapsed_seconds=round(time.monotonic() - started, 1))
        days = [stamp.date() for stamp in pd.DatetimeIndex(spy.index) if dirty_start <= stamp.date() <= end
                and stamp.date() not in REPLAY_CLOSURES]
        self.progress("historical_scans", processed=0, total=len(days), from_day=str(dirty_start))
        for offset in range(0, len(days), 40):
            batch = days[offset:offset + 40]
            scans = rank_daily_universe(frames, batch, rules)
            self.cache.save_scans(setup_name, rule_key, source_key, scans)
            self.progress("historical_scans", processed=offset + len(batch), total=len(days))
        all_days = [stamp.date() for stamp in pd.DatetimeIndex(spy.index) if start <= stamp.date() <= end
                    and stamp.date() not in REPLAY_CLOSURES]
        published = {"version": PRECOMPUTE_VERSION, "source_key": source_key, "inputs_key": inputs_key, "rules_key": rule_key,
            "setup_name": setup_name, "start_day": str(start), "end_day": str(all_days[-1]) if all_days else str(start),
            "symbols": len(frames), "scan_dates": len(all_days), "signatures": signatures,
            "bounds": {"first": str(first), "last": str(all_days[-1]) if all_days else str(end), "symbols": len(frames)},
            "elapsed_seconds": round(time.monotonic() - started, 1)}
        self.cache.save_job("published", published)
        self.progress("ready", **{key: published[key] for key in ("start_day", "end_day", "symbols", "scan_dates")})
        return published

    @staticmethod
    def history_from_artifact(artifact):
        frame = pd.DataFrame(artifact["chart"]["bars"])
        frame.index = pd.to_datetime(frame.pop("time"))
        return frame[["open", "high", "low", "close", "volume"]].rename(columns=str.title)
