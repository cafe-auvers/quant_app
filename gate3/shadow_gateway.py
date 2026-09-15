"""Production-workflow-compatible Gate-3 shadow gateway.

The class deliberately subclasses the sanctioned execution gateway so the
normal Buy Board composition and workflow service are exercised unchanged.
Every destructive method terminates in :class:`ShadowExecutionBoundary`
before a command/order/position ledger or broker can be touched.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.engine import Engine

from gate3.shadow_boundary import ShadowExecutionBoundary
from src.core.execution_mode import ExecutionMode
from src.core.execution_order_record import ExecutionOrderRecord
from src.core.execution_request import (
    CancelExecutionRequest,
    ReplaceExecutionRequest,
    SubmitExecutionRequest,
)
from src.services.execution_command_gateway import ExecutionCommandGateway


class ShadowExecutionGateway(ExecutionCommandGateway):
    """A guarded-mode gateway whose only mutation target is a shadow store."""

    qualification_shadow_only = True

    def __init__(
        self,
        *,
        boundary: ShadowExecutionBoundary,
        isolated_engine: Engine,
    ) -> None:
        # Do not call ExecutionCommandGateway.__init__: doing so can construct
        # a real KIS broker and production persistence dependencies.  Every
        # method reachable in qualification mode is implemented below.
        self.boundary = boundary
        self._isolated_engine = isolated_engine
        self._cached_records: dict[str, ExecutionOrderRecord] = {}

    @property
    def mode(self) -> ExecutionMode:
        return ExecutionMode.GUARDED_ENGINE

    @property
    def database_engine(self) -> Engine:
        return self._isolated_engine

    @property
    def canonical_database_writable(self) -> bool:
        # "Canonical" to the production runtime means the runner's physically
        # isolated SQLite state, never the production MySQL schema.
        return True

    def require_guarded_runtime_ready(self) -> None:
        if not self.boundary.store.audit().passed:
            raise RuntimeError("Shadow store failed its integrity audit")

    def submit_guarded(self, request: SubmitExecutionRequest) -> Any:
        return self.boundary.submit_guarded(request)

    def cancel_guarded(self, request: CancelExecutionRequest) -> Any:
        return self.boundary.cancel_guarded(request)

    def replace_guarded(
        self,
        request: ReplaceExecutionRequest,
        *,
        post_cancel_revalidate: Callable[[], None] | None = None,
    ) -> Any:
        if post_cancel_revalidate is not None:
            post_cancel_revalidate()
        return self.boundary.replace_guarded(request)

    def resume_replace_guarded(
        self,
        request: ReplaceExecutionRequest,
        *,
        post_cancel_revalidate: Callable[[], None],
    ) -> Any:
        post_cancel_revalidate()
        return self.boundary.replace_guarded(request)

    def get_order(self, **kwargs: Any) -> Any:
        return self.boundary.get_order(**kwargs)

    def discover_orders(self, **kwargs: Any) -> Any:
        return self.boundary.discover_orders(**kwargs)

    def get_positions(self, **kwargs: Any) -> Any:
        return self.boundary.get_positions(**kwargs)

    def is_ambiguous_submission_error(self, error: BaseException) -> bool:
        return self.boundary.is_ambiguous_submission_error(error)

    def is_ambiguous_cancellation_error(self, error: BaseException) -> bool:
        return self.boundary.is_ambiguous_cancellation_error(error)

    def cached_emergency_record(self, client_order_id: str) -> None:
        return None

    def cached_execution_record(
        self, client_order_id: str
    ) -> ExecutionOrderRecord | None:
        return self._cached_records.get(str(client_order_id or ""))

    def remember_canonical_execution_record(
        self, record: ExecutionOrderRecord
    ) -> None:
        self._cached_records[record.client_order_id] = record


__all__ = ["ShadowExecutionGateway"]
