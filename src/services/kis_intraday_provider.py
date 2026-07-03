"""KIS-backed intraday provider wrapper."""
from __future__ import annotations

from src.api.kis_account_snapshot_dual import KisAccountClient, KisEnvironment, load_config
from src.api.kis_intraday import (
    KisIntradayClient,
    KisIntradayError,
    KisIntradayNotConfiguredError,
    is_kis_intraday_enabled,
)
from src.services.intraday_provider import (
    IntradayInterval,
    IntradayProviderError,
    IntradayProviderName,
    IntradayRequest,
    IntradayResult,
    resample_ohlcv_bars,
)


class KisIntradayProviderError(IntradayProviderError):
    """Controlled KIS provider failure."""


def fetch_kis_intraday(request: IntradayRequest) -> IntradayResult:
    if not is_kis_intraday_enabled():
        raise KisIntradayProviderError(
            "KIS intraday is disabled. Set KIS_INTRADAY_ENABLED=true only after endpoint/TR_ID/fields are verified."
        )
    try:
        environment = KisEnvironment(request.environment.upper())
        config = load_config(environment, account_no_override=request.account_no or None)
        client = KisAccountClient(config)
        client.authenticate()
        kis_client = KisIntradayClient(client)
        # The rest of the app uses NASD/NYSE/AMEX codes; KIS uses NAS/NYS/AMS.
        # Always try all standard US exchanges so a mismatched code doesn't silently fail.
        raw_result = kis_client.fetch_overseas_1m(
            request.symbol,
        )
    except (KisIntradayNotConfiguredError, KisIntradayError, Exception) as exc:
        raise KisIntradayProviderError(str(exc)) from exc

    bars = raw_result.bars
    if request.interval == IntradayInterval.FIVE_MINUTE.value:
        bars = resample_ohlcv_bars(raw_result.bars, IntradayInterval.FIVE_MINUTE)
    elif request.interval != IntradayInterval.ONE_MINUTE.value:
        raise KisIntradayProviderError(f"Unsupported KIS intraday interval: {request.interval}")

    warnings = []
    if bars.empty:
        warnings.append(f"No {request.interval} KIS intraday rows returned for {request.symbol}.")
    return IntradayResult(
        symbol=request.symbol,
        interval=request.interval,
        source=IntradayProviderName.KIS,
        bars=bars,
        exchange=raw_result.exchange or request.exchange,
        warnings=warnings,
    )
