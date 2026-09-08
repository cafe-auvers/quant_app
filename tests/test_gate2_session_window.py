"""A doomed partial Gate-2 soak must stop before composing a live service."""

from argparse import Namespace
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from gate2 import reporting


@pytest.mark.parametrize("offset", [timedelta(minutes=-10), timedelta(0)])
def test_session_start_accepts_before_or_exactly_at_regular_open(offset):
    opened = datetime(2026, 9, 9, 13, 30, tzinfo=timezone.utc)

    bounds = reporting.validate_session_start(date(2026, 9, 9), opened + offset)

    assert bounds == (opened, datetime(2026, 9, 9, 20, tzinfo=timezone.utc))


def test_session_start_compares_aware_local_time_and_respects_early_close():
    local_open = datetime(2026, 11, 27, 23, 30, tzinfo=ZoneInfo("Asia/Seoul"))

    opened, closed = reporting.validate_session_start(date(2026, 11, 27), local_open)

    assert opened == datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
    assert closed == datetime(2026, 11, 27, 18, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(microseconds=1),
        timedelta(hours=2),
        timedelta(hours=6, minutes=30),
        timedelta(days=1),
    ],
)
def test_session_start_rejects_late_or_already_closed_regular_session(offset):
    opened = datetime(2026, 9, 9, 13, 30, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="cannot cover the full regular session"):
        reporting.validate_session_start(date(2026, 9, 9), opened + offset)


def test_session_start_rejects_already_closed_early_close_session():
    with pytest.raises(RuntimeError, match="next NYSE regular-session open"):
        reporting.validate_session_start(
            date(2026, 11, 27), datetime(2026, 11, 27, 18, tzinfo=timezone.utc)
        )


@pytest.mark.parametrize("session_day", [date(2026, 9, 7), date(2026, 9, 12)])
def test_session_start_rejects_holiday_or_weekend(session_day):
    with pytest.raises(ValueError, match="not an NYSE trading day"):
        reporting.validate_session_start(
            session_day, datetime(2026, 9, 1, tzinfo=timezone.utc)
        )


def test_session_start_requires_aware_now():
    with pytest.raises(ValueError, match="timezone-aware"):
        reporting.validate_session_start(date(2026, 9, 9), datetime(2026, 9, 9, 13))


@pytest.mark.parametrize("hour", [14, 20])
def test_late_soak_never_constructs_evidence_or_live_service(tmp_path, monkeypatch, hour):
    moment = datetime(2026, 9, 9, hour, tzinfo=timezone.utc)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(reporting, "datetime", FixedDatetime)
    monkeypatch.setattr(
        reporting, "_git", lambda _root, *args: "a" * 40 if args[0] == "rev-parse" else ""
    )
    monkeypatch.setattr(
        reporting,
        "runtime_activation_snapshot",
        lambda: dict(reporting.SAFE_RUNTIME_EXPECTATIONS),
    )
    monkeypatch.setattr(reporting.execution_config, "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY", 41)
    monkeypatch.setattr(reporting.execution_config, "BROKER_EVENT_STALE_SECONDS", 2.0)
    monkeypatch.setattr(reporting.execution_config, "LOCAL_RECEIVE_STALE_SECONDS", 2.0)
    monkeypatch.setenv("KIS_WS_HTS_ID", "test-hts-id")
    monkeypatch.setattr(
        reporting,
        "KisWsSymbolKeyStore",
        lambda: SimpleNamespace(
            snapshot=lambda: SimpleNamespace(last_error="", keys={"AAPL": "DNASAAPL"})
        ),
    )
    evidence_factory = Mock(
        side_effect=AssertionError("evidence must not be constructed")
    )
    service_factory = Mock(
        side_effect=AssertionError("live service must not be constructed")
    )
    manifest_loader = Mock(side_effect=AssertionError("manifest must not be loaded"))
    monkeypatch.setattr(reporting, "Gate2Evidence", evidence_factory)
    monkeypatch.setattr(
        reporting, "build_kis_realtime_market_data_from_environment", service_factory
    )
    monkeypatch.setattr(reporting, "load_verified_capability_manifest", manifest_loader)
    gate1_path = tmp_path / "gate1.json"
    gate1_path.write_text(
        json.dumps({"result": "PASSED", "commit_sha": "a" * 40}), encoding="utf-8"
    )
    args = Namespace(
        confirm_read_only=True,
        gate1_report=gate1_path,
        poll_seconds=0.25,
        watchdog_timeout_seconds=2,
        symbols="AAPL",
        session_date="2026-09-09",
    )

    with pytest.raises(RuntimeError, match="cannot cover the full regular session"):
        reporting.run_live_soak(args, tmp_path)

    evidence_factory.assert_not_called()
    service_factory.assert_not_called()
    manifest_loader.assert_not_called()
