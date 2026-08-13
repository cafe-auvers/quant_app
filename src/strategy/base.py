"""Strategy-neutral market, portfolio, and signal contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


def _immutable_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class SignalKind(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


@dataclass(frozen=True)
class MarketSnapshot:
    """Point-in-time market input supplied identically to live and backtest code."""

    symbol: str
    current_price: Optional[float] = None
    bars: Any = None
    as_of: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", str(self.symbol or "").strip().upper())
        object.__setattr__(self, "metadata", _immutable_mapping(self.metadata))


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Read-only portfolio state visible to a strategy at evaluation time."""

    cash: float = 0.0
    equity: float = 0.0
    positions: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        positions = {
            str(symbol or "").strip().upper(): quantity
            for symbol, quantity in dict(self.positions or {}).items()
            if str(symbol or "").strip()
        }
        object.__setattr__(self, "positions", _immutable_mapping(positions))
        object.__setattr__(self, "metadata", _immutable_mapping(self.metadata))


@dataclass(frozen=True)
class Signal:
    """Strategy output consumed by risk before it can become an order intent."""

    strategy_id: str
    symbol: str
    direction: SignalDirection
    kind: SignalKind
    reference_price: float
    trigger_price: Optional[float] = None
    stop_price: Optional[float] = None
    reason: str = ""
    generated_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_id", str(self.strategy_id or "").strip().upper())
        object.__setattr__(self, "symbol", str(self.symbol or "").strip().upper())
        object.__setattr__(self, "metadata", _immutable_mapping(self.metadata))


@runtime_checkable
class Strategy(Protocol):
    """Common strategy interface for live, paper, and future backtest runners."""

    strategy_id: str

    def generate_signal(
        self,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
    ) -> Optional[Signal]:
        ...
