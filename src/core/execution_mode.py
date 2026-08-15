"""Which implementation :class:`~src.services.execution_command_gateway.ExecutionCommandGateway`
actually dispatches destructive broker calls to.

``docs/kanban_production_readiness.md``, Workstream 3 (PR2). The gateway is
the *single* place a destructive broker mutation (submit/cancel/replace) can
happen from (B1) -- but the frozen contract's own rule 4
(``BUYBOARD_ENGINE_ENABLED`` stays ``false`` in production for the entire
duration of this program) means the gateway must still exist, be the only
door, and yet not change what actually happens at the broker while the flag
is off. ``ExecutionMode`` is that split, made explicit rather than left as
an unstated "``false`` means... what, exactly?":

- ``LEGACY_COMPATIBILITY`` (the flag is off, i.e. always, for now): the
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
  ``BUYBOARD_ENGINE_ENABLED=true``, which stays false throughout this
  program.

Landing PR2's code on ``master`` is explicitly not equivalent to activating
it (revision 3.3): every call into the gateway resolves to
``LEGACY_COMPATIBILITY`` today, regardless of how much of ``GUARDED_ENGINE``
exists and passes its own tests.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from src.core import execution_config


class ExecutionMode(str, Enum):
    LEGACY_COMPATIBILITY = "LEGACY_COMPATIBILITY"
    GUARDED_ENGINE = "GUARDED_ENGINE"


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
