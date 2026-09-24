from __future__ import annotations

from argparse import Namespace
from datetime import datetime
from types import SimpleNamespace

from gate4 import runner


class _Journal:
    def read_all(self):
        return [
            SimpleNamespace(
                event_type="CAPABILITY_EVIDENCE_VERIFIED",
                payload={"verified": True},
            )
        ]


class _Collector:
    def __init__(self):
        self.journal = _Journal()
        self.recorded = []

    def record(self, event_type, **payload):
        self.recorded.append((event_type, payload))


def test_start_session_reads_live_control_from_coordination_store(
    monkeypatch, tmp_path
):
    collector = _Collector()
    engine = SimpleNamespace(dispose=lambda: None)
    calls = []
    session_date = datetime.now(runner.US_MARKET_ZONE).date()

    monkeypatch.setattr(runner, "_exact_release", lambda path: ("a" * 40, {}))
    monkeypatch.setattr(runner, "_collector", lambda path, commit: collector)
    monkeypatch.setattr(runner, "is_nyse_trading_day", lambda value: True)
    monkeypatch.setattr(
        runner,
        "init_coordination_engine",
        lambda **kwargs: calls.append(kwargs) or engine,
    )
    monkeypatch.setattr(
        runner,
        "get_live_trading_control",
        lambda value: SimpleNamespace(
            success=True,
            control=SimpleNamespace(enabled=False),
            error="",
        ),
    )

    result = runner.start_session(
        Namespace(
            gate3_report=tmp_path / "gate3.json",
            journal=tmp_path / "gate4.jsonl",
            session_date=session_date.isoformat(),
            supervisor="owner-operator",
        )
    )

    assert result == 0
    assert calls == [{"ensure_schema": False}]
    assert collector.recorded == [
        (
            "SESSION_STARTED",
            {
                "session_date": session_date.isoformat(),
                "supervised": True,
                "supervisor": "owner-operator",
                "armed": False,
            },
        )
    ]
