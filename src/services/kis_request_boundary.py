"""Process-shared scheduling at the actual KIS HTTP request boundary."""
from __future__ import annotations

import threading
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, TypeVar

from src.services.kis_request_scheduler import RequestKind, RequestPriority
from src.services.mutation_budget_protocol import CommandType

T = TypeVar("T")


@dataclass(frozen=True)
class KisRequestContext:
    scheduler: Optional[Any]
    account_no: str
    kind: RequestKind
    priority: RequestPriority
    command_type: CommandType
    endpoint: str = ""
    is_new_entry: bool = False
    mutation_classifier: Optional[Callable[[BaseException], bool]] = None


_context: ContextVar[Optional[KisRequestContext]] = ContextVar(
    "kis_request_context", default=None
)
_scheduler_lock = threading.Lock()
_process_scheduler_ref: Optional[weakref.ReferenceType[Any]] = None


def install_process_kis_request_scheduler(scheduler: Any) -> None:
    """Publish the one scheduler shared by guarded and legacy KIS paths."""

    global _process_scheduler_ref
    if scheduler is None:
        return
    with _scheduler_lock:
        _process_scheduler_ref = weakref.ref(scheduler)


def get_process_kis_request_scheduler() -> Optional[Any]:
    with _scheduler_lock:
        reference = _process_scheduler_ref
    return reference() if reference is not None else None


def has_kis_request_scheduler() -> bool:
    context = _context.get()
    return bool(
        (context is not None and context.scheduler is not None)
        or get_process_kis_request_scheduler() is not None
    )


def defer_kis_requests(seconds: float) -> bool:
    """Apply a broker-wide cooldown to the active process scheduler."""

    context = _context.get()
    scheduler = (
        context.scheduler
        if context is not None and context.scheduler is not None
        else get_process_kis_request_scheduler()
    )
    defer = getattr(scheduler, "defer_requests", None)
    if not callable(defer):
        return False
    defer(seconds)
    return True


@contextmanager
def kis_request_scope(
    *,
    scheduler: Optional[Any],
    account_no: str,
    kind: RequestKind,
    priority: RequestPriority,
    command_type: CommandType = CommandType.SUBMIT,
    endpoint: str = "",
    is_new_entry: bool = False,
    mutation_classifier: Optional[Callable[[BaseException], bool]] = None,
) -> Iterator[None]:
    token = _context.set(
        KisRequestContext(
            scheduler=scheduler,
            account_no=str(account_no or ""),
            kind=kind,
            priority=priority,
            command_type=command_type,
            endpoint=str(endpoint or ""),
            is_new_entry=bool(is_new_entry),
            mutation_classifier=mutation_classifier,
        )
    )
    try:
        yield
    finally:
        _context.reset(token)


def execute_kis_request(
    operation: Callable[[], T],
    *,
    account_no: str,
    endpoint: str,
    default_kind: RequestKind = RequestKind.READ,
    default_priority: RequestPriority = RequestPriority.ACCOUNT_RECONCILIATION,
    default_command_type: CommandType = CommandType.SUBMIT,
    default_is_new_entry: bool = False,
    retry_if: Callable[[BaseException], bool] = lambda _exc: True,
    mutation_classifier: Optional[Callable[[BaseException], bool]] = None,
    force_kind: Optional[RequestKind] = None,
    force_priority: Optional[RequestPriority] = None,
) -> T:
    """Schedule exactly one HTTP request, never its enclosing workflow."""

    context = _context.get()
    scheduler = (
        context.scheduler
        if context is not None and context.scheduler is not None
        else get_process_kis_request_scheduler()
    )
    if scheduler is None:
        return operation()

    kind = force_kind or (context.kind if context is not None else default_kind)
    priority = force_priority or (
        context.priority if context is not None else default_priority
    )
    resolved_account = (
        context.account_no
        if context is not None and context.account_no
        else str(account_no or "")
    )
    resolved_endpoint = (
        context.endpoint
        if kind == RequestKind.MUTATION and context is not None and context.endpoint
        else str(endpoint or "unknown")
    )
    if kind == RequestKind.READ:
        return scheduler.execute_read(
            operation,
            account_no=resolved_account,
            endpoint=resolved_endpoint,
            priority=priority,
            retry_if=retry_if,
        )

    command_type = (
        context.command_type if context is not None else default_command_type
    )
    is_new_entry = (
        context.is_new_entry if context is not None else default_is_new_entry
    )
    classifier = (
        context.mutation_classifier
        if context is not None and context.mutation_classifier is not None
        else mutation_classifier
    )
    return scheduler.execute_mutation(
        operation,
        command_type=command_type,
        account_no=resolved_account,
        endpoint=resolved_endpoint,
        priority=priority,
        is_new_entry=is_new_entry,
        is_confirmed_pre_acceptance_rejection=classifier,
    )
