"""Fail-closed runtime readiness and device-handoff value types.

The UI and worker both consume these types, but they contain no Qt or I/O.
Keeping the aggregate decision pure makes every Workstream 6 health input
independently testable and prevents a caller from accidentally treating a
partial startup as execution-ready.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Tuple


class RuntimeDeviceState(str, Enum):
    STARTING = "STARTING"
    STANDBY = "STANDBY"
    STANDBY_READY = "STANDBY_READY"
    ACTIVE = "ACTIVE"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class EngineReadiness:
    """The complete E1 authorization predicate.

    ``healthy`` intentionally has no fallback or weighting: every input is
    mandatory.  Protective-action policy is applied by the caller only after
    this aggregate has reported the engine unhealthy.
    """

    lease_current: bool
    startup_reconciliation_complete: bool
    account_reconciliation_fresh: bool
    websocket_connected: bool
    critical_trade_subscriptions_acked: bool
    critical_quote_subscriptions_acked: bool
    critical_quotes_fresh: bool
    accumulator_draining_within_budget: bool
    database_writable: bool
    device_active: bool

    STANDBY_CHECK_FIELDS: ClassVar[Tuple[str, ...]] = (
        "startup_reconciliation_complete",
        "account_reconciliation_fresh",
        "websocket_connected",
        "critical_trade_subscriptions_acked",
        "critical_quote_subscriptions_acked",
        "critical_quotes_fresh",
        "accumulator_draining_within_budget",
        "database_writable",
    )

    @property
    def standby_check_results(self) -> Tuple[Tuple[str, bool], ...]:
        """Return each pre-activation gate as explicit observable state.

        The UI uses this to project startup progress without inventing a
        second readiness policy.  Authorization still comes exclusively from
        :attr:`standby_ready`; this is only a read-only explanation of the
        same predicate.
        """

        return tuple(
            (field_name, bool(getattr(self, field_name)))
            for field_name in self.STANDBY_CHECK_FIELDS
        )

    @property
    def standby_blockers(self) -> Tuple[str, ...]:
        return tuple(
            field_name
            for field_name, passed in self.standby_check_results
            if not passed
        )

    @property
    def standby_checks_completed(self) -> int:
        return sum(passed for _, passed in self.standby_check_results)

    @property
    def healthy(self) -> bool:
        return all(
            (
                self.lease_current,
                self.startup_reconciliation_complete,
                self.account_reconciliation_fresh,
                self.websocket_connected,
                self.critical_trade_subscriptions_acked,
                self.critical_quote_subscriptions_acked,
                self.critical_quotes_fresh,
                self.accumulator_draining_within_budget,
                self.database_writable,
                self.device_active,
            )
        )

    @property
    def standby_ready(self) -> bool:
        """Readiness before activation.

        A candidate must prove every operational dependency.  Lease currency
        and ``ACTIVE`` are deliberately excluded: a successor can become
        STANDBY_READY while another device still owns the execution lease.
        The final reconciliation and lease acquisition/recheck promote it to
        ACTIVE later.
        """

        return all(passed for _, passed in self.standby_check_results)


@dataclass(frozen=True)
class ShutdownExposure:
    open_positions: Tuple[str, ...] = ()
    working_orders: Tuple[str, ...] = ()
    inspection_confirmed: bool = True
    inspection_error: str = ""

    @property
    def labels(self) -> Tuple[str, ...]:
        labels = (*self.open_positions, *self.working_orders)
        if not self.inspection_confirmed:
            labels = (
                *labels,
                "UNKNOWN EXPOSURE"
                + (f" ({self.inspection_error})" if self.inspection_error else ""),
            )
        return tuple(dict.fromkeys(labels))

    @property
    def is_clear(self) -> bool:
        return self.inspection_confirmed and not (
            self.open_positions or self.working_orders
        )


@dataclass(frozen=True)
class ShutdownLeaseDecision:
    allowed: bool
    reason: str


def decide_shutdown_lease_release(
    exposure: ShutdownExposure,
    *,
    successor_standby_ready: bool,
    handoff_confirmed: bool,
    unattended: bool,
    explicit_unprotected_acceptance: bool = False,
) -> ShutdownLeaseDecision:
    """Apply E4 without UI side effects."""

    if not exposure.inspection_confirmed:
        names = ", ".join(exposure.labels)
        if unattended:
            return ShutdownLeaseDecision(
                False,
                f"unattended shutdown refused because exposure is unknown: {names}",
            )
        if explicit_unprotected_acceptance:
            return ShutdownLeaseDecision(
                True,
                f"supervised user explicitly accepted unknown exposure: {names}",
            )
        return ShutdownLeaseDecision(
            False,
            f"explicit supervised acceptance is required because exposure is unknown: {names}",
        )
    if exposure.is_clear:
        return ShutdownLeaseDecision(True, "no open positions or working orders")
    if successor_standby_ready and handoff_confirmed:
        return ShutdownLeaseDecision(True, "ready successor handoff confirmed")
    names = ", ".join(exposure.labels)
    if unattended:
        return ShutdownLeaseDecision(
            False,
            "unattended shutdown refused while exposure remains and no confirmed "
            f"STANDBY_READY successor exists: {names}",
        )
    if explicit_unprotected_acceptance:
        return ShutdownLeaseDecision(
            True,
            f"supervised user explicitly accepted unprotected exposure: {names}",
        )
    return ShutdownLeaseDecision(
        False,
        "explicit supervised acceptance is required before leaving exposure "
        f"unprotected: {names}",
    )
