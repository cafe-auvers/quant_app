from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.risk.portfolio import (
    MAX_PORTFOLIO_POSITIONS,
    PortfolioPositionRisk,
    PortfolioProjectedExposure,
    PortfolioRiskLimits,
    PortfolioRiskManager,
    PortfolioRiskSnapshot,
    ProposedPortfolioEntry,
)


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _proposal(**overrides) -> ProposedPortfolioEntry:
    values = {
        "symbol": "MSFT",
        "quantity": 10,
        "reference_price": 100.0,
        "stop_price": 95.0,
        "strategy_id": "ORB",
        "sector": "TECH",
        "industry": "SOFTWARE",
        "correlation_group": "MEGA_CAP_TECH",
    }
    values.update(overrides)
    return ProposedPortfolioEntry(**values)


def _snapshot(**overrides) -> PortfolioRiskSnapshot:
    values = {
        "account_equity_usd": 10_000.0,
        "usable_buying_power_usd": 5_000.0,
        "positions": (),
        "evaluated_at": NOW,
    }
    values.update(overrides)
    return PortfolioRiskSnapshot(**values)


def _projected(symbol: str, *, gross: float = 100.0, risk: float = 1.0):
    return PortfolioProjectedExposure(
        symbol=symbol,
        gross_notional_usd=gross,
        open_risk_usd=risk,
        source="PENDING_BUY",
    )


def test_controlled_live_defaults_and_hard_position_ceiling():
    limits = PortfolioRiskLimits()

    assert limits.max_simultaneous_positions == 30
    assert limits.max_total_open_risk_fraction == 0.10
    assert limits.max_gross_notional_fraction == 2.0
    assert MAX_PORTFOLIO_POSITIONS == 30
    with pytest.raises(ValueError, match="between 1 and 30"):
        PortfolioRiskLimits(max_simultaneous_positions=31)


def test_thirtieth_unique_projected_position_is_permitted():
    projected = tuple(_projected(f"SYM{index:02d}") for index in range(29))
    manager = PortfolioRiskManager(PortfolioRiskLimits())

    decision = manager.evaluate_entry(
        _proposal(symbol="SYM29"),
        _snapshot(projected_exposures=projected),
    )

    assert decision.approved is True
    assert decision.position_count_after == 30


def test_thirty_first_unique_projected_position_is_rejected():
    projected = tuple(_projected(f"SYM{index:02d}") for index in range(30))
    manager = PortfolioRiskManager(PortfolioRiskLimits())

    decision = manager.evaluate_entry(
        _proposal(symbol="SYM30"),
        _snapshot(projected_exposures=projected),
    )

    assert decision.approved is False
    assert decision.position_count_after == 31
    assert any("simultaneous positions" in reason for reason in decision.reasons)


def test_multiple_pending_orders_for_same_symbol_count_as_one_position():
    projected = (
        _projected("AAPL"),
        _projected("aapl"),
        *tuple(_projected(f"SYM{index:02d}") for index in range(28)),
    )
    manager = PortfolioRiskManager(PortfolioRiskLimits())

    decision = manager.evaluate_entry(
        _proposal(symbol="MSFT"),
        _snapshot(projected_exposures=projected),
    )

    assert decision.approved is True
    assert decision.position_count_after == 30
    assert decision.gross_notional_after_usd == 4_000.0


@pytest.mark.parametrize("open_risk_limit", [0.10, 0.20])
def test_controlled_and_full_live_open_risk_boundaries(open_risk_limit):
    equity = 10_000.0
    proposed_open_risk = 50.0
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(max_total_open_risk_fraction=open_risk_limit)
    )
    at_limit = _projected(
        "AAPL",
        gross=1_000.0,
        risk=(equity * open_risk_limit) - proposed_open_risk,
    )
    above_limit = _projected(
        "AAPL",
        gross=1_000.0,
        risk=(equity * open_risk_limit) - proposed_open_risk + 0.01,
    )

    permitted = manager.evaluate_entry(
        _proposal(), _snapshot(projected_exposures=(at_limit,))
    )
    rejected = manager.evaluate_entry(
        _proposal(), _snapshot(projected_exposures=(above_limit,))
    )

    assert permitted.approved is True
    assert permitted.total_open_risk_after_usd == equity * open_risk_limit
    assert rejected.approved is False
    assert any("total open risk" in reason for reason in rejected.reasons)


