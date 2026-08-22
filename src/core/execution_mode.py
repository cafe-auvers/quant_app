"""Which implementation :class:`~src.services.execution_command_gateway.ExecutionCommandGateway`
actually dispatches destructive broker calls to.

``docs/kanban_production_readiness.md``, Workstream 3 (PR2). The gateway is
the *single* place a destructive broker mutation (submit/cancel/replace) can
happen from (B1). ``ExecutionMode`` keeps the recovery-only legacy path
explicit and separate from the normal guarded runtime:

- ``LEGACY_COMPATIBILITY`` (the flag is explicitly off): the
  gateway is a transparent pass-through to the real broker -- no new command
  journal, no new capital reservation, no new ``ExecutionOrderRecord``. The
  already-existing, already-reviewed legacy guard sequence
  (:func:`src.services.order_execution_service.submit_guarded_overseas_order`,
  :func:`src.services.order_reconciliation.cancel_and_reconcile_order`) is
  completely unchanged; only *which object* it calls to reach the broker
  changes, from a raw :class:`~src.services.broker.KisBroker` to this
  gateway. This is deliberately not called a "bypass" or a "legacy path
  around the gateway" -- it is still the gateway, in a mode that does
  nothing extra yet.
- ``GUARDED_ENGINE``: the full A1-A11/B1-B4 sequence this workstream
  specifies -- atomic command+reservation+``PREPARED`` record, durably
  committed ``SUBMITTING`` before any broker call, exact-identity
  requirements for cancel/replace. Implemented and tested in PR2, but never
  selected in production by this PR (see the module's own activation
  criterion in the frozen contract) -- reaching this mode requires
  ``BUYBOARD_ENGINE_ENABLED=true``. The engine may remain available while
  ``KIS_LIVE_EXECUTION_MODE=DISABLED`` independently blocks every real broker
  mutation.

Selecting guarded mode is explicitly not equivalent to arming trading. The
shared trading switch and all live-envelope/runtime fences are rechecked at
the broker boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.core import execution_config


class ExecutionMode(str, Enum):
    LEGACY_COMPATIBILITY = "LEGACY_COMPATIBILITY"
    GUARDED_ENGINE = "GUARDED_ENGINE"


class ExecutionPersistenceMode(str, Enum):
    """Where B4a's mandatory pre-broker command record is committed."""

    CANONICAL_DATABASE = "CANONICAL_DATABASE"
    LOCAL_EMERGENCY_JOURNAL = "LOCAL_EMERGENCY_JOURNAL"


def resolve_execution_mode(override: Optional[bool] = None) -> ExecutionMode:
    """``override`` exists only for tests that need to force
    ``GUARDED_ENGINE`` without mutating process-wide environment state --
    production callers must never pass it, so the flag stays the single
    source of truth outside tests."""
    enabled = execution_config.is_buyboard_engine_enabled() if override is None else bool(override)
    return ExecutionMode.GUARDED_ENGINE if enabled else ExecutionMode.LEGACY_COMPATIBILITY


class ExecutionSource(str, Enum):
    """Which frontend/caller issued a destructive command (Workstream 9's
    "frontend source attribution"). Recorded on every
    :class:`~src.services.execution_command_repository.ExecutionCommand`
    this gateway journals, and used by the mutual-exclusion check in
    :mod:`src.services.execution_command_gateway` so two different sources
    can never race a destructive call for the same account+symbol.
    """

    LEGACY_BUY_DASHBOARD = "LEGACY_BUY_DASHBOARD"
    KANBAN_BOARD = "KANBAN_BOARD"
    SYSTEM = "SYSTEM"  # unspecified/legacy caller that hasn't threaded a source through yet


@dataclass(frozen=True)
class ExecutionLease:
    """A device's belief about which execution lease it currently holds,
    including the ``lease_epoch`` dimension PR1's schemas added. A pure
    value type -- lives in ``src.core`` (not ``src.services``, which does
    the actual I/O-backed verification) so both core request models
    (:mod:`src.core.execution_request`) and the services-layer protocol
    that verifies it (:mod:`src.services.execution_lease_protocol`) can
    depend on it without ``src.core`` depending on ``src.services``,
    which nothing else in this codebase does either.
    """

    device_id: str
    lease_token: str
    lease_epoch: int = 0
