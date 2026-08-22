from src.core.execution_queue import calculate_position_values
from src.core.position_sizer import PositionSizer as LegacyPositionSizer
from src.risk.orb_position import (
    OrbSettings,
    calculate_orb_position_values,
    configure_orb_settings,
    get_orb_settings,
    is_orb_position_plan_valid,
    score_orb_position_recommendation,
    validate_orb_position_values,
)
from src.risk.position_sizer import PositionSizer
from src.ui.main_window import MainWindow


def test_orb_sizing_call_sites_share_authoritative_calculation():
    args = (100_000.0, 0.01, 100.0, 95.0, 8.0)
    expected = calculate_orb_position_values(*args)

    assert MainWindow._calculate_orb_position_values(*args) == expected
    execution_values = calculate_position_values(*args)
    assert execution_values == {
        key: (int(value) if key == "shares" else value)
        for key, value in expected.items()
        if key != "total_risk"
    }


def test_orb_risk_thresholds_remain_inclusive_at_10_and_exclusive_at_30():
    base = {
        "shares": 1.0,
        "stop_loss_percent": 1.0,
        "sl_adr": 50.0,
    }
    at_ten = {**base, "capital_percent": 10.0}
    at_thirty = {**base, "capital_percent": 30.0}

    assert is_orb_position_plan_valid(at_ten, adr_percent=10.0) is True
    assert is_orb_position_plan_valid(at_thirty, adr_percent=10.0) is False
    assert MainWindow._orb_position_plan_is_valid(at_ten, 10.0) is True


def test_orb_recommendation_score_is_shared_across_call_sites():
    sizing = {"capital_percent": 17.5, "sl_adr": 65.0}
    expected = score_orb_position_recommendation(sizing, 0.01)

    assert MainWindow._score_orb_position_recommendation(sizing, 0.01) == expected


def test_pre_p1_position_sizer_import_remains_compatible():
    assert LegacyPositionSizer is PositionSizer


def test_shared_orb_sizing_fails_closed_for_unsafe_inputs():
    for risk_percent in (0.0, 1.01, float("nan"), float("inf")):
        sizing = calculate_orb_position_values(
            100_000.0,
            risk_percent,
            100.0,
            95.0,
            8.0,
        )
        assert sizing["shares"] == 0.0
        assert sizing["investment"] == 0.0

    assert calculate_orb_position_values(
        100_000.0,
        0.01,
        100.0,
        100.0,
        8.0,
    )["shares"] == 0.0


def test_custom_orb_settings_control_bounds_warnings_and_scoring():
    original = get_orb_settings()
    custom = OrbSettings(
        capital_min_percent=20.0,
        capital_ideal_percent=25.0,
        capital_max_percent=40.0,
        stop_adr_min_percent=30.0,
        stop_adr_ideal_percent=45.0,
        stop_adr_max_percent=60.0,
    )
    try:
        configure_orb_settings(custom)
        below_capital = {
            "shares": 1.0,
            "capital_percent": 19.0,
            "stop_loss_percent": 1.0,
            "sl_adr": 45.0,
        }
        ideal = {
            "shares": 1.0,
            "capital_percent": 25.0,
            "stop_loss_percent": 1.0,
            "sl_adr": 45.0,
        }
        old_ideal = {**ideal, "capital_percent": 17.5, "sl_adr": 65.0}

        assert is_orb_position_plan_valid(below_capital, 10.0) is False
        assert "below 20%" in validate_orb_position_values(
            below_capital, 10.0
        )[0]
        assert is_orb_position_plan_valid(ideal, 10.0) is True
        assert score_orb_position_recommendation(
            ideal, 0.01
        ) > score_orb_position_recommendation(old_ideal, 0.01)
    finally:
        configure_orb_settings(original)
