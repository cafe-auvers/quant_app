from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.core.board_workflow import (
    BoardActionContext,
    BoardCardProjection,
    BoardExecutionOrderProjection,
    BoardExternalOrderProjection,
    BoardProjectionContext,
    MoveToBuylist,
    MoveToWatchlist,
)
from src.core.discovered_external_order import new_discovered_external_order
from src.core.execution_order_record import (
    BrokerIdentityStatus,
    ExecutionOrderRecord,
    ExecutionOrderStatus,
    OrderOrigin,
)
from src.core.order_state import OrderIntent, OrderSide
from src.core.trade_card_state import BoardStatus, StopType, TradeCardState
from src.core.watchlist import BuylistItem, BuylistManager, Watchlist
from src.services import trade_card_repository
from src.services.buylist_membership_service import reconcile_buylist_item
from src.services.execution_workflow_service import (
    BoardCommandRejectedError,
    list_board_projections,
    request_board_action,
)
from src.services.discovered_external_order_repository import (
    record_discovered_external_order,
)
from src.services.execution_order_repository import record_execution_order
from src.services.planning_membership_service import (
    PlanningMembershipError,
    add_watchlist_candidate,
    promote_watchlist_to_buylist,
    remove_watchlist_candidate,
    sync_legacy_planning_membership_from_card,
)
from src.services.trade_card_bootstrap import bootstrap_trade_cards_from_current_state


DURABLE_EVIDENCE_CASES = [
    ("broker quantity", {"broker_quantity": 2}),
    ("entry identity", {"entry_client_order_id": "entry-1"}),
    (
        "active stop",
        {
            "stop_type": StopType.MANUAL_PRICE,
            "active_stop_price": 95.0,
            "stop_quantity": 1,
        },
    ),
    ("exit intent", {"exit_all_required": True}),
    ("exit identity", {"exit_client_order_id": "exit-1"}),
    (
        "entry cancellation",
        {"entry_cancel_in_flight": True, "entry_cancel_reason": "USER_CANCEL"},
    ),
    ("capital reservation", {"capital_reservation_id": "reservation-1"}),
]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trade_card_repository,
        "LOCAL_TRADE_CARDS_FILE",
        tmp_path / "trade_cards.json",
    )
    return create_engine(
        f"sqlite:///{tmp_path / 'planning-membership.db'}",
        future=True,
        poolclass=NullPool,
    )


def _buylist_item(symbol="AAPL", *, account_no="1"):
    return BuylistItem(
        symbol=symbol,
        name=f"{symbol} Inc.",
        entry_price=100.0,
        target_price=0.0,
        stop_loss=95.0,
        total_score=0.0,
        status="WATCHING",
        technical_score=0.0,
        setup_score=0.0,
        risk_score=0.0,
        news_score=0.0,
        timing_score=0.0,
        rr=0.0,
        stop_adr=0.0,
        position_percent=0.0,
        ai_summary="",
        warnings=[],
        monitoring_status="WATCHING",
        kis_account_no=account_no,
        environment="PROD",
        breakout_price=101.0,
    )


def test_explicit_watchlist_promotion_is_passive_and_keeps_watchlist_membership(engine):
    watchlist = Watchlist()
    source = watchlist.add("AAPL", "Apple", 99.0)
    source.breakout_price = 101.0
    source.selected_orb_plan = {
        "window": "5m",
        "shares": 40,
        "entry_trigger": 102.0,
        "capital_percent": 25.0,
    }
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
            breakout_price=101.0,
            selected_orb_window="5m",
            planned_quantity=40,
            target_position_quantity=40,
            entry_trigger=102.0,
        ),
    )
    buylist = BuylistManager()

    result = promote_watchlist_to_buylist(
        watchlist,
        buylist,
        "aapl",
        engine=engine,
        default_account_no="1",
        buffer_pct=0.002,
    )

    assert result.action == "promoted_to_buylist"
    assert watchlist.get("AAPL") is source
    mirror = buylist.get("AAPL", "PROD")
    assert mirror is not None
    assert mirror.orb_monitor_enabled is False
    assert mirror.position_percent == 0.0
    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.board_status == BoardStatus.BUYLIST
    assert stored.watchlist_member is True
    assert stored.buylist_member is True
    assert stored.selected_orb_window is None
    assert stored.planned_quantity == 0
    assert stored.target_position_quantity == 0
    assert stored.entry_trigger is None


