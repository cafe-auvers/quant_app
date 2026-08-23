"""Pure portfolio-level risk evaluation for exposure-increasing entries.

The manager owns aggregate arithmetic only.  It performs no I/O and knows
nothing about Qt, KIS, repositories, or order submission.  Runtime composition
builds a fresh :class:`PortfolioRiskSnapshot` and binds any rejection into the
same short-lived pre-trade approval already enforced at the broker gateway.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Tuple


MAX_PORTFOLIO_POSITIONS = 30


def _finite_nonnegative(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _normalized_text(value: str) -> str:
    return str(value or "").strip().upper()


@dataclass(frozen=True)
class PortfolioRiskLimits:
    """Account-level limits; zero disables an optional advanced limit."""

    max_simultaneous_positions: int = MAX_PORTFOLIO_POSITIONS
    max_total_open_risk_fraction: float = 0.10
    max_gross_notional_fraction: float = 10.0
    max_incremental_buying_power_fraction: float = 0.0
    max_daily_loss_fraction: float = 0.0
    max_drawdown_fraction: float = 0.0
    max_sector_notional_fraction: float = 0.0
    max_industry_notional_fraction: float = 0.0
    max_correlation_group_notional_fraction: float = 0.0
    max_strategy_notional_fraction: float = 0.0
    max_fx_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        position_limit = int(self.max_simultaneous_positions)
        if not 1 <= position_limit <= MAX_PORTFOLIO_POSITIONS:
            raise ValueError(
                "max_simultaneous_positions must be between 1 and "
                f"{MAX_PORTFOLIO_POSITIONS}"
            )
        for name in (
            "max_total_open_risk_fraction",
            "max_gross_notional_fraction",
            "max_incremental_buying_power_fraction",
            "max_daily_loss_fraction",
            "max_drawdown_fraction",
            "max_sector_notional_fraction",
            "max_industry_notional_fraction",
            "max_correlation_group_notional_fraction",
            "max_strategy_notional_fraction",
        ):
            _finite_nonnegative(getattr(self, name), name)
        if self.max_fx_age <= timedelta(0):
            raise ValueError("max_fx_age must be positive")


@dataclass(frozen=True)
class PortfolioPositionRisk:
    symbol: str
    quantity: int
    mark_price: float
    stop_price: float
    strategy_id: str = ""
    sector: str = ""
    industry: str = ""
    correlation_group: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normalized_text(self.symbol))
        object.__setattr__(self, "strategy_id", _normalized_text(self.strategy_id))
        object.__setattr__(self, "sector", _normalized_text(self.sector))
        object.__setattr__(self, "industry", _normalized_text(self.industry))
        object.__setattr__(
            self, "correlation_group", _normalized_text(self.correlation_group)
        )
        if not self.symbol:
            raise ValueError("position symbol is required")
        if int(self.quantity) <= 0:
            raise ValueError("position quantity must be positive")
        if _finite_nonnegative(self.mark_price, "mark_price") <= 0:
            raise ValueError("mark_price must be positive")
        _finite_nonnegative(self.stop_price, "stop_price")

    @property
    def notional(self) -> float:
        return int(self.quantity) * float(self.mark_price)

    @property
    def open_risk(self) -> float:
        return int(self.quantity) * max(
            0.0, float(self.mark_price) - float(self.stop_price)
        )


@dataclass(frozen=True)
class PortfolioProjectedExposure:
    """Exposure not yet represented by a filled position.

    ``reservation_id`` is populated when the durable capital-reservation row
    already represents this exposure.  The final gateway transaction excludes
    those rows from its baseline and re-reads them under the account lock, so a
    second symbol cannot pass using a stale pre-trade snapshot.
    """

    symbol: str
    gross_notional_usd: float
    open_risk_usd: float
    source: str
    reservation_id: str = ""
    strategy_id: str = ""
    sector: str = ""
    industry: str = ""
    correlation_group: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _normalized_text(self.symbol))
        object.__setattr__(self, "source", _normalized_text(self.source))
        object.__setattr__(self, "reservation_id", str(self.reservation_id or "").strip())
        object.__setattr__(self, "strategy_id", _normalized_text(self.strategy_id))
        object.__setattr__(self, "sector", _normalized_text(self.sector))
        object.__setattr__(self, "industry", _normalized_text(self.industry))
        object.__setattr__(
            self, "correlation_group", _normalized_text(self.correlation_group)
        )
        if not self.symbol:
            raise ValueError("projected exposure symbol is required")
        if not self.source:
            raise ValueError("projected exposure source is required")
        if _finite_nonnegative(self.gross_notional_usd, "gross_notional_usd") <= 0:
            raise ValueError("projected gross notional must be positive")
        _finite_nonnegative(self.open_risk_usd, "open_risk_usd")

    @property
    def notional(self) -> float:
        return float(self.gross_notional_usd)

    @property
    def open_risk(self) -> float:
        return float(self.open_risk_usd)


@dataclass(frozen=True)
class ProposedPortfolioEntry:
    symbol: str
    quantity: int
    reference_price: float
    stop_price: float
    strategy_id: str
    environment: str = ""
    account_no: str = ""
    sector: str = ""
    industry: str = ""
    correlation_group: str = ""

    def as_position(self) -> PortfolioPositionRisk:
        return PortfolioPositionRisk(
            symbol=self.symbol,
            quantity=self.quantity,
            mark_price=self.reference_price,
            stop_price=self.stop_price,
            strategy_id=self.strategy_id,
            sector=self.sector,
            industry=self.industry,
            correlation_group=self.correlation_group,
        )


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    account_equity_usd: float
    usable_buying_power_usd: float
    positions: Tuple[PortfolioPositionRisk, ...] = ()
    projected_exposures: Tuple[PortfolioProjectedExposure, ...] = ()
    daily_realized_pnl_usd: Optional[float] = None
    daily_unrealized_pnl_usd: Optional[float] = None
    high_water_equity_usd: Optional[float] = None
    equity_source_currency: str = "USD"
    fx_rate_to_usd: Optional[float] = None
    fx_observed_at: Optional[datetime] = None
    evaluated_at: datetime = datetime.min.replace(tzinfo=timezone.utc)

    def __post_init__(self) -> None:
        _finite_nonnegative(self.account_equity_usd, "account_equity_usd")
        _finite_nonnegative(
            self.usable_buying_power_usd, "usable_buying_power_usd"
        )
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(
            self, "projected_exposures", tuple(self.projected_exposures)
        )
        object.__setattr__(
            self, "equity_source_currency", _normalized_text(self.equity_source_currency)
        )
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")


@dataclass(frozen=True)
class PortfolioRiskReservationSpec:
    """Immutable inputs for the gateway's atomic projected-risk reservation."""

    environment: str
    account_no: str
    symbol: str
    proposed_notional_usd: float
    proposed_open_risk_usd: float
    account_equity_usd: float
    baseline_position_symbols: Tuple[str, ...]
    baseline_open_risk_usd: float
    baseline_gross_notional_usd: float
    max_simultaneous_positions: int
    max_total_open_risk_fraction: float
    max_gross_notional_fraction: float
    evaluated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", _normalized_text(self.environment))
        object.__setattr__(self, "account_no", str(self.account_no or "").strip())
        object.__setattr__(self, "symbol", _normalized_text(self.symbol))
        object.__setattr__(
            self,
            "baseline_position_symbols",
            tuple(
                sorted(
                    {
                        _normalized_text(symbol)
                        for symbol in self.baseline_position_symbols
                        if _normalized_text(symbol)
                    }
                )
            ),
        )
        _finite_nonnegative(self.proposed_notional_usd, "proposed_notional_usd")
        _finite_nonnegative(self.proposed_open_risk_usd, "proposed_open_risk_usd")
        _finite_nonnegative(self.account_equity_usd, "account_equity_usd")
        _finite_nonnegative(self.baseline_open_risk_usd, "baseline_open_risk_usd")
        _finite_nonnegative(
            self.baseline_gross_notional_usd, "baseline_gross_notional_usd"
        )
        if not self.environment or not self.account_no or not self.symbol:
            raise ValueError("portfolio reservation scope is incomplete")
        if self.proposed_notional_usd <= 0:
            raise ValueError("portfolio reservation proposed notional must be positive")
        position_limit = int(self.max_simultaneous_positions)
        if not 1 <= position_limit <= MAX_PORTFOLIO_POSITIONS:
            raise ValueError(
                "portfolio reservation position limit must be between 1 and "
                f"{MAX_PORTFOLIO_POSITIONS}"
            )
        _finite_nonnegative(
            self.max_total_open_risk_fraction,
            "max_total_open_risk_fraction",
        )
        _finite_nonnegative(
            self.max_gross_notional_fraction,
            "max_gross_notional_fraction",
        )
        if self.evaluated_at.tzinfo is None:
            raise ValueError("portfolio reservation evaluated_at must be timezone-aware")


