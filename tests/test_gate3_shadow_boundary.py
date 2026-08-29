from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gate3.shadow_boundary import (
    SHADOW_LABEL,
    ShadowEventStore,
    ShadowExecutionBoundary,
    ShadowMutationIntercepted,
)
from gate3.decision_oracle import (
    expected_entry,
    expected_exact_cancel,
    expected_higher_timeframe_replacement,
    expected_protective_sell,
    oracle_source_sha256,
)
from src.core.execution_request import (
    CancelExecutionRequest,
    ReplaceExecutionRequest,
    SubmitExecutionRequest,
)
from src.core.order_state import OrderIntent, OrderSide


class ExplodingBroker:
    def submit_guarded(self, _request):
        raise AssertionError("destructive broker delegate must never be called")


def _boundary(tmp_path):
    return ShadowExecutionBoundary(
        store=ShadowEventStore(tmp_path / "gate3.shadow.jsonl"),
        read_delegate=ExplodingBroker(),
        order_context_lookup=lambda _client_order_id: {
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": 10,
        },
        clock=lambda: datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc),
    )


def test_guarded_buy_is_durably_intercepted_without_fake_ack_or_fill(tmp_path):
    boundary = _boundary(tmp_path)
    request = SubmitExecutionRequest(
        client_order_id="entry-1",
        environment="PROD",
        account_no="secret-account",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=10,
        limit_price=100.0,
    )

    with pytest.raises(ShadowMutationIntercepted) as captured:
        boundary.submit_guarded(request)

    event = captured.value.event
    assert event.event_type == "WOULD_SUBMIT"
    assert event.label == SHADOW_LABEL
    assert event.account_ref != "secret-account"
    assert not hasattr(event, "broker_order_id")
    assert not hasattr(event, "filled_quantity")
    assert boundary.store.read_all() == [event]
    audit = boundary.store.audit()
    assert audit.passed is True
    assert audit.event_counts["WOULD_SUBMIT"] == 1
    assert audit.sha256 == boundary.store.sha256()


def test_guarded_sell_cancel_and_replace_use_distinct_would_events(tmp_path):
    boundary = _boundary(tmp_path)
    sell = SubmitExecutionRequest(
        client_order_id="sell-1",
        environment="PROD",
        account_no="account",
        symbol="AAPL",
        side=OrderSide.SELL,
        intent=OrderIntent.MANUAL_EXIT,
        quantity=10,
        limit_price=99.0,
    )
    cancel = CancelExecutionRequest(
        client_order_id="entry-1",
        cancel_command_id="cancel-1",
        environment="PROD",
        account_no="account",
        symbol="AAPL",
        side="BUY",
        quantity=10,
    )
    replace = ReplaceExecutionRequest(
        client_order_id="entry-1",
        replace_command_id="replace-1",
        new_client_order_id="entry-2",
        new_quantity=11,
        new_limit_price=98.0,
        environment="PROD",
        account_no="account",
    )

    for call in (
        lambda: boundary.submit_guarded(sell),
        lambda: boundary.cancel_guarded(cancel),
        lambda: boundary.replace_guarded(replace),
    ):
        with pytest.raises(ShadowMutationIntercepted):
            call()

    assert [event.event_type for event in boundary.store.read_all()] == [
        "WOULD_SELL",
        "WOULD_CANCEL",
        "WOULD_REPLACE",
    ]


def test_shadow_store_rejects_production_ledger_location(tmp_path):
    production = tmp_path / "production-ledgers"

    with pytest.raises(ValueError, match="production ledger"):
        ShadowEventStore(
            production / "gate3.shadow.jsonl",
            production_paths=(production,),
        )


def test_shadow_boundary_rejects_missing_stable_command_identity(tmp_path):
    boundary = _boundary(tmp_path)

    with pytest.raises(ValueError, match="stable command identity"):
        boundary.submit_order(
            environment="PROD",
            account_no="account",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=1,
            limit_price=100.0,
        )

    assert boundary.store.read_all() == []


def test_shadow_store_audit_exposes_corruption_instead_of_skipping_it(tmp_path):
    boundary = _boundary(tmp_path)
    request = SubmitExecutionRequest(
        client_order_id="entry-1",
        environment="PROD",
        account_no="account",
        symbol="AAPL",
        side=OrderSide.BUY,
        intent=OrderIntent.ENTRY,
        quantity=1,
        limit_price=100.0,
    )
    with pytest.raises(ShadowMutationIntercepted):
        boundary.submit_guarded(request)
    with boundary.store.path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")

    audit = boundary.store.audit()

    assert audit.passed is False
    assert audit.event_count == 1
    assert audit.parse_error_count == 1


def test_gate3_oracle_matches_finalized_entry_and_upgrade_contract():
    entry = expected_entry(
        orb_high=105.0,
        orb_low=95.0,
        breakout_price=100.0,
        execution_price=102.0,
        breakout_confirmed=True,
        last_trade=106.0,
        best_ask=105.5,
        regular_session_open=True,
        quote_fresh=True,
        mutation_enabled=True,
        lease_current=True,
        ownership_current=True,
        reconciliation_clear=True,
    )
    tie_upgrade = expected_higher_timeframe_replacement(
        current_window="1m",
        current_score=10.0,
        candidate_window="5m",
        candidate_score=10.0,
        candidate_confirmed=True,
        zero_fill=True,
        exact_cancel_confirmed=True,
        candidate_plan_valid=True,
        mutation_enabled=True,
        lease_current=True,
        ownership_current=True,
        reconciliation_clear=True,
    )

    assert entry.expected_event == "WOULD_SUBMIT"
    assert entry.matches("WOULD_SUBMIT") is True
    assert tie_upgrade.allowed is False
    assert "not_strictly_better_higher_timeframe" in tie_upgrade.block_reasons
    assert len(oracle_source_sha256()) == 64


def test_gate3_oracle_keeps_entry_risk_out_of_protective_actions():
    cancel = expected_exact_cancel(
        exact_order_owned=True,
        mutation_enabled=True,
        lease_current=True,
        ownership_current=True,
        reconciliation_clear=True,
    )
    sell = expected_protective_sell(
        quantity=5,
        execution_price_available=True,
        mutation_enabled=True,
        lease_current=True,
        ownership_current=True,
        reconciliation_clear=True,
    )

    assert cancel.expected_event == "WOULD_CANCEL"
    assert sell.expected_event == "WOULD_SELL"
