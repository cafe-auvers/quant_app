"""Read-only ORB risk/window combinations for the Buy Board.

The execution queue keeps one optimized candidate for each ORB window.  This
module expands those three market structures across the established risk
cases so the trader can inspect every sizing alternative without creating a
second selection or execution path.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math
from typing import Iterable, Optional, Tuple

from src.core.execution_queue import (
    ExecutionQueueItem,
    OrbCandidateStatus,
    SUPPORTED_ORB_WINDOWS,
)
from src.risk.orb_position import (
    calculate_orb_position_values,
    is_orb_position_plan_valid,
    score_orb_position_recommendation,
    validate_orb_position_values,
)
from src.strategy.orb.entry_policy import build_passive_pullback_plan


ORB_RISK_CASES: Tuple[float, ...] = (
    0.0025,
    0.005,
    0.0075,
    0.01,
    0.0125,
    0.015,
    0.0175,
    0.02,
)


@dataclass(frozen=True)
class OrbPositionCombination:
    window: str
    risk_percent: float
    status: OrbCandidateStatus
    valid: bool
    score: float
    reason: str
    orb_high: Optional[float]
    breakout_price: Optional[float]
    buffer_pct: Optional[float]
    breakout_trigger: Optional[float]
    entry_trigger: Optional[float]
    stop_price: Optional[float]
    account_equity: float
    adr_percent: Optional[float]
    total_risk: float
    risk_per_share: float
    shares: int
    investment: float
    capital_percent: float
    stop_loss_percent: float
    stop_adr: Optional[float]
    # Added at the end so old frozen rejection snapshots without these
    # fields remain readable.  New snapshots expose the exact passive plan.
    orb_low: Optional[float] = None
    entry_floor: Optional[float] = None
    execution_price: Optional[float] = None


def orb_position_combinations_from_snapshot(
    snapshot: dict,
) -> list[OrbPositionCombination]:
    """Restore the exact combinations frozen at terminal ORB rejection."""

    allowed = {item.name for item in fields(OrbPositionCombination)}
    restored: list[OrbPositionCombination] = []
    for raw in list(dict(snapshot or {}).get("combinations") or []):
        if not isinstance(raw, dict):
            continue
        payload = {key: value for key, value in raw.items() if key in allowed}
        try:
            payload["status"] = _status(payload.get("status"))
            restored.append(OrbPositionCombination(**payload))
        except (TypeError, ValueError):
            continue
    return restored


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _candidate_adr_percent(candidate) -> Optional[float]:
    """Recover ADR% from the candidate's persisted stop metrics."""

    stop_loss_percent = _finite(getattr(candidate, "stop_loss_percent", None))
    stop_adr = _finite(getattr(candidate, "stop_adr", None))
    if (
        stop_loss_percent is None
        or stop_loss_percent <= 0
        or stop_adr is None
        or stop_adr <= 0
    ):
        return None
    return stop_loss_percent / stop_adr * 100.0


def _status(value) -> OrbCandidateStatus:
    if isinstance(value, OrbCandidateStatus):
        return value
    try:
        return OrbCandidateStatus(str(value))
    except ValueError:
        return OrbCandidateStatus.NOT_AVAILABLE


def _persisted_account_equity(candidates: dict) -> float:
    """Recover the equity used to size a persisted queue candidate.

    The live buying-power snapshot may not be loaded yet when the dialog is
    opened after a restart. Candidate capital percentage is persisted, so the
    original equity can be reconstructed without a broker or database call.
    """

    for window in SUPPORTED_ORB_WINDOWS:
        candidate = candidates.get(window)
        shares = _finite(getattr(candidate, "shares", None))
        execution_price = _finite(
            getattr(candidate, "execution_price", None)
        )
        # Legacy snapshots predate the explicit passive execution field.
        # Recovering their equity is diagnostic only; current candidates are
        # independently required to carry execution_price before execution.
        entry_price = execution_price or _finite(
            getattr(candidate, "entry_trigger", None)
        )
        capital_percent = _finite(
            getattr(candidate, "capital_percent", None)
        )
        if (
            shares is None
            or shares <= 0
            or entry_price is None
            or entry_price <= 0
            or capital_percent is None
            or capital_percent <= 0
        ):
            continue
        equity = shares * entry_price / (capital_percent / 100.0)
        if math.isfinite(equity) and equity > 0:
            return equity
    return 0.0


