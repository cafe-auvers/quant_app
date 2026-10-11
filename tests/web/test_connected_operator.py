from __future__ import annotations

import json
import platform
import uuid
import pytest
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from src.core.trade_card_state import BoardStatus, PositionRuntimeStatus, StopType, TradeCardState
from src.core.runtime_readiness import RuntimeDeviceState
from src.core.execution_ownership import ExecutionOwner
from src.services import trade_card_repository
from src.services import state_sync as ss
from src.services.operator_command_service import process_next_board_operator_command
from src.services.operator_commands import OperatorCommandStatus
from src.services.runtime_device_state_repository import save_runtime_device_state
from src.services.execution_ownership_repository import get_ownership
from src.services.state_sync import (
    LocalDeviceRole,
    claim_main_device,
    get_operator_control,
    save_local_device_role,
    set_operator_control,
)
from src.web.canonical_planning import CanonicalPlanningSource
from src.web.connected_operator import ConnectedOperatorService
from src.web.connected_planning import ConnectedPlanningService
from src.web.api import create_api_app

from .conftest import login


def _service(web_config, tmp_path, *, same_executor: bool = True):
    engine = create_engine(f"sqlite:///{tmp_path / 'operator.db'}", future=True)
    trade_card_repository.ensure_trade_cards_table(engine)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    role = LocalDeviceRole("operator-pc", platform.node(), False)
    save_local_device_role(role, path=data_dir / "device_role.json")
    executor = role if same_executor else LocalDeviceRole("executor-pc", "EXECUTOR", True)
    assert claim_main_device(engine, executor).success
    assert set_operator_control(engine, executor, role).success
    config = replace(
        web_config,
        mode="CONNECTED",
        canonical_planning_reads=True,
        canonical_planning_writes=True,
        connected_passive_operations=("set_breakout",),
        connected_operator_operations=(
            "move_watchlist",
            "remove_buylist",
            "move_buylist",
            "activate_buy_today",
            "deactivate_buy_today",
            "cancel_entry",
            "request_partial_sell",
            "cancel_partial_sell",
            "request_sell_all",
            "cancel_sell_all",
            "set_orb_stop",
            "set_breakeven_stop",
            "set_manual_stop",
            "reorder_card",
            "publish_today_plan",
            "update_orb_settings",
        ),
        canonical_account_no="account-a",
        pc_repository_path=str(tmp_path),
    )
    source = CanonicalPlanningSource(
        enabled=True,
        environment="PROD",
        account_no="account-a",
        engine=engine,
        cache_seconds=0,
    )
    planning = ConnectedPlanningService(
        config, source, engine=engine, unavailable_reason=""
    )
    return engine, role, ConnectedOperatorService(config, source, planning)


def _buylist_card(engine):
    return trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="account-a",
            symbol="AAPL",
            name="Apple",
            board_status=BoardStatus.BUYLIST,
            buylist_member=True,
            breakout_price=201.25,
        ),
    )


