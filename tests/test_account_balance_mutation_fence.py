import pytest

from src.services import kis_request_boundary as boundary
from src.services.kis_request_scheduler import RequestKind, RequestPriority


@pytest.fixture(autouse=True)
def isolated_boundary(monkeypatch):
    monkeypatch.setattr(boundary, "_account_mutations", {})
    monkeypatch.setattr(boundary, "get_process_kis_request_scheduler", lambda: None)


@pytest.mark.parametrize("scheduled", [False, True])
def test_only_actual_http_mutations_advance_the_account_generation(scheduled):
    class Scheduler:
        def execute_mutation(self, operation, **kwargs):
            assert boundary.account_mutation_generation("a") == (0, 0)
            return operation()

    def send():
        assert boundary.account_mutation_generation("a") == (1, 1)
        assert boundary.account_mutation_generation("b") == (0, 0)
        return "ack"

    with boundary.kis_request_scope(
        scheduler=Scheduler() if scheduled else None, account_no="a",
        kind=RequestKind.MUTATION, priority=RequestPriority.NEW_ENTRY,
    ):
        assert boundary.execute_kis_request(send, account_no="other", endpoint="submit") == "ack"
    assert boundary.account_mutation_generation("a") == (2, 0)
    assert boundary.account_mutation_generation("other") == (0, 0)


def test_pre_dispatch_rejection_does_not_invalidate_a_balance_read():
    def reject():
        raise ValueError("stale equity")

    with boundary.kis_request_scope(
        scheduler=None, account_no="a", kind=RequestKind.MUTATION,
        priority=RequestPriority.NEW_ENTRY, pre_dispatch_check=reject,
    ), pytest.raises(boundary.PreBrokerDispatchAbortedError):
        boundary.execute_kis_request(lambda: pytest.fail("HTTP must not be sent"),
                                     account_no="a", endpoint="submit")
    assert boundary.account_mutation_generation("a") == (0, 0)


def test_uncertain_http_outcome_still_invalidates_overlapping_balance_reads():
    def send():
        raise TimeoutError("ack unavailable")

    with pytest.raises(TimeoutError):
        boundary.execute_kis_request(send, account_no="a", endpoint="cancel",
                                     default_kind=RequestKind.MUTATION)
    assert boundary.account_mutation_generation("a") == (2, 0)


def test_read_requests_do_not_invalidate_balance_reads():
    assert boundary.execute_kis_request(lambda: 10_000, account_no="a", endpoint="balance") == 10_000
    assert boundary.account_mutation_generation("a") == (0, 0)
