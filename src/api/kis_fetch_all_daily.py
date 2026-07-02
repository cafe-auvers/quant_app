import argparse
import io
import json
import logging
import re
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from src.api.kis_config import KIS_BASE_URL, KIS_APP_KEY, KIS_APP_SECRET
except ImportError:
    from kis_config import KIS_BASE_URL, KIS_APP_KEY, KIS_APP_SECRET


REQUEST_SLEEP_SEC = 0.08
REQUEST_TIMEOUT_SEC = 20
MASTER_FILE_TIMEOUT_SEC = 60
FID_ORG_ADJ_PRC = "1"
OVERSEAS_DAILY_PRICE_ENDPOINT = "/uapi/overseas-price/v1/quotations/dailyprice"
OVERSEAS_DAILY_PRICE_TR_ID = "HHDFS76240000"
DEFAULT_US_EXCHANGES = ("NAS", "NYS", "AMS")

MASTER_FILE_URLS = {
    "KOSPI": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "KOSDAQ": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
    "KONEX": "https://new.real.download.dws.co.kr/common/master/konex_code.mst.zip",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def build_retry_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text == "":
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return None


def decode_bytes(raw: bytes) -> str:
    for encoding in ("cp949", "euc-kr", "utf-8", "utf-8-sig", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("decode", raw, 0, 1, "Unable to decode master file")


class KISClient:
    def __init__(self, app_key: str, app_secret: str, base_url: str) -> None:
        if not app_key or not app_secret:
            raise ValueError("KIS_APP_KEY and KIS_APP_SECRET must be set in kis_config.py")

        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.session = build_retry_session()
        self.access_token: Optional[str] = None

    def authenticate(self) -> None:
        url = f"{self.base_url}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        headers = {"content-type": "application/json"}

        response = self.session.post(
            url,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()

        data = response.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"Authentication failed: {data}")

        self.access_token = token
        logger.info("KIS authentication completed")

    def _headers(self, tr_id: str) -> Dict[str, str]:
        if not self.access_token:
            raise RuntimeError("Authenticate first")

        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def get(self, path: str, tr_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        response = self.session.get(
            url,
            headers=self._headers(tr_id),
            params=params,
            timeout=REQUEST_TIMEOUT_SEC,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"KIS GET failed | status={response.status_code} | body={response.text}"
            )

        data = response.json()

        if data.get("rt_cd") != "0":
            raise RuntimeError(
                f"KIS API error | msg_cd={data.get('msg_cd')} | msg1={data.get('msg1')}"
            )

        return data

    def inquire_daily_itemchartprice(
        self,
        symbol: str,
        start_yyyymmdd: str,
        end_yyyymmdd: str,
        market_div_code: str = "J",
        period_div_code: str = "D",
        org_adj_prc: str = FID_ORG_ADJ_PRC,
    ) -> Dict[str, Any]:
        return self.get(
            path="/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr_id="FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": market_div_code,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start_yyyymmdd,
                "FID_INPUT_DATE_2": end_yyyymmdd,
                "FID_PERIOD_DIV_CODE": period_div_code,
                "FID_ORG_ADJ_PRC": org_adj_prc,
            },
        )

    def inquire_overseas_daily_price(
        self,
        symbol: str,
        exchange: str,
        base_yyyymmdd: str,
        period_code: str = "0",
        adjusted_price: str = "1",
    ) -> Dict[str, Any]:
        """Fetch overseas daily price rows.

        KIS returns up to 100 rows ending at BYMD. Common U.S. exchange codes
        are NAS, NYS, and AMS.
        """
        return self.get(
            path=OVERSEAS_DAILY_PRICE_ENDPOINT,
            tr_id=OVERSEAS_DAILY_PRICE_TR_ID,
            params={
                "AUTH": "",
                "EXCD": exchange,
                "SYMB": symbol,
                "GUBN": period_code,
                "BYMD": base_yyyymmdd,
                "MODP": adjusted_price,
            },
        )


def download_master_text(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=MASTER_FILE_TIMEOUT_SEC)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError(f"Empty zip file: {url}")
        target_name = next((name for name in names if name.endswith(".mst")), names[0])
        raw = zf.read(target_name)

    return decode_bytes(raw)


def parse_kospi_master(text: str) -> pd.DataFrame:
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        head = line[: len(line) - 228]
        rows.append(
            {
                "symbol": head[0:9].strip(),
                "std_code": head[9:21].strip(),
                "name": head[21:].strip(),
                "market": "KOSPI",
            }
        )
    return pd.DataFrame(rows)


def parse_kosdaq_master(text: str) -> pd.DataFrame:
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        head = line[: len(line) - 222]
        rows.append(
            {
                "symbol": head[0:9].strip(),
                "std_code": head[9:21].strip(),
                "name": head[21:].strip(),
                "market": "KOSDAQ",
            }
        )
    return pd.DataFrame(rows)


def parse_konex_master(text: str) -> pd.DataFrame:
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        if not line:
            continue
        rows.append(
            {
                "symbol": line[0:9].strip(),
                "std_code": line[9:21].strip(),
                "name": line[21:-184].strip(),
                "market": "KONEX",
            }
        )
    return pd.DataFrame(rows)


def load_all_domestic_symbols(session: requests.Session) -> pd.DataFrame:
    logger.info("Downloading KIS domestic master files")

    kospi_text = download_master_text(session, MASTER_FILE_URLS["KOSPI"])
    kosdaq_text = download_master_text(session, MASTER_FILE_URLS["KOSDAQ"])
    konex_text = download_master_text(session, MASTER_FILE_URLS["KONEX"])

    df = pd.concat(
        [
            parse_kospi_master(kospi_text),
            parse_kosdaq_master(kosdaq_text),
            parse_konex_master(konex_text),
        ],
        ignore_index=True,
    )

    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["name"] = df["name"].astype(str).str.strip()
    df["market"] = df["market"].astype(str).str.strip()

    df = df[df["symbol"].str.fullmatch(r"\d{6}", na=False)].copy()
    df = df.drop_duplicates(subset=["symbol"]).sort_values(["market", "symbol"]).reset_index(drop=True)

    logger.info("Loaded %s listed domestic symbols", len(df))
    return df


def get_last_market_open_date(client: KISClient, reference_symbol: str = "005930") -> date:
    yesterday = date.today() - timedelta(days=1)
    start_date = yesterday - timedelta(days=30)

    data = client.inquire_daily_itemchartprice(
        symbol=reference_symbol,
        start_yyyymmdd=start_date.strftime("%Y%m%d"),
        end_yyyymmdd=yesterday.strftime("%Y%m%d"),
    )

    rows = data.get("output2", [])
    if not rows:
        raise RuntimeError("Could not determine last market date")

    candidate_dates: List[date] = []
    for row in rows:
        ds = str(row.get("stck_bsop_date", "")).strip()
        if not ds:
            continue
        try:
            d = pd.to_datetime(ds, format="%Y%m%d").date()
            if d <= yesterday:
                candidate_dates.append(d)
        except Exception:
            continue

    if not candidate_dates:
        raise RuntimeError("No valid market date found")

    target_date = max(candidate_dates)
    logger.info("Last market open date: %s", target_date.isoformat())
    return target_date


def extract_daily_row(symbol: str, name: str, market: str, row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "market": market,
        "date": row.get("stck_bsop_date"),
        "open": safe_int(row.get("stck_oprc")),
        "high": safe_int(row.get("stck_hgpr")),
        "low": safe_int(row.get("stck_lwpr")),
        "close": safe_int(row.get("stck_clpr")),
        "volume": safe_int(row.get("acml_vol")),
    }


def fetch_one_symbol_daily_bar(
    client: KISClient,
    symbol: str,
    name: str,
    market: str,
    target_yyyymmdd: str,
) -> Optional[Dict[str, Any]]:
    data = client.inquire_daily_itemchartprice(
        symbol=symbol,
        start_yyyymmdd=target_yyyymmdd,
        end_yyyymmdd=target_yyyymmdd,
    )

    rows = data.get("output2", [])
    for row in rows:
        if str(row.get("stck_bsop_date", "")).strip() == target_yyyymmdd:
            return extract_daily_row(symbol, name, market, row)

    return None


def load_watchlist_symbols(path: Path = Path("data/watchlist.json")) -> List[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items", [])
    if not isinstance(items, list):
        return []
    symbols = []
    for item in items:
        if isinstance(item, dict) and item.get("symbol"):
            symbols.append(str(item["symbol"]).strip().upper())
    return list(dict.fromkeys(symbol for symbol in symbols if symbol))


def extract_overseas_daily_row(
    symbol: str,
    exchange: str,
    row: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "market": exchange,
        "date": row.get("xymd"),
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": row.get("clos"),
        "volume": row.get("tvol"),
    }


def fetch_one_overseas_daily_bar(
    client: KISClient,
    symbol: str,
    target_yyyymmdd: str,
    exchanges: Iterable[str] = DEFAULT_US_EXCHANGES,
) -> Optional[Dict[str, Any]]:
    for exchange in exchanges:
        try:
            data = client.inquire_overseas_daily_price(
                symbol=symbol,
                exchange=exchange,
                base_yyyymmdd=target_yyyymmdd,
            )
            rows = data.get("output2") or []
            if rows:
                latest_row = rows[0]
                return extract_overseas_daily_row(symbol, exchange, latest_row)
        except Exception as exc:
            logger.debug("Overseas daily fetch failed | symbol=%s exchange=%s error=%s", symbol, exchange, exc)
        time.sleep(REQUEST_SLEEP_SEC)
    return None


def fetch_watchlist_overseas_daily_bars(
    symbols: Iterable[str],
    target_yyyymmdd: str,
    client: Optional[KISClient] = None,
    exchanges: Iterable[str] = DEFAULT_US_EXCHANGES,
) -> List[Dict[str, Any]]:
    active_client = client or KISClient(
        app_key=KIS_APP_KEY,
        app_secret=KIS_APP_SECRET,
        base_url=KIS_BASE_URL,
    )
    if active_client.access_token is None:
        active_client.authenticate()

    records: List[Dict[str, Any]] = []
    for symbol in symbols:
        item = fetch_one_overseas_daily_bar(
            client=active_client,
            symbol=str(symbol).strip().upper(),
            target_yyyymmdd=target_yyyymmdd,
            exchanges=exchanges,
        )
        if item is not None:
            records.append(item)
        time.sleep(REQUEST_SLEEP_SEC)
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch KIS daily OHLCV data.")
    parser.add_argument(
        "--watchlist-overseas",
        action="store_true",
        help="Fetch latest overseas daily bars for symbols in data/watchlist.json.",
    )
    parser.add_argument(
        "--date",
        help="Base date as YYYYMMDD. Defaults to today.",
    )
    parser.add_argument(
        "--output",
        help="CSV output path. Defaults to kis_watchlist_overseas_daily_<date>.csv for watchlist mode.",
    )
    return parser.parse_args()


def run_watchlist_overseas_fetch(target_yyyymmdd: str, output_path: Optional[str] = None) -> pd.DataFrame:
    symbols = load_watchlist_symbols()
    logger.info("Fetching overseas daily OHLCV for %s watchlist symbols on %s", len(symbols), target_yyyymmdd)
    records = fetch_watchlist_overseas_daily_bars(symbols=symbols, target_yyyymmdd=target_yyyymmdd)
    result_df = pd.DataFrame(records)
    if not result_df.empty:
        result_df = result_df.sort_values(["market", "symbol"]).reset_index(drop=True)

    path = output_path or f"kis_watchlist_overseas_daily_{target_yyyymmdd}.csv"
    result_df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("Rows saved: %s", len(result_df))
    logger.info("Output file: %s", path)
    return result_df


def main() -> None:
    args = parse_args()
    if args.watchlist_overseas:
        target_yyyymmdd = args.date or date.today().strftime("%Y%m%d")
        run_watchlist_overseas_fetch(target_yyyymmdd=target_yyyymmdd, output_path=args.output)
        return

    client = KISClient(
        app_key=KIS_APP_KEY,
        app_secret=KIS_APP_SECRET,
        base_url=KIS_BASE_URL,
    )
    client.authenticate()

    symbols_df = load_all_domestic_symbols(client.session)
    target_date = get_last_market_open_date(client)
    target_yyyymmdd = target_date.strftime("%Y%m%d")

    records: List[Dict[str, Any]] = []
    total = len(symbols_df)

    logger.info("Fetching daily OHLCV for %s symbols on %s", total, target_yyyymmdd)

    for idx, row in enumerate(symbols_df.itertuples(index=False), start=1):
        try:
            item = fetch_one_symbol_daily_bar(
                client=client,
                symbol=row.symbol,
                name=row.name,
                market=row.market,
                target_yyyymmdd=target_yyyymmdd,
            )
            if item is not None:
                records.append(item)

            if idx % 100 == 0 or idx == total:
                logger.info("Progress: %s / %s", idx, total)

            time.sleep(REQUEST_SLEEP_SEC)

        except Exception as exc:
            logger.warning("Failed symbol=%s | error=%s", row.symbol, exc)

    result_df = pd.DataFrame(records)
    result_df = result_df.sort_values(["market", "symbol"]).reset_index(drop=True)

    output_path = f"kis_daily_all_{target_yyyymmdd}.csv"
    result_df.to_csv(output_path, index=False, encoding="utf-8-sig")

    logger.info("Rows saved: %s", len(result_df))
    logger.info("Output file: %s", output_path)


if __name__ == "__main__":
    main()
