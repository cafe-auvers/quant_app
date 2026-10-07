from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.core.board_workflow import ActivateForToday, BoardActionContext, CancelEntry
from src.core.execution_queue import ExecutionQueueItem, ExecutionQueueManager, OrbCandidate, build_orb_candidate
from src.core.orb_combinations import build_orb_position_combinations
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.risk.orb_position import (
    OrbSettings, calculate_orb_position_values, is_orb_position_plan_valid,
    score_orb_position_recommendation, validate_orb_position_values,
)
from src.risk.pre_trade import assess_orb_entry_candidate, orb_candidate_plan_id
from src.services import trade_card_repository as repository
from src.services.buyboard_runtime import _revalidate_and_approve
from src.services.execution_workflow_service import request_board_action
from src.services.operator_command_service import deserialize_board_command, serialize_board_command
from src.web.intraday_monitor import MonitorContext, evaluate_monitor_row


def _bars():
    index = pd.date_range("2026-10-07 09:30", periods=36, freq="min", tz="America/New_York")
    frame = pd.DataFrame({"Open": 99.0, "High": 100.0, "Low": 94.0, "Close": 99.5, "Volume": 1000}, index=index)
    frame.loc[index[-1], ["High", "Close"]] = 101.0
    return frame


def _candidate(is_ep=False):
    return build_orb_candidate(symbol="EPX", window="1m", intraday=_bars(),
        breakout_price=99.0, current_price=101.0, account_size=10_000,
        risk_percent=.01, adr_percent=5.0, is_ep=is_ep)


@pytest.mark.parametrize("ratio,valid", [(49.99, False), (50, True), (100, True), (150, True), (150.01, False)])
def test_ep_bounds_include_endpoints_and_allow_more_than_one_adr(ratio, valid):
    sizing = {"shares": 20, "capital_percent": 20, "stop_loss_percent": ratio / 100 * 5, "sl_adr": ratio}
    assert is_orb_position_plan_valid(sizing, 5, is_ep=True) is valid
    assert bool(validate_orb_position_values(sizing, 5, is_ep=True)) is not valid


@pytest.mark.parametrize("ratio,valid", [(14.99, False), (15, True), (65, True), (90, True), (90.01, False), (100, False), (150, False)])
def test_normal_profile_retains_its_own_bounds(ratio, valid):
    sizing = {"shares": 20, "capital_percent": 20, "stop_loss_percent": ratio / 100 * 5, "sl_adr": ratio}
    assert is_orb_position_plan_valid(sizing, 5) is valid


def test_ep_profile_changes_only_allowance_and_scoring():
    normal, ep = _candidate(), _candidate(True)
    assert not normal.valid and ep.valid
    assert ep.stop_adr == pytest.approx(120)
    assert normal.orb_high == ep.orb_high == 100
    assert normal.orb_low == ep.orb_low == 94
    assert normal.execution_price == ep.execution_price == 100
    assert normal.breakout_trigger == ep.breakout_trigger == 100
    assert calculate_orb_position_values(10_000, .01, 100, 94, 5)["shares"] == 17
    ep_sizing = {"capital_percent": 17.5, "sl_adr": 100}
    normal_sizing = {**ep_sizing, "sl_adr": 65}
    assert score_orb_position_recommendation(ep_sizing, .01, is_ep=True) > score_orb_position_recommendation(normal_sizing, .01, is_ep=True)
    assert score_orb_position_recommendation(normal_sizing, .01) > score_orb_position_recommendation(ep_sizing, .01)


def test_ep_settings_are_independent_and_legacy_settings_keep_normal_values():
    settings = OrbSettings.from_mapping({"stop_adr_min_percent": 15, "stop_adr_ideal_percent": 65, "stop_adr_max_percent": 90})
    assert settings.stop_adr_bounds() == (15, 65, 90)
    assert settings.stop_adr_bounds(True) == (50, 100, 150)
    custom = replace(settings, ep_stop_adr_min_percent=40, ep_stop_adr_ideal_percent=80, ep_stop_adr_max_percent=110)
    assert OrbSettings.from_mapping(custom.to_dict()) == custom
    sizing = {"shares": 20, "capital_percent": 20, "stop_loss_percent": 6, "sl_adr": 120}
    assert not is_orb_position_plan_valid(sizing, 5, custom, is_ep=True)
    assert settings.stop_adr_bounds() == custom.stop_adr_bounds()
    with pytest.raises(ValueError, match="EP"):
        replace(settings, ep_stop_adr_ideal_percent=160)


