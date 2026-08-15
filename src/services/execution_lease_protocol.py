"""Execution-lease verification for the ``GUARDED_ENGINE`` execution path
(Workstream 3, A6/B2).

:class:`~src.services.execution_authority.ExecutionAuthority` already
re-verifies a device's main-device lease at the real broker boundary --
this module does not replace it, it extends it with the ``lease_epoch``
dimension PR1's schemas added (``ExecutionCommand.lease_epoch``,
``ExecutionOrderRecord.lease_epoch``) that ``ExecutionAuthority``'s
existing :class:`~src.services.execution_authority.LeaseHandle`
(``device_id``/``lease_token`` only) doesn't carry.

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

from dataclasses import dataclass
from typing import Optional, Protocol

from sqlalchemy.engine import Engine

from src.services.execution_authority import ExecutionAuthority, LeaseExpiredError, LeaseHandle


class LeaseNotCurrentError(RuntimeError):
    """Raised by :class:`ExecutionLeaseProtocol` implementations -- the
    caller's cached lease (device/token/epoch) no longer matches what is
    currently authoritative. Never silently ignored; a stale lease must
    block every destructive call in ``GUARDED_ENGINE`` mode (A6/B2)."""


@dataclass(frozen=True)
class ExecutionLease:
    """A device's belief about which execution lease it currently holds,
    including the epoch dimension ``LeaseHandle`` doesn't have."""

    device_id: str
    lease_token: str
    lease_epoch: int = 0


class ExecutionLeaseProtocol(Protocol):
    def require_current(self, lease: Optional[ExecutionLease]) -> None:
        """Raise :class:`LeaseNotCurrentError` unless ``lease`` is still
        current. ``lease is None`` means "no fencing requested" (mirrors
        ``ExecutionAuthority.require_current_lease``'s own convention) --
        callers that need fencing must explicitly supply a lease."""
        ...


class DefaultExecutionLeaseProtocol:
    """Delegates device/token verification to the existing, already-real
    ``ExecutionAuthority``. ``lease_epoch`` is accepted but not yet verified
    against anything live -- ``state_sync`` does not persist an epoch value
    today (Workstream 6 / PR5's device-handoff work owns adding that), so
    there is nothing authoritative to check it against yet. This is a known,
    explicitly-logged gap, not a silent assumption that epoch checking is
    real: see the class's own ``epoch_verified`` flag, which every
    ``GUARDED_ENGINE`` test can assert on.
    """

    epoch_verified: bool = False

    def __init__(self, *, authority: Optional[ExecutionAuthority] = None, engine: Optional[Engine] = None) -> None:
        self._authority = authority or ExecutionAuthority()
        self._engine = engine

    def require_current(self, lease: Optional[ExecutionLease]) -> None:
        if lease is None:
            return
        handle = LeaseHandle(device_id=lease.device_id, lease_token=lease.lease_token)
        try:
            self._authority.require_current_lease(self._engine, handle)
        except LeaseExpiredError as exc:
            raise LeaseNotCurrentError(str(exc)) from exc


class FakeExecutionLeaseProtocol:
    """In-memory lease authority for tests -- lets a test directly control
    "what is the current lease" (including ``lease_epoch``, unlike
    :class:`DefaultExecutionLeaseProtocol`) without a real ``state_sync``
    database. This is the double PR2's own ``GUARDED_ENGINE`` tests use to
    exercise the strict epoch gate the doc calls for.
    """

    def __init__(self, *, current: Optional[ExecutionLease] = None) -> None:
        self.current = current

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
