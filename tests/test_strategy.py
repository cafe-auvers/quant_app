import ast
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pandas as pd
import pytest

import src.core.orb as legacy_orb
import src.core.execution_queue as execution_queue
import src.strategy.orb as orb_strategy_module
from src.strategy import (
    MarketSnapshot,
    PortfolioSnapshot,
    SignalDirection,
    SignalKind,
    Strategy,
)
from src.strategy.orb import ORBStrategy, ORBStrategyConfig


ROOT = Path(__file__).resolve().parents[1]


def _completed_session() -> pd.DataFrame:
    index = pd.date_range("2026-08-13 09:30", periods=7, freq="min")
    return pd.DataFrame(
        {
            "Open": [99.5, 100.0, 100.2, 100.4, 100.6, 101.0, 101.5],
            "High": [100.2, 100.5, 100.7, 100.9, 101.0, 101.4, 102.2],
            "Low": [99.0, 99.6, 99.8, 100.0, 100.1, 100.7, 101.1],
            "Close": [100.0, 100.3, 100.5, 100.7, 100.8, 101.2, 102.0],
            "Volume": [1000, 1100, 1050, 1200, 1150, 1300, 1400],
        },
        index=index,
    )


def test_orb_strategy_satisfies_common_strategy_protocol():
    assert isinstance(ORBStrategy(), Strategy)


def test_orb_strategy_matches_existing_entry_signal_for_fixed_market():
    bars = _completed_session()
    orb_range = legacy_orb.calculate_orb_range("aapl", bars, "5m")
    assert orb_range is not None
    direct = legacy_orb.evaluate_orb_entry_signal(
        orb_high=orb_range.high,
        orb_low=orb_range.low,
        breakout_price=100.0,
        current_price=102.0,
        buffer_pct=0.001,
    )

    strategy = ORBStrategy(ORBStrategyConfig(window="5m", buffer_pct=0.001))
    generated = strategy.generate_signal(
        MarketSnapshot(
            symbol="aapl",
            current_price=102.0,
            bars=bars,
            metadata={"breakout_price": 100.0},
        ),
        PortfolioSnapshot(cash=50_000.0, equity=100_000.0),
    )

    assert direct.signal == "confirmed_orb_breakout"
    assert generated is not None
    assert generated.strategy_id == "ORB"
    assert generated.symbol == "AAPL"
    assert generated.direction == SignalDirection.LONG
    assert generated.kind == SignalKind.ENTRY
    assert generated.reason == direct.signal
    assert generated.trigger_price == direct.entry_trigger
    assert generated.reference_price == direct.entry_trigger
    assert generated.stop_price == direct.orb_low
    assert generated.metadata["breakout_trigger"] == direct.breakout_trigger
    assert generated.metadata["size_multiplier"] == direct.suggested_size_multiplier


def test_orb_strategy_emits_no_actionable_signal_before_trigger():
    generated = ORBStrategy(ORBStrategyConfig(window="5m")).generate_signal(
        MarketSnapshot(
            symbol="AAPL",
            current_price=100.5,
            bars=_completed_session(),
            metadata={"breakout_price": 100.0},
        ),
        PortfolioSnapshot(),
    )

    assert generated is None


def test_strategy_contract_snapshots_are_immutable():
    market = MarketSnapshot(
        symbol="aapl",
        current_price=100.0,
        metadata={"breakout_price": 99.0},
    )

    with pytest.raises(FrozenInstanceError):
        market.symbol = "MSFT"
    with pytest.raises(TypeError):
        market.metadata["breakout_price"] = 1.0


def test_core_orb_is_a_static_compatibility_surface():
    assert legacy_orb.calculate_orb_range is orb_strategy_module.calculate_orb_range
    assert (
        legacy_orb.evaluate_orb_entry_signal
        is orb_strategy_module.evaluate_orb_entry_signal
    )
    assert legacy_orb.OrbEntrySignal is orb_strategy_module.OrbEntrySignal
    assert legacy_orb.OrbRange is orb_strategy_module.OrbRange


def test_strategy_layer_has_no_ui_risk_execution_or_broker_dependencies():
    forbidden_prefixes = (
        "src.api",
        "src.infrastructure",
        "src.risk",
        "src.services",
        "src.ui",
    )
    violations = []
    for path in (ROOT / "src" / "strategy").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden_prefixes):
                    violations.append(f"{path}: {module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        violations.append(f"{path}: {alias.name}")
    assert violations == []


def test_live_execution_queue_uses_strategy_plugin_not_legacy_orb_helpers():
    path = ROOT / "src" / "core" / "execution_queue.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "src.strategy.orb" in imported_modules
    assert "src.core.orb" not in imported_modules
    assert "ORBStrategy(" in source


def test_live_queue_fails_closed_when_strategy_emits_no_entry_signal(monkeypatch):
    class MissingSignalORBStrategy(ORBStrategy):
        def evaluate(self, market, portfolio):
            evaluation = super().evaluate(market, portfolio)
            assert evaluation.entry is not None
            assert evaluation.entry.allow_entry is True
            return replace(evaluation, signal=None)

    monkeypatch.setattr(execution_queue, "ORBStrategy", MissingSignalORBStrategy)

    candidate = execution_queue.build_orb_candidate(
        symbol="AAPL",
        window="5m",
        intraday=_completed_session(),
        breakout_price=100.0,
        current_price=102.0,
        account_size=100_000.0,
        risk_percent=0.005,
        adr_percent=4.0,
        stop_loss=99.0,
    )

    assert candidate.valid is False
    assert candidate.status == execution_queue.OrbCandidateStatus.REJECTED
    assert candidate.reason == "ORB strategy did not emit an entry signal"
