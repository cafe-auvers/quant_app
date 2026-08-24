"""Priority-aware KIS request scheduling and rate-budget enforcement.

The scheduler is deliberately synchronous: callers keep ownership of their
database transaction boundaries while this object serializes only the actual
network-operation boundary.  Reads may retry according to policy.  Mutations
may retry only after a typed, explicit pre-acceptance rejection; every other
exception is propagated after the first call so the execution gateway can
route ambiguity to reconciliation (INV-23).

Mutation budget knowledge starts ``UNCERTAIN`` after every process restart.
That deterministic cold-start rule blocks new entries until a broker response
or another authoritative source synchronizes the account/endpoint budget.
Protective requests retain a small, bounded reserve while the budget is
uncertain so display traffic and new entries cannot starve exits.
"""
from __future__ import annotations

import heapq
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from typing import Callable, Dict, Optional, TypeVar

from src.services.mutation_budget_protocol import (
    CommandType,
    MutationBudgetExceededError,
)

T = TypeVar("T")


class RequestKind(str, Enum):
    READ = "READ"
    MUTATION = "MUTATION"


class RequestPriority(IntEnum):
    EMERGENCY_EXIT = 1
    EXIT_CANCEL_OR_RECONCILIATION = 2
    LEASE_OR_HANDOFF = 3
    ACCOUNT_RECONCILIATION = 4
    ENTRY_CANCEL = 5
    NEW_ENTRY = 6
    DISPLAY_REFRESH = 7


class BudgetKnowledge(str, Enum):
    KNOWN = "KNOWN"
    UNCERTAIN = "UNCERTAIN"


class RequestBudgetUncertainError(MutationBudgetExceededError):
    """A request that must fail closed cannot prove budget availability."""


class ConfirmedPreAcceptanceRejection(RuntimeError):
    """The broker explicitly confirmed that it did not accept a mutation.

    ``retry_after_seconds`` may come from a rate-limit response.  This is the
    only mutation exception the scheduler is permitted to retry.
    """

    def __init__(self, message: str, *, retry_after_seconds: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds or 0.0))


@dataclass(frozen=True)
class BudgetPolicy:
    capacity: int
    window_seconds: float

    def __post_init__(self) -> None:
        if int(self.capacity) <= 0:
            raise ValueError("budget capacity must be positive")
        if float(self.window_seconds) <= 0:
            raise ValueError("budget window_seconds must be positive")


@dataclass
class _BudgetBucket:
    capacity: int
    remaining: int
    reset_at: float
    window_seconds: float
    knowledge: BudgetKnowledge


@dataclass(frozen=True)
class SchedulerMetrics:
    queued_requests: int = 0
    active_requests: int = 0
    completed_reads: int = 0
    completed_mutations: int = 0
    read_retries: int = 0
    confirmed_mutation_retries: int = 0
    ambiguous_mutations_not_retried: int = 0
    budget_rejections: int = 0
    uncertain_entry_rejections: int = 0
    highest_waiting_priority: int = 0
    known_mutation_budget_buckets: int = 0
    uncertain_mutation_budget_buckets: int = 0


@dataclass
class _Waiter:
    priority: int
    sequence: int
    released: bool = False


