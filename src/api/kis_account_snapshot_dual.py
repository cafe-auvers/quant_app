"""Fetch KIS account cash and holdings with explicit SIM / PROD profiles.

This script is intentionally standalone so KIS connectivity can be verified
before the client is integrated into the PyQt5 dashboard.

Design goals:
    - SIM and PROD credentials are completely separated.
    - SIM uses the KIS virtual-trading host by default.
    - PROD uses the KIS live host by default.
    - Each environment has its own token cache file.
    - The script is read-only: account balance and holdings only.

Required .env variables:

    # SIM / paper trading
    KIS_SIM_APP_KEY=your_sim_app_key
    KIS_SIM_APP_SECRET=your_sim_app_secret
    KIS_SIM_ACCOUNT_NO=12345678-01

    # PROD / live trading
    KIS_PROD_APP_KEY=your_prod_app_key
    KIS_PROD_APP_SECRET=your_prod_app_secret
    KIS_PROD_ACCOUNT_NO=87654321-01

Optional .env variables:

    KIS_SIM_BASE_URL=https://openapivts.koreainvestment.com:29443
    KIS_PROD_BASE_URL=https://openapi.koreainvestment.com:9443

    KIS_SIM_TOKEN_CACHE=.kis_token_cache_sim.json
    KIS_PROD_TOKEN_CACHE=.kis_token_cache_prod.json

    KIS_SIM_OVERSEAS_EXCHANGES=NASD,NYSE,AMEX
    KIS_PROD_OVERSEAS_EXCHANGES=NASD,NYSE,AMEX
    KIS_SIM_OVERSEAS_CURRENCY=USD
    KIS_PROD_OVERSEAS_CURRENCY=USD

    # Override only if KIS returns a TR_ID-related error for your account/API type.
    KIS_SIM_OVERSEAS_BALANCE_TR_ID=VTTS3012R
    KIS_PROD_OVERSEAS_BALANCE_TR_ID=TTTS3012R

Examples:

    python kis_account_snapshot_dual.py --env SIM --domestic
    python kis_account_snapshot_dual.py --env PROD --domestic
    python kis_account_snapshot_dual.py --env SIM --domestic --overseas
    python kis_account_snapshot_dual.py --env PROD --domestic --save-json data/kis_prod_snapshot.json
    python kis_account_snapshot_dual.py --env SIM --domestic --raw --force-token
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is in requirements.txt, but keep script usable.
    load_dotenv = None


KIS_SIM_BASE_URL = "https://openapivts.koreainvestment.com:29443"
KIS_PROD_BASE_URL = "https://openapi.koreainvestment.com:9443"

TOKEN_ENDPOINT = "/oauth2/tokenP"
DOMESTIC_BALANCE_ENDPOINT = "/uapi/domestic-stock/v1/trading/inquire-balance"
OVERSEAS_BALANCE_ENDPOINT = "/uapi/overseas-stock/v1/trading/inquire-balance"

DOMESTIC_BALANCE_TR_ID = {
    "SIM": "VTTC8434R",
    "PROD": "TTTC8434R",
}

# These are common KIS overseas balance TR_IDs. Keep them configurable because
# overseas APIs can differ by account/product/API version.
OVERSEAS_BALANCE_TR_ID = {
    "SIM": "VTTS3012R",   # v1_해외주식-006 모의투자
    "PROD": "TTTS3012R",  # v1_해외주식-006 실전투자
}

DEFAULT_OVERSEAS_EXCHANGES = ("NASD", "NYSE", "AMEX")
DEFAULT_TIMEOUT = 15
RATE_LIMIT_MSG_CD = "EGW00201"
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = (1.0, 2.0, 4.0)


def _restrict_file_to_current_user(path: Path) -> None:
    """Best-effort lockdown so only the current OS user can read the token cache.

    POSIX chmod only ever restricted owner bits; on Windows it is a no-op, so
    the cached access token sat with default (often inherited, multi-user)
    ACLs. This adds an actual Windows ACL lockdown via icacls, falling back
    silently if it is unavailable so behavior never blocks the caller.
    """
    try:
        if platform.system() == "Windows":
            username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
            if not username:
                try:
                    username = os.getlogin()
                except OSError:
                    username = ""
            if not username:
                return
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(R,W)"],
                capture_output=True,
                check=False,
                timeout=10,
            )
        else:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


class KisEnvironment(str, Enum):
    """Supported KIS execution environments."""

    SIM = "SIM"
    PROD = "PROD"

    @property
    def is_sim(self) -> bool:
        return self is KisEnvironment.SIM

    @property
    def is_prod(self) -> bool:
        return self is KisEnvironment.PROD

    @property
    def default_base_url(self) -> str:
        return KIS_SIM_BASE_URL if self.is_sim else KIS_PROD_BASE_URL

    @property
    def default_token_cache(self) -> Path:
        return Path(".kis_token_cache_sim.json" if self.is_sim else ".kis_token_cache_prod.json")

    @property
    def domestic_balance_tr_id(self) -> str:
        return DOMESTIC_BALANCE_TR_ID[self.value]

    @property
    def overseas_balance_tr_id(self) -> str:
        return OVERSEAS_BALANCE_TR_ID[self.value]


class KisApiError(RuntimeError):
    """Raised when KIS returns an HTTP/API error."""


class KisRateLimitError(KisApiError):
    """Raised when KIS reports a per-second request limit error."""


class KisInvalidAccountError(KisApiError):
    """Raised when KIS rejects the account number or product code."""


class KisTokenError(KisApiError):
    """Raised when KIS rejects the access token as invalid or expired."""


@dataclass(frozen=True)
class KisConfig:
    """Runtime KIS credentials and account settings."""

    environment: KisEnvironment
    app_key: str
    app_secret: str
    cano: str
    account_product_code: str
    base_url: str
    token_cache_path: Optional[Path]
    overseas_exchanges: Tuple[str, ...]
    overseas_currency: str
    overseas_balance_tr_id_override: Optional[str] = None

    @property
    def account_no_masked(self) -> str:
        return f"{self.cano[:2]}******-{self.account_product_code}"

    @property
    def domestic_balance_tr_id(self) -> str:
        return self.environment.domestic_balance_tr_id

    @property
    def overseas_balance_tr_id(self) -> str:
        return self.overseas_balance_tr_id_override or self.environment.overseas_balance_tr_id


class KisAccountClient:
    """Small KIS REST client for authentication and account balance queries."""

    def __init__(self, config: KisConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.access_token: Optional[str] = None

    def authenticate(self, force_refresh: bool = False) -> str:
        """Load a cached access token or request a new one."""
        if not force_refresh:
            cached_token = self._load_cached_token()
            if cached_token:
                self.access_token = cached_token
                return cached_token

        url = f"{self.config.base_url}{TOKEN_ENDPOINT}"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        response = self._request_with_network_retry(
            "POST",
            url,
            headers={"content-type": "application/json"},
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
        data = self._parse_response(response, endpoint=TOKEN_ENDPOINT, check_rt_cd=False)
        token = data.get("access_token")
        if not token:
            raise KisApiError(f"KIS token response did not contain access_token: {data}")

        expires_in = int(data.get("expires_in") or 0)
        if expires_in <= 0:
            # Conservative fallback if the API response omits expires_in.
            expires_in = 60 * 60 * 23

        self.access_token = str(token)
        self._save_cached_token(token=str(token), expires_in=expires_in)
        return str(token)

    def get_domestic_balance(self) -> Dict[str, Any]:
        """Fetch domestic stock holdings and account summary."""
        params = {
            "CANO": self.config.cano,
            "ACNT_PRDT_CD": self.config.account_product_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",  # 01: by loan date, 02: by product
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        data = self._get(
            DOMESTIC_BALANCE_ENDPOINT,
            tr_id=self.config.domestic_balance_tr_id,
            params=params,
        )
        return {
            "summary": self._normalize_domestic_summary(data),
            "holdings": self._normalize_domestic_holdings(data),
            "raw": data,
        }

    def get_overseas_balance(self) -> Dict[str, Any]:
        """Fetch overseas stock holdings across configured exchanges."""
        all_holdings: List[Dict[str, Any]] = []
        summaries: Dict[str, Any] = {}
        raw_by_exchange: Dict[str, Any] = {}

        for exchange in self.config.overseas_exchanges:
            fk200 = ""
            nk200 = ""
            exchange_rows: List[Dict[str, Any]] = []
            page_count = 0

            while True:
                page_count += 1
                params = {
                    "CANO": self.config.cano,
                    "ACNT_PRDT_CD": self.config.account_product_code,
                    "OVRS_EXCG_CD": exchange,
                    "TR_CRCY_CD": self.config.overseas_currency,
                    "CTX_AREA_FK200": fk200,
                    "CTX_AREA_NK200": nk200,
                }
                data, headers = self._get_with_headers(
                    OVERSEAS_BALANCE_ENDPOINT,
                    tr_id=self.config.overseas_balance_tr_id,
                    params=params,
                    tr_cont="" if page_count == 1 else "N",
                )
                raw_by_exchange.setdefault(exchange, []).append(data)
                rows = self._as_list(data.get("output1"))
                exchange_rows.extend(rows)

                next_fk200 = str(data.get("ctx_area_fk200") or data.get("CTX_AREA_FK200") or "")
                next_nk200 = str(data.get("ctx_area_nk200") or data.get("CTX_AREA_NK200") or "")
                tr_cont = str(headers.get("tr_cont") or headers.get("tr-cont") or "").strip()

                # KIS pagination commonly returns tr_cont = F/M when more rows exist.
                has_more = tr_cont in {"F", "M"} and (next_fk200 or next_nk200)
                if not has_more or page_count >= 20:
                    summary = self._normalize_overseas_summary(data)
                    if summary:
                        summaries[exchange] = summary
                    break

                fk200, nk200 = next_fk200, next_nk200
                time.sleep(0.2)

            for row in exchange_rows:
                normalized = self._normalize_overseas_holding(row, exchange=exchange)
                if normalized is not None:
                    all_holdings.append(normalized)
            time.sleep(0.5)

        # KIS returns the same holding in output1 for multiple exchange queries
        # (e.g. DELL appears in both NASD and NYSE responses). Deduplicate on
        # (symbol, name) rather than symbol alone: genuine duplicate rows share
        # an identical name, while two distinct securities that happen to share
        # a ticker on different exchanges will have different names and are
        # kept as separate holdings instead of silently overwriting each other.
        by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for h in all_holdings:
            key = (h.get("symbol", ""), h.get("name", ""))
            if key not in by_key or h.get("evaluation_amount", 0) >= by_key[key].get("evaluation_amount", 0):
                by_key[key] = h
        all_holdings = list(by_key.values())

        return {
            "summary_by_exchange": summaries,
            "holdings": all_holdings,
            "raw_by_exchange": raw_by_exchange,
        }

    def get_account_snapshot(self, include_domestic: bool, include_overseas: bool) -> Dict[str, Any]:
        """Authenticate and fetch requested account sections."""
        self.authenticate()
        snapshot: Dict[str, Any] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "environment": self.config.environment.value,
            "account": self.config.account_no_masked,
            "base_url": self.config.base_url,
            "tr_ids": {
                "domestic_balance": self.config.domestic_balance_tr_id,
                "overseas_balance": self.config.overseas_balance_tr_id,
            },
        }
        if include_domestic:
            snapshot["domestic"] = self.get_domestic_balance()
        if include_overseas:
            snapshot["overseas"] = self.get_overseas_balance()
        return snapshot

    def _headers(self, tr_id: str, tr_cont: str = "") -> Dict[str, str]:
        if not self.access_token:
            self.authenticate()
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": "P",  # personal customer
            "tr_cont": tr_cont,  # "" for first page, "N" for continuation pages
        }

    def _get(self, endpoint: str, tr_id: str, params: Dict[str, str], tr_cont: str = "") -> Dict[str, Any]:
        data, _headers = self._get_with_headers(endpoint, tr_id=tr_id, params=params, tr_cont=tr_cont)
        return data

    def _get_with_headers(
        self,
        endpoint: str,
        tr_id: str,
        params: Dict[str, str],
        tr_cont: str = "",
    ) -> Tuple[Dict[str, Any], requests.structures.CaseInsensitiveDict[str]]:
        """Fetch a balance endpoint, transparently refreshing a stale token once.

        A cached token can be rejected by KIS even though it looked unexpired
        locally (e.g. revoked elsewhere). In that case force a fresh token and
        retry the call exactly once before giving up.
        """
        try:
            return self._get_with_headers_inner(endpoint, tr_id=tr_id, params=params, tr_cont=tr_cont)
        except KisTokenError:
            self.authenticate(force_refresh=True)
            return self._get_with_headers_inner(endpoint, tr_id=tr_id, params=params, tr_cont=tr_cont)

    def _get_with_headers_inner(
        self,
        endpoint: str,
        tr_id: str,
        params: Dict[str, str],
        tr_cont: str = "",
    ) -> Tuple[Dict[str, Any], requests.structures.CaseInsensitiveDict[str]]:
        url = f"{self.config.base_url}{endpoint}"
        last_error: Optional[KisApiError] = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            response = self._request_with_network_retry(
                "GET",
                url,
                headers=self._headers(tr_id=tr_id, tr_cont=tr_cont),
                params=params,
                timeout=DEFAULT_TIMEOUT,
            )
            try:
                return self._parse_response(response, endpoint=endpoint), response.headers
            except KisRateLimitError as exc:
                last_error = exc
                if attempt >= MAX_RATE_LIMIT_RETRIES:
                    break
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS[min(attempt, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)])

        raise last_error or KisRateLimitError("KIS request was rate limited.")

    def _request_with_network_retry(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Retry transient network failures (timeouts, connection resets) with backoff.

        This does not change behavior for any successful or API-level-error
        response; it only protects against momentary connectivity blips.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                return self.session.request(method, url, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_exc = exc
                if attempt >= MAX_RATE_LIMIT_RETRIES:
                    break
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS[min(attempt, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)])

        raise KisApiError(f"Network error calling KIS {url}: {last_exc}") from last_exc

    @staticmethod
    def _parse_response(
        response: requests.Response,
        endpoint: str,
        check_rt_cd: bool = True,
    ) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise KisApiError(
                f"KIS returned non-JSON response from {endpoint}. "
                f"HTTP {response.status_code}: {response.text[:300]}"
            ) from exc

        msg_cd = str(data.get("msg_cd", ""))
        msg1 = str(data.get("msg1", ""))
        if msg_cd in (RATE_LIMIT_MSG_CD, "EGW00215"):
            raise KisRateLimitError(f"KIS rate limit exceeded ({msg_cd}): {msg1}")
        if "token" in msg1.lower():
            raise KisTokenError(f"KIS token rejected by {endpoint}: {msg_cd} {msg1}. Raw={data}")
        if "INVALID_CHECK_ACNO" in msg1:
            raise KisInvalidAccountError(
                "KIS rejected the account number/product code. "
                "Verify the selected KIS account and product code in .env."
            )

        if response.status_code >= 400:
            raise KisApiError(f"KIS HTTP error from {endpoint}: HTTP {response.status_code}: {data}")

        if check_rt_cd and str(data.get("rt_cd", "0")) != "0":
            raise KisApiError(f"KIS API error from {endpoint}: {msg_cd} {msg1}. Raw={data}")

        return data

    def _load_cached_token(self) -> Optional[str]:
        path = self.config.token_cache_path
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        if data.get("environment") != self.config.environment.value:
            return None
        if data.get("app_key") != self.config.app_key:
            return None
        if data.get("base_url") != self.config.base_url:
            return None
        if int(data.get("expires_at", 0)) <= int(time.time()) + 60:
            return None
        token = data.get("access_token")
        return str(token) if token else None

    def _save_cached_token(self, token: str, expires_in: int) -> None:
        path = self.config.token_cache_path
        if path is None:
            return
        payload = {
            "environment": self.config.environment.value,
            "app_key": self.config.app_key,
            "base_url": self.config.base_url,
            "access_token": token,
            "expires_at": int(time.time()) + max(60, expires_in - 60),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            _restrict_file_to_current_user(path)
        except OSError:
            return

    @staticmethod
    def _normalize_domestic_summary(data: Dict[str, Any]) -> Dict[str, Any]:
        summary = first_dict(data.get("output2"))
        return {
            "cash_total_krw": to_number(summary.get("dnca_tot_amt")),
            "d2_deposit_krw": to_number(summary.get("prvs_rcdl_excc_amt")),
            "total_evaluation_krw": to_number(summary.get("tot_evlu_amt")),
            "stock_evaluation_krw": to_number(summary.get("scts_evlu_amt")),
            "purchase_amount_krw": to_number(summary.get("pchs_amt_smtl_amt")),
            "evaluation_profit_loss_krw": to_number(summary.get("evlu_pfls_smtl_amt")),
            "raw_summary": summary,
        }

    @staticmethod
    def _normalize_domestic_holdings(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        holdings = []
        for row in KisAccountClient._as_list(data.get("output1")):
            quantity = to_number(row.get("hldg_qty"))
            if quantity <= 0:
                continue
            holdings.append(
                {
                    "market": "KR",
                    "symbol": clean_str(row.get("pdno")),
                    "name": clean_str(row.get("prdt_name")),
                    "quantity": quantity,
                    "available_quantity": to_number(row.get("ord_psbl_qty")),
                    "average_price": to_number(row.get("pchs_avg_pric")),
                    "current_price": to_number(row.get("prpr")),
                    "purchase_amount": to_number(row.get("pchs_amt")),
                    "evaluation_amount": to_number(row.get("evlu_amt")),
                    "profit_loss": to_number(row.get("evlu_pfls_amt")),
                    "profit_loss_rate_pct": to_number(row.get("evlu_pfls_rt")),
                    "raw": row,
                }
            )
        return holdings

    @staticmethod
    def _normalize_overseas_summary(data: Dict[str, Any]) -> Dict[str, Any]:
        summary = first_dict(data.get("output2"))
        return {
            "cash_balance_usd": first_number(
                summary,
                # frcr_dncl_amt  = foreign currency deposit (available USD cash)
                # frcr_drwg_psbl_amt = withdrawal-available foreign currency
                # frcr_evlu_tota  = total foreign evaluation (fallback)
                ["frcr_dncl_amt", "frcr_drwg_psbl_amt", "ord_psbl_frcr_amt"],
            ),
            "foreign_stock_evaluation": first_number(
                summary,
                ["ovrs_stck_evlu_tota", "frcr_evlu_tota", "tot_evlu_pfls_amt"],
            ),
            "purchase_amount": first_number(
                summary,
                ["pchs_amt_smtl_amt", "ovrs_stck_buy_amt", "frcr_pchs_amt1"],
            ),
            "profit_loss": first_number(
                summary,
                ["evlu_pfls_smtl_amt", "tot_evlu_pfls_amt", "ovrs_tot_pfls"],
            ),
            "raw_summary": summary,
        }

    @staticmethod
    def _normalize_overseas_holding(row: Dict[str, Any], exchange: str) -> Optional[Dict[str, Any]]:
        quantity = first_number(row, ["ovrs_cblc_qty", "hldg_qty"])
        if quantity <= 0:
            return None
        return {
            "market": exchange,
            "symbol": clean_str(first_present(row, ["ovrs_pdno", "pdno"])),
            "name": clean_str(first_present(row, ["ovrs_item_name", "prdt_name"])),
            "quantity": quantity,
            "available_quantity": first_number(row, ["ord_psbl_qty", "ovrs_ord_psbl_qty"]),
            "average_price": first_number(row, ["pchs_avg_pric", "frcr_pchs_amt1"]),
            "current_price": first_number(row, ["now_pric2", "ovrs_now_pric1", "prpr"]),
            "purchase_amount": first_number(row, ["frcr_pchs_amt1", "pchs_amt"]),
            "evaluation_amount": first_number(row, ["ovrs_stck_evlu_amt", "evlu_amt"]),
            "profit_loss": first_number(row, ["frcr_evlu_pfls_amt", "evlu_pfls_amt"]),
            "profit_loss_rate_pct": first_number(row, ["evlu_pfls_rt", "evlu_pfls_rate"]),
            "raw": row,
        }

    @staticmethod
    def _as_list(value: Any) -> List[Dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        return []


def load_config(environment: KisEnvironment, account_no_override: Optional[str] = None) -> KisConfig:
    """Build KisConfig from profile-specific environment variables."""
    if load_dotenv is not None:
        load_dotenv()

    prefix = f"KIS_{environment.value}"
    legacy_prod = load_legacy_prod_config() if environment.is_prod else {}
    app_key = required_env(f"{prefix}_APP_KEY", fallback=legacy_prod.get("app_key"))
    app_secret = required_env(f"{prefix}_APP_SECRET", fallback=legacy_prod.get("app_secret"))
    account_no = account_no_override or required_env(f"{prefix}_ACCOUNT_NO")
    default_product_code = os.getenv(f"{prefix}_ACCOUNT_PRODUCT_CODE", "01").strip() or "01"
    cano, account_product_code = split_account_no(account_no, default_product_code=default_product_code)

    token_cache_raw = os.getenv(
        f"{prefix}_TOKEN_CACHE",
        str(environment.default_token_cache),
    ).strip()
    token_cache_path = None if token_cache_raw.lower() in {"", "none", "false", "0"} else Path(token_cache_raw)

    overseas_exchanges_raw = os.getenv(
        f"{prefix}_OVERSEAS_EXCHANGES",
        ",".join(DEFAULT_OVERSEAS_EXCHANGES),
    )
    overseas_exchanges = tuple(
        item.strip().upper()
        for item in overseas_exchanges_raw.split(",")
        if item.strip()
    ) or DEFAULT_OVERSEAS_EXCHANGES

    return KisConfig(
        environment=environment,
        app_key=app_key,
        app_secret=app_secret,
        cano=cano,
        account_product_code=account_product_code,
        base_url=(
            os.getenv(f"{prefix}_BASE_URL")
            or legacy_prod.get("base_url")
            or environment.default_base_url
        ).rstrip("/"),
        token_cache_path=token_cache_path,
        overseas_exchanges=overseas_exchanges,
        overseas_currency=os.getenv(f"{prefix}_OVERSEAS_CURRENCY", "USD").strip().upper() or "USD",
        overseas_balance_tr_id_override=os.getenv(f"{prefix}_OVERSEAS_BALANCE_TR_ID") or None,
    )


def get_configured_account_numbers(environment: KisEnvironment) -> List[str]:
    """Return configured account numbers for a profile.

    KIS does not expose a general "list my accounts" call in the read-only
    balance/quote flow used here, so the dashboard offers accounts configured
    locally.
    """
    if load_dotenv is not None:
        load_dotenv()

    prefix = f"KIS_{environment.value}"
    raw_values: List[str] = []
    single = os.getenv(f"{prefix}_ACCOUNT_NO", "").strip()
    if single:
        raw_values.append(single)

    accounts_csv = os.getenv(f"{prefix}_ACCOUNTS", "").strip()
    if accounts_csv:
        raw_values.extend(item.strip() for item in accounts_csv.split(",") if item.strip())

    for index in range(1, 21):
        numbered = os.getenv(f"{prefix}_ACCOUNT_NO_{index}", "").strip()
        if numbered:
            raw_values.append(numbered)

    account_numbers: List[str] = []
    seen = set()
    for raw_value in raw_values:
        try:
            default_product_code = os.getenv(f"{prefix}_ACCOUNT_PRODUCT_CODE", "01").strip() or "01"
            cano, product_code = split_account_no(raw_value, default_product_code=default_product_code)
        except ValueError:
            continue
        normalized = f"{cano}-{product_code}"
        if normalized not in seen:
            account_numbers.append(normalized)
            seen.add(normalized)
    return account_numbers


def discover_account_profiles() -> List[Dict[str, str]]:
    """Build dashboard account choices from configured SIM/PROD account numbers."""
    profiles: List[Dict[str, str]] = []
    for environment in (KisEnvironment.SIM, KisEnvironment.PROD):
        for account_no in get_configured_account_numbers(environment):
            default_product_code = os.getenv(f"KIS_{environment.value}_ACCOUNT_PRODUCT_CODE", "01").strip() or "01"
            cano, product_code = split_account_no(account_no, default_product_code=default_product_code)
            profiles.append({
                "environment": environment.value,
                "account_no": account_no,
                "account_no_masked": f"{cano[:2]}******-{product_code}",
                "label": f"{environment.value} {cano[:2]}******-{product_code}",
            })
    return profiles


def split_account_no(account_no: str, default_product_code: str = "01") -> Tuple[str, str]:
    """Accept '12345678', '12345678-01', '1234567801', or '12345678 01'."""
    cleaned = account_no.strip().replace("-", "").replace(" ", "")
    if len(cleaned) == 8 and cleaned.isdigit():
        product_code = default_product_code.strip()
        if len(product_code) != 2 or not product_code.isdigit():
            raise ValueError("KIS account product code must be two digits, such as 01")
        return cleaned, product_code
    if len(cleaned) != 10 or not cleaned.isdigit():
        raise ValueError("KIS account number must look like 12345678, 12345678-01, or 1234567801")
    return cleaned[:8], cleaned[8:]


def load_legacy_prod_config() -> Dict[str, str]:
    """Read legacy PROD credentials from src/api/kis_config.py if available."""
    try:
        try:
            from src.api import kis_config
        except ImportError:
            import kis_config  # type: ignore
    except Exception:
        return {}

    return {
        "app_key": clean_str(getattr(kis_config, "KIS_APP_KEY", "")),
        "app_secret": clean_str(getattr(kis_config, "KIS_APP_SECRET", "")),
        "base_url": clean_str(getattr(kis_config, "KIS_BASE_URL", "")),
    }


def required_env(key: str, fallback: Optional[str] = None) -> str:
    value = os.getenv(key, "").strip()
    if not value and fallback:
        value = str(fallback).strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


def clean_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def to_number(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).replace(",", "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def first_present(row: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def first_number(row: Dict[str, Any], keys: Iterable[str]) -> float:
    return to_number(first_present(row, keys))


def first_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def print_snapshot(snapshot: Dict[str, Any], show_raw: bool = False) -> None:
    """Print a compact human-readable account snapshot."""
    environment = snapshot.get("environment")
    print("\n=== KIS Account Snapshot ===")
    print(f"Fetched at : {snapshot.get('fetched_at')}")
    print(f"Environment: {environment}")
    print(f"Account    : {snapshot.get('account')}")
    print(f"Base URL   : {snapshot.get('base_url')}")

    tr_ids = snapshot.get("tr_ids", {})
    print(f"TR_IDs     : domestic={tr_ids.get('domestic_balance')} overseas={tr_ids.get('overseas_balance')}")

    domestic = snapshot.get("domestic")
    if domestic:
        summary = domestic.get("summary", {})
        holdings = domestic.get("holdings", [])
        print("\n--- Domestic Account ---")
        print(f"Cash total              : {summary.get('cash_total_krw', 0):,.0f} KRW")
        print(f"D+2 deposit             : {summary.get('d2_deposit_krw', 0):,.0f} KRW")
        print(f"Stock evaluation        : {summary.get('stock_evaluation_krw', 0):,.0f} KRW")
        print(f"Total evaluation        : {summary.get('total_evaluation_krw', 0):,.0f} KRW")
        print(f"Evaluation P/L          : {summary.get('evaluation_profit_loss_krw', 0):,.0f} KRW")
        print(f"Holdings                : {len(holdings)}")
        for item in holdings:
            print(
                f"  {item['symbol']:>8} {item['name'][:24]:<24} "
                f"qty={item['quantity']:,.0f} avg={item['average_price']:,.2f} "
                f"now={item['current_price']:,.2f} eval={item['evaluation_amount']:,.0f} "
                f"P/L={item['profit_loss']:,.0f} ({item['profit_loss_rate_pct']:,.2f}%)"
            )

    overseas = snapshot.get("overseas")
    if overseas:
        holdings = overseas.get("holdings", [])
        print("\n--- Overseas Account ---")
        for exchange, summary in overseas.get("summary_by_exchange", {}).items():
            print(f"{exchange} summary: {summary}")
        print(f"Holdings: {len(holdings)}")
        for item in holdings:
            print(
                f"  {item['market']:<4} {item['symbol']:>8} {item['name'][:24]:<24} "
                f"qty={item['quantity']:,.4f} avg={item['average_price']:,.4f} "
                f"now={item['current_price']:,.4f} eval={item['evaluation_amount']:,.2f} "
                f"P/L={item['profit_loss']:,.2f} ({item['profit_loss_rate_pct']:,.2f}%)"
            )

    if show_raw:
        print("\n--- Raw JSON ---")
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))


def save_snapshot(snapshot: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_account_snapshot(
    environment: KisEnvironment | str,
    include_domestic: bool = True,
    include_overseas: bool = False,
    force_token: bool = False,
    account_no: Optional[str] = None,
) -> Dict[str, Any]:
    """Load config, authenticate, and fetch a read-only account snapshot."""
    env = environment if isinstance(environment, KisEnvironment) else KisEnvironment(str(environment).upper())
    config = load_config(env, account_no_override=account_no)
    client = KisAccountClient(config)
    client.authenticate(force_refresh=force_token)
    return client.get_account_snapshot(
        include_domestic=include_domestic,
        include_overseas=include_overseas,
    )


def probe_account_product_codes(
    environment: KisEnvironment | str,
    account_no: str,
    product_codes: Iterable[str],
) -> List[Dict[str, str]]:
    """Try product-code candidates with a domestic balance call.

    This is a read-only diagnostic for resolving INVALID_CHECK_ACNO.
    """
    env = environment if isinstance(environment, KisEnvironment) else KisEnvironment(str(environment).upper())
    cano, _product_code = split_account_no(account_no)
    results: List[Dict[str, str]] = []
    for product_code in product_codes:
        candidate = str(product_code).strip()
        if len(candidate) != 2 or not candidate.isdigit():
            continue
        normalized_account = f"{cano}-{candidate}"
        try:
            snapshot = fetch_account_snapshot(
                env,
                include_domestic=True,
                include_overseas=False,
                account_no=normalized_account,
            )
            holdings = snapshot.get("domestic", {}).get("holdings", [])
            results.append({
                "account": f"{cano[:2]}******-{candidate}",
                "product_code": candidate,
                "status": "ok",
                "message": f"Accepted; holdings={len(holdings)}",
            })
        except Exception as exc:
            results.append({
                "account": f"{cano[:2]}******-{candidate}",
                "product_code": candidate,
                "status": "error",
                "message": str(exc),
            })
        time.sleep(1.0)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch KIS account cash and holdings using SIM or PROD profile.")
    parser.add_argument(
        "--env",
        choices=[item.value for item in KisEnvironment],
        default=KisEnvironment.SIM.value,
        help="KIS environment profile. SIM is paper trading; PROD is live account. Default: SIM.",
    )
    parser.add_argument("--domestic", action="store_true", help="Fetch domestic stock balance.")
    parser.add_argument("--overseas", action="store_true", help="Fetch overseas stock balance.")
    parser.add_argument("--force-token", action="store_true", help="Ignore cached token and request a new one.")
    parser.add_argument("--raw", action="store_true", help="Print raw KIS response JSON as well.")
    parser.add_argument("--save-json", type=Path, help="Save normalized snapshot JSON to this path.")
    parser.add_argument(
        "--probe-product-codes",
        help="Read-only diagnostic. Comma-separated product codes to test for the selected account, e.g. 01,03,22.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt when running --probe-product-codes against PROD.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    environment = KisEnvironment(args.env)

    # Default to domestic-only if neither section is explicitly requested.
    include_domestic = args.domestic or not args.overseas
    include_overseas = args.overseas

    try:
        if args.probe_product_codes:
            if environment.is_prod and not args.yes:
                confirmation = input(
                    "This probes live PROD account/product codes with real API calls. "
                    "Type 'yes' to continue: "
                ).strip().lower()
                if confirmation != "yes":
                    print("Aborted.", file=sys.stderr)
                    return 1
            config = load_config(environment)
            candidates = [item.strip() for item in args.probe_product_codes.split(",")]
            results = probe_account_product_codes(environment, f"{config.cano}-{config.account_product_code}", candidates)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return 0

        snapshot = fetch_account_snapshot(
            environment,
            include_domestic=include_domestic,
            include_overseas=include_overseas,
            force_token=args.force_token,
        )
        print_snapshot(snapshot, show_raw=args.raw)
        if args.save_json:
            save_snapshot(snapshot, args.save_json)
            print(f"\nSaved snapshot to: {args.save_json}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