def build_orb_position_combinations(
    queue_item: ExecutionQueueItem,
    *,
    account_equity: float,
    buffer_pct: Optional[float] = None,
    snapshot_stale: bool = False,
    stale_windows: Iterable[str] = (),
) -> list[OrbPositionCombination]:
    """Expand the queue's three ORB structures into all 24 sizing choices.

    A combination is *valid* when it satisfies the same frozen passive-entry
    geometry and canonical position checks used by execution.  The persisted
    buffer is retained as legacy plan metadata but cannot alter the finalized
    trigger. Current-price readiness remains an observation in
    ``status``/``reason``; it does not erase an otherwise valid position plan.
    """

    candidates = dict(getattr(queue_item, "candidates", {}) or {})
    # Keep every row coherent with the exact queue snapshot being explained.
    # Live equity may have changed after these candidates were built; use the
    # persisted candidate sizing when it is recoverable, then fall back to the
    # supplied snapshot only for incomplete legacy candidates.
    equity = _persisted_account_equity(candidates)
    if equity <= 0:
        equity = _finite(account_equity) or 0.0
    requested_buffer = _finite(buffer_pct)
    if (
        requested_buffer is not None
        and (requested_buffer < 0.0 or requested_buffer > 1.0)
    ):
        requested_buffer = None
    stale_window_set = {
        str(window or "").strip() for window in stale_windows
    }
    combinations: list[OrbPositionCombination] = []

    for risk_percent in ORB_RISK_CASES:
        for window in SUPPORTED_ORB_WINDOWS:
            candidate = candidates.get(window)
            if candidate is None:
                combinations.append(
                    OrbPositionCombination(
                        window=window,
                        risk_percent=risk_percent,
                        status=OrbCandidateStatus.NOT_AVAILABLE,
                        valid=False,
                        score=0.0,
                        reason="ORB plan is not available",
                        orb_high=None,
                        breakout_price=None,
                        buffer_pct=requested_buffer,
                        breakout_trigger=None,
                        entry_trigger=None,
                        stop_price=None,
                        account_equity=equity,
                        adr_percent=None,
                        total_risk=0.0,
                        risk_per_share=0.0,
                        shares=0,
                        investment=0.0,
                        capital_percent=0.0,
                        stop_loss_percent=0.0,
                        stop_adr=None,
                        orb_low=None,
                        entry_floor=None,
                        execution_price=None,
                    )
                )
                continue

            status = _status(getattr(candidate, "status", None))
            orb_high = _finite(getattr(candidate, "orb_high", None))
            orb_low = _finite(getattr(candidate, "orb_low", None))
            breakout_price = _finite(
                getattr(candidate, "breakout_price", None)
            )
            persisted_breakout_trigger = _finite(
                getattr(candidate, "breakout_trigger", None)
            )
            persisted_entry_floor = _finite(
                getattr(candidate, "floor_price", None)
                if getattr(candidate, "floor_price", None) is not None
                else getattr(candidate, "entry_floor", None)
            )
            persisted_entry_trigger = _finite(
                getattr(candidate, "entry_trigger", None)
            )
            execution_price = _finite(
                getattr(candidate, "execution_price", None)
            )
            plan = build_passive_pullback_plan(
                orb_high=orb_high,
                orb_low=orb_low,
                breakout_price=breakout_price,
                execution_price=execution_price,
            )
            candidate_buffer = requested_buffer
            breakout_trigger = plan.breakout_trigger if plan else (
                max(breakout_price, orb_high)
                if breakout_price is not None and orb_high is not None
                else None
            )
            entry_floor = plan.floor_price if plan else (
                max(breakout_price, orb_low)
                if breakout_price is not None and orb_low is not None
                else None
            )
            # Retained only for old snapshot readers. In current candidates,
            # ``entry_trigger`` is the exact execution limit, while
            # ``breakout_trigger`` is the separate confirmation level.
            entry_trigger = execution_price
            stop_price = _finite(getattr(candidate, "stop_loss", None))
            adr_percent = _candidate_adr_percent(candidate)
            sizing = calculate_orb_position_values(
                account_size=equity,
                risk_percent=risk_percent,
                entry_price=execution_price or 0.0,
                stop_price=stop_price or 0.0,
                adr_percent=adr_percent,
            )

            invalid_reasons: list[str] = []
            if snapshot_stale or window in stale_window_set:
                status = OrbCandidateStatus.NOT_AVAILABLE
                invalid_reasons.append(
                    "Current-session ORB minute bars are unavailable"
                )
            if equity <= 0:
                invalid_reasons.append("Account equity is unavailable")
            if status in {
                OrbCandidateStatus.FORMING,
                OrbCandidateStatus.NOT_AVAILABLE,
            }:
                invalid_reasons.append(
                    str(getattr(candidate, "reason", "") or "ORB is still forming")
                )
            elif status == OrbCandidateStatus.REJECTED:
                invalid_reasons.append(
                    str(getattr(candidate, "reason", "") or "ORB plan was rejected")
                )
            elif status == OrbCandidateStatus.RISK_INVALID:
                invalid_reasons.append(
                    str(getattr(candidate, "reason", "") or "ORB risk plan is invalid")
                )
            if breakout_price is None or breakout_price <= 0:
                invalid_reasons.append("Daily breakout price is missing")
            if plan is None:
                invalid_reasons.append(
                    "Passive entry must satisfy max(breakout price, ORL) "
                    "< execution price <= ORH"
                )
            else:
                if (
                    persisted_breakout_trigger is None
                    or not math.isclose(
                        persisted_breakout_trigger,
                        plan.breakout_trigger,
                        rel_tol=1e-9,
                        abs_tol=1e-6,
                    )
                    or persisted_entry_trigger is None
                    or not math.isclose(
                        persisted_entry_trigger,
                        plan.execution_price,
                        rel_tol=1e-9,
                        abs_tol=1e-6,
                    )
                ):
                    invalid_reasons.append(
                        "Persisted execution trigger does not match the finalized policy"
                    )
                if (
                    persisted_entry_floor is None
                    or not math.isclose(
                        persisted_entry_floor,
                        plan.floor_price,
                        rel_tol=1e-9,
                        abs_tol=1e-6,
                    )
                ):
                    invalid_reasons.append(
                        "Persisted entry floor does not match the finalized policy"
                    )
            if (
                execution_price is None
                or stop_price is None
                or stop_price <= 0
                or stop_price >= execution_price
            ):
                invalid_reasons.append("Execution/stop geometry is invalid")
            invalid_reasons.extend(
                validate_orb_position_values(sizing, adr_percent)
            )
            # Preserve order while removing duplicate explanations.
            invalid_reasons = list(dict.fromkeys(invalid_reasons))
            valid = not invalid_reasons and is_orb_position_plan_valid(
                sizing, adr_percent
            )
            score = score_orb_position_recommendation(sizing, risk_percent)
            reason = (
                "; ".join(invalid_reasons)
                if invalid_reasons
                else str(
                    getattr(candidate, "reason", "")
                    or "Valid position plan"
                )
            )
            combinations.append(
                OrbPositionCombination(
                    window=window,
                    risk_percent=risk_percent,
                    status=status,
                    valid=valid,
                    score=score,
                    reason=reason,
                    orb_high=orb_high,
                    breakout_price=breakout_price,
                    buffer_pct=candidate_buffer,
                    breakout_trigger=breakout_trigger,
                    entry_trigger=entry_trigger,
                    stop_price=stop_price,
                    account_equity=equity,
                    adr_percent=adr_percent,
                    total_risk=float(sizing.get("total_risk", 0.0) or 0.0),
                    risk_per_share=float(
                        sizing.get("risk_per_share", 0.0) or 0.0
                    ),
                    shares=int(sizing.get("shares", 0.0) or 0),
                    investment=float(sizing.get("investment", 0.0) or 0.0),
                    capital_percent=float(
                        sizing.get("capital_percent", 0.0) or 0.0
                    ),
                    stop_loss_percent=float(
                        sizing.get("stop_loss_percent", 0.0) or 0.0
                    ),
                    stop_adr=(
                        float(sizing["sl_adr"])
                        if sizing.get("sl_adr") is not None
                        else None
                    ),
                    orb_low=orb_low,
                    entry_floor=entry_floor,
                    execution_price=execution_price,
                )
            )

    return sorted(
        combinations,
        key=lambda item: (
            item.valid,
            item.score,
            -item.risk_percent,
            -SUPPORTED_ORB_WINDOWS.index(item.window),
        ),
        reverse=True,
    )
