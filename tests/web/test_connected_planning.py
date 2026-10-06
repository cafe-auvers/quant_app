from __future__ import annotations

import uuid
from dataclasses import replace

from sqlalchemy import create_engine

from src.core.trade_card_state import BoardStatus, StopType, TradeCardState
from src.services import trade_card_repository
from src.services.state_sync import (
    LocalDeviceRole,
    save_local_device_role,
    set_operator_control,
)
from src.web.canonical_planning import CanonicalPlanningSource
from src.web.connected_planning import ConnectedPlanningService
from src.web.operator_identity import mobile_web_role


def test_connected_buylist_removal_accepts_revised_flat_legacy_planning_stop(web_config, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-planning.db'}", future=True)
    config = replace(web_config, mode="CONNECTED", canonical_planning_reads=True,
                     canonical_planning_writes=True, connected_passive_operations=("remove_buylist",),
                     canonical_account_no="account-a", pc_repository_path=str(tmp_path))
    source = CanonicalPlanningSource(enabled=True, environment="PROD", account_no="account-a",
                                     engine=engine, cache_seconds=0)
    service = ConnectedPlanningService(config, source, engine=engine, unavailable_reason="")
    card = trade_card_repository.create_trade_card(engine, TradeCardState(
        environment="PROD", account_no="account-a", symbol="CDNA",
        board_status=BoardStatus.BUYLIST, buylist_member=True, watchlist_member=True,
        breakout_price=64.81, stop_type=StopType.MANUAL_PRICE, active_stop_price=46.85,
        warnings=["migrated_from_buylist"],
    ))
    for index in range(22):
        card.name = f"Planning update {index}"
        card = trade_card_repository.update_trade_card(engine, card, expected_version=card.version)
    assert card.version == 23

    result = service.apply(command_id=str(uuid.uuid4()), operation="remove_buylist",
                           symbol="CDNA", expected_revision=23, breakout_price=None)

    stored = trade_card_repository.get_trade_card(engine, "PROD", "account-a", "CDNA")
    assert stored.board_status == BoardStatus.WATCHLIST and stored.watchlist_member
    assert not stored.buylist_member and stored.version == 24
    assert stored.breakout_price == 64.81 and stored.active_stop_price is None
    assert stored.stop_type is None and stored.broker_quantity == stored.stop_quantity == 0
    assert result["status"] == "SAVED TO CANONICAL STORE"
    from src.services.execution_order_repository import list_execution_orders_for_card
    assert list_execution_orders_for_card(engine, environment="PROD", account_no="account-a", symbol="CDNA") == []


def test_planning_change_pulse_notifies_pc_listener(
    web_config, tmp_path, monkeypatch
):
    config = replace(web_config, pc_repository_path=str(tmp_path))
    service = ConnectedPlanningService(
        config,
        source=None,
        engine=None,
        unavailable_reason="",
    )
    calls = []
    from src.services import pc_remote_control

    monkeypatch.setattr(
        pc_remote_control,
        "notify_pc_coordination_change",
        lambda event_id, **kwargs: calls.append((event_id, kwargs)) or True,
    )

    assert service._publish_change_pulse("planning-1") is True
    assert calls == [
        (
            "web:planning-1",
            {
                "changed_tables": ("trade_cards",),
                "protocol_version": 3,
                "timeout": 0.25,
            },
        )
    ]


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
