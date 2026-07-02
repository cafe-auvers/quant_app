"""KIS overseas equity order placement."""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from src.core.order_state import BrokerOrder, BrokerOrderStatusSnapshot, OrderIntent, OrderSide, OrderStatus

from .kis_account_snapshot_dual import (
    KisAccountClient,
    KisEnvironment,
    KisInvalidAccountError,
    KisTokenError,
    load_config,
)

logger = logging.getLogger(__name__)

OVERSEAS_ORDER_ENDPOINT = "/uapi/overseas-stock/v1/trading/order"
OVERSEAS_ORDER_INQUIRY_ENDPOINT = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
OVERSEAS_OPEN_ORDER_INQUIRY_ENDPOINT = "/uapi/overseas-stock/v1/trading/inquire-nccs"
OVERSEAS_ORDER_CANCEL_ENDPOINT = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
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

_ORDER_INQUIRY_TR_IDS: Dict[str, str] = {
    "SIM": "VTTS3035R",   # v1_overseas-stock-007 paper: order/fill history
    "PROD": "TTTS3035R",  # v1_overseas-stock-007 live: order/fill history
}

_OPEN_ORDER_INQUIRY_TR_IDS: Dict[str, str] = {
    # Official KIS examples document TTTS3018R for this endpoint and include
    # env_dv without a separate paper TR_ID. Keep this visible and overrideable
    # if KIS publishes an account-specific SIM TR_ID.
    "SIM": "TTTS3018R",
    "PROD": "TTTS3018R",
}