def test_two_hundred_percent_gross_notional_is_an_extreme_ceiling_only():
    manager = PortfolioRiskManager(PortfolioRiskLimits())

    below = manager.evaluate_entry(
        _proposal(),
        _snapshot(projected_exposures=(_projected("AAPL", gross=18_999.99),)),
    )
    at_limit = manager.evaluate_entry(
        _proposal(),
        _snapshot(projected_exposures=(_projected("AAPL", gross=19_000.0),)),
    )
    above = manager.evaluate_entry(
        _proposal(),
        _snapshot(projected_exposures=(_projected("AAPL", gross=19_000.01),)),
    )

    assert below.approved is True
    assert below.reasons == ()
    assert at_limit.approved is True
    assert at_limit.gross_notional_after_usd == 20_000.0
    assert above.approved is False
    assert any("gross notional" in reason for reason in above.reasons)


def test_approves_entry_within_all_aggregate_limits():
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(
            max_simultaneous_positions=3,
            max_total_open_risk_fraction=0.03,
            max_gross_notional_fraction=0.50,
            max_incremental_buying_power_fraction=0.50,
            max_sector_notional_fraction=0.30,
            max_industry_notional_fraction=0.20,
            max_correlation_group_notional_fraction=0.30,
            max_strategy_notional_fraction=0.50,
        )
    )

    decision = manager.evaluate_entry(_proposal(), _snapshot())

    assert decision.approved is True
    assert decision.reasons == ()
    assert decision.position_count_after == 1
    assert decision.total_open_risk_after_usd == 50.0
    assert decision.gross_notional_after_usd == 1_000.0


def test_rejects_collective_position_risk_gross_and_count_before_submit():
    positions = (
        PortfolioPositionRisk("AAPL", 10, 200.0, 180.0, "ORB"),
        PortfolioPositionRisk("NVDA", 10, 100.0, 90.0, "ORB"),
    )
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(
            max_simultaneous_positions=2,
            max_total_open_risk_fraction=0.02,
            max_gross_notional_fraction=0.35,
            max_incremental_buying_power_fraction=1.0,
        )
    )

    decision = manager.evaluate_entry(
        _proposal(), _snapshot(positions=positions)
    )

    assert decision.approved is False
    assert any("simultaneous positions" in reason for reason in decision.reasons)
    assert any("total open risk" in reason for reason in decision.reasons)
    assert any("gross notional" in reason for reason in decision.reasons)


def test_rejects_incremental_buying_power_overuse():
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(max_incremental_buying_power_fraction=0.25)
    )

    decision = manager.evaluate_entry(
        _proposal(), _snapshot(usable_buying_power_usd=2_000.0)
    )

    assert decision.approved is False
    assert any("buying-power" in reason for reason in decision.reasons)


def test_daily_loss_and_drawdown_are_fail_closed_when_enabled():
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(
            max_daily_loss_fraction=0.02,
            max_drawdown_fraction=0.10,
        )
    )
    missing = manager.evaluate_entry(_proposal(), _snapshot())
    breached = manager.evaluate_entry(
        _proposal(),
        _snapshot(
            daily_realized_pnl_usd=-150.0,
            daily_unrealized_pnl_usd=-100.0,
            high_water_equity_usd=12_000.0,
        ),
    )

    assert missing.approved is False
    assert any("daily P&L" in reason for reason in missing.reasons)
    assert any("high-water" in reason for reason in missing.reasons)
    assert breached.approved is False
    assert any("daily realized" in reason for reason in breached.reasons)
    assert any("drawdown" in reason for reason in breached.reasons)


def test_concentration_limits_require_classification_and_include_existing_exposure():
    existing = PortfolioPositionRisk(
        "AAPL",
        10,
        200.0,
        180.0,
        strategy_id="ORB",
        sector="TECH",
        industry="HARDWARE",
        correlation_group="MEGA_CAP_TECH",
    )
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(
            max_sector_notional_fraction=0.25,
            max_industry_notional_fraction=0.20,
            max_correlation_group_notional_fraction=0.25,
            max_strategy_notional_fraction=0.25,
        )
    )
    concentrated = manager.evaluate_entry(
        _proposal(), _snapshot(positions=(existing,))
    )
    missing_classification = manager.evaluate_entry(
        _proposal(sector="", industry="", correlation_group=""), _snapshot()
    )

    assert concentrated.approved is False
    assert any("sector concentration" in reason for reason in concentrated.reasons)
    assert any("correlation group" in reason for reason in concentrated.reasons)
    assert any("strategy concentration" in reason for reason in concentrated.reasons)
    assert missing_classification.approved is False
    assert any("Sector classification" in reason for reason in missing_classification.reasons)
    assert any("Industry classification" in reason for reason in missing_classification.reasons)


