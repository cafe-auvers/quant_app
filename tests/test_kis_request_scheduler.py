from __future__ import annotations

import threading
import time

import pytest

from src.services.kis_request_scheduler import (
    BudgetPolicy,
    ConfirmedPreAcceptanceRejection,
    KisRequestScheduler,
    RequestBudgetUncertainError,
    RequestKind,
    RequestPriority,
)
from src.services.mutation_budget_protocol import (
    CommandType,
    MutationBudgetExceededError,
)


def _scheduler(**kwargs):
    return KisRequestScheduler(
        read_policy=BudgetPolicy(20, 60),
        mutation_policy=BudgetPolicy(5, 60),
        sleeper=kwargs.pop("sleeper", lambda _seconds: None),
        **kwargs,
    )


def test_rate_limited_read_backs_off_and_retries():
    sleeps = []
    scheduler = _scheduler(sleeper=sleeps.append)
    calls = []

    def read():
        calls.append(True)
        if len(calls) < 3:
            raise RuntimeError("HTTP 429")
        return "ok"

    assert scheduler.execute_read(
        read,
        account_no="acct",
        endpoint="balance",
        retry_if=lambda exc: "429" in str(exc),
    ) == "ok"
    assert len(calls) == 3
    assert sleeps == [0.25, 0.5]
    assert scheduler.metrics().read_retries == 2


def test_ambiguous_mutation_failure_is_never_retried_by_the_scheduler():
    scheduler = _scheduler()
    scheduler.synchronize_budget(
        kind=RequestKind.MUTATION,
        account_no="acct",
        endpoint="submit_order",
        remaining=5,
    )
    calls = []

    def mutation():
        calls.append(True)
        raise TimeoutError("response may have been accepted")

    with pytest.raises(TimeoutError):
        scheduler.execute_mutation(
            mutation,
            command_type=CommandType.SUBMIT,
            account_no="acct",
            endpoint="submit_order",
            priority=RequestPriority.NEW_ENTRY,
            is_new_entry=True,
        )

    assert len(calls) == 1
    assert scheduler.metrics().ambiguous_mutations_not_retried == 1


def test_confirmed_pre_acceptance_rejection_is_the_only_mutation_retry():
    scheduler = _scheduler()
    scheduler.synchronize_budget(
        kind=RequestKind.MUTATION,
        account_no="acct",
        endpoint="cancel_order",
        remaining=5,
    )
    calls = []

    def mutation():
        calls.append(True)
        if len(calls) == 1:
            raise ConfirmedPreAcceptanceRejection("rate limited")
        return "cancelled"

    assert scheduler.execute_mutation(
        mutation,
        command_type=CommandType.CANCEL,
        account_no="acct",
        endpoint="cancel_order",
        priority=RequestPriority.EXIT_CANCEL_OR_RECONCILIATION,
    ) == "cancelled"
    assert len(calls) == 2
    assert scheduler.metrics().confirmed_mutation_retries == 1


def test_exit_requests_are_never_starved_by_display_refresh_backlog():
    scheduler = _scheduler()
    first_started = threading.Event()
    release_first = threading.Event()
    order = []

    def first_display():
        first_started.set()
        assert release_first.wait(2)
        order.append("display-0")

    threads = [
        threading.Thread(
            target=lambda: scheduler.execute_read(
                first_display,
                account_no="acct",
                endpoint="quote",
                priority=RequestPriority.DISPLAY_REFRESH,
            )
        )
    ]
    threads[0].start()
    assert first_started.wait(2)

    for label in ("display-1", "display-2"):
        thread = threading.Thread(
            target=lambda name=label: scheduler.execute_read(
                lambda: order.append(name),
                account_no="acct",
                endpoint="quote",
                priority=RequestPriority.DISPLAY_REFRESH,
            )
        )
        threads.append(thread)
        thread.start()
    exit_thread = threading.Thread(
        target=lambda: scheduler.execute_read(
            lambda: order.append("protective-exit"),
            account_no="acct",
            endpoint="order-status",
            priority=RequestPriority.EMERGENCY_EXIT,
        )
    )
    threads.append(exit_thread)
    exit_thread.start()
    deadline = time.time() + 2
    while scheduler.metrics().queued_requests < 3 and time.time() < deadline:
        time.sleep(0.005)
    release_first.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()

    assert order[0] == "display-0"
    assert order[1] == "protective-exit"


def test_new_entries_fail_closed_when_budget_state_is_uncertain():
    scheduler = _scheduler()

    with pytest.raises(RequestBudgetUncertainError):
        scheduler.require_available(
            CommandType.SUBMIT,
            account_no="acct",
            endpoint="submit_order",
            priority=RequestPriority.NEW_ENTRY,
            is_new_entry=True,
        )

    assert scheduler.metrics().uncertain_entry_rejections == 1


def test_budgets_are_isolated_per_account_and_endpoint():
    scheduler = _scheduler()
    for endpoint in ("submit_order", "cancel_order"):
        scheduler.synchronize_budget(
            kind=RequestKind.MUTATION,
            account_no="acct-a",
            endpoint=endpoint,
            remaining=1,
        )

    scheduler.require_available(
        CommandType.SUBMIT,
        account_no="acct-a",
        endpoint="submit_order",
        is_new_entry=True,
    )
    with pytest.raises(MutationBudgetExceededError):
        scheduler.require_available(
            CommandType.SUBMIT,
            account_no="acct-a",
            endpoint="submit_order",
            is_new_entry=True,
        )
    scheduler.require_available(
        CommandType.CANCEL,
        account_no="acct-a",
        endpoint="cancel_order",
        priority=RequestPriority.EXIT_CANCEL_OR_RECONCILIATION,
    )


def test_scheduler_restart_deterministically_returns_entry_budget_to_uncertain():
    first = _scheduler()
    first.synchronize_budget(
        kind=RequestKind.MUTATION,
        account_no="acct",
        endpoint="submit_order",
        remaining=4,
    )
    first.require_available(
        CommandType.SUBMIT,
        account_no="acct",
        endpoint="submit_order",
        is_new_entry=True,
    )

    restarted = _scheduler()
    with pytest.raises(RequestBudgetUncertainError):
        restarted.require_available(
            CommandType.SUBMIT,
            account_no="acct",
            endpoint="submit_order",
            is_new_entry=True,
        )


def test_request_scheduler_metrics_are_exposed():
    scheduler = _scheduler()
    scheduler.execute_read(
        lambda: "ok",
        account_no="acct",
        endpoint="positions",
    )

    metrics = scheduler.metrics()
    snapshot = scheduler.budget_snapshot()

    assert metrics.completed_reads == 1
    assert snapshot["READ:acct:positions"]["remaining"] == 19