def test_watchlist_toggle_on_buylist_preserves_buylist_plan(engine):
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple").breakout_price = 101.0
    buylist = BuylistManager()
    mirror = _buylist_item()
    buylist.add(mirror)
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.BUYLIST,
            watchlist_member=True,
            buylist_member=True,
            breakout_price=101.0,
            buffer_pct=0.003,
        ),
    )

    removed = remove_watchlist_candidate(
        watchlist,
        "AAPL",
        engine=engine,
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert removed.action == "removed_from_watchlist"
    assert watchlist.get("AAPL") is None
    assert buylist.get("AAPL", "PROD") is mirror
    assert stored.board_status == BoardStatus.BUYLIST
    assert stored.watchlist_member is False
    assert stored.buylist_member is True
    assert stored.breakout_price == 101.0
    assert stored.buffer_pct == pytest.approx(0.003)

    added = add_watchlist_candidate(
        watchlist,
        symbol="AAPL",
        name="Apple",
        engine=engine,
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert added.action == "added_to_watchlist"
    assert watchlist.get("AAPL") is not None
    assert watchlist.get("AAPL").breakout_price == 101.0
    assert buylist.get("AAPL", "PROD") is mirror
    assert stored.board_status == BoardStatus.BUYLIST
    assert stored.watchlist_member is True
    assert stored.buylist_member is True


def test_promotion_does_not_rewrite_an_advanced_canonical_lifecycle(engine):
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple")
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.BUY_TODAY,
            buylist_member=True,
        ),
    )
    buylist = BuylistManager()

    with pytest.raises(PlanningMembershipError, match="BUY_TODAY"):
        promote_watchlist_to_buylist(
            watchlist,
            buylist,
            "AAPL",
            engine=engine,
            default_account_no="1",
        )

    assert watchlist.get("AAPL") is not None
    assert buylist.get("AAPL", "PROD") is None


@pytest.mark.parametrize("_label,evidence", DURABLE_EVIDENCE_CASES)
def test_explicit_promotion_preflights_all_durable_evidence_before_cas(
    engine, _label, evidence
):
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple").breakout_price = 101.0
    original = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
            breakout_price=101.0,
            **evidence,
        ),
    )
    buylist = BuylistManager()
    mirror = _buylist_item(account_no="")
    buylist.add(mirror)

    with pytest.raises(PlanningMembershipError, match="WATCHLIST"):
        promote_watchlist_to_buylist(
            watchlist,
            buylist,
            "AAPL",
            engine=engine,
            default_account_no="1",
        )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.version == original.version
    for field, expected in evidence.items():
        assert getattr(stored, field) == expected
    assert watchlist.get("AAPL") is not None
    assert buylist.get("AAPL", "PROD") is mirror
    assert mirror.kis_account_no == ""


@pytest.mark.parametrize("_label,evidence", DURABLE_EVIDENCE_CASES)
def test_stale_buylist_mirror_cannot_bypass_durable_evidence_preflight(
    engine, _label, evidence
):
    original = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
            breakout_price=101.0,
            **evidence,
        ),
    )
    buylist = BuylistManager()
    mirror = _buylist_item()
    buylist.add(mirror)

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=buylist,
        watchlist=Watchlist(),
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert result.buylist_promoted_keys == ()
    assert result.changed is False
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.version == original.version
    for field, expected in evidence.items():
        assert getattr(stored, field) == expected
    assert buylist.get("AAPL", "PROD") is mirror


