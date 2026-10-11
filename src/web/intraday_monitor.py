"""Shared advisory quotes/ORB cache, independent of scanning and execution."""
from __future__ import annotations

import datetime as dt
import copy
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from src.core.opening_liquidity import opening_liquidity_rejection, opening_volume_totals
from src.core.orb_entry_logic import passive_entry_prices
from src.risk.orb_position import (
    OrbSettings, calculate_orb_position_values, is_orb_position_plan_valid,
    score_orb_position_recommendation, validate_orb_position_values,
)
from src.strategy.orb import calculate_orb_range
from src.utils.market_calendar import (
    US_MARKET_ZONE, is_nyse_trading_day, nyse_regular_session_close_time,
    previous_nyse_trading_day,
)


logger = logging.getLogger(__name__)
ORB_WINDOWS = {"1m": 1, "5m": 5, "30m": 30}
RISK_CASES = (0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.0175, 0.02)


def positive(value: object) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass
class MonitorContext:
    rows: list[dict[str, Any]] = field(default_factory=list)
    settings: OrbSettings | None = None
    equity: float | None = None
    position_error: str = ""


def merge_monitor_rows(plans: list[dict], drafts: list[dict]) -> list[dict]:
    """Union memberships without monitoring orphaned drafts or breakout-only cards."""
    rows = {}
    for card in plans:
        if not any(card.get(key) for key in ("watchlist_member", "buylist_member", "buy_today_member")):
            continue
        rows[card["symbol"]] = dict(card)
    for draft in drafts:
        card = rows.get(draft["symbol"])
        if card and card.get("buylist_member") and (
            draft.get("card_version") is None
            or int(draft["card_version"]) == int(card.get("version") or 0)
        ):
            card["buy_today_member"] = True
    return [rows[symbol] for symbol in sorted(rows)]


class YahooMonitorProvider:
    """Bound downloads to four threads; never retry each ticker in a failed batch."""

    def fetch(self, symbols: list[str], *, interval: str, period: str | None = None) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        result = {}
        aliases = {symbol: symbol.replace(".", "-").replace("/", "-") for symbol in symbols}
        names = list(dict.fromkeys(aliases.values()))
        for offset in range(0, len(names), 40):
            chunk = names[offset:offset + 40]
            try:
                data = yf.download(
                    chunk, period=period or ("1d" if interval == "1m" else "6mo"),
                    interval=interval, group_by="ticker", auto_adjust=False,
                    prepost=interval == "1m", threads=4, progress=False,
                    timeout=8, ignore_tz=False,
                )
            except Exception:
                logger.warning("Monitor %s batch unavailable", interval, exc_info=True)
                continue
            if not isinstance(data, pd.DataFrame) or data.empty:
                continue
            for symbol, alias in aliases.items():
                if alias not in chunk:
                    continue
                frame = None
                if isinstance(data.columns, pd.MultiIndex):
                    for level in range(data.columns.nlevels):
                        if alias in data.columns.get_level_values(level):
                            frame = data.xs(alias, axis=1, level=level)
                            break
                elif len(chunk) == 1:
                    frame = data
                if frame is not None and {"Open", "High", "Low", "Close"} <= set(frame.columns):
                    frame = frame.copy()
                    # Yahoo float32 prices otherwise look like illegal equity ticks.
                    for column in ("Open", "High", "Low", "Close"):
                        values = pd.to_numeric(frame[column], errors="coerce")
                        frame[column] = values.round(2).where(values >= 1, values.round(4))
                    frame = frame.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
                    if not frame.empty:
                        result[symbol] = frame
        return result


def daily_adr(frame: pd.DataFrame, today: dt.date) -> float | None:
    """Use completed daily bars, matching the desktop's 20-session ADR basis."""
    if frame.empty:
        return None
    bars = frame[pd.DatetimeIndex(frame.index).date < today]
    if len(bars) < 21:
        return None
    previous = pd.to_numeric(bars.Close, errors="coerce").shift(1).replace(0, float("nan"))
    ranges = (pd.to_numeric(bars.High, errors="coerce") - pd.to_numeric(bars.Low, errors="coerce")) / previous * 100
    return positive(ranges.tail(20).mean()) if ranges.tail(20).notna().all() else None