@dataclass(frozen=True)
class PortfolioRiskDecision:
    approved: bool
    reasons: Tuple[str, ...]
    position_count_after: int
    total_open_risk_after_usd: float
    gross_notional_after_usd: float
    proposed_notional_usd: float
    reservation_spec: Optional[PortfolioRiskReservationSpec] = None


def _group_notional(
    positions: Iterable[PortfolioPositionRisk], attribute: str, key: str
) -> float:
    if not key:
        return 0.0
    return sum(
        position.notional
        for position in positions
        if getattr(position, attribute) == key
    )


class PortfolioRiskManager:
    """Evaluate one proposed entry against aggregate account exposure."""

    def __init__(self, limits: PortfolioRiskLimits) -> None:
        self.limits = limits

    def evaluate_entry(
        self,
        proposal: ProposedPortfolioEntry,
        snapshot: PortfolioRiskSnapshot,
    ) -> PortfolioRiskDecision:
        reasons: list[str] = []
        try:
            proposed = proposal.as_position()
        except (TypeError, ValueError, OverflowError) as exc:
            return PortfolioRiskDecision(
                approved=False,
                reasons=(f"Invalid portfolio-risk proposal: {exc}",),
                position_count_after=len(snapshot.positions),
                total_open_risk_after_usd=sum(
                    position.open_risk for position in snapshot.positions
                ),
                gross_notional_after_usd=sum(
                    position.notional for position in snapshot.positions
                ),
                proposed_notional_usd=0.0,
            )

        equity = float(snapshot.account_equity_usd)
        buying_power = float(snapshot.usable_buying_power_usd)
        existing = tuple(snapshot.positions) + tuple(snapshot.projected_exposures)
        existing_symbols = {position.symbol for position in existing}
        position_count_after = len(existing_symbols | {proposed.symbol})
        total_open_risk = sum(position.open_risk for position in existing)
        total_open_risk_after = total_open_risk + proposed.open_risk
        gross_notional = sum(position.notional for position in existing)
        gross_notional_after = gross_notional + proposed.notional

        if equity <= 0:
            reasons.append("Fresh positive account equity is required")
        if buying_power <= 0:
            reasons.append("Fresh positive usable buying power is required")
        if position_count_after > self.limits.max_simultaneous_positions:
            reasons.append(
                "Maximum simultaneous positions would be exceeded "
                f"({position_count_after}>{self.limits.max_simultaneous_positions})"
            )
        if equity > 0 and self.limits.max_total_open_risk_fraction > 0:
            fraction = total_open_risk_after / equity
            if fraction > self.limits.max_total_open_risk_fraction:
                reasons.append(
                    "Maximum total open risk would be exceeded "
                    f"({fraction:.2%}>{self.limits.max_total_open_risk_fraction:.2%})"
                )
        if equity > 0 and self.limits.max_gross_notional_fraction > 0:
            fraction = gross_notional_after / equity
            if fraction > self.limits.max_gross_notional_fraction:
                reasons.append(
                    "Maximum gross notional would be exceeded "
                    f"({fraction:.2%}>{self.limits.max_gross_notional_fraction:.2%})"
                )
        if buying_power > 0 and self.limits.max_incremental_buying_power_fraction > 0:
            fraction = proposed.notional / buying_power
            if fraction > self.limits.max_incremental_buying_power_fraction:
                reasons.append(
                    "Maximum incremental buying-power utilization would be exceeded "
                    f"({fraction:.2%}>{self.limits.max_incremental_buying_power_fraction:.2%})"
                )

        daily_values = (
            snapshot.daily_realized_pnl_usd,
            snapshot.daily_unrealized_pnl_usd,
        )
        if self.limits.max_daily_loss_fraction > 0:
            if any(value is None for value in daily_values):
                reasons.append("Fresh realized and unrealized daily P&L are required")
            elif equity > 0:
                daily_pnl = sum(float(value) for value in daily_values if value is not None)
                if daily_pnl < -(equity * self.limits.max_daily_loss_fraction):
                    reasons.append("Maximum daily realized plus unrealized loss reached")

        if self.limits.max_drawdown_fraction > 0:
            high_water = snapshot.high_water_equity_usd
            if high_water is None or not math.isfinite(float(high_water)) or high_water <= 0:
                reasons.append("Fresh high-water equity is required for drawdown control")
            else:
                drawdown = max(0.0, (float(high_water) - equity) / float(high_water))
                if drawdown >= self.limits.max_drawdown_fraction:
                    reasons.append("Maximum portfolio drawdown reached")

        self._check_concentration(
            reasons,
            existing,
            proposed,
            equity,
            attribute="sector",
            limit=self.limits.max_sector_notional_fraction,
            label="sector",
        )
        self._check_concentration(
            reasons,
            existing,
            proposed,
            equity,
            attribute="industry",
            limit=self.limits.max_industry_notional_fraction,
            label="industry",
        )
        self._check_concentration(
            reasons,
            existing,
            proposed,
            equity,
            attribute="correlation_group",
            limit=self.limits.max_correlation_group_notional_fraction,
            label="correlation group",
        )
        self._check_concentration(
            reasons,
            existing,
            proposed,
            equity,
            attribute="strategy_id",
            limit=self.limits.max_strategy_notional_fraction,
            label="strategy",
        )

        if snapshot.equity_source_currency not in {"", "USD"}:
            observed = snapshot.fx_observed_at
            rate = snapshot.fx_rate_to_usd
            if (
                observed is None
                or observed.tzinfo is None
                or rate is None
                or not math.isfinite(float(rate))
                or float(rate) <= 0
                or snapshot.evaluated_at.astimezone(timezone.utc)
                - observed.astimezone(timezone.utc)
                > self.limits.max_fx_age
            ):
                reasons.append("FX rate used for account equity is missing or stale")

        atomic_baseline = tuple(snapshot.positions) + tuple(
            exposure
            for exposure in snapshot.projected_exposures
            if not exposure.reservation_id
        )
        reservation_spec = None
        if str(proposal.environment or "").strip() and str(
            proposal.account_no or ""
        ).strip():
            reservation_spec = PortfolioRiskReservationSpec(
                environment=proposal.environment,
                account_no=proposal.account_no,
                symbol=proposed.symbol,
                proposed_notional_usd=proposed.notional,
                proposed_open_risk_usd=proposed.open_risk,
                account_equity_usd=equity,
                baseline_position_symbols=tuple(
                    position.symbol for position in atomic_baseline
                ),
                baseline_open_risk_usd=sum(
                    position.open_risk for position in atomic_baseline
                ),
                baseline_gross_notional_usd=sum(
                    position.notional for position in atomic_baseline
                ),
                max_simultaneous_positions=self.limits.max_simultaneous_positions,
                max_total_open_risk_fraction=self.limits.max_total_open_risk_fraction,
                max_gross_notional_fraction=self.limits.max_gross_notional_fraction,
                evaluated_at=snapshot.evaluated_at,
            )
        return PortfolioRiskDecision(
            approved=not reasons,
            reasons=tuple(dict.fromkeys(reasons)),
            position_count_after=position_count_after,
            total_open_risk_after_usd=total_open_risk_after,
            gross_notional_after_usd=gross_notional_after,
            proposed_notional_usd=proposed.notional,
            reservation_spec=reservation_spec,
        )

    @staticmethod
    def _check_concentration(
        reasons: list[str],
        existing: Tuple[PortfolioPositionRisk, ...],
        proposed: PortfolioPositionRisk,
        equity: float,
        *,
        attribute: str,
        limit: float,
        label: str,
    ) -> None:
        if limit <= 0:
            return
        key = getattr(proposed, attribute)
        if not key:
            reasons.append(f"{label.title()} classification is required")
            return
        if equity <= 0:
            return
        notional = _group_notional(existing, attribute, key) + proposed.notional
        fraction = notional / equity
        if fraction > limit:
            reasons.append(
                f"Maximum {label} concentration would be exceeded "
                f"({fraction:.2%}>{limit:.2%})"
            )


