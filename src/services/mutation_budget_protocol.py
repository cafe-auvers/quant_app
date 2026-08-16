"""Compatibility protocol for B2's mutation-budget gate.

``docs/kanban_production_readiness.md``, PR2 second-pass review (finding
9): "For the future Workstream 10 dependency, PR2 can define a protocol
now... Guarded mode should require an injected implementation. A
permissive production default would silently weaken B2." Accordingly,
:class:`~src.services.execution_command_gateway.ExecutionCommandGateway`
requires a ``mutation_budget`` to be explicitly supplied for
``GUARDED_ENGINE`` mode (fails closed if omitted) -- there is no silent
default anywhere in this module. :class:`AllowAllMutationBudget` remains a
test-only double. Production guarded composition uses
``src.services.kis_request_scheduler.KisRequestScheduler``.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Protocol, TypeVar

T = TypeVar("T")


class CommandType(str, Enum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


class MutationBudgetExceededError(RuntimeError):
    """Raised by a real :class:`MutationBudgetProtocol` implementation
    (Workstream 10) when the account/endpoint has no remaining mutation
    budget for this command type."""


class MutationBudgetProtocol(Protocol):
    def require_available(self, command_type: CommandType, **context: Any) -> None:
        """Raise :class:`MutationBudgetExceededError` (or a subclass) if
        this command type has no budget remaining. Must not raise for any
        other reason -- a budget check is not the place to validate
        anything else about the command."""
        ...


class AllowAllMutationBudget:
    """Test-only permissive scheduler double; never a production default."""

    context_aware = True

    def require_available(self, command_type: CommandType, **context: Any) -> None:
        return None

    def execute_mutation(
        self,
        operation: Callable[[], T],
        **context: Any,
    ) -> T:
        return operation()
