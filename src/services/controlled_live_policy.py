"""Fail-closed supervised-live envelope for the Kanban execution runtime.

This policy does not arm trading. It is an independent broker-authority fence
for every production mutation, regardless of whether the guarded Kanban engine
or legacy recovery composition reaches the gateway. Controlled live and full
live use the same gateway/broker path; promotion changes configuration, not
execution code.
"""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy.engine import Engine

from src.core import execution_config
from src.core.order_state import OrderSide
from src.core.trade_card_state import BoardStatus, PositionRuntimeStatus
from src.services import trade_card_repository


CONTROLLED_LIVE = "CONTROLLED_LIVE"
FULL_LIVE = "FULL_LIVE"
DISABLED = "DISABLED"
MIN_CONTROLLED_LIVE_MUTATION_SPACING_SECONDS = 0.1
_ACTIVE_ENTRY_STATUSES = frozenset(
    {
        BoardStatus.BUY_TODAY,
        BoardStatus.ENTRY_PENDING,
    }
)


def _card_authorizes_entry(card: Any) -> bool:
    """Return whether one canonical card currently permits another BUY.

    A partial first fill moves the card to ``OPEN_POSITION`` before the
    remaining target is submitted.  That narrow ``ENTRY_COMPLETING`` state
    is still part of the original reviewed entry; ordinary open positions
    must never become a general permission to add shares.
    """

    board_status = getattr(card, "board_status", None)
    if board_status in _ACTIVE_ENTRY_STATUSES:
        return True
    return bool(
        board_status == BoardStatus.OPEN_POSITION
        and getattr(card, "position_runtime_status", None)
        == PositionRuntimeStatus.ENTRY_COMPLETING
        and int(getattr(card, "entry_remaining_target_quantity", 0) or 0) > 0
        and not bool(getattr(card, "entry_submission_unresolved", False))
        and not bool(getattr(card, "entry_cancel_in_flight", False))
        and not bool(getattr(card, "exit_all_required", False))
        and not bool(getattr(card, "sell_all_at_market_open", False))
    )


class LiveExecutionEnvelopeError(RuntimeError):
    """The configured live mode cannot authorize this production mutation."""


def _configuration_error(reason: str) -> LiveExecutionEnvelopeError:
    return LiveExecutionEnvelopeError(
        f"Production activation blocked: {reason}. No broker mutation was sent. "
        "Correct the reviewed live configuration, restart or refresh readiness, "
        "then retry."
    )


def _mode() -> str:
    return str(execution_config.KIS_LIVE_EXECUTION_MODE or DISABLED).strip().upper()


def require_controlled_live_configuration(
    *, environment: str, scheduler: Any | None = None
) -> None:
    """Validate the production envelope before the active worker starts.

    ``DISABLED`` is a valid engine-running state. It keeps Kanban,
    reconciliation, and position monitoring available while
    :func:`require_live_mutation_allowed` rejects every real broker mutation.
    """

    if str(environment or "").strip().upper() != "PROD":
        return
    mode = _mode()
    if mode == DISABLED:
        return
    if mode not in {CONTROLLED_LIVE, FULL_LIVE}:
        raise _configuration_error(
            "KIS_LIVE_EXECUTION_MODE must be DISABLED, CONTROLLED_LIVE, or FULL_LIVE"
        )
    if not execution_config.KIS_MUTATION_BUDGET_VERIFIED or any(
        int(value) <= 0
        for value in (
            execution_config.KIS_SUBMIT_MUTATION_CAPACITY,
            execution_config.KIS_CANCEL_MUTATION_CAPACITY,
            execution_config.KIS_REPLACE_MUTATION_CAPACITY,
        )
    ):
        raise _configuration_error(
            "CONTROLLED_LIVE requires positive reviewed mutation budgets"
        )
    if (
        float(execution_config.KIS_MUTATION_MIN_SPACING_SECONDS)
        < MIN_CONTROLLED_LIVE_MUTATION_SPACING_SECONDS
    ):
        raise _configuration_error(
            "CONTROLLED_LIVE mutation spacing must be at least 0.1 seconds"
        )
    if not (
        execution_config.KIS_WS_ENABLED
        and execution_config.KIS_WS_PROTOCOL_VERIFIED
        and execution_config.KIS_MARKET_DATA_MODE == "WEBSOCKET"
    ):
        raise _configuration_error(
            "CONTROLLED_LIVE requires the reviewed production WebSocket path"
        )
    if mode == CONTROLLED_LIVE:
        if not math.isfinite(
            execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL
        ) or execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL <= 0:
            raise _configuration_error(
                "CONTROLLED_LIVE requires a positive maximum entry notional"
            )
        if int(execution_config.KIS_MUTATION_MAX_CONFIRMED_ATTEMPTS) != 1:
            raise _configuration_error(
                "CONTROLLED_LIVE forbids automatic mutation retries"
            )
    if scheduler is not None:
        spacing = getattr(scheduler, "min_mutation_spacing_seconds", None)
        attempts = getattr(scheduler, "max_confirmed_mutation_attempts", None)
        if spacing is None or float(spacing) < float(
            execution_config.KIS_MUTATION_MIN_SPACING_SECONDS
        ):
            raise _configuration_error(
                "runtime scheduler does not enforce the configured mutation spacing"
            )
        if mode == CONTROLLED_LIVE and attempts != 1:
            raise _configuration_error(
                "runtime scheduler permits an automatic mutation retry"
            )


def require_live_mutation_allowed(
    *, environment: str, action: str
) -> None:
    """Reject every real production mutation while the envelope is disabled."""

    if str(environment or "").strip().upper() != "PROD":
        return
    mode = _mode()
    if mode == DISABLED:
        raise LiveExecutionEnvelopeError(
            f"Blocked production {action}: KIS_LIVE_EXECUTION_MODE is DISABLED. "
            "No broker mutation was sent. Retry only after the operator deliberately "
            "selects CONTROLLED_LIVE or FULL_LIVE and the corresponding readiness "
            "checks pass."
        )
    require_controlled_live_configuration(environment=environment)