@pytest.mark.parametrize("same_executor", [True, False])
@pytest.mark.parametrize("endpoint", ["planning", "board"])
def test_ep_publication_api_persists_on_shared_card_and_replay_checks_profile(
    web_config, services, tmp_path, monkeypatch, same_executor, endpoint
):
    engine, _role, operator = _service(web_config, tmp_path, same_executor=same_executor)
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    card = _buylist_card(engine)
    services.config = operator.config
    services.canonical_planning = operator.source
    services.connected_planning = operator.planning
    services.connected_operator = operator
    with TestClient(create_api_app(operator.config, services=services), base_url="http://localhost:8080") as phone:
        session = login(phone)
        phone.headers.update({"Origin": "http://localhost:8080", "X-CSRF-Token": session["csrf_token"]})
        assert phone.post("/api/v1/operator/control", json={"target": "mobile"}).status_code == 200
        payload = {"command_id": str(uuid.uuid4()), "expected_revision": card.version, "is_ep": True}
        if endpoint == "planning":
            path = "/api/v1/planning/AAPL/activate-buy-today"
            payload["enabled"] = True
        else:
            path = "/api/v1/operator/board-actions"
            payload.update(action="activate_buy_today", symbol="AAPL")
        response = phone.post(path, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["card"]["is_ep"] is True
        assert response.json()["broker_order_placed"] is False
        stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
        assert stored.is_ep and stored.board_status == BoardStatus.BUY_TODAY
        assert phone.get("/api/v1/planning/AAPL").json()["card"]["is_ep"]
        board = phone.get("/api/v1/buyboard").json()
        assert board["rows"][0]["is_ep"] and "EP" not in board["columns"]
        assert phone.post(path, json=payload).status_code == 200
        assert phone.post(path, json={**payload, "is_ep": False}).status_code == 409


def test_custom_ep_settings_survive_old_client_normal_settings_save(web_config, services, tmp_path):
    engine, _role, operator = _service(web_config, tmp_path)
    services.config = operator.config
    services.canonical_planning = operator.source
    services.connected_planning = operator.planning
    services.connected_operator = operator
    with TestClient(create_api_app(operator.config, services=services), base_url="http://localhost:8080") as phone:
        session = login(phone)
        phone.headers.update({"Origin": "http://localhost:8080", "X-CSRF-Token": session["csrf_token"]})
        phone.post("/api/v1/operator/control", json={"target": "mobile"})
        initial = phone.get("/api/v1/operator/orb-settings").json()
        payload = {**initial["settings"], "command_id": str(uuid.uuid4()), "expected_revision": initial["revision"],
            "ep_stop_adr_min_percent": 40, "ep_stop_adr_ideal_percent": 90, "ep_stop_adr_max_percent": 140}
        response = phone.put("/api/v1/operator/orb-settings", json=payload)
        assert response.status_code == 200, response.text
        legacy_payload = {key: value for key, value in response.json()["settings"].items() if not key.startswith("ep_")}
        legacy_payload.update(command_id=str(uuid.uuid4()), expected_revision=response.json()["revision"], stop_adr_max_percent=90)
        saved = phone.put("/api/v1/operator/orb-settings", json=legacy_payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["settings"]["ep_stop_adr_max_percent"] == 140
        assert saved.json()["settings"]["ep_stop_adr_ideal_percent"] == 90
        assert saved.json()["settings"]["ep_stop_adr_min_percent"] == 40
        assert saved.json()["settings"]["stop_adr_max_percent"] == 90
        invalid = {**payload, "command_id": str(uuid.uuid4()), "expected_revision": saved.json()["revision"], "ep_stop_adr_ideal_percent": 160}
        assert phone.put("/api/v1/operator/orb-settings", json=invalid).status_code == 422


@pytest.mark.parametrize("same_executor", [True, False])
def test_mobile_reactivates_a_fully_closed_stock_with_original_breakout(
    web_config, tmp_path, monkeypatch, same_executor
):
    engine, _role, service = _service(web_config, tmp_path, same_executor=same_executor)
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    card = _buylist_card(engine)
    card.board_status = BoardStatus.CLOSED
    card.position_runtime_status = PositionRuntimeStatus.CLOSED
    card.average_entry_price = 200.0
    card = trade_card_repository.update_trade_card(engine, card, expected_version=card.version)
    service.set_operator_control_target("mobile")
    result = service.set_buy_today(command_id=str(uuid.uuid4()), symbol="AAPL",
        expected_revision=card.version, enabled=True)
    assert result["queued"] is False
    assert result["card"]["canonical_stage"] == "BUY_TODAY"
    assert result["card"]["breakout_price"] == 201.25
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
    assert stored.position_runtime_status == PositionRuntimeStatus.NONE
    assert stored.average_entry_price == 0 and stored.planned_quantity == 0


@pytest.mark.parametrize("same_executor", [True, False])
def test_mobile_withdraws_unsubmitted_regular_exit_directly_and_preserves_stop(
    web_config, tmp_path, monkeypatch, same_executor
):
    engine, _role, service = _service(web_config, tmp_path, same_executor=same_executor)
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    card = trade_card_repository.create_trade_card(engine, TradeCardState(
        environment="PROD", account_no="account-a", symbol="SVIA",
        board_status=BoardStatus.SELL_ALL, position_runtime_status=PositionRuntimeStatus.LIQUIDATING,
        broker_quantity=643, orderable_quantity=643, average_entry_price=4.35,
        stop_type=StopType.ORB_LOW, active_stop_price=4.24, stop_quantity=643,
        exit_all_required=True, exit_attempt_count=3, last_exit_error="retry limit reached",
    ))
    before = service.board_snapshot(force=True)["rows"][0]
    assert before["can_cancel_sell_all"] is True and before["sell_all_at_market_open"] is False
    result = service.apply_board_action(
        command_id=str(uuid.uuid4()), action="cancel_sell_all", symbol="SVIA",
        expected_revision=card.version,
    )
    assert result["queued"] is False and result["command_status"] == "COMPLETED"
    assert result["broker_order_placed"] is False
    assert result["card"]["board_status"] == "OPEN_POSITION"
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "SVIA")
    assert stored.broker_quantity == stored.stop_quantity == 643 and stored.active_stop_price == 4.24
    assert stored.exit_attempt_count == 0 and stored.last_exit_error == ""


def test_mobile_cannot_withdraw_reserved_sell_all(web_config, tmp_path, monkeypatch):
    from src.web.store import ConflictError

    engine, _role, service = _service(web_config, tmp_path, same_executor=False)
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    card = trade_card_repository.create_trade_card(engine, TradeCardState(
        environment="PROD", account_no="account-a", symbol="SVIA",
        board_status=BoardStatus.SELL_ALL, broker_quantity=643,
        exit_all_required=True, reserved_sell_quantity=643,
    ))
    assert service.board_snapshot(force=True)["rows"][0]["can_cancel_sell_all"] is False
    with pytest.raises(ConflictError, match="unsubmitted Sell All"):
        service.apply_board_action(command_id=str(uuid.uuid4()), action="cancel_sell_all",
                                   symbol="SVIA", expected_revision=card.version)
    assert trade_card_repository.get_trade_card(engine, "PROD", "account-a", "SVIA").version == card.version


def test_operator_change_pulse_notifies_pc_listener(
    web_config, tmp_path, monkeypatch
):
    _engine, _role, service = _service(web_config, tmp_path)
    calls = []
    from src.services import pc_remote_control

    monkeypatch.setattr(
        pc_remote_control,
        "notify_pc_coordination_change",
        lambda event_id, **kwargs: calls.append((event_id, kwargs)) or True,
    )

    assert service._publish_change_pulse("settings-1", "app_state_sync") is True
    assert calls == [
        (
            "web-operator:settings-1",
            {
                "changed_tables": ("app_state_sync",),
                "protocol_version": 3,
                "timeout": 0.25,
            },
        )
    ]


def test_mobile_operator_direct_buy_today_round_trip(web_config, tmp_path, monkeypatch):
    engine, _role, service = _service(web_config, tmp_path, same_executor=True)
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    card = _buylist_card(engine)

    activated = service.set_buy_today(
        command_id=str(uuid.uuid4()),
        symbol="AAPL",
        expected_revision=card.version,
        enabled=True,
    )
    assert activated["published"] is True
    assert activated["queued"] is False
    assert activated["broker_order_placed"] is False
    assert activated["card"]["canonical_stage"] == "BUY_TODAY"
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
    assert stored.board_status == BoardStatus.BUY_TODAY
    ownership = get_ownership(
        engine, environment="PROD", account_no="account-a", symbol="AAPL"
    )
    assert ownership.owner == ExecutionOwner.KANBAN

    removed = service.set_buy_today(
        command_id=str(uuid.uuid4()),
        symbol="AAPL",
        expected_revision=stored.version,
        enabled=False,
    )
    assert removed["published"] is False
    assert removed["queued"] is False
    assert removed["card"]["canonical_stage"] == "BUYLIST"
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
    assert stored.board_status == BoardStatus.BUYLIST


def test_mobile_operator_commits_buy_today_intent_with_a_separate_execution_owner(
    web_config, tmp_path, monkeypatch
):
    engine, _role, service = _service(web_config, tmp_path, same_executor=False)
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    card = _buylist_card(engine)

    result = service.set_buy_today(
        command_id=str(uuid.uuid4()),
        symbol="AAPL",
        expected_revision=card.version,
        enabled=True,
    )
    assert result["queued"] is False
    assert result["published"] is True
    assert result["command_status"] == "COMPLETED"
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
    assert stored.board_status == BoardStatus.BUY_TODAY


@pytest.mark.parametrize("action", ["deactivate_buy_today", "cancel_entry"])
def test_entry_cancellation_uses_current_revision_and_retries_runtime_race(
    web_config, tmp_path, monkeypatch, action
):
    from src.services import execution_workflow_service

    engine, _role, service = _service(web_config, tmp_path, same_executor=False)
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    original = _buylist_card(engine)
    card = replace(original, board_status=BoardStatus.BUY_TODAY)
    card = trade_card_repository.update_trade_card(engine, card, expected_version=card.version)
    request = execution_workflow_service.request_board_action
    attempts = []

    def racing_request(engine, command, **kwargs):
        attempts.append(command.expected_card_version)
        if len(attempts) == 1:
            live = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
            live.entry_block_reason = "live ORB observation"
            trade_card_repository.update_trade_card(engine, live, expected_version=live.version)
        return request(engine, command, **kwargs)

    monkeypatch.setattr(execution_workflow_service, "request_board_action", racing_request)
    result = service.apply_board_action(command_id=str(uuid.uuid4()), action=action,
        symbol="AAPL", expected_revision=original.version)
    assert attempts == [card.version, card.version + 1]
    assert result["queued"] is False and result["broker_order_placed"] is False
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
    assert stored.board_status == BoardStatus.BUYLIST
    assert stored.breakout_price == original.breakout_price


@pytest.mark.parametrize("stage,filled_quantity", [
    (BoardStatus.ENTRY_PENDING, 0), (BoardStatus.ENTRY_PENDING, 3), (BoardStatus.OPEN_POSITION, 3),
])
def test_mobile_cancels_remaining_entry_and_preserves_confirmed_fills(
    web_config, tmp_path, monkeypatch, filled_quantity, stage
):
    engine, _role, service = _service(web_config, tmp_path, same_executor=False)
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    card = trade_card_repository.create_trade_card(engine, TradeCardState(
        environment="PROD", account_no="account-a", symbol="AAPL", buylist_member=True,
        board_status=stage, breakout_price=201.25,
        entry_client_order_id="working-buy", entry_remaining_target_quantity=7,
        broker_quantity=filled_quantity, orderable_quantity=filled_quantity,
        average_entry_price=202 if filled_quantity else 0,
        stop_quantity=filled_quantity, active_stop_price=199, stop_type=StopType.ORB_LOW,
    ))
    result = service.set_buy_today(command_id=str(uuid.uuid4()), symbol="AAPL",
        expected_revision=card.version - 1, enabled=False)
    assert result["queued"] is False and result["broker_order_placed"] is False
    assert result["status"] == "ENTRY CANCELLATION REQUESTED"
    assert result["card"]["entry_cancellation_pending"] is True
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
    assert stored.board_status == stage
    assert stored.entry_block_reason == "cancel_requested"
    assert stored.broker_quantity == stored.stop_quantity == filled_quantity
    assert stored.active_stop_price == 199 and stored.entry_client_order_id == "working-buy"
    again = service.set_buy_today(command_id=str(uuid.uuid4()), symbol="AAPL",
        expected_revision=card.version, enabled=False)
    assert again["status"] == "ENTRY CANCELLATION REQUESTED"
    assert trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL").version == stored.version


def test_mobile_cancels_unsubmitted_completion_without_removing_position_protection(
    web_config, tmp_path, monkeypatch
):
    engine, _role, service = _service(web_config, tmp_path)
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    card = trade_card_repository.create_trade_card(engine, TradeCardState(
        environment="PROD", account_no="account-a", symbol="AAPL", buylist_member=True,
        board_status=BoardStatus.OPEN_POSITION, position_runtime_status=PositionRuntimeStatus.ENTRY_COMPLETING,
        breakout_price=201.25, entry_remaining_target_quantity=7,
        broker_quantity=3, orderable_quantity=3, average_entry_price=202,
        stop_quantity=3, active_stop_price=199, stop_type=StopType.ORB_LOW,
    ))
    result = service.set_buy_today(command_id=str(uuid.uuid4()), symbol="AAPL",
        expected_revision=0, enabled=False)
    assert result["card"]["entry_cancellation_pending"] is False
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
    assert stored.board_status == BoardStatus.OPEN_POSITION and stored.entry_remaining_target_quantity == 0
    assert stored.position_runtime_status == PositionRuntimeStatus.OPEN
    assert stored.broker_quantity == stored.stop_quantity == 3 and stored.active_stop_price == 199


def test_mobile_cancellation_is_idempotent_and_does_not_sell_a_completed_entry(
    web_config, tmp_path, monkeypatch
):
    from src.web.store import ConflictError

    engine, _role, service = _service(web_config, tmp_path)
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    card = _buylist_card(engine)
    result = service.set_buy_today(command_id=str(uuid.uuid4()), symbol="AAPL",
        expected_revision=0, enabled=False)
    assert result["status"] == "REMOVED FROM TODAY"
    assert trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL").version == card.version
    card.board_status = BoardStatus.OPEN_POSITION
    card.broker_quantity = card.stop_quantity = 10
    card.active_stop_price = 199
    card = trade_card_repository.update_trade_card(engine, card, expected_version=card.version)
    with pytest.raises(ConflictError, match="confirmed holdings are preserved"):
        service.set_buy_today(command_id=str(uuid.uuid4()), symbol="AAPL",
            expected_revision=0, enabled=False)
    assert trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL").version == card.version


def test_mobile_can_select_mobile_pc_and_laptop_operator_control(
    web_config, tmp_path
):
    engine, role, service = _service(web_config, tmp_path, same_executor=True)
    save_runtime_device_state(
        engine,
        device_id=role.device_id,
        hostname=role.hostname,
        state=RuntimeDeviceState.ACTIVE,
        details={"device_kind": "PC", "main_py_alive": True},
    )
    laptop = LocalDeviceRole("operator-laptop", "TRADING-LAPTOP", False)
    save_runtime_device_state(
        engine,
        device_id=laptop.device_id,
        hostname=laptop.hostname,
        state=RuntimeDeviceState.STANDBY_READY,
        details={"device_kind": "Laptop", "main_py_alive": True},
    )

    targets = {row["key"]: row for row in service.operator_control_targets()}
    assert targets["pc"]["available"] is True
    assert targets["laptop"]["available"] is True
    assert targets["mobile"]["available"] is True

    mobile_status = service.set_operator_control_target("mobile")
    assert mobile_status["delegated"] is True
    assert mobile_status["operator_control"] == "MOBILE"
    assert get_operator_control(engine).control.hostname == "Mobile Web"

    laptop_status = service.set_operator_control_target("laptop")
    assert laptop_status["delegated"] is False
    assert laptop_status["operator_control"] == "TRADING-LAPTOP"
    assert get_operator_control(engine).control.device_id == laptop.device_id

    pc_status = service.set_operator_control_target("pc")
    assert pc_status["delegated"] is True
    assert pc_status["operator_control"] == "PC"
    assert get_operator_control(engine).control.device_id == role.device_id


def test_mobile_operator_updates_one_shared_orb_settings_revision(
    web_config, tmp_path
):
    engine, _role, service = _service(web_config, tmp_path, same_executor=True)
    service.set_operator_control_target("mobile")

    initial = service.orb_settings_snapshot()
    assert initial["settings"]["capital_max_percent"] == 30.0
    assert initial["revision"] == 0

    from src.risk.orb_position import OrbSettings

    updated = service.update_orb_settings(
        expected_revision=initial["revision"],
        settings=OrbSettings(
            capital_min_percent=8.0,
            capital_ideal_percent=16.0,
            capital_max_percent=32.0,
            stop_adr_min_percent=12.0,
            stop_adr_ideal_percent=60.0,
            stop_adr_max_percent=70.0,
        ),
    )

    assert updated["revision"] == 1
    assert updated["settings"]["capital_max_percent"] == 32.0
    assert updated["updated_by"] == "Mobile Web"


def test_mobile_plan_publish_uses_guarded_full_snapshot(
    web_config, tmp_path, monkeypatch
):
    engine, _role, service = _service(web_config, tmp_path, same_executor=True)
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    _buylist_card(engine)
    monkeypatch.setattr(
        "src.web.connected_operator.is_regular_session_open", lambda: False
    )
    data_dir = tmp_path / "data"
    documents = {
        "watchlist.json": {"items": []},
        "buylist.json": {"items": []},
        "trade_plans.json": {"plans": []},
        "execution_queue.json": {"items": {}},
        "scanner_setups.json": {"setups": []},
        "settings.json": {"orb_settings": {}},
    }
    for name, value in documents.items():
        (data_dir / name).write_text(json.dumps(value), encoding="utf-8")

    result = service.publish_today_plan(command_id=str(uuid.uuid4()))

    assert result["status"] == "TODAY'S PLAN PUBLISHED"
    assert result["broker_order_placed"] is False
    assert set(result["revisions"]) == {
        "watchlist",
        "buylist",
        "trade_plans",
        "execution_queue",
        "scanner_setups",
        "settings",
    }


def test_mobile_buyboard_remove_buylist_preserves_non_watchlist_state(
    web_config, tmp_path, monkeypatch
):
    engine, _role, service = _service(web_config, tmp_path, same_executor=False)
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    card = _buylist_card(engine)

    snapshot = service.board_snapshot(force=True)
    assert snapshot["rows"][0]["board_status"] == BoardStatus.BUYLIST.value
    assert snapshot["rows"][0]["breakout_price"] == 201.25

    result = service.apply_board_action(
        command_id=str(uuid.uuid4()),
        action="remove_buylist",
        symbol="AAPL",
        expected_revision=card.version,
    )

    assert result["queued"] is False
    assert result["card"] is None
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.watchlist_member is False
    assert stored.buylist_member is False
    assert stored.breakout_price == 201.25


def test_mobile_removes_untouched_flat_legacy_blkb_without_publishing_entry_intent(
    web_config, tmp_path, monkeypatch
):
    engine, _role, service = _service(web_config, tmp_path, same_executor=False)
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", tmp_path / "cards.json")
    card = trade_card_repository.create_trade_card(engine, TradeCardState(
        environment="PROD", account_no="account-a", symbol="BLKB",
        board_status=BoardStatus.BUYLIST, buylist_member=True, breakout_price=45.6,
        stop_type=StopType.MANUAL_PRICE, active_stop_price=44.54,
        warnings=["migrated_from_buylist"],
    ))
    result = service.apply_board_action(
        command_id=str(uuid.uuid4()), action="remove_buylist", symbol="BLKB",
        expected_revision=card.version,
    )
    assert result["card"] is None
    assert result["queued"] is False
    assert result["executable_intent"] is False
    assert result["broker_order_placed"] is False
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "BLKB")
    assert stored.version == card.version + 1
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.buylist_member is False
    assert stored.breakout_price == 45.6
    assert stored.stop_type is None
    assert stored.active_stop_price is None
    assert service.board_snapshot(force=True)["rows"] == []


