"""Fail-closed supervised-live envelope for the Kanban execution runtime.

This policy does not arm trading. It is an additional fence used only when
the production Kanban engine is explicitly enabled. Controlled live and full
live use the same gateway/broker path; promotion changes configuration, not
execution code.
"""
from __future__ import annotations

import math
from typing import Any

from src.core import execution_config
from src.core.order_state import OrderSide


CONTROLLED_LIVE = "CONTROLLED_LIVE"
FULL_LIVE = "FULL_LIVE"
DISABLED = "DISABLED"
MIN_CONTROLLED_LIVE_MUTATION_SPACING_SECONDS = 0.1


class LiveExecutionEnvelopeError(RuntimeError):
    """The configured live mode cannot authorize this production mutation."""


def _mode() -> str:
    return str(execution_config.KIS_LIVE_EXECUTION_MODE or DISABLED).strip().upper()


def require_controlled_live_configuration(
    *, environment: str, scheduler: Any | None = None
) -> None:
    """Validate the production pilot fence before the active worker starts."""

    if str(environment or "").strip().upper() != "PROD":
        return
    mode = _mode()
    if mode not in {CONTROLLED_LIVE, FULL_LIVE}:
        raise LiveExecutionEnvelopeError(
            "KIS_LIVE_EXECUTION_MODE must be CONTROLLED_LIVE or FULL_LIVE"
        )
    if not execution_config.KIS_MUTATION_BUDGET_VERIFIED or any(
        int(value) <= 0
        for value in (
            execution_config.KIS_SUBMIT_MUTATION_CAPACITY,
            execution_config.KIS_CANCEL_MUTATION_CAPACITY,
            execution_config.KIS_REPLACE_MUTATION_CAPACITY,
        )
    ):
        raise LiveExecutionEnvelopeError(
            "CONTROLLED_LIVE requires positive reviewed mutation budgets"
        )
    if (
        float(execution_config.KIS_MUTATION_MIN_SPACING_SECONDS)
        < MIN_CONTROLLED_LIVE_MUTATION_SPACING_SECONDS
    ):
        raise LiveExecutionEnvelopeError(
            "CONTROLLED_LIVE mutation spacing must be at least 0.1 seconds"
        )
    if not (
        execution_config.KIS_WS_ENABLED
        and execution_config.KIS_WS_PROTOCOL_VERIFIED
        and execution_config.KIS_MARKET_DATA_MODE == "WEBSOCKET"
    ):
        raise LiveExecutionEnvelopeError(
            "CONTROLLED_LIVE requires the reviewed production WebSocket path"
        )
    if mode == CONTROLLED_LIVE:
        if not execution_config.KIS_CONTROLLED_LIVE_SYMBOLS:
            raise LiveExecutionEnvelopeError(
                "CONTROLLED_LIVE requires KIS_CONTROLLED_LIVE_SYMBOLS"
            )
        if not math.isfinite(
            execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL
        ) or execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL <= 0:
            raise LiveExecutionEnvelopeError(
                "CONTROLLED_LIVE requires a positive maximum entry notional"
            )
        if int(execution_config.KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS) != 1:
            raise LiveExecutionEnvelopeError(
                "CONTROLLED_LIVE forbids automatic mutation retries"
            )
    if scheduler is not None:
        spacing = getattr(scheduler, "min_mutation_spacing_seconds", None)
        attempts = getattr(scheduler, "max_confirmed_mutation_attempts", None)
        if spacing is None or float(spacing) < float(
            execution_config.KIS_MUTATION_MIN_SPACING_SECONDS
        ):
            raise LiveExecutionEnvelopeError(
                "runtime scheduler does not enforce the configured mutation spacing"
            )
        if mode == CONTROLLED_LIVE and attempts != 1:
            raise LiveExecutionEnvelopeError(
                "runtime scheduler permits an automatic mutation retry"
            )


def require_live_entry_allowed(
    *,
    environment: str,
    symbol: str,
    side: OrderSide | str,
    quantity: int,
    limit_price: float,
) -> None:
    """Fence a real production BUY at the final broker adapter boundary.

    SELLs remain available for protection and liquidation. Every path still
    passes through the existing kill switch, lease, ownership, scheduler, and
    reconciliation gates.
    """

    if str(environment or "").strip().upper() != "PROD":
        return
    if not execution_config.is_buyboard_engine_enabled():
        return
    require_controlled_live_configuration(environment=environment)
    normalized_side = side if isinstance(side, OrderSide) else OrderSide(str(side).upper())
    if normalized_side != OrderSide.BUY or _mode() == FULL_LIVE:
        return
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol not in set(execution_config.KIS_CONTROLLED_LIVE_SYMBOLS):
        raise LiveExecutionEnvelopeError(
            f"CONTROLLED_LIVE refuses entry for unapproved symbol {normalized_symbol}"
        )
    notional = int(quantity) * float(limit_price)
    if not math.isfinite(notional) or notional <= 0:
        raise LiveExecutionEnvelopeError(
            "CONTROLLED_LIVE entry notional must be positive and finite"
        )
    if notional > float(execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL):
        raise LiveExecutionEnvelopeError(
            "CONTROLLED_LIVE entry exceeds the configured maximum notional"
        )


def automatic_mutation_retry_permitted(*, environment: str) -> bool:
    """Whether a low-level KIS mutation may repeat inside the same call.

    Controlled live always returns false. The scheduler and the API adapter
    both consult the same mode so a token-expiry branch cannot bypass the
    one-attempt pilot promise.
    """

    return not (
        str(environment or "").strip().upper() == "PROD"
        and execution_config.is_buyboard_engine_enabled()
        and _mode() == CONTROLLED_LIVE
    )