_ORDER_CANCEL_TR_IDS: Dict[str, str] = {
    "SIM": "VTTT1004U",   # v1_overseas-stock-003 paper: revise/cancel
    "PROD": "TTTT1004U",  # v1_overseas-stock-003 live: revise/cancel
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


def _env_key(environment: str) -> str:
    return str(environment or "").strip().upper()


def _order_tr_id(mapping: Dict[str, str], environment: str, suffix: str) -> str:
    env_key = _env_key(environment)
    override = os.getenv(f"KIS_{env_key}_OVERSEAS_{suffix}_TR_ID", "").strip()
    tr_id = override or mapping.get(env_key, "")
    if not tr_id:
        raise ValueError(f"No KIS TR_ID configured for environment={environment!r} operation={suffix!r}")
    return tr_id


def _as_list(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _output_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for key in ("output", "output1", "output2"):
        rows.extend(_as_list(data.get(key)))
    return rows


def _row_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        for candidate in (key, key.lower(), key.upper()):
            if candidate in row and row[candidate] not in (None, ""):
                return row[candidate]
    return None


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").strip() or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_side(value: Any) -> OrderSide:
    text = str(value or "").strip().upper()
    if text in {"01", "SELL", "SLL", "S"} or "SELL" in text or "매도" in text:
        return OrderSide.SELL
    return OrderSide.BUY


def _side_query_code(side: str) -> str:
    text = str(side or "").strip().upper()
    if text == "SELL":
        return "01"
    if text == "BUY":
        return "02"
    return "00"


def _default_order_dates(start_date: Optional[str], end_date: Optional[str]) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    start = start_date or (today - timedelta(days=14)).strftime("%Y%m%d")
    end = end_date or today.strftime("%Y%m%d")
    return start.replace("-", ""), end.replace("-", "")


def _broker_order_id_from_row(row: Dict[str, Any]) -> str:
    return str(_row_value(row, "odno", "ODNO", "order_no", "ord_no", "KIS_ORDER_NO") or "")


def _status_from_order_row(row: Dict[str, Any], *, source: str = "query") -> OrderStatus:
    requested = _to_int(_row_value(row, "ft_ord_qty", "ORD_QTY", "ord_qty", "qty", "quantity"))
    filled = _to_int(_row_value(row, "ft_ccld_qty", "CCLD_QTY", "filled_quantity", "filled_qty"))
    remaining = _to_int(_row_value(row, "nccs_qty", "NCCS_QTY", "remaining_quantity", "remaining_qty"))
    status_text = " ".join(
        str(_row_value(row, key) or "")
        for key in (
            "prcs_stat_name",
            "prcs_stat_cd",
            "rjct_rson",
            "rjct_rson_name",
            "rvse_cncl_dvsn",
            "rvse_cncl_dvsn_name",
            "rvse_cncl_dvsn_cd",
            "status",
            "ord_stat",
            "ord_stat_name",
            "msg1",
        )
    ).upper()

    if any(token in status_text for token in ("REJECT", "REJECTED", "RJECT", "RJCT", "거부", "거절")):
        return OrderStatus.REJECTED
    if any(token in status_text for token in ("EXPIRE", "EXPIRED", "만료")):
        return OrderStatus.EXPIRED
    if any(token in status_text for token in ("CANCELLED", "CANCELED", "CANCEL", "취소")):
        return OrderStatus.CANCELLED

    if source == "cancel_response":
        return OrderStatus.CANCEL_REQUESTED

    if requested > 0 and filled >= requested and remaining <= 0:
        return OrderStatus.FILLED
    if filled > 0 and (remaining > 0 or requested <= 0 or filled < requested):
        return OrderStatus.PARTIALLY_FILLED
    if remaining > 0:
        return OrderStatus.WORKING
    if source == "open_orders":
        return OrderStatus.WORKING
    if any(token in status_text for token in ("ACCEPT", "RECEIVED", "접수", "처리")):
        return OrderStatus.ACCEPTED
    if any(token in status_text for token in ("FILLED", "체결", "완료")):
        return OrderStatus.FILLED if requested <= 0 or filled >= requested else OrderStatus.PARTIALLY_FILLED
    return OrderStatus.UNKNOWN


def parse_broker_order_status_snapshot(
    row: Dict[str, Any],
    *,
    environment: str,
    account_no: str,
    client_order_id: str = "",
    source: str = "query",
) -> BrokerOrderStatusSnapshot:
    """Normalize one KIS overseas order row into the app's broker snapshot model."""
    broker_order_id = _broker_order_id_from_row(row)
    requested = _to_int(_row_value(row, "ft_ord_qty", "ORD_QTY", "ord_qty", "qty", "quantity"))
    filled = _to_int(_row_value(row, "ft_ccld_qty", "CCLD_QTY", "filled_quantity", "filled_qty"))
    remaining = _to_int(_row_value(row, "nccs_qty", "NCCS_QTY", "remaining_quantity", "remaining_qty"))
    if requested <= 0 and filled + remaining > 0:
        requested = filled + remaining
    if remaining <= 0 and requested > 0 and filled > 0:
        remaining = max(0, requested - filled)
    symbol = str(_row_value(row, "pdno", "PDNO", "symbol", "ovrs_pdno") or "").upper()
    side = _normalize_side(_row_value(row, "sll_buy_dvsn_cd", "sll_buy_dvsn_cd_name", "side"))
    status = _status_from_order_row(row, source=source)

    return BrokerOrderStatusSnapshot(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        side=side,
        status=status,
        quantity_requested=requested,
        filled_quantity=filled,
        remaining_quantity=remaining,
        avg_fill_price=_to_float(_row_value(row, "ft_ccld_unpr3", "avg_fill_price", "avg_price")),
        limit_price=_to_float(_row_value(row, "ft_ord_unpr3", "OVRS_ORD_UNPR", "limit_price", "ord_unpr")),
        raw_response=dict(row),
    )


def _matches_order_filter(
    snapshot: BrokerOrderStatusSnapshot,
    *,
    symbol: str = "",
    broker_order_id: str = "",
    side: str = "",
) -> bool:
    if broker_order_id and snapshot.broker_order_id != str(broker_order_id):
        return False
    if symbol and snapshot.symbol and snapshot.symbol != str(symbol).upper():
        return False
    if side and snapshot.side != _normalize_side(side):
        return False
    return True


def _unknown_snapshot(
    *,
    environment: str,
    account_no: str,
    symbol: str,
    broker_order_id: str = "",
    client_order_id: str = "",
    side: str = "",
    raw_response: Dict[str, Any],
) -> BrokerOrderStatusSnapshot:
    return BrokerOrderStatusSnapshot(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        side=_normalize_side(side),
        status=OrderStatus.UNKNOWN,
        raw_response=raw_response,
    )


def _query_pages(
    client: KisAccountClient,
    *,
    endpoint: str,
    tr_id: str,
    params: Dict[str, str],
    max_pages: int = 10,
) -> List[Dict[str, Any]]:
    pages: List[Dict[str, Any]] = []
    fk200 = params.get("CTX_AREA_FK200", "")
    nk200 = params.get("CTX_AREA_NK200", "")
    tr_cont = ""
    for page_index in range(max(1, max_pages)):
        page_params = dict(params)
        page_params["CTX_AREA_FK200"] = fk200
        page_params["CTX_AREA_NK200"] = nk200
        data, headers = client._get_with_headers(
            endpoint,
            tr_id=tr_id,
            params=page_params,
            tr_cont=tr_cont,
        )
        pages.append(data)
        next_fk200 = str(data.get("ctx_area_fk200") or data.get("CTX_AREA_FK200") or "")
        next_nk200 = str(data.get("ctx_area_nk200") or data.get("CTX_AREA_NK200") or "")
        header_tr_cont = str(headers.get("tr_cont") or headers.get("tr-cont") or "").strip()
        if header_tr_cont not in {"F", "M"} or (not next_fk200 and not next_nk200):
            break
        fk200, nk200 = next_fk200, next_nk200
        tr_cont = "N"
        time.sleep(0.2)
    return pages


def query_overseas_order(
    *,
    environment: str,
    account_no: str,
    symbol: str = "",
    broker_order_id: str = "",
    client_order_id: str = "",
    side: str = "",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    exchange: str = "NASD",
) -> List[BrokerOrderStatusSnapshot]:
    """Query KIS for overseas order status and return normalized snapshots.

    The official KIS examples used here are:
    - overseas-stock/trading/inquire-nccs for open/unfilled orders
    - overseas-stock/trading/inquire-ccnl for order/fill history
    """
    env_key = _env_key(environment)
    env = KisEnvironment(env_key)
    config = load_config(env, account_no_override=account_no)
    client = KisAccountClient(config)
    client.authenticate()

    symbol = str(symbol or "").strip().upper()
    broker_order_id = str(broker_order_id or "").strip()
    side_code = _side_query_code(side)
    exchange = str(exchange or "NASD").strip().upper()
    start, end = _default_order_dates(start_date, end_date)

    all_pages: Dict[str, List[Dict[str, Any]]] = {"open": [], "history": []}
    snapshots: List[BrokerOrderStatusSnapshot] = []

    open_params = {
        "CANO": config.cano,
        "ACNT_PRDT_CD": config.account_product_code,
        "OVRS_EXCG_CD": exchange or "NASD",
        "SORT_SQN": "DS",
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": "",
    }
    open_tr_id = _order_tr_id(_OPEN_ORDER_INQUIRY_TR_IDS, env_key, "NCCS")
    try:
        all_pages["open"] = _query_pages(
            client,
            endpoint=OVERSEAS_OPEN_ORDER_INQUIRY_ENDPOINT,
            tr_id=open_tr_id,
            params=open_params,
        )
    except Exception as exc:
        logger.warning("KIS open-order query failed for %s %s: %s", env_key, account_no, exc)

    for page in all_pages["open"]:
        for row in _output_rows(page):
            snapshot = parse_broker_order_status_snapshot(
                row,
                environment=env_key,
                account_no=account_no,
                client_order_id=client_order_id,
                source="open_orders",
            )
            if _matches_order_filter(snapshot, symbol=symbol, broker_order_id=broker_order_id, side=side):
                snapshots.append(snapshot)

    history_params = {
        "CANO": config.cano,
        "ACNT_PRDT_CD": config.account_product_code,
        "PDNO": "" if env_key == "SIM" else (symbol or "%"),
        "ORD_STRT_DT": start,
        "ORD_END_DT": end,
        "SLL_BUY_DVSN": "00" if env_key == "SIM" else side_code,
        "CCLD_NCCS_DVSN": "00",
        "OVRS_EXCG_CD": "" if env_key == "SIM" else (exchange or "NASD"),
        "SORT_SQN": "DS",
        "ORD_DT": "",
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "CTX_AREA_NK200": "",
        "CTX_AREA_FK200": "",
    }
    history_tr_id = _order_tr_id(_ORDER_INQUIRY_TR_IDS, env_key, "CCNL")
    all_pages["history"] = _query_pages(
        client,
        endpoint=OVERSEAS_ORDER_INQUIRY_ENDPOINT,
        tr_id=history_tr_id,
        params=history_params,
    )
    for page in all_pages["history"]:
        for row in _output_rows(page):
            snapshot = parse_broker_order_status_snapshot(
                row,
                environment=env_key,
                account_no=account_no,
                client_order_id=client_order_id,
                source="history",
            )
            if _matches_order_filter(snapshot, symbol=symbol, broker_order_id=broker_order_id, side=side):
                snapshots.append(snapshot)

    by_key: Dict[tuple, BrokerOrderStatusSnapshot] = {}
    for snapshot in snapshots:
        key = (
            snapshot.broker_order_id or snapshot.client_order_id or snapshot.symbol,
            snapshot.status.value,
            snapshot.filled_quantity,
            snapshot.remaining_quantity,
        )
        by_key[key] = snapshot
    snapshots = list(by_key.values())
    if snapshots:
        return snapshots

    return [
        _unknown_snapshot(
            environment=env_key,
            account_no=account_no,
            symbol=symbol,
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            side=side,
            raw_response={
                "not_found": True,
                "query": {
                    "symbol": symbol,
                    "broker_order_id": broker_order_id,
                    "side": side,
                    "start_date": start,
                    "end_date": end,
                    "exchange": exchange,
                },
                "raw_pages": all_pages,
            },
        )
    ]


def cancel_overseas_order(
    *,
    environment: str,
    account_no: str,
    symbol: str,
    broker_order_id: str,
    quantity: Optional[int] = None,
    side: Optional[str] = None,
    exchange: str = "NASD",
) -> BrokerOrderStatusSnapshot:
    """Submit a KIS overseas order cancel request and return a normalized snapshot."""
    if not str(broker_order_id or "").strip():
        raise ValueError("broker_order_id is required for KIS overseas cancel")
    if quantity is None or int(quantity or 0) <= 0:
        raise ValueError("quantity is required for KIS overseas cancel because order-rvsecncl requires ORD_QTY")

    env_key = _env_key(environment)
    env = KisEnvironment(env_key)
    config = load_config(env, account_no_override=account_no)
    client = KisAccountClient(config)
    client.authenticate()

    tr_id = _order_tr_id(_ORDER_CANCEL_TR_IDS, env_key, "CANCEL")
    body = {
        "CANO": config.cano,
        "ACNT_PRDT_CD": config.account_product_code,
        "OVRS_EXCG_CD": str(exchange or "NASD").strip().upper(),
        "PDNO": str(symbol or "").strip().upper(),
        "ORGN_ODNO": str(broker_order_id).strip(),
        "RVSE_CNCL_DVSN_CD": "02",
        "ORD_QTY": str(int(quantity)),
        "OVRS_ORD_UNPR": "0",
        "MGCO_APTM_ODNO": "",
        "ORD_SVR_DVSN_CD": "0",
    }
    url = f"{config.base_url}{OVERSEAS_ORDER_CANCEL_ENDPOINT}"

    def _post_cancel() -> Dict[str, Any]:
        response = client.session.post(
            url,
            headers=client._headers(tr_id=tr_id),
            json=body,
            timeout=15,
        )
        return client._parse_response(response, endpoint=OVERSEAS_ORDER_CANCEL_ENDPOINT)

    try:
        result = _post_cancel()
    except KisTokenError:
        logger.info("KIS cancel token expired for %s; refreshing token and retrying once.", env_key)
        client.authenticate(force_refresh=True)
        result = _post_cancel()

    output = _output_rows(result)
    row = dict(output[0]) if output else {}
    row.setdefault("PDNO", str(symbol or "").strip().upper())
    row.setdefault("ORGN_ODNO", str(broker_order_id).strip())
    row.setdefault("ORD_QTY", str(int(quantity)))
    if side:
        row.setdefault("side", str(side).upper())
    row.setdefault("msg1", result.get("msg1", ""))
    snapshot = parse_broker_order_status_snapshot(
        row,
        environment=env_key,
        account_no=account_no,
        source="cancel_response",
    )
    snapshot.broker_order_id = str(broker_order_id).strip()
    snapshot.status = _status_from_order_row(row, source="cancel_response")
    snapshot.raw_response = result
    return snapshot


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