class PortfolioRiskReservationSpecError(ValueError):
    """A projected-risk reservation is stale or does not match its order."""


def require_portfolio_risk_reservation_spec(
    spec: object,
    *,
    environment: str,
    account_no: str,
    symbol: str,
    quantity: int,
    reference_price: float,
    evaluated_at: datetime,
) -> PortfolioRiskReservationSpec:
    """Validate that an atomic reservation spec belongs to this exact entry."""

    if not isinstance(spec, PortfolioRiskReservationSpec):
        raise PortfolioRiskReservationSpecError(
            "ENTRY order requires an account-wide projected-risk reservation"
        )
    expected_scope = (
        _normalized_text(environment),
        str(account_no or "").strip(),
        _normalized_text(symbol),
    )
    if (spec.environment, spec.account_no, spec.symbol) != expected_scope:
        raise PortfolioRiskReservationSpecError(
            "Projected-risk reservation belongs to a different account or symbol"
        )
    expected_notional = int(quantity) * float(reference_price)
    if not math.isclose(
        spec.proposed_notional_usd,
        expected_notional,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise PortfolioRiskReservationSpecError(
            "Projected-risk reservation does not match the requested quantity and price"
        )
    if spec.proposed_open_risk_usd < 0 or spec.proposed_open_risk_usd > (
        spec.proposed_notional_usd + 1e-6
    ):
        raise PortfolioRiskReservationSpecError(
            "Projected open risk is outside the exact entry notional"
        )
    if spec.account_equity_usd <= 0:
        raise PortfolioRiskReservationSpecError(
            "Projected-risk reservation requires fresh positive account equity"
        )
    if spec.evaluated_at.astimezone(timezone.utc) != evaluated_at.astimezone(
        timezone.utc
    ):
        raise PortfolioRiskReservationSpecError(
            "Projected-risk reservation is not from the current pre-trade decision"
        )
    return spec
