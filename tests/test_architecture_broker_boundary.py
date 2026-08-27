"""Architecture-boundary tests for Workstream 3/9 (PR2): the execution
gateway is the only application component permitted to invoke destructive
broker operations.

docs/kanban_production_readiness.md: "Direct broker-mutation imports
outside approved adapter/gateway modules fail an architecture test... A
practical enforcement test should scan imports or monkeypatch the broker
mutation methods and prove that no legacy/Kanban path can reach them
except through the gateway."

Two complementary checks:

1. A static source scan (this module) -- the real KIS mutation entry
   points (:mod:`src.api.kis_order`'s ``place_overseas_order``/
   ``place_overseas_reserved_market_on_open_sell``/``cancel_overseas_order``/
   ``cancel_overseas_reserved_order``) may only be *called* from
   :mod:`src.services.broker` (which exists specifically to wrap them), and
   :class:`~src.services.broker.KisBroker` may only be *constructed* from
   an explicit, narrow allowlist -- the broker adapter itself, the
   execution gateway, and read-only reconciliation workers that never
   reach ``submit_order``/``cancel_order`` (confirmed by their own
   docstrings/behavior, not merely assumed).
2. A runtime wiring proof -- patching ``KisBroker.submit_order``/
   ``cancel_order`` and confirming the *sanctioned* entry point
   (:mod:`src.services.execution_workflow_service`) actually reaches them,
   through the gateway, exactly once. A static allowlist alone can't prove
   the plumbing is real and connected rather than accidentally dead code.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import pytest

pytestmark = pytest.mark.usefixtures("authorized_full_live")

from src.brokers.execution_broker_protocol import BrokerSubmissionResult
from src.core.execution_mode import ExecutionSource
from src.core.order_state import BrokerOrderStatusSnapshot, OrderIntent, OrderSide, OrderStatus
from src.services import broker as broker_module
from src.services import execution_workflow_service

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

# The only module allowed to call these -- src.services.broker exists
# specifically to be that one wrapper (see its own module docstring).
_KIS_MUTATION_FUNCTIONS = [
    "place_overseas_order",
    "place_overseas_reserved_market_on_open_sell",
    "cancel_overseas_order",
    "cancel_overseas_reserved_order",
]
_MUTATION_CALL_ALLOWLIST = {"src/services/broker.py"}

# Every file permitted to *construct* a KisBroker -- the adapter itself,
# the execution gateway (the sanctioned door), and reconciliation code
# that only ever reads broker state (get_order/discover_orders/
# get_positions), never submits or cancels. Each read-only entry is
# annotated with why it's safe, not merely grandfathered.
_KIS_BROKER_CONSTRUCTION_ALLOWLIST = {
    "src/services/execution_command_gateway.py": "the sanctioned gateway itself",
    # Read-only reconciliation -- query_and_reconcile_unresolved_orders
    # only calls query_overseas_order/query_overseas_reserved_order.
    "src/services/order_reconciliation.py": "read-only order-status reconciliation only",
    # Read-only device-handoff reconciliation -- never submits/cancels.
    "src/services/handoff_reconciliation.py": "read-only handoff reconciliation only",
    # QThread wrappers around the above two read-only reconciliation paths.
    "src/ui/order_workers.py": "wraps read-only reconciliation workers only",
}


def _iter_src_files():
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        yield rel, path


def _strip_comments_and_strings_roughly(text: str) -> str:
    """Good enough to avoid false positives from a docstring/comment that
    merely *mentions* ``KisBroker()`` in prose (several already do) --
    not a full tokenizer, but sufficient for this narrow check."""
    # Drop triple-quoted docstrings first, then line comments.
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    text = re.sub(r"'''[\s\S]*?'''", "", text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def test_kis_order_mutation_functions_are_called_only_from_broker_py():
    pattern = re.compile(r"kis_order\.(" + "|".join(_KIS_MUTATION_FUNCTIONS) + r")\(")
    violations: Dict[str, List[str]] = {}
    for rel, path in _iter_src_files():
        if rel in _MUTATION_CALL_ALLOWLIST:
            continue
        code = _strip_comments_and_strings_roughly(path.read_text(encoding="utf-8"))
        matches = pattern.findall(code)
        if matches:
            violations[rel] = matches
    assert violations == {}, (
        f"Direct kis_order mutation calls found outside {_MUTATION_CALL_ALLOWLIST}: {violations}"
    )


def test_kisbroker_is_constructed_only_from_the_allowlist():
    pattern = re.compile(r"\bKisBroker\(")
    violations: Dict[str, int] = {}
    for rel, path in _iter_src_files():
        if rel in _KIS_BROKER_CONSTRUCTION_ALLOWLIST:
            continue
        code = _strip_comments_and_strings_roughly(path.read_text(encoding="utf-8"))
        count = len(pattern.findall(code))
        if count:
            violations[rel] = count
    assert violations == {}, (
        f"KisBroker constructed outside the allowlist {sorted(_KIS_BROKER_CONSTRUCTION_ALLOWLIST)}: "
        f"{violations}"
    )


def test_every_allowlisted_construction_site_still_exists_and_is_used():
    """Guards the allowlist itself against rot -- if a file is removed or
    stops constructing KisBroker, its allowlist entry should be removed
    too, not silently widen coverage for something else."""
    pattern = re.compile(r"\bKisBroker\(")
    for rel in _KIS_BROKER_CONSTRUCTION_ALLOWLIST:
        path = SRC_ROOT.parent / rel
        assert path.exists(), f"Allowlisted file no longer exists: {rel}"
        code = _strip_comments_and_strings_roughly(path.read_text(encoding="utf-8"))
        assert pattern.search(code), f"Allowlisted file no longer constructs KisBroker: {rel}"


# --- runtime wiring proof ----------------------------------------------------


def test_the_sanctioned_workflow_service_actually_reaches_the_real_broker_adapter(monkeypatch, tmp_path):
    """Patches KisBroker.submit_order/cancel_order and proves
    execution_workflow_service.request_submit/request_cancel -- the
    sanctioned entry point -- actually reaches them through the gateway,
    exactly once each, using the real default gateway (not a fake). A
    static allowlist alone can't prove the plumbing is connected rather
    than dead code."""
    submit_calls = []
    cancel_calls = []

    def _fake_submit(self, **kwargs):
        submit_calls.append(kwargs)
        return BrokerSubmissionResult(broker_order_id="B-ARCH-1", raw_response={"ok": True})

    def _fake_cancel(self, **kwargs):
        cancel_calls.append(kwargs)
        return BrokerOrderStatusSnapshot(
            environment="PROD", account_no="12345678-01", symbol="AAPL", status=OrderStatus.CANCELLED,
        )

    monkeypatch.setattr(broker_module.KisBroker, "submit_order", _fake_submit)
    monkeypatch.setattr(broker_module.KisBroker, "cancel_order", _fake_cancel)
    monkeypatch.setattr(
        broker_module.trading_state, "require_trading_enabled", lambda *a, **k: None
    )

    orders_path = tmp_path / "orders.json"
    # SELL/MANUAL_EXIT deliberately -- avoids needing a PreTradeRiskDecision
    # (only required for ENTRY orders), which is orthogonal to what this
    # test proves (the broker-boundary wiring, not the risk gate).
    order = execution_workflow_service.request_submit(
        source=ExecutionSource.KANBAN_BOARD, environment="PROD", account_no="12345678-01",
        symbol="AAPL", side=OrderSide.SELL, intent=OrderIntent.MANUAL_EXIT, quantity=1, limit_price=100.0,
        path=orders_path,
    )
    assert len(submit_calls) == 1
    assert order.broker_order_id == "B-ARCH-1"

    execution_workflow_service.request_cancel(
        source=ExecutionSource.KANBAN_BOARD, client_order_id=order.client_order_id, path=orders_path,
    )
    assert len(cancel_calls) == 1
