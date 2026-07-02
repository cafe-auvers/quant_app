"""KIS overseas equity order placement."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.core.order_state import BrokerOrder, OrderIntent, OrderSide, OrderStatus

from .kis_account_snapshot_dual import KisAccountClient, KisEnvironment, KisTokenError, load_config

logger = logging.getLogger(__name__)

OVERSEAS_ORDER_ENDPOINT = "/uapi/overseas-stock/v1/trading/order"

_ORDER_TR_IDS: Dict[tuple, str] = {
    ("SIM",  "buy"):  "VTTT1002U",  # v1_해외주식-001 모의투자 매수
    ("SIM",  "sell"): "VTTT1006U",  # v1_해외주식-001 모의투자 매도
    ("PROD", "buy"):  "TTTT1002U",  # v1_해외주식-001 실전투자 매수
    ("PROD", "sell"): "TTTT1006U",  # v1_해외주식-001 실전투자 매도
}


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
        order.status = OrderStatus.REJECTED
        order.error_message = str(exc)
        order.touch()
        return order