class KisRequestScheduler:
    """Account/endpoint budgets plus a strict cross-class priority queue."""

    context_aware = True

    def __init__(
        self,
        *,
        read_policy: BudgetPolicy = BudgetPolicy(20, 1.0),
        mutation_policy: BudgetPolicy = BudgetPolicy(5, 1.0),
        uncertain_protective_reserve: int = 2,
        max_read_attempts: int = 3,
        max_confirmed_mutation_attempts: int = 2,
        min_request_spacing_seconds: float = 0.0,
        min_mutation_spacing_seconds: float = 0.0,
        backoff_seconds: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._policies = {
            RequestKind.READ: read_policy,
            RequestKind.MUTATION: mutation_policy,
        }
        self._uncertain_protective_reserve = max(
            0, int(uncertain_protective_reserve)
        )
        self._max_read_attempts = max(1, int(max_read_attempts))
        self._max_confirmed_mutation_attempts = max(
            1, int(max_confirmed_mutation_attempts)
        )
        self._min_request_spacing_seconds = max(
            0.0, float(min_request_spacing_seconds)
        )
        self._min_mutation_spacing_seconds = max(
            0.0, float(min_mutation_spacing_seconds)
        )
        self._backoff_seconds = max(0.0, float(backoff_seconds))
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._condition = threading.Condition(threading.RLock())
        self._queue: list[tuple[int, int, _Waiter]] = []
        self._sequence = 0
        self._active = False
        self._last_request_started_at: Optional[float] = None
        self._last_mutation_started_at: Optional[float] = None
        self._requests_not_before_at = 0.0
        self._buckets: Dict[tuple[RequestKind, str, str], _BudgetBucket] = {}
        self._metrics = SchedulerMetrics()

    @property
    def max_confirmed_mutation_attempts(self) -> int:
        return self._max_confirmed_mutation_attempts

    @property
    def min_request_spacing_seconds(self) -> float:
        return self._min_request_spacing_seconds

    @property
    def min_mutation_spacing_seconds(self) -> float:
        return self._min_mutation_spacing_seconds

    @staticmethod
    def _key(
        kind: RequestKind, account_no: str, endpoint: str
    ) -> tuple[RequestKind, str, str]:
        return (
            kind,
            str(account_no or "").strip(),
            str(endpoint or "").strip().lower() or "unknown",
        )

    def _new_bucket(self, kind: RequestKind, now: float) -> _BudgetBucket:
        policy = self._policies[kind]
        if kind == RequestKind.READ:
            return _BudgetBucket(
                capacity=policy.capacity,
                remaining=policy.capacity,
                reset_at=now + policy.window_seconds,
                window_seconds=policy.window_seconds,
                knowledge=BudgetKnowledge.KNOWN,
            )
        return _BudgetBucket(
            capacity=policy.capacity,
            remaining=min(policy.capacity, self._uncertain_protective_reserve),
            reset_at=now + policy.window_seconds,
            window_seconds=policy.window_seconds,
            knowledge=BudgetKnowledge.UNCERTAIN,
        )

    def _bucket(
        self, kind: RequestKind, account_no: str, endpoint: str
    ) -> _BudgetBucket:
        now = self._monotonic()
        key = self._key(kind, account_no, endpoint)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._new_bucket(kind, now)
            self._buckets[key] = bucket
            self._refresh_budget_metrics_locked()
        elif now >= bucket.reset_at:
            bucket.remaining = (
                bucket.capacity
                if bucket.knowledge == BudgetKnowledge.KNOWN
                else min(bucket.capacity, self._uncertain_protective_reserve)
            )
            bucket.reset_at = now + bucket.window_seconds
        return bucket

    def synchronize_budget(
        self,
        *,
        kind: RequestKind,
        account_no: str,
        endpoint: str,
        remaining: int,
        reset_after_seconds: Optional[float] = None,
    ) -> None:
        """Record authoritative remaining-budget information from KIS."""

        policy = self._policies[kind]
        reset_after = (
            policy.window_seconds
            if reset_after_seconds is None
            else max(0.001, float(reset_after_seconds))
        )
        with self._condition:
            self._buckets[self._key(kind, account_no, endpoint)] = _BudgetBucket(
                capacity=policy.capacity,
                remaining=max(0, min(policy.capacity, int(remaining))),
                reset_at=self._monotonic() + reset_after,
                window_seconds=reset_after,
                knowledge=BudgetKnowledge.KNOWN,
            )
            self._refresh_budget_metrics_locked()

    def configure_verified_mutation_budget(
        self,
        *,
        account_no: str,
        endpoint: str,
        capacity: int,
        window_seconds: float,
    ) -> None:
        """Initialize one account/endpoint budget from verified WS0 evidence.

        This is intentionally idempotent. Re-running a heartbeat must never
        refill a partially consumed window. A fresh scheduler process starts
        uncertain again and can become known only when production composition
        supplies an explicitly verified positive policy.
        """

        capacity = int(capacity)
        window_seconds = float(window_seconds)
        if capacity <= 0 or window_seconds <= 0:
            raise ValueError("verified mutation budget must be positive")
        key = self._key(RequestKind.MUTATION, account_no, endpoint)
        with self._condition:
            if key in self._buckets:
                return
            now = self._monotonic()
            self._buckets[key] = _BudgetBucket(
                capacity=capacity,
                remaining=capacity,
                reset_at=now + window_seconds,
                window_seconds=window_seconds,
                knowledge=BudgetKnowledge.KNOWN,
            )
            self._refresh_budget_metrics_locked()

    def mark_budget_uncertain(
        self, *, kind: RequestKind, account_no: str, endpoint: str
    ) -> None:
        with self._condition:
            bucket = self._bucket(kind, account_no, endpoint)
            bucket.knowledge = BudgetKnowledge.UNCERTAIN
            if kind == RequestKind.MUTATION:
                bucket.remaining = min(
                    bucket.remaining, self._uncertain_protective_reserve
                )
            self._refresh_budget_metrics_locked()

    def _refresh_budget_metrics_locked(self) -> None:
        mutation_buckets = [
            bucket
            for (kind, _account, _endpoint), bucket in self._buckets.items()
            if kind == RequestKind.MUTATION
        ]
        self._metrics = replace(
            self._metrics,
            known_mutation_budget_buckets=sum(
                bucket.knowledge == BudgetKnowledge.KNOWN
                for bucket in mutation_buckets
            ),
            uncertain_mutation_budget_buckets=sum(
                bucket.knowledge == BudgetKnowledge.UNCERTAIN
                for bucket in mutation_buckets
            ),
        )

    def require_available(
        self,
        command_type: CommandType,
        *,
        account_no: str = "",
        endpoint: str = "",
        priority: RequestPriority = RequestPriority.NEW_ENTRY,
        is_new_entry: bool = False,
        consume: bool = True,
    ) -> None:
        """Enforce one mutation budget at the account/endpoint boundary."""

        del command_type  # endpoint and request class are the budget dimensions.
        with self._condition:
            bucket = self._bucket(RequestKind.MUTATION, account_no, endpoint)
            if is_new_entry and bucket.knowledge != BudgetKnowledge.KNOWN:
                self._metrics = replace(
                    self._metrics,
                    budget_rejections=self._metrics.budget_rejections + 1,
                    uncertain_entry_rejections=(
                        self._metrics.uncertain_entry_rejections + 1
                    ),
                )
                raise RequestBudgetUncertainError(
                    "New entries require an authoritative KIS mutation budget"
                )
            if bucket.remaining <= 0:
                self._metrics = replace(
                    self._metrics,
                    budget_rejections=self._metrics.budget_rejections + 1,
                )
                raise MutationBudgetExceededError(
                    f"No mutation budget remains for account={account_no!r} "
                    f"endpoint={endpoint!r} priority={int(priority)}"
                )
            if consume:
                bucket.remaining -= 1

    def _acquire_turn(self, priority: RequestPriority) -> _Waiter:
        with self._condition:
            self._sequence += 1
            waiter = _Waiter(int(priority), self._sequence)
            heapq.heappush(self._queue, (waiter.priority, waiter.sequence, waiter))
            self._refresh_queue_metrics_locked()
            while self._active or self._queue[0][2] is not waiter:
                self._condition.wait()
            heapq.heappop(self._queue)
            self._active = True
            waiter.released = True
            self._refresh_queue_metrics_locked()
            return waiter

    def _release_turn(self) -> None:
        with self._condition:
            self._active = False
            self._refresh_queue_metrics_locked()
            self._condition.notify_all()

    def defer_requests(self, seconds: float) -> None:
        """Pause every request class after a broker-wide throttle refusal."""

        delay = max(0.0, float(seconds or 0.0))
        if delay <= 0:
            return
        with self._condition:
            self._requests_not_before_at = max(
                self._requests_not_before_at,
                self._monotonic() + delay,
            )

    def _defer_for_exception(self, exc: BaseException) -> float:
        try:
            retry_after = max(
                0.0,
                float(getattr(exc, "retry_after_seconds", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            retry_after = 0.0
        if retry_after > 0:
            self.defer_requests(retry_after)
        return retry_after

    def _wait_for_request_spacing(self, kind: RequestKind) -> None:
        """Enforce shared KIS pacing plus the stricter mutation-only floor."""

        now = self._monotonic()
        required_at = self._requests_not_before_at
        previous_request = self._last_request_started_at
        if previous_request is not None:
            required_at = max(
                required_at,
                previous_request + self._min_request_spacing_seconds,
            )
        previous_mutation = self._last_mutation_started_at
        if kind == RequestKind.MUTATION and previous_mutation is not None:
            required_at = max(
                required_at,
                previous_mutation + self._min_mutation_spacing_seconds,
            )
        delay = max(0.0, required_at - now)
        if delay > 0:
            self._sleeper(delay)
        # A deterministic test sleeper need not advance its fake clock. Keep
        # the logical start monotonic while a real sleeper records real time.
        observed = self._monotonic()
        logical_start = max(observed, required_at)
        self._last_request_started_at = logical_start
        if kind == RequestKind.MUTATION:
            self._last_mutation_started_at = logical_start

    def _refresh_queue_metrics_locked(self) -> None:
        highest = self._queue[0][0] if self._queue else 0
        self._metrics = replace(
            self._metrics,
            queued_requests=len(self._queue),
            active_requests=1 if self._active else 0,
            highest_waiting_priority=int(highest),
        )

    def execute_read(
        self,
        operation: Callable[[], T],
        *,
        account_no: str,
        endpoint: str,
        priority: RequestPriority = RequestPriority.ACCOUNT_RECONCILIATION,
        retry_if: Callable[[BaseException], bool] = lambda _exc: True,
    ) -> T:
        """Run a read with bounded retry/backoff; reads are idempotent."""

        attempt = 0
        while True:
            attempt += 1
            self._acquire_turn(priority)
            try:
                with self._condition:
                    bucket = self._bucket(RequestKind.READ, account_no, endpoint)
                    if bucket.remaining <= 0:
                        raise MutationBudgetExceededError(
                            f"No read budget remains for {account_no}/{endpoint}"
                        )
                    bucket.remaining -= 1
                self._wait_for_request_spacing(RequestKind.READ)
                result = operation()
            except Exception as exc:
                self._defer_for_exception(exc)
                if attempt >= self._max_read_attempts or not retry_if(exc):
                    raise
                self._metrics = replace(
                    self._metrics,
                    read_retries=self._metrics.read_retries + 1,
                )
                delay = self._backoff_seconds * (2 ** (attempt - 1))
            else:
                self._metrics = replace(
                    self._metrics,
                    completed_reads=self._metrics.completed_reads + 1,
                )
                return result
            finally:
                self._release_turn()
            self._sleeper(delay)

    def execute_mutation(
        self,
        operation: Callable[[], T],
        *,
        command_type: CommandType,
        account_no: str,
        endpoint: str,
        priority: RequestPriority,
        is_new_entry: bool = False,
        is_confirmed_pre_acceptance_rejection: Optional[
            Callable[[BaseException], bool]
        ] = None,
    ) -> T:
        """Run one mutation, retrying only explicit pre-acceptance refusal."""

        classifier = is_confirmed_pre_acceptance_rejection or (
            lambda exc: isinstance(exc, ConfirmedPreAcceptanceRejection)
        )
        attempt = 0
        while True:
            attempt += 1
            self._acquire_turn(priority)
            try:
                self.require_available(
                    command_type,
                    account_no=account_no,
                    endpoint=endpoint,
                    priority=priority,
                    is_new_entry=is_new_entry,
                    consume=True,
                )
                self._wait_for_request_spacing(RequestKind.MUTATION)
                result = operation()
            except Exception as exc:
                retry_after = self._defer_for_exception(exc)
                confirmed = False
                try:
                    confirmed = bool(classifier(exc))
                except Exception:
                    confirmed = False
                if not confirmed:
                    self._metrics = replace(
                        self._metrics,
                        ambiguous_mutations_not_retried=(
                            self._metrics.ambiguous_mutations_not_retried + 1
                        ),
                    )
                    raise
                if attempt >= self._max_confirmed_mutation_attempts:
                    raise
                self._metrics = replace(
                    self._metrics,
                    confirmed_mutation_retries=(
                        self._metrics.confirmed_mutation_retries + 1
                    ),
                )
                delay = max(
                    retry_after, self._backoff_seconds * (2 ** (attempt - 1))
                )
            else:
                self._metrics = replace(
                    self._metrics,
                    completed_mutations=self._metrics.completed_mutations + 1,
                )
                return result
            finally:
                self._release_turn()
            self._sleeper(delay)

    def metrics(self) -> SchedulerMetrics:
        with self._condition:
            return replace(self._metrics)

    def budget_snapshot(self) -> Dict[str, Dict[str, object]]:
        """Read-only diagnostics for the Health tab and tests."""

        with self._condition:
            snapshot: Dict[str, Dict[str, object]] = {}
            for (kind, account, endpoint), bucket in self._buckets.items():
                snapshot[f"{kind.value}:{account}:{endpoint}"] = {
                    "remaining": bucket.remaining,
                    "capacity": bucket.capacity,
                    "knowledge": bucket.knowledge.value,
                    "reset_at_monotonic": bucket.reset_at,
                }
            return snapshot
