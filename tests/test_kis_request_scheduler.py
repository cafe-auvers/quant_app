from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.usefixtures("authorized_full_live")

from src.services.kis_request_scheduler import (
    BudgetPolicy,
    ConfirmedPreAcceptanceRejection,
    KisRequestScheduler,
    RequestBudgetUncertainError,
    RequestKind,
    RequestPriority,
)
from src.services.kis_request_boundary import (
    execute_kis_request,
    kis_request_scope,
)
from src.services.mutation_budget_protocol import (
    CommandType,
    MutationBudgetExceededError,
)
from src.services.broker import KisBroker
from src.services.execution_command_gateway import ExecutionCommandGateway
from src.api.kis_account_snapshot_dual import KisApiError, KisRateLimitError
from src.api import kis_order
from src.core.order_state import OrderSide


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


def test_controlled_live_scheduler_disables_even_confirmed_mutation_retry():
    scheduler = _scheduler(max_confirmed_mutation_attempts=1)
    scheduler.synchronize_budget(
        kind=RequestKind.MUTATION,
        account_no="acct",
        endpoint="submit_order",
        remaining=5,
    )
    calls = []

    with pytest.raises(ConfirmedPreAcceptanceRejection):
        scheduler.execute_mutation(
            lambda: calls.append(True)
            or (_ for _ in ()).throw(
                ConfirmedPreAcceptanceRejection("explicit refusal")
            ),
            command_type=CommandType.SUBMIT,
            account_no="acct",
            endpoint="submit_order",
            priority=RequestPriority.NEW_ENTRY,
            is_new_entry=True,
        )

    assert calls == [True]
    assert scheduler.metrics().confirmed_mutation_retries == 0


