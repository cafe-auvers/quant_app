from __future__ import annotations

import json
import platform
import uuid
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from src.core.trade_card_state import BoardStatus, TradeCardState
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

        payload = {
            "command_id": str(uuid.uuid4()),
            "expected_revision": initial.json()["revision"],
            "capital_min_percent": 10.0,
            "capital_ideal_percent": 18.0,
            "capital_max_percent": 35.0,
            "stop_adr_min_percent": 15.0,
            "stop_adr_ideal_percent": 65.0,
            "stop_adr_max_percent": 66.0,
        }
        saved = phone.put("/api/v1/operator/orb-settings", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1
        assert saved.json()["settings"]["capital_max_percent"] == 35.0

        stale = phone.put(
            "/api/v1/operator/orb-settings",
            json={**payload, "command_id": str(uuid.uuid4())},
        )
        assert stale.status_code == 409
        assert stale.json()["current"]["revision"] == 1
        assert ss.pull_state(engine, ss.SETTINGS_KEY).state.payload[
            "orb_settings"
        ]["capital_max_percent"] == 35.0
