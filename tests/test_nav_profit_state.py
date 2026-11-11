"""PC/web state compatibility for the shared automatic profit lifecycle."""
import pytest

from src.core.nav_profit_exit import NavProfitExitState
from src.core.trade_card_state import TradeCardState


@pytest.mark.parametrize("status", ["ARMED", "SELLING", "DONE", "OVERRIDDEN"])
def test_trade_card_roundtrip_preserves_frozen_target_and_fill_evidence(status):
    state = NavProfitExitState(cycle_id="cycle-a", nav_usd=11000, target_profit_usd=220,
        status=status, target_quantity=0 if status == "ARMED" else 50,
        fills={} if status == "ARMED" else {"exit-one": 50 if status == "DONE" else 20},
        retired_orders=[] if status == "ARMED" else ["exit-one"])
    card = TradeCardState(environment="PROD", account_no="1", symbol="AAPL", nav_profit_exit=state)
    restored = TradeCardState.from_dict(card.to_dict())
    assert restored.nav_profit_exit.to_dict() == state.to_dict()


def test_legacy_card_starts_waiting_without_inventing_a_completed_sale():
    card = TradeCardState.from_dict(dict(environment="PROD", account_no="1", symbol="AAPL"))
    assert card.nav_profit_exit.status == "WAITING"
    assert card.nav_profit_exit.filled_quantity == 0


@pytest.mark.parametrize("bad", [
    "bad", {"status": "unknown"}, {"status": "ARMED"},
    {"status": "WAITING", "fills": {"exit-one": 20}},
    {"status": "SELLING", "nav_usd": float("nan")},
])
def test_invalid_state_blocks_automatic_sales(bad):
    assert NavProfitExitState.from_dict(bad).status == "BLOCKED"


def test_mobile_projection_exposes_target_progress_without_private_order_identities():
    from src.web.canonical_planning import CanonicalPlanningSource

    source = object.__new__(CanonicalPlanningSource)
    state = NavProfitExitState(cycle_id="private-cycle", nav_usd=11000, target_profit_usd=220,
        status="SELLING", target_quantity=50, fills={"private-order": 20})
    card = TradeCardState(environment="PROD", account_no="private-account", symbol="AAPL", nav_profit_exit=state)
    projection = source._project_board_card(card)
    assert projection["nav_profit_exit"] == dict(status="SELLING", nav_usd=11000,
        target_profit_usd=220, target_quantity=50, filled_quantity=20)
    assert "private-cycle" not in str(projection)
    assert "private-order" not in str(projection)
    assert "private-account" not in str(projection)