def latest_daily_close(frame: pd.DataFrame, now: dt.datetime) -> dict | None:
    """Timestamp completed daily prices at the actual regular-session close."""
    if frame.empty or "Close" not in frame:
        return None
    now = now.astimezone(US_MARKET_ZONE)
    index = pd.DatetimeIndex(frame.index)
    if index.tz is not None:
        index = index.tz_convert(US_MARKET_ZONE)
    for offset in range(len(frame) - 1, -1, -1):
        day = index[offset].date()
        if not is_nyse_trading_day(day):
            continue
        closed_at = dt.datetime.combine(day, nyse_regular_session_close_time(day), US_MARKET_ZONE)
        price = positive(frame.Close.iloc[offset])
        if closed_at <= now and price is not None:
            return {"price": price, "as_of": closed_at.isoformat()}
    return None


def apply_daily_close_fallback(row: dict, quote: dict | None, now: dt.datetime) -> None:
    """Keep a known close visible without treating daily bars as intraday signals."""
    if quote is None:
        return
    quoted = dt.datetime.fromisoformat(quote["as_of"])
    if row.get("quote_as_of") and dt.datetime.fromisoformat(row["quote_as_of"]) >= quoted:
        return
    now = now.astimezone(US_MARKET_ZONE)
    active = is_nyse_trading_day(now.date()) and dt.time(4) <= now.time() < dt.time(20)
    expected_day = now.date() if active else previous_nyse_trading_day(now.date() - dt.timedelta(days=1))
    if is_nyse_trading_day(now.date()) and now.time() >= dt.time(20):
        expected_day = now.date()
    row.update(current_price=quote["price"], quote_as_of=quote["as_of"], quote_source="DAILY_CLOSE")
    suppress_stale_signals(row)
    row["quote_status"] = "CLOSED" if not active and quoted.date() >= expected_day else "STALE"
    row["breakout_status"] = "CLOSED" if row.get("breakout_price") and not active else row["breakout_status"]
    if row.get("breakout_price"):
        row["distance_percent"] = round((quote["price"] / row["breakout_price"] - 1) * 100, 2)


def suppress_stale_signals(row: dict) -> None:
    row["quote_status"] = "STALE" if row.get("current_price") else "UNAVAILABLE"
    row["breakout_status"] = "UNKNOWN" if row.get("breakout_price") else "NO_LEVEL"
    row["broke_out_today"] = None
    for window in row.get("orb", []):
        # An observed crossing remains a historical fact for this session.
        # It does not certify the current quote or authorize an entry.
        if window.get("price_status") != "PASS":
            window.update(price_status="UNKNOWN", price_reason="Quote is stale")


def apply_daily_performance(row: dict, daily: pd.DataFrame, now: dt.datetime) -> None:
    """Use the quoted price and completed daily bars, matching 21/63-bar growth."""
    row.update(change_percent=None, return_1m=None, return_3m=None)
    price = positive(row.get("current_price"))
    if price is None or not row.get("quote_as_of") or daily.empty or "Close" not in daily:
        return
    quote_day = dt.datetime.fromisoformat(row["quote_as_of"]).astimezone(US_MARKET_ZONE).date()
    index = pd.DatetimeIndex(daily.index)
    if index.tz is not None:
        index = index.tz_convert(US_MARKET_ZONE)
    history = daily.copy()
    history.index = index
    history = history.loc[index.date < quote_day].sort_index()
    if history.empty or history.index.has_duplicates:
        return
    expected = previous_nyse_trading_day(quote_day - dt.timedelta(days=1))
    if history.index[-1].date() != expected:
        return
    close = pd.to_numeric(history["Close"], errors="coerce")
    for key, periods in (("change_percent", 1), ("return_1m", 21), ("return_3m", 63)):
        if len(close) < periods or (key == "change_percent" and quote_day != now.astimezone(US_MARKET_ZONE).date()):
            continue
        base = positive(close.iloc[-periods])
        if base is not None:
            value = (price / base - 1) * 100
            row[key] = value if math.isfinite(value) else None


