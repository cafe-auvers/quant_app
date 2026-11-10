import pytest

from src.core.board_workflow import RequestPartialSell
from src.core.trade_card_state import TradeCardState
from src.services.operator_command_service import serialize_board_command


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf"), 3.505, 0.12345,
                                 1.9999999999, 0.99999999999, 1e308])
def test_partial_sell_rejects_invalid_limit_instead_of_rounding_down(price):
    with pytest.raises(ValueError):
        RequestPartialSell(environment="PROD", account_no="1", symbol="TEST", expected_card_version=1,
                           quantity=10, limit_price=price)


@pytest.mark.parametrize("price", [None, 3.5, 0.1234])
def test_partial_sell_price_survives_command_and_card_persistence(price):
    command = RequestPartialSell(environment="PROD", account_no="1", symbol="TEST", expected_card_version=1,
                                 quantity=10, limit_price=price)
    assert serialize_board_command(command)["board_command"]["limit_price"] == price
    card = TradeCardState(environment="PROD", account_no="1", symbol="TEST",
                          pending_partial_sell_quantity=10, pending_partial_sell_price=price)
    restored = TradeCardState.from_dict(card.to_dict())
    assert restored.pending_partial_sell_price == price


def test_old_card_without_partial_price_keeps_immediate_behavior():
    card = TradeCardState(environment="PROD", account_no="1", symbol="TEST")
    payload = card.to_dict()
    payload.pop("pending_partial_sell_price")
    assert TradeCardState.from_dict(payload).pending_partial_sell_price is None
