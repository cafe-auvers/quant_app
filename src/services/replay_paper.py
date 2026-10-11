"""Independent $10,000 trade studies, with saved legacy account review."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from collections import OrderedDict
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select

from src.core.breakout_replay import regular_session
from src.core.replay_paper import paper_ledger
from src.services.replay_intraday import IntradayUnavailable
from src.infrastructure.database.repositories.breakout_replay import now
from src.utils.market_calendar import nyse_regular_session_close_time as market_close_time


class ReplayPaperService:
    def __init__(self, repository, archive_loader, lock):
        self.repo, self.archive_loader, self.lock = repository, archive_loader, lock
        self._packets = OrderedDict()

    def _packet(self, symbol, day, source, cached_only=False):
        key = (symbol, day, source)
        if key in self._packets:
            self._packets.move_to_end(key)
            return self._packets[key].copy()
        try:
            if cached_only:
                actual_source = self.repo.cached_intraday_source(symbol, day, source)
                if actual_source is None:
                    return {"bars": [], "reason": "Open this session's intraday study to verify the open position and stop."}
                frame = self.repo.intraday(symbol, day, actual_source)
            else:
                frame, actual_source, _ = self.archive_loader(symbol, day, source)
        except IntradayUnavailable as exc:
            return {"bars": [], "reason": str(exc)}
        frame = regular_session(frame, day)
        expected = int((dt.datetime.combine(day, market_close_time(day)) - dt.datetime.combine(day, dt.time(9, 30))).total_seconds() / 300)
        complete = bool(len(frame) == expected and frame.index[0].time() == dt.time(9, 30)
            and (frame.index.to_series().diff().dropna() == pd.Timedelta(minutes=5)).all())
        packet = {"source": actual_source, "complete": complete,
            "bars": [{"time": int(stamp.timestamp()), **{name.lower(): float(row[name])
                for name in ("Open", "High", "Low", "Close", "Volume")}} for stamp, row in frame.iterrows()]}
        if packet["bars"]:
            self._packets[key] = packet
            if len(self._packets) > 512:
                self._packets.popitem(last=False)
        return packet.copy()

    def _calculate(self, symbol, day, actions, source, cutoff=None, cached_only=False):
        dates = self.repo.daily_dates(symbol, min(a["day"] for a in actions)) if actions else []
        def load(stamp):
            packet = self._packet(symbol, stamp, source, cached_only)
            if cutoff is not None and stamp == day:
                packet["bars"] = [b for b in packet["bars"] if b["time"] <= cutoff]
            return packet
        selected = [a for a in actions if cutoff is None or a["time"] <= cutoff]
        result = paper_ledger(selected, dates, load, day)
        result["latest_action_time"] = max((a["time"] for a in actions), default=None)
        result["symbol"] = symbol
        result["asof_time"] = cutoff
        return result

    def _study(self, symbol, day, studies, actions, source, cutoff, study_id, cached_only=False):
        if study_id == "legacy":
            selected = [a for a in actions if not a.get("study_id")]
            result = self._calculate(symbol, day, selected, source, cutoff, cached_only)
            result.update(study_id="legacy", study_label="Earlier cumulative account", read_only=True,
                scope="Saved earlier account · review only")
            return None, selected, result
        if study_id:
            study = next((s for s in studies if s["id"] == study_id), None)
            if not study or study["start_day"] > day:
                raise ValueError("Choose a saved study for this stock on or before the selected day")
            selected = [a for a in actions if a.get("study_id") == study_id]
            return study, selected, self._calculate(symbol, day, selected, source, cutoff, cached_only)
        for study in sorted(studies, key=lambda s: (s["start_day"], s["created_at"]), reverse=True):
            if study["start_day"] > day:
                continue
            saved = study["payload"]
            if study["start_day"] < day and saved.get("fills") and not saved.get("quantity") and not saved.get("pending") and saved["fills"][-1]["day"] < day.isoformat():
                continue
            selected = [a for a in actions if a.get("study_id") == study["id"]]
            result = self._calculate(symbol, day, selected, source, cutoff, cached_only)
            # Follow an open trade across sessions, but never carry a closed result into a new entry.
            if study["start_day"] == day or result["quantity"] or result["pending"] or any(f["day"] == day.isoformat() for f in result["fills"]):
                return study, selected, result
        return None, [], self._calculate(symbol, day, [], source, cutoff, cached_only)

    @staticmethod
    def _identify(result, study):
        if result.get("study_id") == "legacy":
            return result
        result.update(study_id=study["id"] if study else None,
            study_label=study["label"] if study else "New independent study",
            study_start=study["start_day"].isoformat() if study else result["asof"], read_only=False,
            scope="Independent trade study · $10,000 start · 1% NAV risk")
        for fill in result["fills"]:
            fill["study_id"] = result["study_id"]
        return result

    def _menu(self, symbol, day, studies, actions):
        menu = [{"id": s["id"], "start_day": s["start_day"].isoformat(), "label": s["label"]}
            for s in studies if s["start_day"] <= day]
        if any(not a.get("study_id") for a in actions):
            menu.append({"id": "legacy", "label": "Earlier cumulative account · saved", "start_day": None})
        return menu

    def create_study(self, symbol, day, identity=None):
        with self.lock:
            identity = identity or str(uuid.uuid4())
            rows = self.repo.paper_study_rows(symbol)
            global_rows = self.repo.rows(select(self.repo.paper_studies).where(self.repo.paper_studies.c.id == identity))
            existing = global_rows[0] if global_rows else None
            if existing:
                if existing["symbol"] != symbol or existing["start_day"] != day:
                    raise ValueError("This study ID belongs to a different stock or entry day")
                return identity
            study = {"id": identity, "symbol": symbol, "start_day": day,
                "label": f"{day} · study {sum(s['start_day'] == day for s in rows) + 1}", "created_at": now()}
            result = self._identify(self._calculate(symbol, day, [], "auto"), study)
            self.repo.save_study_projection(symbol, study, result)
            self.repo.event("paper_study_created", {"study_id": identity, "initial_cash": 10000, "risk_pct": 1}, symbol, day)
            return identity

    def reconcile(self, symbol, day, source="auto", cutoff=None, study_id=None, cached_only=False):
        with self.lock:
            actions = self.repo.paper_action_rows(symbol)
            studies = self.repo.paper_study_rows(symbol)
            study, selected, result = self._study(symbol, day, studies, actions, source, cutoff, study_id, cached_only)
            self._identify(result, study)
            if study and cutoff is None:
                self.repo.save_study_projection(symbol, study, result)
            result["studies"] = self._menu(symbol, day, studies, actions)
            saved = [json.loads(r["payload"]) for r in self.repo.rows(select(self.repo.paper_fills)
                .where(self.repo.paper_fills.c.symbol == symbol, self.repo.paper_fills.c.day <= day))]
            result["chart_fills"] = sorted({f["id"]: f for f in [*saved, *result["fills"]]
                if cutoff is None or f["time"] <= cutoff}.values(), key=lambda f: f["time"])
            return result

    def trade(self, symbol, day, stamp, action, price, stop_pct, percent, source, request_id, interval_minutes=5, study_id=None):
        with self.lock:
            prior = self.repo.rows(select(self.repo.paper_actions).where(self.repo.paper_actions.c.id == request_id))
            if prior:
                if prior[0]["symbol"] != symbol:
                    raise ValueError("This action ID belongs to a different stock")
                original = json.loads(prior[0]["payload"])
                saved_id = original.get("study_id") or "legacy"
                if study_id and study_id != saved_id:
                    raise ValueError("This action ID belongs to a different study")
                return self.reconcile(symbol, day, source, study_id=saved_id)
            ny_day = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
            if ny_day != day:
                raise ValueError("Trade time must belong to the selected New York session")
            if action not in ("buy", "partial_sell", "sell_all") or not 0 < percent < 100 or not 0 < stop_pct < 100:
                raise ValueError("Invalid paper-trade action or percentage")
            all_actions = self.repo.paper_action_rows(symbol)
            studies = self.repo.paper_study_rows(symbol)
            study, actions, at_entry = self._study(symbol, day, studies, all_actions, source, stamp, study_id)
            if at_entry.get("read_only"):
                raise ValueError("Earlier cumulative accounts are saved for review. Start a new $10,000 study to trade.")
            if actions and stamp < actions[-1]["time"]:
                raise ValueError("Trades inside one study must follow chronologically. Start a new $10,000 study to test an earlier entry.")
            if action == "buy" and actions and not at_entry["quantity"] and not at_entry["pending"]:
                raise ValueError("This trade study has closed. Start a new $10,000 study for another entry.")
            if not study:
                if action != "buy":
                    raise ValueError("There is no open position in the selected study")
                study = {"id": request_id, "symbol": symbol, "start_day": day,
                    "label": f"{day} · study {sum(s['start_day'] == day for s in studies) + 1}", "created_at": now()}
            proposed = {"id": request_id, "day": day, "time": stamp, "action": action,
                "price": price, "stop": price * (1 - stop_pct / 100) if action == "buy" else None,
                "percent": percent, "source": source, "sequence": len(actions), "entry_window_minutes": interval_minutes,
                "study_id": study["id"]}
            result = self._identify(self._calculate(symbol, day, [*actions, proposed], source), study)
            fill = next((f for f in result["fills"] if f["id"] == request_id), None)
            if fill is None:
                raise ValueError("A prior stop check needs missing five-minute data. Resolve that session before adding a trade.")
            proposed.update(shares=fill["shares"], source=fill["source"], risk_budget=fill["risk_budget"])
            self.repo.save_study_projection(symbol, study, result, proposed)
            return result
