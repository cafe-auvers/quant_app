"""``MutationBudgetProtocol`` -- B2's rate-limit/mutation-budget gate,
defined now so the gateway's B2 sequence has a real seam for it, even
though the actual rate-limited implementation is Workstream 10's job
(PR6).

``docs/kanban_production_readiness.md``, PR2 second-pass review (finding
9): "For the future Workstream 10 dependency, PR2 can define a protocol
now... Guarded mode should require an injected implementation. A
permissive production default would silently weaken B2." Accordingly,
:class:`~src.services.execution_command_gateway.ExecutionCommandGateway`
requires a ``mutation_budget`` to be explicitly supplied for
``GUARDED_ENGINE`` mode (fails closed if omitted) -- there is no silent
default anywhere in this module. :class:`AllowAllMutationBudget` exists
for tests and for the guarded composition root to use *visibly*, with an
explicit comment that it is a placeholder, not a real rate limiter.
"""
from __future__ import annotations

from enum import Enum
from typing import Protocol


class CommandType(str, Enum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


class MutationBudgetExceededError(RuntimeError):
    """Raised by a real :class:`MutationBudgetProtocol` implementation
    (Workstream 10) when the account/endpoint has no remaining mutation
    budget for this command type."""


class MutationBudgetProtocol(Protocol):
    def require_available(self, command_type: CommandType) -> None:
        """Raise :class:`MutationBudgetExceededError` (or a subclass) if
        this command type has no budget remaining. Must not raise for any
        other reason -- a budget check is not the place to validate
        anything else about the command."""
        ...


class AllowAllMutationBudget:
    """Permissive placeholder standing in for Workstream 10's real,
    rate-limit-aware implementation. Never used as a silent default
    anywhere in this program -- every caller that wants it must construct
    and inject it explicitly, so its presence in a composition root is
    always a visible, greppable acknowledgement that the real budget gate
    isn't wired in yet."""

    def require_available(self, command_type: CommandType) -> None:
        return None
