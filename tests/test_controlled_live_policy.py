from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.api import kis_order
from src.api.kis_account_snapshot_dual import KisTokenError
from src.core import execution_config
from src.core.order_state import OrderSide
from src.core.trade_card_state import (
    BoardStatus,
    PositionRuntimeStatus,
    TradeCardState,
)
from src.services import controlled_live_policy
from src.services import trade_card_repository
from src.services.broker import KisBroker
from src.services.controlled_live_policy import (
    LiveExecutionEnvelopeError,
    controlled_live_symbols,
    live_entry_symbol_allowed,
    require_controlled_live_configuration,
    require_live_entry_allowed,
)
from src.services.kis_request_scheduler import KisRequestScheduler


def _configure_controlled_live(monkeypatch) -> None:
    monkeypatch.setattr(execution_config, "KIS_LIVE_EXECUTION_MODE", "CONTROLLED_LIVE")
    monkeypatch.setattr(
        execution_config, "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL", 500.0
    )
    monkeypatch.setattr(execution_config, "KIS_MUTATION_BUDGET_VERIFIED", True)
    monkeypatch.setattr(execution_config, "KIS_SUBMIT_MUTATION_CAPACITY", 2)
    monkeypatch.setattr(execution_config, "KIS_CANCEL_MUTATION_CAPACITY", 2)
    monkeypatch.setattr(execution_config, "KIS_REPLACE_MUTATION_CAPACITY", 2)
    monkeypatch.setattr(execution_config, "KIS_MUTATION_MIN_SPACING_SECONDS", 0.2)
    monkeypatch.setattr(execution_config, "KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS", 1)
    monkeypatch.setattr(execution_config, "KIS_WS_ENABLED", True)
    monkeypatch.setattr(execution_config, "KIS_WS_PROTOCOL_VERIFIED", True)
    monkeypatch.setattr(execution_config, "KIS_MARKET_DATA_MODE", "WEBSOCKET")
    monkeypatch.setattr(execution_config, "is_buyboard_engine_enabled", lambda: True)


def _engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'controlled-live.db'}",
        future=True,
        poolclass=NullPool,
    )


def _persist_card(engine, *, account_no="1", symbol="AAPL", **overrides):
    fields = {
        "environment": "PROD",
        "account_no": account_no,
        "symbol": symbol,
        "board_status": BoardStatus.BUY_TODAY,
    }
    fields.update(overrides)
    return trade_card_repository.create_trade_card(
        engine,
        TradeCardState(**fields),
    )


def test_canonical_cards_not_recovery_snapshot_authorize_controlled_live(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path)
    path = tmp_path / "trade_cards.json"
    _persist_card(engine, symbol="AAPL", board_status=BoardStatus.ENTRY_PENDING)
    _persist_card(engine, symbol="MSFT", board_status=BoardStatus.BUYLIST)
    trade_card_repository.save_local_trade_cards_snapshot(
        [
            TradeCardState(
                environment="PROD",
                account_no="1",
                symbol="RNG",
                board_status=BoardStatus.BUY_TODAY,
            ),
        ],
        path=path,
    )
    monkeypatch.setattr(trade_card_repository, "LOCAL_TRADE_CARDS_FILE", path)
    monkeypatch.setattr(execution_config, "KIS_LIVE_EXECUTION_MODE", "CONTROLLED_LIVE")

    # Engine-less reporting may show the recovery snapshot, but final
    # authority comes only from the exact canonical database row.
    assert controlled_live_symbols() == ("RNG",)
    assert controlled_live_symbols(engine=engine, account_no="1") == ("AAPL",)
    assert live_entry_symbol_allowed(
        environment="PROD", account_no="1", symbol="AAPL", engine=engine
    )
    assert not live_entry_symbol_allowed(
        environment="PROD", account_no="1", symbol="RNG", engine=engine
    )
    assert not live_entry_symbol_allowed(
        environment="PROD", account_no="2", symbol="AAPL", engine=engine
    )
    assert not live_entry_symbol_allowed(
        environment="PROD", account_no="1", symbol="AAPL"
    )