def test_split_owner_consumes_mobile_cancel_partial_sell(
    web_config, tmp_path, monkeypatch
):
    engine, _role, service = _service(web_config, tmp_path, same_executor=False)
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    card = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="account-a",
            symbol="AAPL",
            name="Apple",
            board_status=BoardStatus.PARTIAL_SELL,
            buylist_member=True,
            broker_quantity=10,
            orderable_quantity=10,
            pending_partial_sell_quantity=3,
        ),
    )

    result = service.apply_board_action(
        command_id=str(uuid.uuid4()),
        action="cancel_partial_sell",
        symbol="AAPL",
        expected_revision=card.version,
    )
    assert result["queued"] is True

    executor = LocalDeviceRole("executor-pc", "EXECUTOR", True)
    outcome = process_next_board_operator_command(engine, executor)
    assert outcome is not None
    assert outcome.status == OperatorCommandStatus.COMPLETED
    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "AAPL")
    assert stored.board_status == BoardStatus.OPEN_POSITION


def test_mobile_buyboard_api_returns_columns_and_applies_typed_action(
    web_config, services, tmp_path, monkeypatch
):
    engine, _role, operator = _service(web_config, tmp_path, same_executor=True)
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    card = _buylist_card(engine)
    services.config = operator.config
    services.canonical_planning = operator.source
    services.connected_planning = operator.planning
    services.connected_operator = operator

    with TestClient(
        create_api_app(operator.config, services=services),
        base_url="http://localhost:8080",
    ) as phone:
        session = login(phone)
        phone.headers.update(
            {
                "Origin": "http://localhost:8080",
                "X-CSRF-Token": session["csrf_token"],
            }
        )
        board = phone.get("/api/v1/buyboard")
        assert board.status_code == 200
        assert board.json()["columns"] == [
            "BUYLIST",
            "BUY_TODAY",
            "ENTRY_PENDING",
            "OPEN_POSITION",
            "PARTIAL_SELL",
            "SELL_ALL",
        ]
        control = phone.post(
            "/api/v1/operator/control",
            json={"target": "mobile"},
        )
        assert control.status_code == 200, control.text
        assert control.json()["operator_control"] == "MOBILE"
        assert control.json()["delegated"] is True
        action = phone.post(
            "/api/v1/operator/board-actions",
            json={
                "command_id": str(uuid.uuid4()),
                "action": "activate_buy_today",
                "symbol": "AAPL",
                "expected_revision": card.version,
            },
        )
        assert action.status_code == 200, action.text
        assert action.json()["queued"] is False
        assert action.json()["card"]["board_status"] == "BUY_TODAY"


