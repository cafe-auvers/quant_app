from sqlalchemy import inspect

from src.core.trade_card_state import BoardStatus, TradeCardState
from src.infrastructure.database.operational_engine import (
    init_local_operational_engine,
)
from src.services import trade_card_repository


def test_local_operational_engine_seeds_cards_but_contains_no_market_history(
    tmp_path, monkeypatch,
):
    snapshot_path = tmp_path / "trade_cards.json"
    monkeypatch.setattr(
        trade_card_repository, "LOCAL_TRADE_CARDS_FILE", snapshot_path
    )
    trade_card_repository.save_local_trade_cards_snapshot(
        [
            TradeCardState(
                environment="PROD",
                account_no="account-1",
                symbol="AAPL",
                board_status=BoardStatus.BUY_TODAY,
                breakout_price=200.0,
            )
        ],
        path=snapshot_path,
    )

    engine = init_local_operational_engine(tmp_path / "kanban.sqlite3")

    assert engine is not None
    card = trade_card_repository.get_trade_card(
        engine, "PROD", "account-1", "AAPL"
    )
    assert card is not None
    assert card.board_status == BoardStatus.BUY_TODAY
    assert card.breakout_price == 200.0
    tables = set(inspect(engine).get_table_names())
    assert "trade_cards" in tables
    assert not {
        "price_history",
        "hourly_price_history",
        "intraday_price_history",
    }.intersection(tables)