@pytest.mark.parametrize("_label,evidence", DURABLE_EVIDENCE_CASES)
def test_reconcile_explicit_promotion_returns_unsafe_without_mutating_card(
    engine, _label, evidence
):
    original = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
            **evidence,
        ),
    )

    result = reconcile_buylist_item(
        engine,
        _buylist_item(),
        explicit_watchlist_promotion=True,
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert result.action == "unsafe"
    assert result.changed is False
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.version == original.version


def test_promotion_mirror_uses_newer_canonical_target(engine):
    watchlist = Watchlist()
    source = watchlist.add("AAPL", "Apple")
    source.breakout_price = 101.0
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            name="Apple Canonical",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
            breakout_price=105.0,
            buffer_pct=0.003,
        ),
    )
    buylist = BuylistManager()

    result = promote_watchlist_to_buylist(
        watchlist,
        buylist,
        "AAPL",
        engine=engine,
        default_account_no="1",
    )

    mirror = buylist.get("AAPL", "PROD")
    assert result.card.breakout_price == 105.0
    assert mirror.breakout_price == 105.0
    assert mirror.buffer_pct == pytest.approx(0.003)
    assert mirror.name == "Apple Canonical"


def test_move_to_watchlist_clears_execution_geometry_and_syncs_json(engine):
    buylist = BuylistManager()
    buylist.add(_buylist_item())
    watchlist = Watchlist()
    card = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            name="Apple",
            board_status=BoardStatus.BUYLIST,
            buylist_member=True,
            breakout_price=101.0,
            selected_orb_window="5m",
            planned_quantity=20,
            target_position_quantity=20,
            entry_trigger=102.0,
        ),
    )

    moved = request_board_action(
        engine,
        MoveToWatchlist(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            expected_card_version=card.version,
        ),
        context=BoardActionContext(),
    ).card
    synced = sync_legacy_planning_membership_from_card(watchlist, buylist, moved)

    assert moved.board_status == BoardStatus.WATCHLIST
    assert moved.previous_board_status == BoardStatus.BUYLIST
    assert moved.watchlist_member is True
    assert moved.buylist_member is False
    assert moved.selected_orb_window is None
    assert moved.planned_quantity == 0
    assert moved.target_position_quantity == 0
    assert moved.entry_trigger is None
    assert synced.action == "synced_watchlist"
    assert watchlist.get("AAPL") is not None
    assert watchlist.get("AAPL").breakout_price == 101.0
    assert buylist.get("AAPL", "PROD") is None


@pytest.mark.parametrize("_label,evidence", DURABLE_EVIDENCE_CASES)
@pytest.mark.parametrize(
    "current_status,command_type",
    [
        (BoardStatus.BUYLIST, MoveToWatchlist),
        (BoardStatus.WATCHLIST, MoveToBuylist),
    ],
)
def test_planning_stage_commands_reject_all_durable_execution_evidence(
    engine, current_status, command_type, _label, evidence
):
    card = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=current_status,
            watchlist_member=current_status == BoardStatus.WATCHLIST,
            buylist_member=current_status == BoardStatus.BUYLIST,
            **evidence,
        ),
    )

    with pytest.raises(BoardCommandRejectedError, match="Planning membership"):
        request_board_action(
            engine,
            command_type(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                expected_card_version=card.version,
            ),
            context=BoardActionContext(),
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.board_status == current_status
    assert stored.version == card.version
    for field, expected in evidence.items():
        assert getattr(stored, field) == expected


def test_move_to_watchlist_cannot_hide_active_unowned_external_order(engine):
    card = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.BUYLIST,
            buylist_member=True,
        ),
    )
    record_discovered_external_order(
        engine,
        new_discovered_external_order(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            broker_order_id="external-buy-1",
            quantity_requested=2,
            broker_status=ExecutionOrderStatus.WORKING,
        ),
    )

    with pytest.raises(BoardCommandRejectedError, match="unowned external"):
        request_board_action(
            engine,
            MoveToWatchlist(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                expected_card_version=card.version,
            ),
            context=BoardActionContext(),
        )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.board_status == BoardStatus.BUYLIST
    assert stored.version == card.version


