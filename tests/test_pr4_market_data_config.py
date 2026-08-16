from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from src.utils.market_calendar import (
    is_regular_session_open,
    nyse_regular_session_close_time,
    seconds_until_regular_session_close,
)
from src.services.kis_realtime_market_data import (
    build_kis_realtime_market_data_from_environment,
)
from src.core import execution_config


ROOT = Path(__file__).resolve().parents[1]


def test_pr4_market_data_configuration_is_present_and_fail_closed():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for name in (
        "KIS_PROD_WS_URL",
        "KIS_SIM_WS_URL",
        "KIS_WS_ENABLED=false",
        "KIS_WS_PROTOCOL_VERIFIED=false",
        "KIS_WS_HTS_ID",
        "KIS_MARKET_DATA_MODE=REST_DISPLAY_ONLY",
        "BROKER_EVENT_STALE_SECONDS",
        "LOCAL_RECEIVE_STALE_SECONDS",
        "MAX_MARKET_DATA_QUEUE_DELAY_SECONDS",
        "KIS_WS_RAW_CAPTURE_ENABLED=false",
        "BUYBOARD_ENGINE_ENABLED=false",
    ):
        assert name in env_example
    assert "websockets==15.0.1" in requirements


def test_early_close_is_used_for_market_session_decisions():
    # 2026-11-27 is the Friday after US Thanksgiving.
    eastern = ZoneInfo("America/New_York")
    before = dt.datetime(2026, 11, 27, 12, 59, tzinfo=eastern)
    after = dt.datetime(2026, 11, 27, 13, 1, tzinfo=eastern)

    assert nyse_regular_session_close_time(before.date()) == dt.time(13, 0)
    assert is_regular_session_open(before)
    assert not is_regular_session_open(after)
    assert seconds_until_regular_session_close(before) == 60


def test_live_factory_requires_both_enable_and_protocol_verification(monkeypatch):
    monkeypatch.setattr(execution_config, "KIS_WS_ENABLED", True)
    monkeypatch.setattr(execution_config, "KIS_WS_PROTOCOL_VERIFIED", False)

    try:
        build_kis_realtime_market_data_from_environment()
    except RuntimeError as exc:
        assert "Workstream 0" in str(exc)
    else:
        raise AssertionError("unverified KIS protocol must fail closed")
