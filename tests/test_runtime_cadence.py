import pytest

from src.ui.buyboard.runtime_worker import _cycle_wait_milliseconds


@pytest.mark.parametrize(("elapsed", "expected"), [(0, 1000), (.25, 750), (.8, 200), (1, 1), (8, 1)])
def test_engine_work_consumes_the_existing_cycle_budget(elapsed, expected):
    assert _cycle_wait_milliseconds(1, 0, elapsed) == expected


def test_monotonic_deadline_does_not_add_one_second_after_a_slow_reconciliation():
    # The old schedule took 8s of work plus a fresh 1s wait. The next eligible
    # cycle now resumes immediately, retaining every reconciliation fence.
    assert _cycle_wait_milliseconds(1, 10, 18) == 1