def test_hidden_watchlist_card_never_suppresses_active_order_warning_rows(engine):
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
        ),
    )
    record_execution_order(
        engine,
        ExecutionOrderRecord(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            intent=OrderIntent.UNKNOWN,
            client_order_id="owned-live-1",
            status=ExecutionOrderStatus.WORKING,
            broker_identity_status=BrokerIdentityStatus.EXACT,
            broker_order_id="owned-broker-1",
            submitted_quantity=2,
            remaining_quantity=2,
            origin=OrderOrigin.USER_ADOPTED,
            adopted_from_external_order_id="adopted-source-1",
        ),
    )
    external = record_discovered_external_order(
        engine,
        new_discovered_external_order(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            broker_order_id="external-live-1",
            quantity_requested=3,
            broker_status=ExecutionOrderStatus.WORKING,
        ),
    )

    projections = list_board_projections(
        engine,
        context=BoardProjectionContext(readiness_generation=7),
        board_statuses=(BoardStatus.WATCHLIST,),
    )

    assert any(isinstance(row, BoardCardProjection) for row in projections)
    owned_rows = [
        row for row in projections if isinstance(row, BoardExecutionOrderProjection)
    ]
    external_rows = [
        row for row in projections if isinstance(row, BoardExternalOrderProjection)
    ]
    assert [row.order.client_order_id for row in owned_rows] == ["owned-live-1"]
    assert [row.order.external_order_id for row in external_rows] == [
        external.external_order_id
    ]


def test_stale_buylist_mirror_cannot_undo_intentional_canonical_demotion(engine):
    buylist = BuylistManager()
    buylist.add(_buylist_item())
    card = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.BUYLIST,
            buylist_member=True,
        ),
    )
    request_board_action(
        engine,
        MoveToWatchlist(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            expected_card_version=card.version,
        ),
        context=BoardActionContext(),
    )

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=buylist,
        watchlist=Watchlist(),
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert result.buylist_promoted_keys == ()
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.buylist_member is False


def test_remove_watchlist_archives_canonical_before_removing_json(engine):
    watchlist = Watchlist()
    source = watchlist.add("AAPL", "Apple", 99.0)
    source.breakout_price = 101.0
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
            breakout_price=101.0,
        ),
    )

    result = remove_watchlist_candidate(
        watchlist,
        "AAPL",
        engine=engine,
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert result.action == "removed_from_watchlist"
    assert watchlist.get("AAPL") is None
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.watchlist_member is False
    assert stored.buylist_member is False
    assert stored.breakout_price is None


def test_remove_watchlist_leaves_local_membership_on_canonical_rejection(engine):
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple")
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
            broker_quantity=2,
        ),
    )

    with pytest.raises(PlanningMembershipError, match="not a passive"):
        remove_watchlist_candidate(
            watchlist,
            "AAPL",
            engine=engine,
            default_account_no="1",
        )

    assert watchlist.get("AAPL") is not None


def test_archived_watchlist_candidate_can_be_added_again(engine):
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple")
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=False,
        ),
    )

    result = add_watchlist_candidate(
        watchlist,
        symbol="AAPL",
        name="Apple Inc.",
        entry_price=99.0,
        engine=engine,
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert result.action == "added_to_watchlist"
    assert stored.watchlist_member is True
    assert stored.buylist_member is False
    assert watchlist.get("AAPL").name == "Apple Inc."


def test_selected_account_must_match_the_only_canonical_symbol_card(engine):
    canonical = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="2",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
        ),
    )
    watchlist = Watchlist()

    with pytest.raises(PlanningMembershipError, match="canonical account 2"):
        add_watchlist_candidate(
            watchlist,
            symbol="AAPL",
            name="Apple",
            engine=engine,
            default_account_no="1",
        )
    assert watchlist.get("AAPL") is None

    watchlist.add("AAPL", "Apple")
    buylist = BuylistManager()
    with pytest.raises(PlanningMembershipError, match="canonical account 2"):
        promote_watchlist_to_buylist(
            watchlist,
            buylist,
            "AAPL",
            engine=engine,
            default_account_no="1",
        )

    assert trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL") is None
    stored = trade_card_repository.get_trade_card(engine, "PROD", "2", "AAPL")
    assert stored.version == canonical.version
    assert stored.board_status == BoardStatus.WATCHLIST
    assert watchlist.get("AAPL") is not None
    assert buylist.get("AAPL", "PROD") is None


def test_watchlist_bootstrap_never_duplicates_an_existing_other_account(engine):
    canonical = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="2",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
        ),
    )
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple")

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=BuylistManager(),
        watchlist=watchlist,
        default_account_no="1",
    )

    assert result.created_keys == ()
    assert result.skipped_watchlist_symbols == ("AAPL",)
    assert trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL") is None
    stored = trade_card_repository.get_trade_card(engine, "PROD", "2", "AAPL")
    assert stored.version == canonical.version


