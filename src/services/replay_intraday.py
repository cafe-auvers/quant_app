"""Historical bars from free data plans, with explicit source provenance."""
from __future__ import annotations

import datetime as dt
import threading
import time
from collections import deque
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import pandas as pd
import requests
import yfinance as yf

from src.core.breakout_replay import regular_session
from src.utils.config import get_env_value


class IntradayUnavailable(ValueError):
    pass


class RequestLimiter:
    """Reserve each HTTP request in a shared rolling 60-second budget."""
    def __init__(self, limit=180, clock=time.monotonic, sleep=time.sleep):
        self.limit, self.clock, self.sleep = limit, clock, sleep
        self.timestamps = deque()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            while True:
                current = self.clock()
                while self.timestamps and self.timestamps[0] <= current - 60:
                    self.timestamps.popleft()
                if len(self.timestamps) < self.limit:
                    self.timestamps.append(current)
                    return
                self.sleep(max(0.01, 60 - (current - self.timestamps[0]) + 0.01))


class ReplayIntradayProvider:
    def __init__(self):
        self._massive_lock = threading.Lock()
        self._massive_next = 0.0
        # Basic permits 200/min; reserve 20 requests of headroom for other tools.
        self.alpaca_limiter = RequestLimiter(min(180, max(1, int(get_env_value("REPLAY_ALPACA_REQUESTS_PER_MINUTE", "180")))))
        self.massive_spacing = max(13, float(get_env_value("REPLAY_MASSIVE_REQUEST_INTERVAL_SECONDS", "13")))

    def configuration(self):
        return {
            "alpaca_sip": bool(get_env_value("ALPACA_API_KEY_ID") and get_env_value("ALPACA_API_SECRET_KEY")),
            "massive": bool(get_env_value("MASSIVE_API_KEY")),
            "yfinance": True,
        }

    def resolve(self, source):
        available = self.configuration()
        if source == "auto":
            return next((name for name in ("alpaca_sip", "massive") if available[name]), "yfinance")
        if source not in available:
            raise ValueError("Unknown intraday source")
        if not available[source]:
            raise IntradayUnavailable("Add this provider's API credentials to .env, then try again. See Data sources.")
        return source

    def alpaca_data_endpoint(self):
        configured = get_env_value("ALPACA_ENDPOINT", "https://data.alpaca.markets")
        parsed = urlparse(configured)
        if parsed.scheme != "https" or parsed.hostname not in ("data.alpaca.markets", "paper-api.alpaca.markets", "api.alpaca.markets"):
            raise IntradayUnavailable("ALPACA_ENDPOINT must be an official Alpaca HTTPS endpoint.")
        # Paper/live account keys both use the dedicated market-data host.
        return "https://data.alpaca.markets"

    @staticmethod
    def _response(response, name):
        if response.status_code in (401, 403):
            raise IntradayUnavailable(f"{name} declined access. Check your API keys and historical-data entitlement.")
        if response.status_code == 429:
            raise IntradayUnavailable(f"{name} rate limit reached. Wait a minute and retry.")
        if response.status_code != 200:
            raise IntradayUnavailable(f"{name} returned HTTP {response.status_code}. Try again later.")
        return response.json()

    def fetch(self, symbol, day, source):
        source = self.resolve(source)
        zone = ZoneInfo("America/New_York")
        start = dt.datetime.combine(day, dt.time(9, 30), tzinfo=zone)
        end = dt.datetime.combine(day, dt.time(16), tzinfo=zone)
        if end > dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16):
            raise IntradayUnavailable("This replay uses completed sessions. The requested session is not yet available.")
        try:
            if source == "alpaca_sip":
                params = {"symbols": symbol, "timeframe": "5Min", "feed": "sip", "adjustment": "raw",
                    "start": start.isoformat(), "end": end.isoformat(), "limit": 10000, "sort": "asc"}
                headers = {"APCA-API-KEY-ID": get_env_value("ALPACA_API_KEY_ID"),
                    "APCA-API-SECRET-KEY": get_env_value("ALPACA_API_SECRET_KEY")}
                records = []
                while True:
                    self.alpaca_limiter.acquire()
                    response = requests.get(self.alpaca_data_endpoint() + "/v2/stocks/bars", params=params, headers=headers, timeout=25)
                    payload = self._response(response, "Alpaca")
                    records.extend(payload.get("bars", {}).get(symbol, []))
                    token = payload.get("next_page_token")
                    if not token:
                        break
                    params["page_token"] = token
                frame = pd.DataFrame(records)
                if not frame.empty:
                    frame.index = pd.to_datetime(frame["t"], utc=True)
                    frame = frame.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
            elif source == "massive":
                # Basic: five requests/minute. Cache each selected session permanently.
                with self._massive_lock:
                    time.sleep(max(0, self._massive_next - time.monotonic()))
                    self._massive_next = time.monotonic() + self.massive_spacing
                    response = requests.get(f"https://api.massive.com/v2/aggs/ticker/{symbol}/range/5/minute/{day}/{day}",
                        params={"adjusted": "false", "sort": "asc", "limit": 50000},
                        headers={"Authorization": f"Bearer {get_env_value('MASSIVE_API_KEY')}"}, timeout=25)
                payload = self._response(response, "Massive")
                frame = pd.DataFrame(payload.get("results", []))
                if not frame.empty:
                    frame.index = pd.to_datetime(frame["t"], unit="ms", utc=True)
                    frame = frame.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
            else:
                if day < dt.datetime.now(zone).date() - dt.timedelta(days=59):
                    raise IntradayUnavailable("Yahoo cannot return this old session at 5m or 30m. Set up free Alpaca SIP or Massive in Data sources.")
                frame = yf.Ticker(symbol).history(start=day.isoformat(), end=(day + dt.timedelta(days=1)).isoformat(),
                    interval="5m", auto_adjust=False, actions=False, prepost=False, timeout=25, raise_errors=True)
        except Exception as exc:
            if isinstance(exc, IntradayUnavailable):
                raise
            # Never relay request URLs, headers, or API credentials to the browser/log.
            raise IntradayUnavailable(f"{source} download failed. Check connectivity and retry.") from None
        if frame.empty:
            raise IntradayUnavailable("No intraday bars returned for this symbol/session. It may be outside provider coverage.")
        frame = regular_session(frame[["Open", "High", "Low", "Close", "Volume"]], day).dropna()
        if frame.empty:
            raise IntradayUnavailable("No regular-session bars returned. No synthetic candles were added.")
        return frame, source
