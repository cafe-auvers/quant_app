"""Cached, staleness-aware KIS buying-power snapshot.

``buydashboard_to_kanban.md`` code review P0-1: the Kanban entry engine's
``buying_power_provider`` must never be a manual/hardcoded account-size
figure -- it must reflect real, recently-refreshed KIS account state and
fail closed when that state is too old to trust an unattended entry
decision on.

This module does not perform any new synchronous KIS network call --
:mod:`src.services.buyboard_runtime`'s module docstring explicitly asks for
"the same cached/most-recently-refreshed value" the legacy dashboard's
``KisAccountWorker`` already fetches asynchronously
(``src.ui.mixins.dashboard_mixin._on_trade_account_snapshot_finished``). The
UI records a snapshot here every time that worker completes
(:func:`record_snapshot`); the entry engine only ever reads from this
in-memory cache via a provider built by :func:`make_buying_power_provider`/
:func:`make_account_equity_provider`.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Spec section 3 (BuyingPowerSnapshot): "Entry should fail closed when the
# snapshot exceeds a configured age, for example 10-15 seconds during an
# active session."
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 15.0


@dataclass(frozen=True)
class BuyingPowerSnapshot:
    environment: str
    account_no: str
    usable_buying_power_usd: float
    total_equity_usd: float
    received_at: datetime
    source: str


_lock = threading.Lock()
_snapshots: Dict[Tuple[str, str], BuyingPowerSnapshot] = {}


def _key(environment: str, account_no: str) -> Tuple[str, str]:
    return (str(environment or "").upper(), str(account_no or ""))


def record_snapshot(
    *,
    environment: str,
    account_no: str,
    usable_buying_power_usd: float,
    total_equity_usd: float,
    source: str = "kis_account_snapshot",
    received_at: Optional[datetime] = None,
) -> BuyingPowerSnapshot:
    """Records this account's most-recently-fetched buying power/equity.

    Call this every time a fresh KIS account snapshot arrives (currently:
    ``src.ui.mixins.dashboard_mixin._on_trade_account_snapshot_finished``)
    -- never from the 1-second heartbeat itself, which must only ever read
    the cache via the providers below.
    """
    snapshot = BuyingPowerSnapshot(
        environment=str(environment or "").upper(),
        account_no=str(account_no or ""),
        usable_buying_power_usd=max(0.0, float(usable_buying_power_usd or 0.0)),
        total_equity_usd=max(0.0, float(total_equity_usd or 0.0)),
        received_at=received_at or datetime.now(timezone.utc),
        source=str(source or ""),
    )
    with _lock:
        _snapshots[_key(environment, account_no)] = snapshot
    return snapshot


def get_snapshot(environment: str, account_no: str) -> Optional[BuyingPowerSnapshot]:
    with _lock:
        return _snapshots.get(_key(environment, account_no))


def clear() -> None:
    """Test/reset hook -- drops every cached snapshot."""
    with _lock:
        _snapshots.clear()


def _fresh_snapshot(
    environment: str,
    account_no: str,
    *,
    max_age_seconds: float,
    clock: Callable[[], datetime],
) -> Optional[BuyingPowerSnapshot]:
    snapshot = get_snapshot(environment, account_no)
    if snapshot is None:
        return None
    age_seconds = (clock() - snapshot.received_at).total_seconds()
    if age_seconds > max_age_seconds:
        logger.warning(
            "Buying-power snapshot for %s:%s is %.1fs old (> %.0fs max) -- "
            "failing closed rather than sizing/reserving off stale capital.",
            environment, account_no, age_seconds, max_age_seconds,
        )
        return None
    return snapshot


def make_buying_power_provider(
    *,
    max_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Callable[[str, str], float]:
    """Builds an ``EntryAttemptManager``-compatible ``buying_power_provider``.

    Fails closed (returns 0.0 -- no capital available) when no snapshot has
    ever been recorded for this exact ``(environment, account_no)``, or the
    snapshot is older than ``max_age_seconds``. Correctly account-scoped --
    unlike the manual-account-size figure this replaces, two different
    ``account_no`` values for the same environment never share one number.
    """

    def provider(environment: str, account_no: str) -> float:
        snapshot = _fresh_snapshot(
            environment, account_no, max_age_seconds=max_age_seconds, clock=clock
        )
        return snapshot.usable_buying_power_usd if snapshot is not None else 0.0

    return provider


def make_account_equity_provider(
    *,
    max_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Callable[[str, str], float]:
    """Same staleness/fail-closed policy as :func:`make_buying_power_provider`,
    for the risk-sizing (total equity) base instead of the capital-
    availability (usable cash) base -- see
    :func:`src.services.buyboard_runtime.build_buyboard_runtime`'s
    ``account_equity_provider`` docstring for why these two can differ.
    """

    def provider(environment: str, account_no: str) -> float:
        snapshot = _fresh_snapshot(
            environment, account_no, max_age_seconds=max_age_seconds, clock=clock
        )
        return snapshot.total_equity_usd if snapshot is not None else 0.0

    return provider
