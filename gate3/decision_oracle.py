"""Independent, pure Gate-3 mutation-decision oracle.

The production runtime emits its decision separately.  Gate-3 evidence compares
that result with these small policy functions and records every disagreement;
the oracle performs no I/O and cannot submit an order.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Optional

from src.strategy.orb.entry_policy import (
    build_passive_pullback_plan,
    is_strict_higher_timeframe_upgrade,
    passive_limit_submission_ready,
)


@dataclass(frozen=True)
class OracleDecision:
    expected_event: Optional[str]
    block_reasons: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.expected_event is not None

    def matches(self, runtime_event: Optional[str]) -> bool:
        return runtime_event == self.expected_event


def _universal_fences(
    *,
    mutation_enabled: bool,
    lease_current: bool,
    ownership_current: bool,
    reconciliation_clear: bool,
) -> list[str]:
    reasons = []
    if not mutation_enabled:
        reasons.append("kill_switch")
    if not lease_current:
        reasons.append("lease_loss")
    if not ownership_current:
        reasons.append("ownership_mismatch")
    if not reconciliation_clear:
        reasons.append("reconciliation_ambiguous")
    return reasons


def expected_entry(
    *,
    orb_high: object,
    orb_low: object,
    breakout_price: object,
    execution_price: object,
    breakout_confirmed: bool,
    last_trade: object,
    best_ask: object,
    regular_session_open: bool,
    quote_fresh: bool,
    mutation_enabled: bool,
    lease_current: bool,
    ownership_current: bool,
    reconciliation_clear: bool,
) -> OracleDecision:
    reasons = _universal_fences(
        mutation_enabled=mutation_enabled,
        lease_current=lease_current,
        ownership_current=ownership_current,
        reconciliation_clear=reconciliation_clear,
    )
    plan = build_passive_pullback_plan(
        orb_high=orb_high,
        orb_low=orb_low,
        breakout_price=breakout_price,
        execution_price=execution_price,
    )
    if plan is None:
        reasons.append("invalid_pullback_geometry")
    if not breakout_confirmed:
        reasons.append("breakout_unconfirmed")
    if not regular_session_open:
        reasons.append("outside_regular_session")
    if not quote_fresh:
        reasons.append("stale_quote")
    if plan is not None and quote_fresh and not passive_limit_submission_ready(
        last_trade=last_trade,
        best_ask=best_ask,
        execution_price=plan.execution_price,
    ):
        reasons.append("passive_quote_not_ready")
    return OracleDecision(None if reasons else "WOULD_SUBMIT", tuple(reasons))


def expected_exact_cancel(
    *,
    exact_order_owned: bool,
    mutation_enabled: bool,
    lease_current: bool,
    ownership_current: bool,
    reconciliation_clear: bool,
) -> OracleDecision:
    reasons = _universal_fences(
        mutation_enabled=mutation_enabled,
        lease_current=lease_current,
        ownership_current=ownership_current,
        reconciliation_clear=reconciliation_clear,
    )
    if not exact_order_owned:
        reasons.append("cancel_order_not_exact_owned")
    return OracleDecision(None if reasons else "WOULD_CANCEL", tuple(reasons))


def expected_higher_timeframe_replacement(
    *,
    current_window: object,
    current_score: object,
    candidate_window: object,
    candidate_score: object,
    candidate_confirmed: bool,
    zero_fill: bool,
    exact_cancel_confirmed: bool,
    candidate_plan_valid: bool,
    mutation_enabled: bool,
    lease_current: bool,
    ownership_current: bool,
    reconciliation_clear: bool,
) -> OracleDecision:
    reasons = _universal_fences(
        mutation_enabled=mutation_enabled,
        lease_current=lease_current,
        ownership_current=ownership_current,
        reconciliation_clear=reconciliation_clear,
    )
    if not is_strict_higher_timeframe_upgrade(
        current_window=current_window,
        current_score=current_score,
        candidate_window=candidate_window,
        candidate_score=candidate_score,
    ):
        reasons.append("not_strictly_better_higher_timeframe")
    if not candidate_confirmed:
        reasons.append("candidate_unconfirmed")
    if not zero_fill:
        reasons.append("existing_order_has_fill")
    if not exact_cancel_confirmed:
        reasons.append("exact_cancel_not_confirmed")
    if not candidate_plan_valid:
        reasons.append("replacement_plan_invalid")
    return OracleDecision(None if reasons else "WOULD_REPLACE", tuple(reasons))


def expected_protective_sell(
    *,
    quantity: int,
    execution_price_available: bool,
    mutation_enabled: bool,
    lease_current: bool,
    ownership_current: bool,
    reconciliation_clear: bool,
) -> OracleDecision:
    """Entry caps are intentionally absent from protective-SELL logic."""

    reasons = _universal_fences(
        mutation_enabled=mutation_enabled,
        lease_current=lease_current,
        ownership_current=ownership_current,
        reconciliation_clear=reconciliation_clear,
    )
    if int(quantity or 0) <= 0:
        reasons.append("no_sellable_quantity")
    if not execution_price_available:
        reasons.append("no_bounded_exit_price")
    return OracleDecision(None if reasons else "WOULD_SELL", tuple(reasons))


def oracle_source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


__all__ = [
    "OracleDecision",
    "expected_entry",
    "expected_exact_cancel",
    "expected_higher_timeframe_replacement",
    "expected_protective_sell",
    "oracle_source_sha256",
]
