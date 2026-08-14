"""Tests for the KIS-only execution-grade fallback split (spec section 21)."""
from __future__ import annotations

import pandas as pd
import pytest

from src.services import intraday_data_service as svc
from src.services.intraday_provider import (
    IntradayInterval,
    IntradayProviderName,
    IntradayRequest,
    IntradayResult,
)


def _request(allow_fallback=True):
    return IntradayRequest(symbol="AAPL", interval=IntradayInterval.ONE_MINUTE, allow_fallback=allow_fallback)


def _bars():
    return pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1]},
        index=pd.to_datetime(["2026-01-05 14:30:00"]),
    )


def test_raises_when_kis_disabled_even_if_fallback_allowed(monkeypatch):
    monkeypatch.setattr(svc, "is_kis_intraday_enabled", lambda: False)
    with pytest.raises(svc.ExecutionGradeDataUnavailableError):
        svc.fetch_execution_grade_intraday(_request(allow_fallback=True))


def test_raises_on_kis_failure_without_falling_back_to_yfinance(monkeypatch):
    monkeypatch.setattr(svc, "is_kis_intraday_enabled", lambda: True)

    def failing_fetch(request):
        raise RuntimeError("KIS down")

    monkeypatch.setattr(svc, "fetch_kis_intraday", failing_fetch)
    called = {"yfinance": False}
    monkeypatch.setattr(
        svc,
        "fetch_yfinance_intraday",
        lambda request: called.__setitem__("yfinance", True),
    )

    with pytest.raises(svc.ExecutionGradeDataUnavailableError):
        svc.fetch_execution_grade_intraday(_request(allow_fallback=True))
    assert called["yfinance"] is False


def test_raises_on_empty_kis_bars(monkeypatch):
    monkeypatch.setattr(svc, "is_kis_intraday_enabled", lambda: True)
    empty = IntradayResult(
        symbol="AAPL", interval="1m", source=IntradayProviderName.KIS, bars=pd.DataFrame()
    )
    monkeypatch.setattr(svc, "fetch_kis_intraday", lambda request: empty)

    with pytest.raises(svc.ExecutionGradeDataUnavailableError):
        svc.fetch_execution_grade_intraday(_request())


def test_returns_kis_result_when_available(monkeypatch):
    monkeypatch.setattr(svc, "is_kis_intraday_enabled", lambda: True)
    result = IntradayResult(
        symbol="AAPL", interval="1m", source=IntradayProviderName.KIS, bars=_bars()
    )
    monkeypatch.setattr(svc, "fetch_kis_intraday", lambda request: result)

    returned = svc.fetch_execution_grade_intraday(_request())
    assert returned.source == IntradayProviderName.KIS.value
    assert not returned.bars.empty


def test_display_fallback_path_unaffected_and_can_use_yfinance(monkeypatch):
    """fetch_intraday_with_fallback (charts/scanner) must keep its existing
    yfinance-fallback behavior -- this split is additive."""
    monkeypatch.setattr(svc, "is_kis_intraday_enabled", lambda: False)
    yfinance_result = IntradayResult(
        symbol="AAPL", interval="1m", source=IntradayProviderName.YFINANCE, bars=_bars()
    )
    monkeypatch.setattr(svc, "fetch_yfinance_intraday", lambda request: yfinance_result)

    returned = svc.fetch_intraday_with_fallback(_request(allow_fallback=True))
    assert returned.source == IntradayProviderName.YFINANCE.value