def require_live_entry_allowed(
    *,
    environment: str,
    account_no: str = "",
    symbol: str,
    side: OrderSide | str,
    quantity: int,
    limit_price: float,
    engine: Engine | None = None,
) -> None:
    """Fence a real production BUY at the final broker adapter boundary.

    SELLs remain available for protection and liquidation. Every path still
    passes through the existing kill switch, lease, ownership, scheduler, and
    reconciliation gates.
    """

    if str(environment or "").strip().upper() != "PROD":
        return
    require_live_mutation_allowed(environment=environment, action="order submission")
    normalized_side = side if isinstance(side, OrderSide) else OrderSide(str(side).upper())
    if normalized_side != OrderSide.BUY:
        return
    invalid_entry_configuration = execution_config.entry_configuration_issues()
    if invalid_entry_configuration:
        names = ", ".join(item.split(":", 1)[0] for item in invalid_entry_configuration)
        raise _configuration_error(
            f"invalid entry-risk runtime override(s): {names}"
        )
    if _mode() == FULL_LIVE:
        return
    normalized_symbol = str(symbol or "").strip().upper()
    if not live_entry_symbol_allowed(
        environment=environment,
        account_no=account_no,
        symbol=normalized_symbol,
        engine=engine,
    ):
        raise LiveExecutionEnvelopeError(
            f"Blocked production BUY for unapproved symbol {normalized_symbol}: it is "
            "not backed by an active persisted Trade Card. No broker mutation was sent. "
            "Move the reviewed plan to Buy Today and refresh readiness before retrying."
        )
    notional = int(quantity) * float(limit_price)
    if not math.isfinite(notional) or notional <= 0:
        raise LiveExecutionEnvelopeError(
            "Blocked production BUY: CONTROLLED_LIVE entry notional must be positive "
            "and finite. No broker mutation was sent. Correct the order and retry."
        )
    if notional > float(execution_config.KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL):
        raise LiveExecutionEnvelopeError(
            "Blocked production BUY: the entry exceeds the CONTROLLED_LIVE maximum "
            "notional. No broker mutation was sent. Reduce the order or deliberately "
            "review the configured ceiling before retrying."
        )


def controlled_live_symbols(
    *,
    environment: str = "PROD",
    account_no: str = "",
    engine: Engine | None = None,
) -> tuple[str, ...]:
    """Return the persisted live-entry stock list for status reporting.

    Trade Cards are the canonical user-owned plan state. Moving a reviewed
    card to Buy Today is the explicit authorization event; Entry Pending stays
    authorized while its durable order is being tracked, and a narrowly
    defined Entry Completing position may finish its original target. When an
    engine is supplied the canonical database is read. Without one, the local
    recovery snapshot is used for non-authoritative UI/preflight reporting
    only. The broker boundary never relies on that snapshot.
    """

    normalized_environment = str(environment or "").strip().upper()
    normalized_account = str(account_no or "").strip()
    try:
        if engine is not None:
            cards = trade_card_repository.list_trade_cards(
                engine,
                environment=normalized_environment,
                account_no=normalized_account or None,
                raise_on_error=True,
            )
        else:
            cards = trade_card_repository.load_local_trade_cards_snapshot(
                path=trade_card_repository.LOCAL_TRADE_CARDS_FILE
            )
    except Exception:
        return ()
    try:
        symbols = {
            str(card.symbol or "").strip().upper()
            for card in cards
            if str(card.environment or "").strip().upper()
            == normalized_environment
            and (
                not normalized_account
                or str(card.account_no or "").strip() == normalized_account
            )
            and _card_authorizes_entry(card)
            and str(card.symbol or "").strip()
        }
    except Exception:
        return ()
    return tuple(sorted(symbols))


def live_entry_card_allowed(card: Any) -> bool:
    """Authorize execution scope directly from an authoritative loaded card."""

    if str(getattr(card, "environment", "") or "").strip().upper() != "PROD":
        return True
    if _mode() != CONTROLLED_LIVE:
        return True
    try:
        return bool(
            str(getattr(card, "symbol", "") or "").strip()
            and _card_authorizes_entry(card)
        )
    except Exception:
        return False


def live_entry_symbol_allowed(
    *,
    environment: str,
    symbol: str,
    account_no: str = "",
    engine: Engine | None = None,
) -> bool:
    """Check exact canonical card state for final controlled-live authority.

    An unavailable database, missing engine, malformed row, account mismatch,
    or inactive card all fail closed. The local JSON recovery snapshot is
    deliberately excluded from this broker-authority decision.
    """

    if str(environment or "").strip().upper() != "PROD":
        return True
    if _mode() != CONTROLLED_LIVE:
        return True
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_account = str(account_no or "").strip()
    if not normalized_symbol or not normalized_account or engine is None:
        return False
    try:
        card = trade_card_repository.get_trade_card(
            engine,
            str(environment or "").strip().upper(),
            normalized_account,
            normalized_symbol,
            raise_on_error=True,
        )
        return bool(card is not None and _card_authorizes_entry(card))
    except Exception:
        return False


def automatic_mutation_retry_permitted(*, environment: str) -> bool:
    """Whether a low-level KIS mutation may repeat inside the same call.

    Controlled live always returns false. The scheduler and the API adapter
    both consult the same mode so a token-expiry branch cannot bypass the
    one-attempt pilot promise.
    """

    return not (
        str(environment or "").strip().upper() == "PROD"
        and _mode() == CONTROLLED_LIVE
    )