def test_controlled_live_allows_only_narrow_entry_completing_position(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path)
    monkeypatch.setattr(execution_config, "KIS_LIVE_EXECUTION_MODE", "CONTROLLED_LIVE")
    _persist_card(
        engine,
        symbol="RNG",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.ENTRY_COMPLETING,
        entry_remaining_target_quantity=4,
    )
    _persist_card(
        engine,
        symbol="OPEN",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.OPEN,
        entry_remaining_target_quantity=4,
    )
    _persist_card(
        engine,
        symbol="EXIT",
        board_status=BoardStatus.OPEN_POSITION,
        position_runtime_status=PositionRuntimeStatus.ENTRY_COMPLETING,
        entry_remaining_target_quantity=4,
        exit_all_required=True,
    )

    assert controlled_live_symbols(engine=engine) == ("RNG",)
    assert live_entry_symbol_allowed(
        environment="PROD", account_no="1", symbol="RNG", engine=engine
    )
    assert not live_entry_symbol_allowed(
        environment="PROD", account_no="1", symbol="OPEN", engine=engine
    )
    assert not live_entry_symbol_allowed(
        environment="PROD", account_no="1", symbol="EXIT", engine=engine
    )


def test_controlled_live_fails_closed_when_canonical_lookup_fails(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path)
    monkeypatch.setattr(execution_config, "KIS_LIVE_EXECUTION_MODE", "CONTROLLED_LIVE")
    _persist_card(engine)
    monkeypatch.setattr(
        trade_card_repository,
        "get_trade_card",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    assert not live_entry_symbol_allowed(
        environment="PROD", account_no="1", symbol="AAPL", engine=engine
    )

    monkeypatch.setattr(
        trade_card_repository,
        "get_trade_card",
        lambda *_args, **_kwargs: SimpleNamespace(
            board_status=BoardStatus.OPEN_POSITION,
            position_runtime_status=PositionRuntimeStatus.ENTRY_COMPLETING,
            entry_remaining_target_quantity="malformed",
        ),
    )
    assert not live_entry_symbol_allowed(
        environment="PROD", account_no="1", symbol="AAPL", engine=engine
    )


def test_controlled_live_configuration_requires_no_retry_spacing_and_budgets(
    monkeypatch,
):
    _configure_controlled_live(monkeypatch)
    scheduler = KisRequestScheduler(
        max_confirmed_mutation_attempts=1,
        min_mutation_spacing_seconds=0.2,
    )

    require_controlled_live_configuration(
        environment="PROD", scheduler=scheduler
    )

    monkeypatch.setattr(execution_config, "KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS", 2)
    with pytest.raises(LiveExecutionEnvelopeError, match="forbids automatic"):
        require_controlled_live_configuration(
            environment="PROD", scheduler=scheduler
        )


def test_invalid_entry_risk_override_blocks_buy_but_not_protective_sell(
    monkeypatch,
):
    _configure_controlled_live(monkeypatch)
    monkeypatch.setattr(
        execution_config,
        "entry_configuration_issues",
        lambda: ("PORTFOLIO_MAX_GROSS_NOTIONAL_FRACTION: invalid",),
    )

    require_live_entry_allowed(
        environment="PROD",
        symbol="AAPL",
        side=OrderSide.SELL,
        quantity=1,
        limit_price=100.0,
    )
    with pytest.raises(LiveExecutionEnvelopeError, match="entry-risk"):
        require_live_entry_allowed(
            environment="PROD",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=1,
            limit_price=100.0,
        )


def test_buyboard_engine_defaults_enabled_but_accepts_explicit_recovery_disable(
    monkeypatch,
):
    monkeypatch.delenv("BUYBOARD_ENGINE_ENABLED", raising=False)
    assert execution_config.is_buyboard_engine_enabled() is True

    monkeypatch.setenv("BUYBOARD_ENGINE_ENABLED", "false")
    assert execution_config.is_buyboard_engine_enabled() is False


@pytest.mark.parametrize("engine_enabled", [True, False])
@pytest.mark.usefixtures("trading_enabled")
def test_disabled_live_envelope_runs_engine_but_blocks_submit_sell_and_cancel(
    monkeypatch, engine_enabled,
):
    monkeypatch.setattr(execution_config, "KIS_LIVE_EXECUTION_MODE", "DISABLED")
    monkeypatch.setattr(
        execution_config,
        "is_buyboard_engine_enabled",
        lambda: engine_enabled,
    )
    calls = []
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: calls.append(("submit", kwargs)),
    )
    monkeypatch.setattr(
        kis_order,
        "cancel_overseas_order",
        lambda **kwargs: calls.append(("cancel", kwargs)),
    )

    # Engine startup/reconciliation is allowed in the mutation-blocked mode.
    require_controlled_live_configuration(environment="PROD")
    broker = KisBroker()
    for side in (OrderSide.BUY, OrderSide.SELL):
        with pytest.raises(LiveExecutionEnvelopeError, match="DISABLED"):
            broker.submit_order(
                environment="PROD",
                account_no="1",
                symbol="AAPL",
                side=side,
                quantity=1,
                limit_price=100.0,
            )
    with pytest.raises(LiveExecutionEnvelopeError, match="DISABLED"):
        broker.cancel_order(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            broker_order_id="B-1",
            quantity=1,
        )

    assert calls == []


