from __future__ import annotations

import uuid
from dataclasses import replace

from sqlalchemy import create_engine

from src.services import trade_card_repository
from src.services.state_sync import (
    LocalDeviceRole,
    save_local_device_role,
    set_operator_control,
)
from src.web.canonical_planning import CanonicalPlanningSource
from src.web.connected_planning import ConnectedPlanningService
from src.web.operator_identity import mobile_web_role


def test_connected_passive_planning_round_trip_uses_canonical_cards(
    web_config, tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.db'}", future=True)
    trade_card_repository.ensure_trade_cards_table(engine)
    operations = (
        "add_watchlist",
        "remove_watchlist",
        "promote_buylist",
        "remove_buylist",
        "move_watchlist",
        "set_breakout",
        "clear_breakout",
    )
    config = replace(
        web_config,
        mode="CONNECTED",
        canonical_planning_reads=True,
        canonical_planning_writes=True,
        connected_passive_operations=operations,
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
    service = ConnectedPlanningService(config, source, engine=engine, unavailable_reason="")
    monkeypatch.setattr(service, "_require_operator_control", lambda: None)

    added = service.apply(
        command_id=str(uuid.uuid4()),
        operation="add_watchlist",
        symbol="AAPL",
        expected_revision=0,
        breakout_price=None,
    )
    assert added["card"]["watchlist_member"] is True
    assert added["card"]["version"] == 1

    breakout = service.apply(
        command_id=str(uuid.uuid4()),
        operation="set_breakout",
        symbol="AAPL",
        expected_revision=1,
        breakout_price=201.25,
    )
    assert breakout["card"]["breakout_price"] == 201.25

    promoted = service.apply(
        command_id=str(uuid.uuid4()),
        operation="promote_buylist",
        symbol="AAPL",
        expected_revision=2,
        breakout_price=None,
    )
    assert promoted["card"]["buylist_member"] is True

    unsaved = service.apply(
        command_id=str(uuid.uuid4()),
        operation="remove_watchlist",
        symbol="AAPL",
        expected_revision=3,
        breakout_price=None,
    )
    assert unsaved["card"]["watchlist_member"] is False
    assert unsaved["card"]["buylist_member"] is True

    readded = service.apply(
        command_id=str(uuid.uuid4()),
        operation="add_watchlist",
        symbol="AAPL",
        expected_revision=4,
        breakout_price=None,
    )
    assert readded["card"]["watchlist_member"] is True
    assert readded["card"]["buylist_member"] is True
    assert readded["card"]["breakout_price"] == 201.25

    unsaved_again = service.apply(
        command_id=str(uuid.uuid4()),
        operation="remove_watchlist",
        symbol="AAPL",
        expected_revision=5,
        breakout_price=None,
    )
    assert unsaved_again["card"]["watchlist_member"] is False
    assert unsaved_again["card"]["buylist_member"] is True

    removed = service.apply(
        command_id=str(uuid.uuid4()),
        operation="remove_buylist",
        symbol="AAPL",
        expected_revision=6,
        breakout_price=None,
    )
    assert removed["card"]["watchlist_member"] is False
    assert removed["card"]["buylist_member"] is False
    assert removed["card"]["breakout_price"] == 201.25
    assert removed["change_notification"] == "PUBLISHED"
    assert (tmp_path / "data" / "coordination_change_inbound.json").is_file()
    assert (tmp_path / "data" / "coordination_change_outbound.json").is_file()


def test_connected_command_ledger_replays_same_payload_and_rejects_reuse(
    services,
):
    web_store = services.store
    command_id = str(uuid.uuid4())
    payload = {
        "operation": "add_watchlist",
        "symbol": "AAPL",
        "expected_revision": 0,
        "breakout_price": None,
    }
    response = {"status": "SAVED TO CANONICAL STORE", "card": {"symbol": "AAPL"}}
    stored = web_store.record_connected_command(
        command_id=command_id,
        payload=payload,
        response=response,
        actor="owner",
    )
    assert stored["status"] == "SAVED TO CANONICAL STORE"
    replay = web_store.connected_command_replay(
        command_id=command_id, payload=payload
    )
    assert replay["idempotent_replay"] is True

    changed = {**payload, "symbol": "MSFT"}
    from src.web.store import ConflictError

    try:
        web_store.connected_command_replay(command_id=command_id, payload=changed)
    except ConflictError:
        pass
    else:
        raise AssertionError("reusing a connected command ID must fail")


def test_mobile_operator_control_can_modify_breakout_price(web_config, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.db'}", future=True)
    trade_card_repository.ensure_trade_cards_table(engine)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    host = LocalDeviceRole("host-pc", "HOST-PC", False)
    save_local_device_role(host, path=data_dir / "device_role.json")
    config = replace(
        web_config,
        mode="CONNECTED",
        canonical_planning_reads=True,
        canonical_planning_writes=True,
        connected_passive_operations=("set_breakout",),
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
    service = ConnectedPlanningService(config, source, engine=engine, unavailable_reason="")
    card = trade_card_repository.create_trade_card(
        engine,
        trade_card_repository.TradeCardState(
            environment="PROD",
            account_no="account-a",
            symbol="CURV",
            name="CURV",
            breakout_price=2.64,
        ),
    )
    assert set_operator_control(engine, host, mobile_web_role(config)).success

    result = service.apply(
        command_id=str(uuid.uuid4()),
        operation="set_breakout",
        symbol="CURV",
        expected_revision=card.version,
        breakout_price=2.75,
    )

    assert result["card"]["breakout_price"] == 2.75