def test_ep_does_not_bypass_capital_bounds():
    sizing = {"shares": 20, "capital_percent": 31, "stop_loss_percent": 6, "sl_adr": 120}
    assert not is_orb_position_plan_valid(sizing, 5, is_ep=True)


def test_ep_queue_and_combinations_preserve_profile():
    manager = ExecutionQueueManager()
    item = SimpleNamespace(symbol="EPX", name="EP stock", breakout_price=99, stop_loss=None)
    queue = manager.build_or_update_from_watchlist_item(item, {window: _bars() for window in ("1m", "5m", "30m")},
        current_price=101, account_size=10_000, risk_percent=.01, adr_percent=5, is_ep=True)
    restored = ExecutionQueueItem.from_dict(queue.to_dict())
    assert restored.is_ep and all(candidate.is_ep for candidate in restored.candidates.values())
    combinations = build_orb_position_combinations(restored, account_equity=10_000)
    assert any(combination.valid and combination.stop_adr == pytest.approx(120) for combination in combinations)
    restored.is_ep = False
    assert not any(combination.valid for combination in build_orb_position_combinations(restored, account_equity=10_000))


def test_risk_boundary_and_card_revalidation_use_ep_profile():
    candidate = _candidate(True)
    candidate.status = type(candidate.status).EXECUTE_READY
    decision = assess_orb_entry_candidate(candidate, environment="PROD", account_no="1", symbol="EPX",
        quantity=candidate.shares, reference_price=100, plan_id=orb_candidate_plan_id(candidate))
    assert decision.approved
    card = TradeCardState(environment="PROD", account_no="1", symbol="EPX", is_ep=True, entry_orb_low=94, stop_adr=120)
    assert _revalidate_and_approve(card, quantity=17, limit_price=100, exchange="NASD", account_size=10_000).approved
    card.is_ep = False
    assert not _revalidate_and_approve(card, quantity=17, limit_price=100, exchange="NASD", account_size=10_000).approved


def test_mobile_monitor_uses_same_ep_allowance():
    context = MonitorContext(settings=OrbSettings(), equity=10_000)
    card = {"symbol": "EPX", "breakout_price": 99, "buy_today_member": True, "is_ep": True}
    kwargs = dict(now=_bars().index[-1].to_pydatetime(), context=context, adr=5)
    ep = evaluate_monitor_row(card, _bars(), **kwargs)
    normal = evaluate_monitor_row({**card, "is_ep": False}, _bars(), **kwargs)
    assert all(window["position_status"] == "PASS" for window in ep["orb"])
    assert all(window["position_status"] == "FAIL" for window in normal["orb"])


def test_publication_persists_ep_and_normal_republication_resets_it(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    engine = create_engine(f"sqlite:///{tmp_path / 'ep.db'}")
    card = repository.create_trade_card(engine, TradeCardState(environment="PROD", account_no="1", symbol="EPX",
        board_status=BoardStatus.BUYLIST, buylist_member=True, breakout_price=99))
    context = BoardActionContext(session_date=date(2026, 10, 7), local_operator_control=True)
    command = ActivateForToday(environment="PROD", account_no="1", symbol="EPX", expected_card_version=card.version, is_ep=True)
    payload = serialize_board_command(command)
    restored = deserialize_board_command(SimpleNamespace(payload=payload))
    assert restored.is_ep
    published = request_board_action(engine, restored, context=context).card
    assert published.board_status == BoardStatus.BUY_TODAY and published.is_ep
    assert TradeCardState.from_dict(published.to_dict()).is_ep
    cancelled = request_board_action(engine, CancelEntry(environment="PROD", account_no="1", symbol="EPX",
        expected_card_version=published.version), context=context).card
    normal = request_board_action(engine, ActivateForToday(environment="PROD", account_no="1", symbol="EPX",
        expected_card_version=cancelled.version), context=context).card
    assert not normal.is_ep and normal.board_status == BoardStatus.BUY_TODAY
    legacy = normal.to_dict()
    legacy.pop("is_ep")
    assert not TradeCardState.from_dict(legacy).is_ep
    assert not OrbCandidate.from_dict({"symbol": "EPX", "window": "1m"}).is_ep
