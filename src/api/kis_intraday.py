"""KIS intraday market-data adapter for ORB workflows.

The ORB engine consumes normalized OHLCV bars. This module isolates KIS
endpoint details so dashboard, worker, and strategy code never depend on raw
KIS field names.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import logging
import os
from typing import Any, Dict, Iterable, Optional

import pandas as pd

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - requirements include python-dotenv.
    load_dotenv = None

from src.api.kis_fetch_all_daily import DEFAULT_US_EXCHANGES
from src.services.intraday_provider import normalize_ohlcv_frame


logger = logging.getLogger(__name__)

KIS_INTRADAY_ENABLED_KEY = "KIS_INTRADAY_ENABLED"
KIS_INTRADAY_ENDPOINT_KEY = "KIS_OVERSEAS_INTRADAY_ENDPOINT"
KIS_INTRADAY_TR_ID_KEY = "KIS_OVERSEAS_INTRADAY_TR_ID"
KIS_INTRADAY_OUTPUT_FIELD_KEY = "KIS_OVERSEAS_INTRADAY_OUTPUT_FIELD"
KIS_INTRADAY_PARAMS_JSON_KEY = "KIS_OVERSEAS_INTRADAY_PARAMS_JSON"

KIS_INTRADAY_FIELD_KEYS = {
    "time_field": "KIS_OVERSEAS_INTRADAY_TIME_FIELD",
    "open_field": "KIS_OVERSEAS_INTRADAY_OPEN_FIELD",
    "high_field": "KIS_OVERSEAS_INTRADAY_HIGH_FIELD",
    "low_field": "KIS_OVERSEAS_INTRADAY_LOW_FIELD",
    "close_field": "KIS_OVERSEAS_INTRADAY_CLOSE_FIELD",
    "volume_field": "KIS_OVERSEAS_INTRADAY_VOLUME_FIELD",
}


class KisIntradayError(RuntimeError):
    """Base class for controlled KIS intraday failures."""


class KisIntradayNotConfiguredError(KisIntradayError, NotImplementedError):
    """Raised when KIS intraday is disabled or missing verified mappings."""


@dataclass(frozen=True)
class IntradayFetchResult:
    symbol: str
    exchange: str
    bars: pd.DataFrame
    source: str = "kis"


class KisIntradayClient:
    """Fetch and normalize configured KIS intraday bars.

    KIS intraday endpoint values must be verified from official KIS
    documentation or a successful manual API test before enabling.
    """

    def __init__(self, client: Any):
        self.client = client

    def fetch_overseas_1m(
        self,
        symbol: str,
        trading_date: Optional[date] = None,
        exchanges: Iterable[str] = DEFAULT_US_EXCHANGES,
    ) -> IntradayFetchResult:
        if not is_kis_intraday_enabled():
            raise KisIntradayNotConfiguredError(
                "KIS intraday is disabled. Set KIS_INTRADAY_ENABLED=true only after endpoint/TR_ID/fields are verified."
            )

        config = _load_intraday_endpoint_config()
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            raise KisIntradayError("KIS intraday symbol is required.")

        last_error: Optional[Exception] = None
        for exchange in exchanges:
            exchange_code = str(exchange or "").strip().upper()
            params = _configured_params(symbol=symbol, exchange=exchange_code, trading_date=trading_date)
            try:
                data = self._get(config["endpoint"], tr_id=config["tr_id"], params=params)
                rows = _rows_from_output(data, config["output_field"])
                result = normalize_intraday_rows(
                    symbol=symbol,
                    exchange=exchange_code,
                    rows=rows,
                    time_field=config["time_field"],
                    open_field=config["open_field"],
                    high_field=config["high_field"],
                    low_field=config["low_field"],
                    close_field=config["close_field"],
                    volume_field=config["volume_field"],
                )
                if not result.bars.empty:
                    return result
                last_error = KisIntradayError(
                    f"KIS intraday returned no normalized rows for {symbol} on {exchange_code}."
                )
            except KisIntradayNotConfiguredError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "KIS intraday fetch failed for %s on %s using configured endpoint/TR_ID: %s",
                    symbol,
                    exchange_code,
                    exc,
                )

        raise KisIntradayError(f"KIS intraday fetch failed for {symbol}: {last_error}")

    def _get(self, endpoint: str, tr_id: str, params: Dict[str, str]) -> Dict[str, Any]:
        if hasattr(self.client, "_get"):
            return self.client._get(endpoint, tr_id=tr_id, params=params)
        if hasattr(self.client, "get"):
            return self.client.get(endpoint, tr_id=tr_id, params=params)
        raise KisIntradayError("KIS client does not expose a compatible GET method.")


def is_kis_intraday_enabled() -> bool:
    _load_dotenv_once()
    return str(os.environ.get(KIS_INTRADAY_ENABLED_KEY, "")).strip().lower() in {"1", "true", "yes", "on"}


def normalize_intraday_rows(
    symbol: str,
    exchange: str,
    rows: Iterable[Dict[str, object]],
    time_field: str,
    open_field: str,
    high_field: str,
    low_field: str,
    close_field: str,
    volume_field: str,
) -> IntradayFetchResult:
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            timestamp = pd.to_datetime(str(row[time_field]))
            records.append(
                {
                    "timestamp": timestamp,
                    "Open": float(str(row[open_field]).replace(",", "")),
                    "High": float(str(row[high_field]).replace(",", "")),
                    "Low": float(str(row[low_field]).replace(",", "")),
                    "Close": float(str(row[close_field]).replace(",", "")),
                    "Volume": float(str(row.get(volume_field, 0) or 0).replace(",", "")),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    frame = pd.DataFrame(records)
    if frame.empty:
        bars = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    else:
        bars = normalize_ohlcv_frame(
            frame.drop_duplicates(subset=["timestamp"]).set_index("timestamp").sort_index()
        )

    return IntradayFetchResult(
        symbol=symbol.upper(),
        exchange=exchange,
        bars=bars,
        source="kis",
    )


def _load_intraday_endpoint_config() -> Dict[str, str]:
    _load_dotenv_once()
    endpoint = _required_env(KIS_INTRADAY_ENDPOINT_KEY)
    tr_id = _required_env(KIS_INTRADAY_TR_ID_KEY)
    config = {
        "endpoint": endpoint,
        "tr_id": tr_id,
        "output_field": os.environ.get(KIS_INTRADAY_OUTPUT_FIELD_KEY, "output2").strip() or "output2",
    }
    missing_fields = []
    for config_name, env_key in KIS_INTRADAY_FIELD_KEYS.items():
        value = os.environ.get(env_key, "").strip()
        if not value:
            missing_fields.append(env_key)
        else:
            config[config_name] = value
    if missing_fields:
        raise KisIntradayNotConfiguredError(
            "KIS intraday field mapping is incomplete. Missing: " + ", ".join(missing_fields)
        )
    return config


def _required_env(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        raise KisIntradayNotConfiguredError(f"KIS intraday configuration is missing {key}.")
    return value


def _configured_params(symbol: str, exchange: str, trading_date: Optional[date]) -> Dict[str, str]:
    raw = os.environ.get(KIS_INTRADAY_PARAMS_JSON_KEY, "").strip()
    if not raw:
        return {}
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KisIntradayNotConfiguredError(f"{KIS_INTRADAY_PARAMS_JSON_KEY} is not valid JSON: {exc}") from exc
    if not isinstance(params, dict):
        raise KisIntradayNotConfiguredError(f"{KIS_INTRADAY_PARAMS_JSON_KEY} must be a JSON object.")
    placeholders = {
        "symbol": symbol,
        "exchange": exchange,
        "date": trading_date.strftime("%Y%m%d") if trading_date else "",
    }
    configured = {}
    for key, value in params.items():
        configured[str(key)] = str(value).format(**placeholders)
    return configured


def _rows_from_output(data: Dict[str, Any], output_field: str) -> Iterable[Dict[str, Any]]:
    output = data.get(output_field)
    if output is None:
        output = data.get("output")
    if isinstance(output, list):
        return [row for row in output if isinstance(row, dict)]
    if isinstance(output, dict):
        return [output]
    return []


def _load_dotenv_once() -> None:
    if load_dotenv is not None:
        load_dotenv()