@pytest.mark.usefixtures("trading_enabled")
def test_controlled_live_entry_envelope_blocks_unlisted_or_oversized_buy(
    tmp_path, monkeypatch,
):
    _configure_controlled_live(monkeypatch)
    engine = _engine(tmp_path)
    _persist_card(engine)
    calls = []
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: calls.append(kwargs)
        or {"rt_cd": "0", "output": {"ODNO": "B-1"}},
    )
    broker = KisBroker(live_entry_engine=engine)

    accepted = broker.submit_order(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=2,
        limit_price=100.0,
    )
    assert accepted.broker_order_id == "B-1"

    with pytest.raises(LiveExecutionEnvelopeError, match="unapproved symbol") as unlisted:
        broker.submit_order(
            environment="PROD",
            account_no="1",
            symbol="MSFT",
            side=OrderSide.BUY,
            quantity=1,
            limit_price=100.0,
        )
    with pytest.raises(LiveExecutionEnvelopeError, match="maximum notional"):
        broker.submit_order(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=6,
            limit_price=100.0,
        )

    assert len(calls) == 1
    assert broker.is_ambiguous_submission_error(unlisted.value) is False


@pytest.mark.usefixtures("trading_enabled")
def test_controlled_live_entry_cap_never_blocks_protective_sell(monkeypatch):
    _configure_controlled_live(monkeypatch)
    calls = []
    monkeypatch.setattr(
        kis_order,
        "place_overseas_order",
        lambda **kwargs: calls.append(kwargs)
        or {"rt_cd": "0", "output": {"ODNO": "S-1"}},
    )

    result = KisBroker().submit_order(
        environment="PROD",
        account_no="1",
        symbol="UNLISTED",
        side=OrderSide.SELL,
        quantity=100,
        limit_price=100.0,
    )

    assert result.broker_order_id == "S-1"
    assert calls[0]["side"] == "sell"


def test_controlled_live_envelope_allows_tracked_cancellation(monkeypatch):
    _configure_controlled_live(monkeypatch)
    expected = object()
    calls = []
    monkeypatch.setattr(
        kis_order,
        "cancel_overseas_order",
        lambda **kwargs: calls.append(kwargs) or expected,
    )

    result = KisBroker().cancel_order(
        environment="PROD",
        account_no="1",
        symbol="AAPL",
        broker_order_id="B-1",
        quantity=1,
    )

    assert result is expected
    assert calls == [
        {
            "environment": "PROD",
            "account_no": "1",
            "symbol": "AAPL",
            "broker_order_id": "B-1",
            "quantity": 1,
        }
    ]


@pytest.mark.usefixtures("trading_enabled")
@pytest.mark.parametrize(
    "operation", ["submit", "cancel", "reserved_submit", "reserved_cancel"]
)
def test_controlled_live_token_expiry_never_repeats_a_mutation(
    monkeypatch, operation
):
    _configure_controlled_live(monkeypatch)
    auth_calls = []
    post_calls = []

    class FakeClient:
        def __init__(self, _config):
            pass

        def authenticate(self, force_refresh=False):
            auth_calls.append(bool(force_refresh))

        def _headers(self, **_kwargs):
            return {}

    monkeypatch.setattr(
        kis_order,
        "load_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            cano="12345678",
            account_product_code="01",
            base_url="https://example.invalid",
        ),
    )
    monkeypatch.setattr(kis_order, "KisAccountClient", FakeClient)

    def token_expired(*_args, **_kwargs):
        post_calls.append(True)
        raise KisTokenError("expired before acceptance")

    monkeypatch.setattr(kis_order, "_scheduled_post_json", token_expired)

    with pytest.raises(KisTokenError):
        if operation == "submit":
            kis_order.place_overseas_order(
                environment="PROD",
                account_no="12345678-01",
                symbol="AAPL",
                quantity=1,
                price=100.0,
                side="buy",
            )
        elif operation == "cancel":
            kis_order.cancel_overseas_order(
                environment="PROD",
                account_no="12345678-01",
                symbol="AAPL",
                broker_order_id="B-1",
                quantity=1,
            )
        elif operation == "reserved_submit":
            kis_order.place_overseas_reserved_market_on_open_sell(
                environment="PROD",
                account_no="12345678-01",
                symbol="AAPL",
                quantity=1,
            )
        else:
            kis_order.cancel_overseas_reserved_order(
                environment="PROD",
                account_no="12345678-01",
                broker_order_id="R-1",
                reservation_date="20260817",
            )

    assert post_calls == [True]
    assert auth_calls == [False]