def evaluate_monitor_row(
    card: dict, frame: pd.DataFrame, *, now: dt.datetime, context: MonitorContext,
    adr: float | None = None, fetch_failed: bool = False, stale_seconds: float = 180,
) -> dict:
    now = now.astimezone(US_MARKET_ZONE)
    breakout = positive(card.get("breakout_price"))
    row = {**card, "breakout_price": breakout, "current_price": None,
           "quote_as_of": None, "quote_status": "UNAVAILABLE", "quote_source": "MINUTE", "distance_percent": None,
           "breakout_status": "UNKNOWN", "broke_out_today": None,
           "orb_session_date": now.date().isoformat(), "orb": [],
           "change_percent": None, "return_1m": None, "return_3m": None}
    if not frame.empty and frame.index.tz is not None:
        frame = frame.copy()
        frame.index = frame.index.tz_convert(US_MARKET_ZONE)
        frame = frame[frame.index <= now]
    else:
        frame = pd.DataFrame()
    if frame.empty:
        return row
    latest = frame.index[-1]
    price = positive(frame.Close.iloc[-1])
    active = is_nyse_trading_day(now.date()) and dt.time(4) <= now.time() < dt.time(20)
    expected_day = now.date() if is_nyse_trading_day(now.date()) and now.time() >= dt.time(4) else previous_nyse_trading_day(now.date() - dt.timedelta(days=1))
    stale = fetch_failed or price is None or latest.date() < expected_day or (active and (now - latest).total_seconds() > stale_seconds)
    row.update(current_price=price, quote_as_of=latest.isoformat(),
               quote_status="STALE" if stale else "CURRENT" if active else "CLOSED")
    if breakout and price:
        row["distance_percent"] = round((price / breakout - 1) * 100, 2)
    close = nyse_regular_session_close_time(now.date())
    session = frame[(frame.index.date == now.date()) & (frame.index.time >= dt.time(9, 30)) & (frame.index.time < close)]
    start = pd.Timestamp(dt.datetime.combine(now.date(), dt.time(9, 30), US_MARKET_ZONE))
    opening_volume, opening_minutes = opening_volume_totals(session, session.index, start) if not session.empty else (None, 0)
    liquidity_reason = opening_liquidity_rejection(opening_volume, opening_minutes, settings=context.settings) if context.settings else "Shared ORB settings unavailable"
    liquidity_status = "PASS" if not liquidity_reason else "FAIL" if liquidity_reason.startswith("Low opening liquidity") else "UNKNOWN"
    row.update(opening_volume=opening_volume, opening_volume_minutes=opening_minutes,
               liquidity_status=liquidity_status, liquidity_reason=liquidity_reason)
    if breakout is None:
        row["breakout_status"] = "NO_LEVEL"
    elif not stale and not session.empty:
        crossed = bool((pd.to_numeric(session.High, errors="coerce") > breakout).any())
        regular_price = positive(session.Close.iloc[-1])
        row["broke_out_today"] = crossed
        row["breakout_status"] = (
            "ABOVE" if regular_price and regular_price > breakout
            else "PULLED_BACK" if crossed else "WAITING"
        )
    elif not stale and session.empty:
        row["breakout_status"] = "PRE_MARKET" if now.time() < dt.time(9, 30) else "CLOSED"
    for window, minutes in ORB_WINDOWS.items():
        result = {"window": window, "price_status": "UNKNOWN", "position_status": "UNKNOWN",
                  "liquidity_status": liquidity_status, "liquidity_reason": liquidity_reason,
                  "price_reason": "Current regular-session data unavailable", "position_reason": "",
                  "high": None, "low": None, "risk_percent": None}
        row["orb"].append(result)
        if session.empty or breakout is None:
            continue
        end = start + pd.Timedelta(minutes=minutes)
        if now < end:
            result.update(price_status="FORMING", price_reason="Opening range still forming")
            continue
        # Missing minutes cannot produce a confident opening range.
        expected = pd.date_range(start, periods=minutes, freq="min")
        if not expected.isin(session.index).all():
            result["price_reason"] = "Opening range has missing minute bars"
            continue
        orb_range = calculate_orb_range(card["symbol"], session, window)
        if orb_range is None:
            result["price_reason"] = "Waiting for a post-range minute bar"
            continue
        high, low = orb_range.high, orb_range.low
        floor, trigger, execution, reason = passive_entry_prices(
            breakout_price=breakout, orb_high=high, orb_low=low,
        )
        result.update(high=high, low=low, breakout_trigger=max(breakout, high),
                      range_closed_at=end.isoformat())
        # Separate the observed price test from the passive entry/position geometry.
        post_range = session[session.index >= end]
        confirmed = bool((pd.to_numeric(post_range.High) > max(breakout, high)).any())
        result.update(price_status="PASS" if confirmed else "WAITING",
                      price_reason="Post-range price exceeded breakout and ORH" if confirmed
                      else "Waiting for price above breakout and ORH")
        if reason:
            if reason == "No valid passive-pullback execution zone":
                reason += (
                    f": breakout ${breakout:g}, range high ${high:g}, range low ${low:g}; "
                    "entry must be above breakout and range low, and at or below range high"
                )
            result.update(position_status="FAIL", position_reason=reason)
            continue
        if context.settings is None or context.equity is None or adr is None:
            result["position_reason"] = context.position_error or "Completed daily ADR unavailable"
            continue
        candidates = []
        for risk in RISK_CASES:
            sizing = calculate_orb_position_values(context.equity, risk, float(execution), low, adr)
            valid = is_orb_position_plan_valid(sizing, adr, context.settings, is_ep=card.get("is_ep") is True)
            score = score_orb_position_recommendation(sizing, risk, context.settings, is_ep=card.get("is_ep") is True)
            candidates.append((valid, score, risk, sizing))
        valid, score, risk, sizing = max(candidates, key=lambda item: (item[0], item[1]))
        warnings = validate_orb_position_values(sizing, adr, context.settings, is_ep=card.get("is_ep") is True)
        result.update(position_status="PASS" if valid else "FAIL", risk_percent=risk * 100, score=score,
                      capital_percent=round(sizing["capital_percent"], 2),
                      stop_adr_percent=round(sizing["sl_adr"], 2) if sizing["sl_adr"] is not None else None,
                      position_reason="Capital and stop/ADR bounds passed" if valid else "; ".join(warnings))
    if stale:
        suppress_stale_signals(row)
    return row


