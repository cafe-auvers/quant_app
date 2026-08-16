"""Execution-lease verification for the ``GUARDED_ENGINE`` execution path
(Workstream 3, A6/B2).

:class:`~src.services.execution_authority.ExecutionAuthority` re-verifies a
device's main-device lease at the real broker boundary. This protocol adapts
the core ``ExecutionLease`` value to that authority and exposes whether epoch
verification is genuinely available to the guarded gateway.

Workstream 6 persists that epoch beside the main-device token, so the real
protocol now verifies all three dimensions at each guarded boundary.

This lease check only ever runs in ``GUARDED_ENGINE`` mode. Legacy
(``LEGACY_COMPATIBILITY``) submissions already re-verify their own lease via
``ExecutionAuthority``/``LeaseHandle`` *before* reaching the gateway (see
``order_execution_service.submit_guarded_overseas_order``'s existing
``require_current_lease`` calls) -- adding a second, stricter check inside
the gateway for that mode would risk rejecting a submission legacy's own,
already-reviewed gate would have allowed, which is exactly the behavioral
change PR2 must not make.
"""
from __future__ import annotations

from typing import Optional, Protocol

from sqlalchemy.engine import Engine

from src.core.execution_mode import ExecutionLease
from src.services.execution_authority import ExecutionAuthority, LeaseExpiredError, LeaseHandle

__all__ = [
    "ExecutionLease",
    "ExecutionLeaseProtocol",
    "DefaultExecutionLeaseProtocol",
    "FakeExecutionLeaseProtocol",
    "LeaseNotCurrentError",
]


class LeaseNotCurrentError(RuntimeError):
    """Raised by :class:`ExecutionLeaseProtocol` implementations -- the
    caller's cached lease (device/token/epoch) no longer matches what is
    currently authoritative. Never silently ignored; a stale lease must
    block every destructive call in ``GUARDED_ENGINE`` mode (A6/B2)."""


class ExecutionLeaseProtocol(Protocol):
    def require_current(self, lease: Optional[ExecutionLease]) -> None:
        """Raise :class:`LeaseNotCurrentError` unless ``lease`` is still
        current. ``lease is None`` means "no fencing requested" (mirrors
        ``ExecutionAuthority.require_current_lease``'s own convention) --
        callers that need fencing must explicitly supply a lease."""
        ...


class DefaultExecutionLeaseProtocol:
    """Verify device, token, and epoch against durable main-device state."""

    epoch_verified: bool = True

    def __init__(self, *, authority: Optional[ExecutionAuthority] = None, engine: Optional[Engine] = None) -> None:
        self._authority = authority or ExecutionAuthority()
        self._engine = engine

    def require_current(self, lease: Optional[ExecutionLease]) -> None:
        if lease is None:
            return
        if int(lease.lease_epoch or 0) <= 0:
            raise LeaseNotCurrentError(
                "execution lease has no positive authoritative epoch"
            )
        handle = LeaseHandle(
            device_id=lease.device_id,
            lease_token=lease.lease_token,
            lease_epoch=lease.lease_epoch,
        )
        try:
            self._authority.require_current_lease(self._engine, handle)
        except LeaseExpiredError as exc:
            raise LeaseNotCurrentError(str(exc)) from exc


class FakeExecutionLeaseProtocol:
    """In-memory lease authority for tests -- lets a test directly control
    "what is the current lease" (including ``lease_epoch``) without a real ``state_sync``
    database. This is the double PR2's own ``GUARDED_ENGINE`` tests use to
    exercise the strict epoch gate the doc calls for.

    ``epoch_verified`` defaults to ``True``. Set it to ``False`` on an instance to exercise the
    gateway's "epoch cannot be verified -- reject" path instead.
    """

    def __init__(self, *, current: Optional[ExecutionLease] = None, epoch_verified: bool = True) -> None:
        self.current = current
        self.epoch_verified = epoch_verified

    def grant(self, lease: ExecutionLease) -> None:
        self.current = lease

    def revoke(self) -> None:
        self.current = None

    def require_current(self, lease: Optional[ExecutionLease]) -> None:
        if lease is None:
            return
        if self.current is None:
            raise LeaseNotCurrentError("no active execution lease is currently held")
        if (
            self.current.device_id != lease.device_id
            or not self.current.lease_token
            or self.current.lease_token != lease.lease_token
            or self.current.lease_epoch != lease.lease_epoch
        ):
            raise LeaseNotCurrentError(
                f"execution lease no longer current for device {lease.device_id!r} "
                f"(epoch {lease.lease_epoch})"
            )