def test_newer_local_membership_revives_passive_canonical_watchlist_tombstone(engine):
    removed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            name="Old name",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=False,
            buylist_member=False,
            board_status_updated_at=removed_at,
        ),
    )
    watchlist = Watchlist()
    added = watchlist.add("AAPL", "Apple Restored", 99.0)
    added.breakout_price = 101.0
    added.added_date = removed_at + timedelta(seconds=1)

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=BuylistManager(),
        watchlist=watchlist,
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert result.changed is True
    assert result.watchlist_revived_keys == ("PROD:1:AAPL",)
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.watchlist_member is True
    assert stored.buylist_member is False
    assert stored.name == "Apple Restored"
    assert stored.breakout_price == 101.0


def test_older_local_membership_cannot_revive_a_newer_canonical_tombstone(engine):
    removed_at = datetime.now(timezone.utc)
    original = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=False,
            buylist_member=False,
            board_status_updated_at=removed_at,
        ),
    )
    watchlist = Watchlist()
    local = watchlist.add("AAPL", "Older local copy")
    local.added_date = removed_at - timedelta(seconds=1)

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=BuylistManager(),
        watchlist=watchlist,
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert result.watchlist_revived_keys == ()
    assert result.changed is False
    assert stored.watchlist_member is False
    assert stored.version == original.version


def test_stale_local_watchlist_cannot_promote_a_canonical_tombstone(engine):
    watchlist = Watchlist()
    watchlist.add("AAPL", "Stale local copy")
    original = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=False,
            buylist_member=False,
        ),
    )
    buylist = BuylistManager()

    with pytest.raises(PlanningMembershipError, match="WATCHLIST"):
        promote_watchlist_to_buylist(
            watchlist,
            buylist,
            "AAPL",
            engine=engine,
            default_account_no="1",
        )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert stored.watchlist_member is False
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.version == original.version
    assert watchlist.get("AAPL") is not None
    assert buylist.get("AAPL", "PROD") is None


@pytest.mark.parametrize(
    "status, evidence",
    [
        (BoardStatus.BUYLIST, {}),
        (BoardStatus.BUY_TODAY, {}),
        (BoardStatus.WATCHLIST, {"broker_quantity": 2}),
    ],
)
def test_newer_local_membership_never_revives_or_pulls_back_nonpassive_state(
    engine, status, evidence
):
    original = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=status,
            watchlist_member=False,
            buylist_member=status != BoardStatus.WATCHLIST,
            **evidence,
        ),
    )
    watchlist = Watchlist()
    local = watchlist.add("AAPL", "Stale local add")
    local.breakout_price = 101.0
    local.added_date = original.board_status_updated_at + timedelta(seconds=1)

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=BuylistManager(),
        watchlist=watchlist,
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert result.watchlist_revived_keys == ()
    assert result.changed is False
    assert stored.board_status == status
    assert stored.watchlist_member is False
    assert stored.version == original.version
    assert stored.broker_quantity == int(evidence.get("broker_quantity", 0))


def test_offline_watchlist_add_is_rejected_without_mutating_local_membership():
    watchlist = Watchlist()

    with pytest.raises(PlanningMembershipError, match="was not added"):
        add_watchlist_candidate(
            watchlist,
            symbol="AAPL",
            name="Apple",
            entry_price=99.0,
            engine=None,
            default_account_no="",
        )

    assert watchlist.get("AAPL") is None


def test_offline_promotion_is_rejected_without_mutating_local_membership():
    watchlist = Watchlist()
    source = watchlist.add("AAPL", "Apple", 99.0)
    source.breakout_price = 101.0
    source.selected_orb_plan = {
        "window": "1m",
        "shares": 500,
        "capital_percent": 80.0,
    }
    buylist = BuylistManager()

    with pytest.raises(PlanningMembershipError, match="was not promoted"):
        promote_watchlist_to_buylist(
            watchlist,
            buylist,
            "AAPL",
            engine=None,
            default_account_no="",
        )

    assert watchlist.get("AAPL") is source
    assert buylist.get("AAPL", "PROD") is None


