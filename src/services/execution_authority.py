"""Fencing at the actual broker boundary: is our main-device lease still current?

``_state_sync_allows_order_submission`` (``src/ui/main_window.py``) is a
UI-thread, last-known-value check -- good for UX (blocks a button early,
shows a clear dialog) but not a safety boundary by itself, since nothing
re-verifies it at the moment of the actual non-idempotent broker call. A
device could pass that check and then, seconds later while a background
worker is mid-submission, lose main-device status to another device that
auto-claimed via ``state_sync.claim_main_device_if_stale`` -- without a
re-check at the broker boundary, both devices could end up believing they're
authorized to trade the same account at once.

``ExecutionAuthority`` closes that gap: it re-reads the live ownership row
and raises unless both the device_id *and* the lease_token the caller
believes it holds still match. Wired into
``order_execution_service.submit_guarded_overseas_order`` at the same two
re-check boundaries already used for the trading kill switch and risk
approval.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.engine import Engine

from src.services.state_sync import MAIN_DEVICE_KEY, PULL_OK, pull_state

logger = logging.getLogger(__name__)


class LeaseExpiredError(RuntimeError):
    """Raised when a caller's cached main-device lease no longer matches live ownership."""


@dataclass(frozen=True)
class LeaseHandle:
    """A device's belief about which main-device lease it currently holds.

    Minted by ``state_sync.claim_main_device`` / ``claim_main_device_if_stale``
    (as the ``lease_token`` field on the ownership row) and cached by
    ``MainWindow`` for as long as it believes it is the main device.
    """

    device_id: str
    lease_token: str


class ExecutionAuthority:
    """Re-verifies a device's main-device lease at the actual broker boundary."""

    def require_current_lease(
        self, engine: Optional[Engine], expected: Optional[LeaseHandle]
    ) -> None:
        """Raise ``LeaseExpiredError`` unless ``expected`` still matches live ownership.

        ``expected is None`` is treated as "no fencing requested" (single-
        device use, standalone callers, existing tests) rather than an
        error -- callers that need fencing must explicitly supply a lease.
        """
        if expected is None:
            return
        if engine is None:
            raise LeaseExpiredError(
                "State sync database is unavailable; cannot verify main-device lease."
            )
        pulled = pull_state(engine, MAIN_DEVICE_KEY)
        if pulled.status != PULL_OK or pulled.state is None:
            raise LeaseExpiredError(
                "No active main-device lease could be read; refusing to submit."
            )
        payload = pulled.state.payload
        current_device_id = str(payload.get("device_id") or "").strip()
        current_lease_token = str(payload.get("lease_token") or "").strip()
        if (
            current_device_id != expected.device_id
            or not current_lease_token
            or current_lease_token != expected.lease_token
        ):
            logger.warning(
                "Main-device lease no longer current for device %s; refusing to submit.",
                expected.device_id,
            )
            raise LeaseExpiredError(
                "Main-device lease changed since this order was queued; another "
                "device may now be authoritative. Refusing to submit."
            )