def test_process_wide_mutation_spacing_applies_across_endpoint_buckets():
    now = [10.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    scheduler = _scheduler(
        max_confirmed_mutation_attempts=1,
        min_mutation_spacing_seconds=0.2,
        monotonic=lambda: now[0],
        sleeper=sleep,
    )
    for endpoint in ("submit_order", "cancel_order"):
        scheduler.synchronize_budget(
            kind=RequestKind.MUTATION,
            account_no="acct",
            endpoint=endpoint,
            remaining=5,
        )
    starts = []
    for endpoint, command_type in (
        ("submit_order", CommandType.SUBMIT),
        ("cancel_order", CommandType.CANCEL),
    ):
        scheduler.execute_mutation(
            lambda: starts.append(now[0]),
            command_type=command_type,
            account_no="acct",
            endpoint=endpoint,
            priority=RequestPriority.EMERGENCY_EXIT,
        )

    assert starts == pytest.approx([10.0, 10.2])
    assert sleeps == pytest.approx([0.2])


def test_process_wide_request_spacing_applies_across_reads_and_mutations():
    now = [10.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    scheduler = _scheduler(
        max_confirmed_mutation_attempts=1,
        min_request_spacing_seconds=0.1,
        monotonic=lambda: now[0],
        sleeper=sleep,
    )
    scheduler.synchronize_budget(
        kind=RequestKind.MUTATION,
        account_no="acct",
        endpoint="cancel_order",
        remaining=1,
    )
    starts = []
    scheduler.execute_read(
        lambda: starts.append(now[0]),
        account_no="acct",
        endpoint="balance",
    )
    scheduler.execute_mutation(
        lambda: starts.append(now[0]),
        command_type=CommandType.CANCEL,
        account_no="acct",
        endpoint="cancel_order",
        priority=RequestPriority.EMERGENCY_EXIT,
    )

    assert starts == pytest.approx([10.0, 10.1])
    assert sleeps == pytest.approx([0.1])


def test_retry_after_failure_pauses_every_request_class():
    now = [10.0]
    sleeps = []
    calls = []

    class RateLimited(RuntimeError):
        retry_after_seconds = 1.0

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    scheduler = _scheduler(
        monotonic=lambda: now[0],
        sleeper=sleep,
    )

    def read():
        calls.append(now[0])
        if len(calls) == 1:
            raise RateLimited("broker-wide rate limit")
        return "ok"

    assert scheduler.execute_read(
        read,
        account_no="acct",
        endpoint="balance",
    ) == "ok"
    assert calls == pytest.approx([10.0, 11.0])
    assert sleeps == pytest.approx([0.25, 0.75])


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


def test_verified_configuration_initializes_once_without_heartbeat_refill():
    scheduler = _scheduler()
    scheduler.configure_verified_mutation_budget(
        account_no="acct",
        endpoint="submit_order",
        capacity=2,
        window_seconds=60,
    )
    scheduler.require_available(
        CommandType.SUBMIT,
        account_no="acct",
        endpoint="submit_order",
        is_new_entry=True,
    )
    scheduler.configure_verified_mutation_budget(
        account_no="acct",
        endpoint="submit_order",
        capacity=2,
        window_seconds=60,
    )
    assert scheduler.budget_snapshot()["MUTATION:acct:submit_order"]["remaining"] == 1


def test_real_kis_classifier_only_retries_typed_rate_limit_rejections():
    assert KisBroker.is_confirmed_pre_acceptance_rejection(
        KisRateLimitError("KIS explicitly refused before acceptance")
    )
    assert not KisBroker.is_confirmed_pre_acceptance_rejection(
        KisApiError("generic API failure may be ambiguous")
    )
    assert not KisBroker.is_confirmed_pre_acceptance_rejection(
        TimeoutError("network timeout")
    )


def test_emergency_request_overtakes_multi_request_discovery_before_next_http_call(
    monkeypatch,
):
    scheduler = _scheduler(max_read_attempts=1)
    gateway = ExecutionCommandGateway(
        real_broker=KisBroker(),
        request_scheduler=scheduler,
        mode_override=False,
    )
    first_request_started = threading.Event()
    release_first_request = threading.Event()
    order = []
    request_number = [0]

    monkeypatch.setattr(
        "src.services.broker.get_env_value", lambda *_args, **_kwargs: "NASD"
    )

    def one_http_request(source):
        request_number[0] += 1
        number = request_number[0]

        def network_call():
            if number == 1:
                first_request_started.set()
                assert release_first_request.wait(2)
            order.append(f"read-{number}-{source}")
            return []

        return execute_kis_request(
            network_call,
            account_no="acct",
            endpoint="/uapi/orders",
        )

    monkeypatch.setattr(
        kis_order,
        "query_overseas_order",
        lambda **_kwargs: one_http_request("regular"),
    )
    monkeypatch.setattr(
        kis_order,
        "query_overseas_reserved_order",
        lambda **_kwargs: one_http_request("reserved"),
    )

    discovery = threading.Thread(
        target=lambda: gateway.discover_orders(
            environment="PROD", account_no="acct"
        )
    )
    discovery.start()
    assert first_request_started.wait(2)

    def emergency_request():
        with kis_request_scope(
            scheduler=scheduler,
            account_no="acct",
            kind=RequestKind.MUTATION,
            priority=RequestPriority.EMERGENCY_EXIT,
            command_type=CommandType.SUBMIT,
            endpoint="submit_order",
        ):
            execute_kis_request(
                lambda: order.append("emergency-exit"),
                account_no="acct",
                endpoint="submit_order",
                default_kind=RequestKind.MUTATION,
                default_priority=RequestPriority.EMERGENCY_EXIT,
            )

    emergency = threading.Thread(target=emergency_request)
    emergency.start()
    deadline = time.time() + 2
    while scheduler.metrics().queued_requests < 1 and time.time() < deadline:
        time.sleep(0.005)
    release_first_request.set()
    discovery.join(2)
    emergency.join(2)
    assert not discovery.is_alive()
    assert not emergency.is_alive()
    assert order == ["read-1-regular", "emergency-exit", "read-2-reserved"]
    assert scheduler.metrics().completed_reads == 2


def test_legacy_entry_and_kanban_emergency_share_one_request_budget(
    monkeypatch,
):
    scheduler = _scheduler(max_read_attempts=1)
    scheduler.synchronize_budget(
        kind=RequestKind.MUTATION,
        account_no="acct",
        endpoint="submit_order",
        remaining=2,
    )
    guarded_broker = KisBroker()
    guarded_gateway = ExecutionCommandGateway(
        real_broker=guarded_broker,
        request_scheduler=scheduler,
        mode_override=True,
    )
    legacy_gateway = ExecutionCommandGateway(
        real_broker=KisBroker(), mode_override=False
    )
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    order = []
    monkeypatch.setattr(
        "src.services.broker.trading_state.require_trading_enabled",
        lambda *_args, **_kwargs: None,
    )

    def fake_place_overseas_order(**kwargs):
        is_entry = str(kwargs["side"]).lower() == "buy"
        label = "legacy-entry" if is_entry else "kanban-emergency"
        return execute_kis_request(
            lambda: order.append(label) or {"output": {"ODNO": label}},
            account_no=str(kwargs["account_no"]),
            endpoint="submit_order",
            default_kind=RequestKind.MUTATION,
            default_priority=(
                RequestPriority.NEW_ENTRY
                if is_entry
                else RequestPriority.EMERGENCY_EXIT
            ),
            default_command_type=CommandType.SUBMIT,
            default_is_new_entry=is_entry,
            mutation_classifier=KisBroker.is_confirmed_pre_acceptance_rejection,
        )

    monkeypatch.setattr(kis_order, "place_overseas_order", fake_place_overseas_order)

    blocker = threading.Thread(
        target=lambda: scheduler.execute_read(
            lambda: (
                blocker_started.set(),
                release_blocker.wait(2),
                order.append("blocker"),
            ),
            account_no="acct",
            endpoint="display",
            priority=RequestPriority.DISPLAY_REFRESH,
        )
    )
    blocker.start()
    assert blocker_started.wait(2)

    legacy = threading.Thread(
        target=lambda: legacy_gateway.submit_order(
            environment="PROD",
            account_no="acct",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=1,
            limit_price=100.0,
        )
    )
    legacy.start()

    emergency = threading.Thread(
        target=lambda: guarded_gateway._execute_scheduled_mutation(
            lambda: guarded_broker.submit_order(
                environment="PROD",
                account_no="acct",
                symbol="AAPL",
                side=OrderSide.SELL,
                quantity=1,
                limit_price=99.0,
            ),
            command_type=CommandType.SUBMIT,
            account_no="acct",
            endpoint="submit_order",
            priority=RequestPriority.EMERGENCY_EXIT,
            is_new_entry=False,
        )
    )
    emergency.start()

    deadline = time.time() + 2
    while scheduler.metrics().queued_requests < 2 and time.time() < deadline:
        time.sleep(0.005)
    release_blocker.set()
    for thread in (blocker, legacy, emergency):
        thread.join(2)
        assert not thread.is_alive()

    assert order == ["blocker", "kanban-emergency", "legacy-entry"]
    snapshot = scheduler.budget_snapshot()
    assert snapshot["MUTATION:acct:submit_order"]["remaining"] == 0
