"""Explicit final approval boundary for entry-order risk decisions."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Optional, Tuple

from src.risk.orb_position import validate_orb_position_values


class PreTradeRiskRejectedError(RuntimeError):
    """Raised before ledger reservation when an entry lacks risk approval."""


@dataclass(frozen=True)
class PreTradeRiskDecision:
    """Immutable approval produced by a strategy/risk evaluation.

    ``quantity`` binds the approval to the exact share quantity submitted, so
    a caller cannot reuse a valid decision after changing the order size.
    """

    approved: bool
    quantity: int
    reasons: Tuple[str, ...] = ()

    @classmethod
    def approve(cls, quantity: int) -> "PreTradeRiskDecision":
        return cls(approved=True, quantity=quantity)

    @classmethod
    def reject(
        cls, quantity: int, *reasons: str
    ) -> "PreTradeRiskDecision":
        normalized = tuple(str(reason).strip() for reason in reasons if str(reason).strip())
        return cls(approved=False, quantity=quantity, reasons=normalized)


def require_pre_trade_risk_approval(
    decision: Optional[PreTradeRiskDecision], requested_quantity: int
) -> None:
    """Fail closed unless an entry has approval for its exact quantity."""
    if decision is None:
        raise PreTradeRiskRejectedError(
            "ENTRY order requires an explicit pre-trade risk approval"
        )
    if not isinstance(decision, PreTradeRiskDecision):
        raise PreTradeRiskRejectedError("Invalid pre-trade risk decision")
    try:
        approved_quantity = int(decision.quantity)
    except (TypeError, ValueError, OverflowError):
        approved_quantity = 0
    if approved_quantity <= 0 or approved_quantity != int(requested_quantity):
        raise PreTradeRiskRejectedError(
            "Pre-trade risk approval quantity does not match the requested order"
        )
    if not decision.approved:
        detail = "; ".join(decision.reasons) or "risk evaluation rejected the entry"
        raise PreTradeRiskRejectedError(f"ENTRY order rejected: {detail}")


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def assess_orb_entry_candidate(
    candidate: Any, requested_quantity: int
) -> PreTradeRiskDecision:
    """Revalidate a selected ORB candidate immediately before submission."""
    reasons = []
    if candidate is None:
        return PreTradeRiskDecision.reject(
            requested_quantity, "No selected ORB candidate"
        )

    status = str(getattr(getattr(candidate, "status", ""), "value", getattr(candidate, "status", "")) or "").upper()
    if status != "EXECUTE_READY" or not bool(getattr(candidate, "valid", False)):
        reasons.append(f"ORB candidate is {status or 'not execution-ready'}")

    try:
        planned_quantity = int(getattr(candidate, "shares", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        planned_quantity = 0
    if planned_quantity != int(requested_quantity):
        reasons.append(
            "Requested quantity does not match the risk-approved ORB quantity"
        )

    entry = _finite_float(getattr(candidate, "entry_trigger", None))
    stop = _finite_float(getattr(candidate, "stop_loss", None))
    capital_percent = _finite_float(getattr(candidate, "capital_percent", None))
    stop_loss_percent = _finite_float(
        getattr(candidate, "stop_loss_percent", None)
    )
    stop_adr = _finite_float(getattr(candidate, "stop_adr", None))
    if entry is None or entry <= 0:
        reasons.append("Entry trigger must be positive and finite")
    if stop is None or stop <= 0 or entry is None or stop >= entry:
        reasons.append("Stop loss must be positive and below the entry trigger")
    if capital_percent is None or stop_loss_percent is None:
        reasons.append("ORB risk metrics are incomplete or non-finite")
    else:
        reasons.extend(
            validate_orb_position_values(
                {
                    "shares": planned_quantity,
                    "capital_percent": capital_percent,
                    "stop_loss_percent": stop_loss_percent,
                    "sl_adr": stop_adr,
                },
                adr_percent=None,
            )
        )

    reasons.extend(str(value) for value in (getattr(candidate, "warnings", ()) or ()))
    reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    if reasons:
        return PreTradeRiskDecision.reject(requested_quantity, *reasons)
    return PreTradeRiskDecision.approve(requested_quantity)
