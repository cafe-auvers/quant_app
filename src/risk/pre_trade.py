"""Explicit final approval boundary for exposure-increasing orders."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from src.core.order_state import OrderIntent, OrderSide
from src.risk.orb_position import validate_orb_position_values

DEFAULT_APPROVAL_TTL = timedelta(seconds=30)


class PreTradeRiskRejectedError(RuntimeError):
    """Raised before ledger reservation when an entry lacks valid approval."""


def normalize_share_quantity(value: Any) -> int:
    """Return a positive whole share quantity without permissive coercion."""
    if isinstance(value, bool):
        raise ValueError("Share quantity must not be a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Share quantity must be a positive whole number") from exc
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        raise ValueError("Share quantity must be a positive whole finite number")
    return int(number)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Risk-decision timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class PreTradeRiskDecision:
    """Immutable approval bound to one exact order command and plan."""

    approved: bool
    environment: str
    account_no: str
    symbol: str
    side: OrderSide
    intent: OrderIntent
    quantity: int
    reference_price: float
    exchange: str
    execution_policy: str
    strategy_id: str
    plan_id: str
    evaluated_at: datetime
    expires_at: datetime
    reasons: Tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        approved: bool,
        environment: str,
        account_no: str,
        symbol: str,
        side: OrderSide,
        intent: OrderIntent,
        quantity: Any,
        reference_price: Any,
        exchange: str,
        execution_policy: str,
        strategy_id: str,
        plan_id: str,
        reasons: Tuple[str, ...] = (),
        evaluated_at: Optional[datetime] = None,
        expires_at: Optional[datetime] = None,
    ) -> "PreTradeRiskDecision":
        if not isinstance(approved, bool):
            raise ValueError("Risk-decision approved flag must be a boolean")
        evaluated = _aware_utc(evaluated_at or datetime.now(timezone.utc))
        expires = _aware_utc(expires_at or (evaluated + DEFAULT_APPROVAL_TTL))
        price = float(reference_price)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Risk-decision reference price must be positive and finite")
        return cls(
            approved=approved,
            environment=str(environment or "").strip().upper(),
            account_no=str(account_no or "").strip(),
            symbol=str(symbol or "").strip().upper(),
            side=side if isinstance(side, OrderSide) else OrderSide(str(side).upper()),
            intent=(
                intent
                if isinstance(intent, OrderIntent)
                else OrderIntent(str(intent).upper())
            ),
            quantity=normalize_share_quantity(quantity),
            reference_price=price,
            exchange=str(exchange or "").strip().upper(),
            execution_policy=str(execution_policy or "").strip().upper(),
            strategy_id=str(strategy_id or "").strip(),
            plan_id=str(plan_id or "").strip(),
            evaluated_at=evaluated,
            expires_at=expires,
            reasons=tuple(
                str(reason).strip() for reason in reasons if str(reason).strip()
            ),
        )

    @classmethod
    def approve(cls, **kwargs: Any) -> "PreTradeRiskDecision":
        return cls.create(approved=True, **kwargs)

    @classmethod
    def reject(cls, *, reasons: Tuple[str, ...], **kwargs: Any) -> "PreTradeRiskDecision":
        return cls.create(approved=False, reasons=reasons, **kwargs)


def require_pre_trade_risk_approval(
    decision: Optional[PreTradeRiskDecision],
    *,
    environment: str,
    account_no: str,
    symbol: str,
    side: OrderSide,
    intent: OrderIntent,
    quantity: Any,
    reference_price: Any,
    exchange: str,
    execution_policy: str,
    strategy_id: str,
    plan_id: str,
    now: Optional[datetime] = None,
) -> None:
    """Fail closed unless approval matches the exact order fingerprint."""
    if decision is None:
        raise PreTradeRiskRejectedError(
            "ENTRY order requires an explicit pre-trade risk approval"
        )
    if not isinstance(decision, PreTradeRiskDecision):
        raise PreTradeRiskRejectedError("Invalid pre-trade risk decision")

    try:
        requested_quantity = normalize_share_quantity(quantity)
        price = float(reference_price)
        approved_price = float(decision.reference_price)
        approved_quantity = normalize_share_quantity(decision.quantity)
        evaluated = _aware_utc(decision.evaluated_at)
        expires = _aware_utc(decision.expires_at)
        current = _aware_utc(now or datetime.now(timezone.utc))
    except (TypeError, ValueError, OverflowError) as exc:
        raise PreTradeRiskRejectedError(f"Invalid pre-trade risk decision: {exc}") from exc

    expected = (
        str(environment or "").strip().upper(),
        str(account_no or "").strip(),
        str(symbol or "").strip().upper(),
        side,
        intent,
        requested_quantity,
        str(exchange or "").strip().upper(),
        str(execution_policy or "").strip().upper(),
        str(strategy_id or "").strip(),
        str(plan_id or "").strip(),
    )
    actual = (
        decision.environment,
        decision.account_no,
        decision.symbol,
        decision.side,
        decision.intent,
        approved_quantity,
        decision.exchange,
        decision.execution_policy,
        decision.strategy_id,
        decision.plan_id,
    )
    if actual != expected or not math.isclose(
        approved_price, price, rel_tol=0.0, abs_tol=1e-9
    ):
        raise PreTradeRiskRejectedError(
            "Pre-trade risk approval does not match the requested order"
        )
    if not decision.strategy_id or not decision.plan_id:
        raise PreTradeRiskRejectedError(
            "Pre-trade risk approval requires strategy and plan identifiers"
        )
    if (
        expires <= evaluated
        or expires - evaluated > DEFAULT_APPROVAL_TTL
        or current < evaluated
        or current > expires
    ):
        raise PreTradeRiskRejectedError("Pre-trade risk approval has expired")
    if decision.approved is not True:
        detail = "; ".join(decision.reasons) or "risk evaluation rejected the entry"
        raise PreTradeRiskRejectedError(f"ENTRY order rejected: {detail}")


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def orb_candidate_plan_id(candidate: Any) -> str:
    """Return a stable fingerprint for the risk-relevant ORB plan fields."""
    if candidate is None:
        return "ORB:MISSING"
    payload = {
        "symbol": str(getattr(candidate, "symbol", "") or "").upper(),
        "window": str(getattr(candidate, "window", "") or ""),
        "entry_trigger": _finite_float(getattr(candidate, "entry_trigger", None)),
        "stop_loss": _finite_float(getattr(candidate, "stop_loss", None)),
        "shares": getattr(candidate, "shares", None),
        "capital_percent": _finite_float(
            getattr(candidate, "capital_percent", None)
        ),
        "stop_loss_percent": _finite_float(
            getattr(candidate, "stop_loss_percent", None)
        ),
        "stop_adr": _finite_float(getattr(candidate, "stop_adr", None)),
        "risk_percent": _finite_float(getattr(candidate, "risk_percent", None)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"ORB:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def assess_orb_entry_candidate(
    candidate: Any,
    *,
    environment: str,
    account_no: str,
    symbol: str,
    quantity: Any,
    reference_price: Any,
    plan_id: str,
    exchange: str = "NASD",
    execution_policy: str = "REGULAR_LIMIT",
    evaluated_at: Optional[datetime] = None,
) -> PreTradeRiskDecision:
    """Revalidate and bind a selected ORB candidate immediately before submit."""
    requested_quantity = normalize_share_quantity(quantity)
    fingerprint = {
        "environment": environment,
        "account_no": account_no,
        "symbol": symbol,
        "side": OrderSide.BUY,
        "intent": OrderIntent.ENTRY,
        "quantity": requested_quantity,
        "reference_price": reference_price,
        "exchange": exchange,
        "execution_policy": execution_policy,
        "strategy_id": "ORB",
        "plan_id": plan_id,
        "evaluated_at": evaluated_at,
    }
    reasons = []
    if candidate is None:
        reasons.append("No selected ORB candidate")
    else:
        if plan_id != orb_candidate_plan_id(candidate):
            reasons.append("ORB plan identifier does not match the selected candidate")
        raw_status = getattr(candidate, "status", "")
        status = str(getattr(raw_status, "value", raw_status) or "").upper()
        if status != "EXECUTE_READY" or not bool(getattr(candidate, "valid", False)):
            reasons.append(f"ORB candidate is {status or 'not execution-ready'}")

        try:
            planned_quantity = normalize_share_quantity(
                getattr(candidate, "shares", 0)
            )
        except ValueError:
            planned_quantity = 0
        if planned_quantity != requested_quantity:
            reasons.append(
                "Requested quantity does not match the risk-approved ORB quantity"
            )

        entry = _finite_float(getattr(candidate, "entry_trigger", None))
        stop = _finite_float(getattr(candidate, "stop_loss", None))
        capital_percent = _finite_float(
            getattr(candidate, "capital_percent", None)
        )
        stop_loss_percent = _finite_float(
            getattr(candidate, "stop_loss_percent", None)
        )
        stop_adr = _finite_float(getattr(candidate, "stop_adr", None))
        submitted_price = _finite_float(reference_price)
        if entry is None or entry <= 0:
            reasons.append("Entry trigger must be positive and finite")
        elif submitted_price is None or not math.isclose(
            entry, submitted_price, rel_tol=0.0, abs_tol=1e-9
        ):
            reasons.append("Submitted price does not match the ORB entry trigger")
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
        reasons.extend(
            str(value) for value in (getattr(candidate, "warnings", ()) or ())
        )

    normalized_reasons = tuple(dict.fromkeys(reason for reason in reasons if reason))
    if normalized_reasons:
        return PreTradeRiskDecision.reject(
            reasons=normalized_reasons, **fingerprint
        )
    return PreTradeRiskDecision.approve(**fingerprint)