def test_mobile_orb_settings_api_round_trip_is_revision_safe(
    web_config, services, tmp_path
):
    engine, _role, operator = _service(web_config, tmp_path, same_executor=True)
    services.config = operator.config
    services.canonical_planning = operator.source
    services.connected_planning = operator.planning
    services.connected_operator = operator

    with TestClient(
        create_api_app(operator.config, services=services),
        base_url="http://localhost:8080",
    ) as phone:
        session = login(phone)
        phone.headers.update(
            {
                "Origin": "http://localhost:8080",
                "X-CSRF-Token": session["csrf_token"],
            }
        )
        control = phone.post(
            "/api/v1/operator/control", json={"target": "mobile"}
        )
        assert control.status_code == 200
        initial = phone.get("/api/v1/operator/orb-settings")
        assert initial.status_code == 200
        assert initial.json()["settings"]["capital_max_percent"] == 30.0
        assert initial.json()["settings"]["opening_min_shares_per_minute"] == 200.0

        payload = {
            "command_id": str(uuid.uuid4()),
            "expected_revision": initial.json()["revision"],
            "capital_min_percent": 10.0,
            "capital_ideal_percent": 18.0,
            "capital_max_percent": 35.0,
            "stop_adr_min_percent": 15.0,
            "stop_adr_ideal_percent": 65.0,
            "stop_adr_max_percent": 66.0,
            "opening_min_shares_per_minute": 350.0,
        }
        saved = phone.put("/api/v1/operator/orb-settings", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1
        assert saved.json()["settings"]["capital_max_percent"] == 35.0
        assert saved.json()["settings"]["opening_min_shares_per_minute"] == 350.0

        invalid = phone.put(
            "/api/v1/operator/orb-settings",
            json={**payload, "command_id": str(uuid.uuid4()), "opening_min_shares_per_minute": -1},
        )
        assert invalid.status_code == 422

        legacy_payload = {key: value for key, value in payload.items() if key != "opening_min_shares_per_minute"}
        legacy_payload.update(command_id=str(uuid.uuid4()), expected_revision=saved.json()["revision"])
        legacy_saved = phone.put("/api/v1/operator/orb-settings", json=legacy_payload)
        assert legacy_saved.status_code == 200
        assert legacy_saved.json()["settings"]["opening_min_shares_per_minute"] == 350.0

        stale = phone.put(
            "/api/v1/operator/orb-settings",
            json={**payload, "command_id": str(uuid.uuid4())},
        )
        assert stale.status_code == 409
        assert stale.json()["current"]["revision"] == legacy_saved.json()["revision"]
        assert ss.pull_state(engine, ss.SETTINGS_KEY).state.payload[
            "orb_settings"
        ]["capital_max_percent"] == 35.0
