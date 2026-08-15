"""``ExecutionBrokerProtocol`` -- what the execution gateway (Workstream 3)
needs from a real or fake broker.

``docs/kanban_production_readiness.md`` names this file explicitly as part
of PR2's interface structure, with the intent that KIS-specific transport
detail stays *outside* the gateway. :mod:`src.services.broker` already
defines exactly that boundary -- :class:`~src.services.broker.Broker`,
:class:`~src.services.broker.BrokerSubmissionResult`, and
:class:`~src.services.broker.KisBroker` -- built for the same reason
(``KisBroker`` is "the small amount of KIS-specific response/error
normalization required to keep the execution... services broker-neutral").

Deliberately **not** a second, parallel protocol: the whole reason
``order_execution_service.submit_guarded_overseas_order`` and
``order_reconciliation.cancel_and_reconcile_order`` already accept a
``broker: Optional[Broker]`` parameter is so a new implementation --
including :class:`~src.services.execution_command_gateway.ExecutionCommandGateway`
itself -- can be substituted without those functions changing at all. A
second, subtly-different protocol here would fork that boundary in two,
and the gateway would need to translate between them at exactly the point
(the real, non-idempotent broker call) where a translation bug is most
dangerous. This module instead re-exports the existing protocol under the
name this workstream's documentation uses, and is where a *fake* broker
implementation lives for PR2's own tests.

Cancellation acknowledgement reuses
:class:`~src.core.order_state.BrokerOrderStatusSnapshot` (``Broker.cancel_order``'s
existing return type) rather than introducing a parallel
``BrokerCancellationResult`` -- same reasoning: one shape for "what the
broker said about this cancel," not two.
"""
from __future__ import annotations

from src.core.order_state import BrokerOrderStatusSnapshot
from src.services.broker import Broker, BrokerSubmissionResult, KisBroker

# The name this workstream's documentation and tests use. Not a subclass or
# a redefinition -- literally the same protocol, see the module docstring.
ExecutionBrokerProtocol = Broker

__all__ = [
    "ExecutionBrokerProtocol",
    "Broker",
    "BrokerSubmissionResult",
    "BrokerOrderStatusSnapshot",
    "KisBroker",
]
