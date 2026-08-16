"""KIS WebSocket approval-key issuance and bounded refresh.

The approval key is intentionally kept in memory only.  The REST access-
token cache is not reused: KIS exposes a separate ``/oauth2/Approval``
credential for WebSocket subscriptions.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class KisWsAuthError(RuntimeError):
    """Approval-key issuance failed after the bounded retry budget."""


class KisWsProtocolNotVerifiedError(KisWsAuthError):
    """Live WebSocket use was requested before Workstream 0 sign-off."""


@dataclass(frozen=True)
class KisWsApprovalKey:
    value: str
    issued_at: datetime
    expires_at: datetime

    def is_usable(self, *, now: datetime, refresh_margin_seconds: float) -> bool:
        return now + timedelta(seconds=max(0.0, refresh_margin_seconds)) < self.expires_at


class KisWsApprovalKeyProvider:
    """Thread-safe, injectable approval-key provider.

    KIS's official sample refreshes the key daily.  The actual lifetime is
    configurable and must be confirmed by the capability matrix before live
    activation; the provider never treats a failed response as a usable key.
    """

    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        ttl_seconds: float,
        max_retries: int = 3,
        request_timeout_seconds: float = 10.0,
        refresh_margin_seconds: float = 60.0,
        protocol_verified: bool = False,
        session: Optional[requests.Session] = None,
        clock: Callable[[], datetime] = _utc_now,
        sleeper: Callable[[float], None] = time.sleep,
        critical_alert: Callable[[str], None] = lambda message: None,
    ) -> None:
        self._base_url = str(base_url or "").rstrip("/")
        self._app_key = str(app_key or "")
        self._app_secret = str(app_secret or "")
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_retries = max(1, int(max_retries))
        self._request_timeout_seconds = max(0.1, float(request_timeout_seconds))
        self._refresh_margin_seconds = max(0.0, float(refresh_margin_seconds))
        self._protocol_verified = bool(protocol_verified)
        self._session = session or requests.Session()
        self._clock = clock
        self._sleeper = sleeper
        self._critical_alert = critical_alert
        self._cached: Optional[KisWsApprovalKey] = None
        self._lock = threading.Lock()

    def approval_key_age_seconds(self, *, now: Optional[datetime] = None) -> Optional[float]:
        with self._lock:
            if self._cached is None:
                return None
            return max(0.0, ((now or self._clock()) - self._cached.issued_at).total_seconds())

    def get(self, *, force_refresh: bool = False) -> KisWsApprovalKey:
        if not self._protocol_verified:
            raise KisWsProtocolNotVerifiedError(
                "KIS WebSocket protocol use is blocked until docs/kis_capability_matrix.md is verified"
            )
        if not self._base_url or not self._app_key or not self._app_secret:
            raise KisWsAuthError("KIS WebSocket approval-key credentials are not configured")

        with self._lock:
            now = self._clock()
            if (
                not force_refresh
                and self._cached is not None
                and self._cached.is_usable(
                    now=now, refresh_margin_seconds=self._refresh_margin_seconds
                )
            ):
                return self._cached

            last_error = "unknown approval-key failure"
            for attempt in range(1, self._max_retries + 1):
                try:
                    response = self._session.post(
                        f"{self._base_url}/oauth2/Approval",
                        headers={"content-type": "application/json"},
                        json={
                            "grant_type": "client_credentials",
                            "appkey": self._app_key,
                            "secretkey": self._app_secret,
                        },
                        timeout=self._request_timeout_seconds,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    value = str(payload.get("approval_key") or "").strip()
                    if not value:
                        raise KisWsAuthError("approval-key response omitted approval_key")
                    issued_at = self._clock()
                    self._cached = KisWsApprovalKey(
                        value=value,
                        issued_at=issued_at,
                        expires_at=issued_at + timedelta(seconds=self._ttl_seconds),
                    )
                    return self._cached
                except Exception as exc:  # requests and malformed vendor replies
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "KIS WebSocket approval-key attempt %s/%s failed: %s",
                        attempt,
                        self._max_retries,
                        last_error,
                    )
                    if attempt < self._max_retries:
                        self._sleeper(min(2 ** (attempt - 1), 8))

            message = (
                "KIS WebSocket approval-key issuance exhausted its bounded "
                f"retry budget ({self._max_retries} attempts): {last_error}"
            )
            self._critical_alert(message)
            raise KisWsAuthError(message)

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
