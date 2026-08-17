from __future__ import annotations

import datetime as dt
import json
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
        "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY=0",
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


def test_ws0_contract_explicitly_allows_only_inactive_provisional_adapters():
    contract = (ROOT / "docs" / "kanban_production_readiness.md").read_text(
        encoding="utf-8"
    )
    matrix = (ROOT / "docs" / "kis_capability_matrix.md").read_text(
        encoding="utf-8"
    )

    assert "revision 3.4 amendment recorded" in contract
    assert "May be written provisionally before evidence" in contract
    assert "KIS_WS_PROTOCOL_VERIFIED=true or a live connection/subscription" in contract
    assert "non-zero production/simulation channel capacity" in contract
    assert "provisional D1/D3/D11 adapter may be implemented inactive" in matrix


def test_ws0_credentialed_capacity_evidence_matches_fail_closed_runtime_contract():
    fixture_dir = ROOT / "tests" / "fixtures" / "kis_protocol"
    capacity = json.loads(
        (fixture_dir / "ws0_20260817_subscription_capacity.json").read_text(
            encoding="utf-8"
        )
    )
    acknowledgements = json.loads(
        (fixture_dir / "ws0_20260817_subscription_acks.json").read_text(
            encoding="utf-8"
        )
    )
    simulated_rejection = json.loads(
        (fixture_dir / "ws0_20260817_sim_mutation_rejection.json").read_text(
            encoding="utf-8"
        )
    )

    assert capacity["broker_mutations"] == 0
    assert capacity["accepted_registrations"] == 41
    assert capacity["first_rejection"] == {
        "ordinal": 42,
        "tr_id": "HDFSASP0",
        "tr_key": "DNASBKNG",
        "rt_cd": "1",
        "msg_cd": "OPSP0008",
        "msg1": "MAX SUBSCRIBE OVER",
    }
    assert acknowledgements["broker_mutations"] == 0
    assert simulated_rejection["submit"]["msg_cd"] == "40100000"
    assert not simulated_rejection["submit"]["accepted"]
    assert simulated_rejection["open_order_check"]["matching_probe_order_count"] == 0
    assert simulated_rejection["safety"]["production_endpoints_called"] == 0
    assert execution_config.KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY == 0


def test_ws0_committed_evidence_contains_no_credential_or_account_material():
    fixture_dir = ROOT / "tests" / "fixtures" / "kis_protocol"
    forbidden = (
        "approval_key",
        "access_token",
        "appsecret",
        "authorization",
        "account_no",
        "acnt_no",
        "cano",
    )
    for path in sorted(fixture_dir.glob("ws0_*.json")):
        text = path.read_text(encoding="utf-8").lower()
        assert not any(token in text for token in forbidden), path.name
