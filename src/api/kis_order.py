"""KIS overseas equity order placement."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

import requests

from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus

from .kis_account_snapshot_dual import (
    KisAccountClient,
    KisEnvironment,
    KisInvalidAccountError,
    KisTokenError,
    load_config,
)

logger = logging.getLogger(__name__)

OVERSEAS_ORDER_ENDPOINT = "/uapi/overseas-stock/v1/trading/order"
AMBIGUOUS_ORDER_HTTP_STATUS_CODES = {502, 503, 504}

_AMBIGUOUS_ORDER_ERROR_FRAGMENTS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "connection refused",
    "network unreachable",
    "network error",
    "temporary failure in name resolution",
    "temporary dns",
    "temporary network",
    "name resolution",
    "max retries exceeded",
    "remote end closed connection",
    "connection broken",
    "non-json response",
    "invalid response",
    "invalid json",
    "empty response",
    "no response",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)

_CLEAR_ORDER_ERROR_FRAGMENTS = (
    "quantity must be positive",
    "limit_price must be positive",
    "price must be positive",
    "no tr_id",
    "invalid symbol",
    "invalid quantity",
    "invalid pdno",
    "invalid_check_acno",
    "insufficient",
    "not enough",
    "account",
    "auth",
    "unauthorized",
    "forbidden",
    "token",
    "unsupported",
    "does not provide this task",
    "not provide this task",
    "parameter",
    "validation",
    "rejected",
    "reject",
    "missing required environment variable",
    "must look like",
    "not configured",
    "rate limit",
    "duplicate",
    "already exists",
)

_ORDER_TR_IDS: Dict[tuple, str] = {
    ("SIM",  "buy"):  "VTTT1002U",  # v1_해외주식-001 모의투자 매수
    ("SIM",  "sell"): "VTTT1006U",  # v1_해외주식-001 모의투자 매도
    ("PROD", "buy"):  "TTTT1002U",  # v1_해외주식-001 실전투자 매수
    ("PROD", "sell"): "TTTT1006U",  # v1_해외주식-001 실전투자 매도
}


def _flatten_error_message(value: Any) -> str:
    parts = []
    current = value
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{current.__class__.__name__}: {current}" if isinstance(current, BaseException) else str(current))
        if not isinstance(current, BaseException):
            break
        current = current.__cause__ or current.__context__
    return " | ".join(part for part in parts if part)


def _http_status_from_error(value: Any, message: str) -> Optional[int]:
    response = getattr(value, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        try:
            return int(status_code)
        except (TypeError, ValueError):
            pass

    match = re.search(r"\bHTTP\s+(\d{3})\b", message, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def is_ambiguous_order_submission_error(exc_or_message: Any) -> bool:
    """Return True when a submission failure may still have reached KIS."""
    message = _flatten_error_message(exc_or_message)
    lowered = message.lower()
    status_code = _http_status_from_error(exc_or_message, message)

    if status_code in AMBIGUOUS_ORDER_HTTP_STATUS_CODES or (status_code is not None and 500 <= status_code < 600):
        return True
    if status_code is not None and 400 <= status_code < 500:
        return False

    if isinstance(exc_or_message, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc_or_message, (ValueError, KisTokenError, KisInvalidAccountError)):
        return False

    if any(fragment in lowered for fragment in _AMBIGUOUS_ORDER_ERROR_FRAGMENTS):
        return True
    if "kis api error" in lowered or "rt_cd" in lowered:
        return False
    if any(fragment in lowered for fragment in _CLEAR_ORDER_ERROR_FRAGMENTS):
        return False

    return isinstance(exc_or_message, BaseException)


def place_overseas_order(
    *,
    environment: str,
    symbol: str,
    quantity: int,
    price: float,
    side: str,
    exchange: str = "NASD",
    order_type: str = "limit",
    account_no: Optional[str] = None,
) -> Dict[str, Any]:
    """Place an overseas equity buy or sell order via KIS API.

    Args:
        environment: "SIM" or "PROD"
        symbol: Ticker symbol, e.g. "NVDA"
        quantity: Number of shares
        price: Limit price (ignored for market orders)
        side: "buy" or "sell"
        exchange: KIS exchange code — "NASD", "NYSE", or "AMEX"
        order_type: "limit" or "market"
        account_no: Optional account number override
    Returns:
        Parsed API response dict
    Raises:
        ValueError: unknown environment/side combination
        RuntimeError: API error response
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")

    tr_id = _ORDER_TR_IDS.get((environment.upper(), side.lower()))
    if not tr_id:
        raise ValueError(f"No tr_id for environment={environment!r} side={side!r}")

    env = KisEnvironment(environment.upper())
    config = load_config(env, account_no_override=account_no)
    client = KisAccountClient(config)
    client.authenticate()

    # KIS overseas (US) orders only support limit (ORD_DVSN=00).
    # "market" is not accepted — callers must pass an aggressive limit price instead.
    ord_dvsn = "00"
    ovrs_ord_unpr = f"{price:.2f}"

    body: Dict[str, str] = {
        "CANO": config.cano,
        "ACNT_PRDT_CD": config.account_product_code,
        "OVRS_EXCG_CD": exchange.upper(),
        "PDNO": symbol.upper(),
        "ORD_DVSN": ord_dvsn,
        "ORD_QTY": str(int(quantity)),
        "OVRS_ORD_UNPR": ovrs_ord_unpr,
        "SLL_TYPE": "00" if side.lower() == "sell" else "",
        "ORD_SVR_DVSN_CD": "0",
        "CTAC_TLNO": "",
        "MGCO_APTM_ODNO": "",
    }

    logger.info(
        "KIS %s order: %s %s x%d @ %s on %s (tr_id=%s)",
        environment, side.upper(), symbol, quantity, ovrs_ord_unpr, exchange, tr_id,
    )

    url = f"{config.base_url}{OVERSEAS_ORDER_ENDPOINT}"
    def _post_order() -> Dict[str, Any]:
        response = client.session.post(
            url,
            headers=client._headers(tr_id=tr_id),
            json=body,
            timeout=15,
        )
        return client._parse_response(response, endpoint=OVERSEAS_ORDER_ENDPOINT)

    try:
        result = _post_order()
    except KisTokenError:
        logger.info("KIS order token expired for %s; refreshing token and retrying once.", environment)
        client.authenticate(force_refresh=True)
        result = _post_order()

    logger.info("KIS order result: %s", result)
    return result