class IntradayMonitor:
    """One worker/cache per web process; requests never wait for Yahoo."""

    def __init__(
        self, load_context: Callable[[], MonitorContext], *, enabled: bool,
        refresh_seconds: float = 60, stale_seconds: float = 180,
        provider=None, now=None, monotonic=None,
    ):
        self.load_context = load_context
        self.enabled = enabled
        self.refresh_seconds = max(60, refresh_seconds)
        self.stale_seconds = max(60, stale_seconds)
        self.on_update = None
        self.provider = provider or YahooMonitorProvider()
        self.now = now or (lambda: dt.datetime.now(US_MARKET_ZONE))
        self.monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._active_until = 0.0
        self._next_refresh = 0.0
        self._frames = {}
        self._failed_symbols = set()
        self._adr = {}
        self._daily_quotes = {}
        self._daily_frames = {}
        self._adr_retry_after = {}
        self._adr_session = None
        self._context = MonitorContext()
        self._snapshot = {"rows": [], "as_of": None, "error": "", "refreshing": False}

    def snapshot(self) -> dict:
        with self._lock:
            self._active_until = self.monotonic() + 180
            if self.enabled and self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name="web-intraday-monitor", daemon=True)
                self._thread.start()
            snapshot = copy.deepcopy(self._snapshot)
            context = self._context
        self._wake.set()
        # All ORB work happens once in the worker; readers only check quote age.
        now = self.now().astimezone(US_MARKET_ZONE)
        active = is_nyse_trading_day(now.date()) and dt.time(4) <= now.time() < dt.time(20)
        expected_day = now.date() if is_nyse_trading_day(now.date()) and now.time() >= dt.time(4) else previous_nyse_trading_day(now.date() - dt.timedelta(days=1))
        for row in snapshot["rows"]:
            if not row.get("quote_as_of"):
                continue
            quoted = dt.datetime.fromisoformat(row["quote_as_of"]).astimezone(US_MARKET_ZONE)
            if quoted.date() < expected_day or (active and (now - quoted).total_seconds() > self.stale_seconds):
                suppress_stale_signals(row)
            elif not active and row.get("quote_status") != "STALE":
                row["quote_status"] = "CLOSED"
            if quoted.date() != now.date():
                row["change_percent"] = None
                row["broke_out_today"] = None
                if row["quote_status"] != "STALE":
                    row["breakout_status"] = "CLOSED" if row.get("breakout_price") else "NO_LEVEL"
                for window in row.get("orb", []):
                    window.update(price_status="UNKNOWN", position_status="UNKNOWN",
                                  price_reason="Current regular-session data unavailable",
                                  position_reason="Current regular-session data unavailable")
        return {**snapshot, "enabled": self.enabled, "source": "Yahoo Finance · 1-minute bars",
                "refresh_seconds": self.refresh_seconds, "stale_seconds": self.stale_seconds, "advisory": True,
                "position_context_error": context.position_error}

    def invalidate(self) -> None:
        # Changes join the next cycle; repeated edits cannot multiply downloads.
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            tick = self.monotonic()
            if tick < self._active_until and tick >= self._next_refresh:
                self.refresh_once()
            self._wake.wait(timeout=1)
            self._wake.clear()

    def refresh_once(self) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            now = self.now().astimezone(US_MARKET_ZONE)
            self._next_refresh = self.monotonic() + self.refresh_seconds
            with self._lock:
                self._snapshot["refreshing"] = True
            context = self.load_context()
            symbols = [row["symbol"] for row in context.rows]
            active = is_nyse_trading_day(now.date()) and dt.time(4) <= now.time() < dt.time(20)
            # Off-hours: read plans each minute, fetch each symbol once per date.
            due = symbols if active else [s for s in symbols if s not in self._frames or s in self._failed_symbols or self._adr_session != now.date()]
            frames = self.provider.fetch(due, interval="1m", period="1d" if active else "5d") if due else {}
            adr = dict(self._adr) if self._adr_session == now.date() else {}
            missing_adr = [s for s in symbols if s not in adr or (adr[s] is None and self.monotonic() >= self._adr_retry_after.get(s, 0))]
            if missing_adr:
                daily = self.provider.fetch(missing_adr, interval="1d")
                self._daily_frames.update(daily)
                adr.update({s: daily_adr(daily.get(s, pd.DataFrame()), now.date()) for s in missing_adr})
                for symbol, frame in daily.items():
                    quote = latest_daily_close(frame, now)
                    if quote is not None:
                        self._daily_quotes[symbol] = quote
                self._adr_retry_after.update({s: self.monotonic() + 900 for s in missing_adr})
            self._frames = {s: frames.get(s, self._frames.get(s, pd.DataFrame())) for s in symbols}
            self._failed_symbols = (self._failed_symbols | (set(due) - frames.keys())) - frames.keys()
            self._failed_symbols.intersection_update(symbols)
            rows = [evaluate_monitor_row(
                row, self._frames[row["symbol"]], now=self.now(), context=context,
                adr=adr.get(row["symbol"]), fetch_failed=row["symbol"] in self._failed_symbols,
                stale_seconds=self.stale_seconds,
            ) for row in context.rows]
            for row in rows:
                apply_daily_close_fallback(row, self._daily_quotes.get(row["symbol"]), self.now())
                apply_daily_performance(row, self._daily_frames.get(row["symbol"], pd.DataFrame()), self.now())
            self._daily_quotes = {s: quote for s, quote in self._daily_quotes.items() if s in symbols}
            self._daily_frames = {s: frame for s, frame in self._daily_frames.items() if s in symbols}
            with self._lock:
                self._adr = {s: adr.get(s) for s in symbols}
                self._adr_session = now.date()
                self._context = context
                self._snapshot = {"rows": rows, "as_of": self.now().isoformat(), "error": "", "refreshing": False}
                if due and not frames:
                    if not any(row.get("current_price") for row in rows):
                        self._snapshot["error"] = f"Yahoo quotes unavailable; next check in {self.refresh_seconds * 2:g} seconds"
                    self._next_refresh = self.monotonic() + self.refresh_seconds * 2
            if self.on_update and not self._stop.is_set():
                self.on_update()
        except Exception:
            logger.warning("Intraday monitor refresh unavailable", exc_info=True)
            with self._lock:
                self._snapshot["error"] = "Monitor refresh unavailable; showing the last cached prices"
                self._snapshot["refreshing"] = False
                for row in self._snapshot["rows"]:
                    suppress_stale_signals(row)
            self._next_refresh = self.monotonic() + self.refresh_seconds * 2
        finally:
            # Slow provider calls get a short breathing space before another batch.
            self._next_refresh = max(self._next_refresh, self.monotonic() + 10)
            self._refresh_lock.release()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
