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


class PreBrokerDispatchAbortedError(RuntimeError):
    """A final queued-request check failed before any HTTP mutation."""


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
    pre_dispatch_check: Optional[Callable[[], None]] = None


_context: ContextVar[Optional[KisRequestContext]] = ContextVar(
    "kis_request_context", default=None
)
_read_priority: ContextVar[Optional[RequestPriority]] = ContextVar("kis_read_priority", default=None)


def effective_kis_read_priority(default):
    override = _read_priority.get()
    return min(default, override) if override is not None else default


@contextmanager
def kis_read_priority_scope(priority):
    token = _read_priority.set(priority)
    try:
        yield
    finally:
        _read_priority.reset(token)
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
    pre_dispatch_check: Optional[Callable[[], None]] = None,
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
            pre_dispatch_check=pre_dispatch_check,
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
    kind = force_kind or (context.kind if context is not None else default_kind)
    if scheduler is None:
        if kind == RequestKind.MUTATION and context is not None and context.pre_dispatch_check is not None:
            try:
                context.pre_dispatch_check()
            except Exception as exc:
                raise PreBrokerDispatchAbortedError(str(exc)) from exc
        return operation()

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
        priority = effective_kis_read_priority(priority)
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
    def checked_operation():
        if context is not None and context.pre_dispatch_check is not None:
            try:
                context.pre_dispatch_check()
            except Exception as exc:
                raise PreBrokerDispatchAbortedError(str(exc)) from exc
        return operation()
    return scheduler.execute_mutation(
        checked_operation,
        command_type=command_type,
        account_no=resolved_account,
        endpoint=resolved_endpoint,
        priority=priority,
        is_new_entry=is_new_entry,
        is_confirmed_pre_acceptance_rejection=classifier,
    )