def test_non_usd_equity_rejects_missing_or_stale_fx():
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(max_fx_age=timedelta(minutes=5))
    )
    missing = manager.evaluate_entry(
        _proposal(), _snapshot(equity_source_currency="KRW")
    )
    stale = manager.evaluate_entry(
        _proposal(),
        _snapshot(
            equity_source_currency="KRW",
            fx_rate_to_usd=0.00075,
            fx_observed_at=NOW - timedelta(minutes=6),
        ),
    )
    fresh = manager.evaluate_entry(
        _proposal(),
        _snapshot(
            equity_source_currency="KRW",
            fx_rate_to_usd=0.00075,
            fx_observed_at=NOW - timedelta(minutes=1),
        ),
    )

    assert missing.approved is False
    assert stale.approved is False
    assert fresh.approved is True


def test_pending_reserved_and_unresolved_buy_exposure_is_account_wide():
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(
            max_simultaneous_positions=2,
            max_total_open_risk_fraction=0.08,
            max_gross_notional_fraction=0.25,
        )
    )
    projected = (
        PortfolioProjectedExposure(
            symbol="AAPL",
            gross_notional_usd=1_000.0,
            open_risk_usd=100.0,
            source="PENDING_BUY",
            reservation_id="RES-1",
        ),
        PortfolioProjectedExposure(
            symbol="NVDA",
            gross_notional_usd=1_000.0,
            open_risk_usd=1_000.0,
            source="UNRESOLVED_EXTERNAL_BUY",
        ),
    )

    decision = manager.evaluate_entry(
        _proposal(
            environment="PROD",
            account_no="1",
            symbol="MSFT",
            quantity=5,
            reference_price=100.0,
            stop_price=90.0,
        ),
        _snapshot(projected_exposures=projected),
    )

    assert decision.approved is False
    assert decision.position_count_after == 3
    assert decision.gross_notional_after_usd == 2_500.0
    assert decision.total_open_risk_after_usd == 1_150.0
    assert any("simultaneous positions" in reason for reason in decision.reasons)
    assert any("total open risk" in reason for reason in decision.reasons)


def test_atomic_spec_excludes_durable_reservations_but_keeps_external_exposure():
    manager = PortfolioRiskManager(PortfolioRiskLimits(max_simultaneous_positions=5))
    decision = manager.evaluate_entry(
        _proposal(environment="PROD", account_no="1"),
        _snapshot(
            projected_exposures=(
                PortfolioProjectedExposure(
                    symbol="AAPL",
                    gross_notional_usd=1_000.0,
                    open_risk_usd=100.0,
                    source="PENDING_BUY",
                    reservation_id="RES-1",
                ),
                PortfolioProjectedExposure(
                    symbol="NVDA",
                    gross_notional_usd=500.0,
                    open_risk_usd=500.0,
                    source="UNRESOLVED_EXTERNAL_BUY",
                ),
            )
        ),
    )

    spec = decision.reservation_spec
    assert spec is not None
    assert spec.baseline_position_symbols == ("NVDA",)
    assert spec.baseline_gross_notional_usd == 500.0
    assert spec.baseline_open_risk_usd == 500.0


def test_replacement_evaluates_net_exposure_without_dropping_filled_risk():
    manager = PortfolioRiskManager(
        PortfolioRiskLimits(
            max_total_open_risk_fraction=0.10,
            max_gross_notional_fraction=0.12,
        )
    )
    filled = PortfolioPositionRisk("AAPL", 2, 100.0, 95.0, "ORB")
    pending = PortfolioProjectedExposure(
        symbol="AAPL",
        gross_notional_usd=1_000.0,
        open_risk_usd=50.0,
        source="PENDING_BUY",
        reservation_id="ORIGINAL-RESERVATION",
    )

    decision = manager.evaluate_entry(
        _proposal(
            environment="PROD",
            account_no="1",
            symbol="AAPL",
            quantity=10,
        ),
        _snapshot(positions=(filled,), projected_exposures=(pending,)),
        replaced_reservation_id="ORIGINAL-RESERVATION",
    )

    assert decision.approved is True
    assert decision.position_count_after == 1
    assert decision.gross_notional_after_usd == 1_200.0
    assert decision.total_open_risk_after_usd == 60.0
    assert decision.reservation_spec is not None
    assert decision.reservation_spec.baseline_gross_notional_usd == 200.0