def _find_broker_order_id(result: Dict[str, Any]) -> str:
    candidates = [
        "ODNO",
        "odno",
        "order_no",
        "ord_no",
        "ORDER_NO",
        "KIS_ORDER_NO",
        "ORD_NO",
    ]

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            for key in candidates:
                if value.get(key):
                    return str(value[key])
            for item in value.values():
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return ""

    return walk(result)


def submit_overseas_order(
    *,
    environment: str,
    symbol: str,
    quantity: int,
    price: float,
    side: str,
    exchange: str = "NASD",
    order_type: str = "limit",
    account_no: Optional[str] = None,
    intent: OrderIntent | str = OrderIntent.UNKNOWN,
    buylist_symbol_key: str = "",
) -> BrokerOrder:
    """Submit an overseas order and return broker acceptance state only.

    A successful KIS API response means the broker accepted/received the order
    request. It does not imply a fill, average cost, or position change.
    """
    side_enum = OrderSide(str(side).upper())
    order = BrokerOrder.create(
        environment=environment,
        account_no=account_no or "",
        symbol=symbol,
        side=side_enum,
        intent=intent,
        quantity_requested=quantity,
        limit_price=price,
        exchange=exchange,
        status=OrderStatus.SUBMITTING,
        buylist_symbol_key=buylist_symbol_key or symbol,
    )
    try:
        result = place_overseas_order(
            environment=environment,
            symbol=symbol,
            quantity=quantity,
            price=price,
            side=side.lower(),
            exchange=exchange,
            order_type=order_type,
            account_no=account_no,
        )
        order.raw_submit_response = result
        order.broker_order_id = _find_broker_order_id(result)
        order.status = OrderStatus.ACCEPTED
        order.remaining_quantity = order.quantity_requested
        order.touch()
        return order
    except Exception as exc:
        if is_ambiguous_order_submission_error(exc):
            order.status = OrderStatus.UNKNOWN_SUBMISSION_STATE
            logger.warning(
                "KIS order submission result unknown for %s %s %s: %s",
                environment,
                side.upper(),
                symbol,
                exc,
            )
        else:
            order.status = OrderStatus.REJECTED
        order.error_message = str(exc)
        order.touch()
        return order