def test_offline_promotion_cannot_override_newer_canonical_demotion(engine):
    card = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.BUYLIST,
            buylist_member=True,
        ),
    )
    demoted = request_board_action(
        engine,
        MoveToWatchlist(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            expected_card_version=card.version,
        ),
        context=BoardActionContext(),
    ).card
    assert demoted.previous_board_status == BoardStatus.BUYLIST

    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple").breakout_price = 101.0
    buylist = BuylistManager()
    with pytest.raises(PlanningMembershipError, match="was not promoted"):
        promote_watchlist_to_buylist(
            watchlist,
            buylist,
            "AAPL",
            engine=None,
            default_account_no="1",
        )

    result = bootstrap_trade_cards_from_current_state(
        engine,
        buylist_manager=buylist,
        watchlist=watchlist,
        default_account_no="1",
    )

    stored = trade_card_repository.get_trade_card(engine, "PROD", "1", "AAPL")
    assert result.buylist_promoted_keys == ()
    assert result.changed is False
    assert stored.board_status == BoardStatus.WATCHLIST
    assert stored.version == demoted.version
    assert watchlist.get("AAPL") is not None
    assert buylist.get("AAPL", "PROD") is None


def test_offline_remove_is_rejected_so_reconnect_cannot_resurrect_it(engine):
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple")
    card = trade_card_repository.create_trade_card(
        engine,
        TradeCardState(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            board_status=BoardStatus.WATCHLIST,
            watchlist_member=True,
        ),
    )
    buylist = BuylistManager()

    with pytest.raises(PlanningMembershipError, match="was not removed"):
        remove_watchlist_candidate(
            watchlist,
            "AAPL",
            engine=None,
            default_account_no="1",
        )

    assert watchlist.get("AAPL") is not None
    # When the canonical store becomes reachable again, mirror convergence is
    # idempotent rather than making a supposedly removed symbol reappear.
    synced = sync_legacy_planning_membership_from_card(watchlist, buylist, card)
    assert synced.changed is False
    assert watchlist.get("AAPL") is not None


def test_watchlist_sync_reports_canonical_target_update_once():
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple").breakout_price = 100.0
    buylist = BuylistManager()
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        name="Apple",
        board_status=BoardStatus.WATCHLIST,
        watchlist_member=True,
        breakout_price=101.0,
    )

    first = sync_legacy_planning_membership_from_card(watchlist, buylist, card)
    second = sync_legacy_planning_membership_from_card(watchlist, buylist, card)

    assert first.changed is True
    assert watchlist.get("AAPL").breakout_price == 101.0
    assert second.changed is False


def test_buylist_sync_with_matching_existing_mirror_is_idempotent():
    watchlist = Watchlist()
    buylist = BuylistManager()
    buylist.add(_buylist_item())
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        name="AAPL Inc.",
        board_status=BoardStatus.BUYLIST,
        buylist_member=True,
        breakout_price=101.0,
        buffer_pct=0.001,
    )

    first = sync_legacy_planning_membership_from_card(watchlist, buylist, card)
    second = sync_legacy_planning_membership_from_card(watchlist, buylist, card)

    assert first.changed is False
    assert second.changed is False


def test_buylist_sync_retains_overlapping_watchlist_mirror():
    watchlist = Watchlist()
    buylist = BuylistManager()
    buylist.add(_buylist_item())
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        name="Apple",
        board_status=BoardStatus.BUYLIST,
        watchlist_member=True,
        buylist_member=True,
        breakout_price=101.0,
    )

    first = sync_legacy_planning_membership_from_card(watchlist, buylist, card)
    second = sync_legacy_planning_membership_from_card(watchlist, buylist, card)

    assert first.changed is True
    assert watchlist.get("AAPL") is not None
    assert watchlist.get("AAPL").breakout_price == 101.0
    assert second.changed is False


def test_sync_ignores_nonplanning_card_and_keeps_local_state():
    watchlist = Watchlist()
    watchlist.add("AAPL", "Apple")
    buylist = BuylistManager()
    card = TradeCardState(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        board_status=BoardStatus.BUY_TODAY,
    )

    result = sync_legacy_planning_membership_from_card(watchlist, buylist, card)

    assert result.action == "ignored_non_passive"
    assert watchlist.get("AAPL") is not None
