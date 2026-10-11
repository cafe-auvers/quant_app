"""Preload a stock's daily archive while returning only the replay's visible prefix."""
from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
import datetime as dt
import threading

import pandas as pd

from src.core.breakout_replay import STUDY_START, daily_chart, metrics_as_of, normalize_daily
from src.utils.market_calendar import expected_latest_market_data_date


class DailyReplayArchive:
    @classmethod
    def from_prepared(cls, symbol, start_day, artifact, spy_artifact):
        from src.services.replay_preparation import ReplayPreparation

        instance = cls.__new__(cls)
        instance.symbol = symbol
        instance.history = ReplayPreparation.history_from_artifact(artifact)
        instance.spy = ReplayPreparation.history_from_artifact(spy_artifact)
        warmup = start_day - dt.timedelta(days=520)
        instance.chart_history = instance.history.loc[pd.Timestamp(warmup):]
        instance.chart_spy = instance.spy.loc[pd.Timestamp(warmup):]
        instance.full_chart = artifact["chart"]
        instance.chart = {"bars": [dict(row) for row in artifact["chart"]["bars"] if row["time"] >= warmup.isoformat()],
                          "rs_anchor": None}
        valid = next((row for row in instance.chart["bars"] if row["rs"] is not None), None)
        if valid:
            baseline = valid["rs"] + 100
            for row in instance.chart["bars"]:
                if row["rs"] is not None:
                    row["rs"] = ((row["rs"] + 100) / baseline - 1) * 100
        instance.chart["rs_anchor"] = instance.chart["bars"][0]["time"] if instance.chart["bars"] else None
        instance.bar_days = [row["time"] for row in instance.chart["bars"]]
        instance.days = [dt.date.fromisoformat(row["time"]) for row in artifact["chart"]["bars"] if row["time"] >= STUDY_START.isoformat()]
        instance.dates = [day for day in instance.days if day >= start_day]
        instance.metrics = {dt.date.fromisoformat(day): value for day, value in artifact["metrics"].items()}
        instance.lock = threading.RLock()
        return instance

    def __init__(self, symbol, start_day, histories):
        self.symbol = symbol
        self.history = normalize_daily(histories[symbol])
        self.spy = normalize_daily(histories["SPY"])
        warmup = pd.Timestamp(start_day - dt.timedelta(days=520))
        self.chart_history = self.history.loc[warmup:]
        self.chart_spy = self.spy.loc[warmup:]
        self.days = [stamp.date() for stamp in self.history.index if stamp.date() >= STUDY_START]
        self.dates = [day for day in self.days if day >= start_day]
        self.chart = daily_chart(self.chart_history, self.chart_spy, self.dates[-1])
        self.bar_days = [row["time"] for row in self.chart["bars"]]
        self.full_chart = None
        self.metrics = {}
        self.lock = threading.RLock()

    def metrics_at(self, day):
        with self.lock:
            if day not in self.metrics:
                self.metrics[day] = metrics_as_of(self.symbol, self.chart_history, self.chart_spy, day)
            return self.metrics[day]

    def visible(self, day, full_history=False):
        if full_history:
            with self.lock:
                if self.full_chart is None:
                    self.full_chart = daily_chart(self.history, self.spy, self.dates[-1])
                return self.full_chart
        end = bisect_right(self.bar_days, day.isoformat())
        return {"bars": self.chart["bars"][:end], "rs_anchor": self.chart["rs_anchor"]}

    def warm(self, day):
        start = self.dates.index(day)
        for stamp in self.dates[start:start + 51]:
            self.metrics_at(stamp)


class ReplayDailyCache:
    def __init__(self, repository, capacity=8):
        self.repo, self.capacity = repository, capacity
        self.archives = OrderedDict()
        self.revisions = {}
        self.lock = threading.RLock()

    def get(self, session, refresh=False):
        key = (session["symbol"], session["start_day"])
        with self.lock:
            revision = self.repo.prepared.archive_revision(session["symbol"])
            if refresh or self.revisions.get(key) != revision:
                self.archives.pop(key, None)
            if key not in self.archives:
                artifact = self.repo.prepared.archive(session["symbol"])
                spy_artifact = self.repo.prepared.archive("SPY") if artifact else None
                if artifact and spy_artifact:
                    self.archives[key] = DailyReplayArchive.from_prepared(session["symbol"], session["start_day"], artifact, spy_artifact)
                    self.revisions[key] = self.repo.prepared.archive_revision(session["symbol"])
                    while len(self.archives) > self.capacity:
                        self.archives.popitem(last=False)
                    return self.archives[key]
                start = min(STUDY_START, session["start_day"] - dt.timedelta(days=520))
                histories = self.repo.histories([session["symbol"], "SPY"], start, expected_latest_market_data_date())
                if session["symbol"] not in histories or "SPY" not in histories:
                    raise ValueError("The stock and SPY daily archives are required for replay")
                self.archives[key] = DailyReplayArchive(session["symbol"], session["start_day"], histories)
                self.revisions[key] = revision
                while len(self.archives) > self.capacity:
                    self.archives.popitem(last=False)
            self.archives.move_to_end(key)
            return self.archives[key]

    def clear(self):
        with self.lock:
            self.archives.clear()
            self.revisions.clear()
