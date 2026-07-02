"""Shared intraday provider contracts and OHLCV helpers."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import List

import pandas as pd


class IntradayProviderName(str, Enum):
    KIS = "kis"
    YFINANCE = "yfinance"
    NONE = "none"


class IntradayInterval(str, Enum):
    ONE_MINUTE = "1m"
    FIVE_MINUTE = "5m"


OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


@dataclass(frozen=True)
class IntradayRequest:
    symbol: str
    interval: IntradayInterval | str
    window_days: int = 7
    environment: str = "SIM"
    account_no: str = ""
    exchange: str = "NASD"
    allow_fallback: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", str(self.symbol or "").strip().upper())
        object.__setattr__(self, "interval", _interval_value(self.interval))
        object.__setattr__(self, "window_days", max(1, min(7, int(self.window_days or 7))))
        object.__setattr__(self, "environment", str(self.environment or "SIM").strip().upper())
        object.__setattr__(self, "account_no", str(self.account_no or "").strip())
        object.__setattr__(self, "exchange", str(self.exchange or "NASD").strip().upper())


@dataclass
class IntradayResult:
    symbol: str
    interval: IntradayInterval | str
    source: IntradayProviderName | str
    bars: pd.DataFrame
    exchange: str = "NASD"
    as_of: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol or "").strip().upper()
        self.interval = _interval_value(self.interval)
        self.source = _provider_value(self.source)
        self.exchange = str(self.exchange or "").strip().upper()
        self.bars = normalize_ohlcv_frame(self.bars)
        self.warnings = [str(warning) for warning in (self.warnings or []) if str(warning).strip()]


class IntradayProviderError(RuntimeError):
    """Base class for controlled intraday provider failures."""


def empty_intraday_result(
    request: IntradayRequest,
    source: IntradayProviderName | str = IntradayProviderName.NONE,
    warning: str = "",
) -> IntradayResult:
    warnings = [warning] if warning else []
    return IntradayResult(
        symbol=request.symbol,
        interval=request.interval,
        source=source,
        bars=pd.DataFrame(columns=OHLCV_COLUMNS),
        exchange=request.exchange,
        warnings=warnings,
    )


def normalize_ohlcv_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    normalized = frame.copy()
    rename_map = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    normalized = normalized.rename(columns={key: value for key, value in rename_map.items() if key in normalized.columns})
    for column in OHLCV_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = 0.0
    normalized = normalized[OHLCV_COLUMNS].apply(pd.to_numeric, errors="coerce").dropna(how="any")
    if normalized.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    normalized.index = pd.to_datetime(normalized.index)
    normalized = normalized[~normalized.index.duplicated(keep="last")].sort_index()
    return normalized


def resample_ohlcv_bars(frame: pd.DataFrame, interval: IntradayInterval | str) -> pd.DataFrame:
    normalized = normalize_ohlcv_frame(frame)
    if normalized.empty:
        return normalized
    interval_value = _interval_value(interval)
    if interval_value == IntradayInterval.ONE_MINUTE.value:
        return normalized
    if interval_value != IntradayInterval.FIVE_MINUTE.value:
        raise ValueError(f"Unsupported provider interval: {interval_value}")
    return (
        normalized.resample("5min")
        .agg(
            {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
        )
        .dropna(how="any")
    )


def _interval_value(value: IntradayInterval | str) -> str:
    if isinstance(value, IntradayInterval):
        return value.value
    return IntradayInterval(str(value or "").strip()).value


def _provider_value(value: IntradayProviderName | str) -> str:
    if isinstance(value, IntradayProviderName):
        return value.value
    raw = str(value or "").strip().lower()
    if not raw:
        return IntradayProviderName.NONE.value
    try:
        return IntradayProviderName(raw).value
    except ValueError:
        return raw
