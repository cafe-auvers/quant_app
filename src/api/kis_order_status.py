"""Placeholders for direct KIS order status/cancel operations.

The repository currently contains verified KIS order placement endpoints, but
not verified overseas order-status or cancellation endpoint/TR_ID values. These
functions intentionally do not guess production trading APIs.
"""
from __future__ import annotations

from typing import Any, Dict


def query_overseas_order_status(*args, **kwargs) -> Dict[str, Any]:
    raise NotImplementedError(
        "Direct KIS overseas order-status query is not implemented because "
        "this repository does not contain verified endpoint/TR_ID values. "
        "Use holdings-snapshot reconciliation instead."
    )


def cancel_overseas_order(*args, **kwargs) -> Dict[str, Any]:
    raise NotImplementedError(
        "Direct KIS overseas order cancellation is not implemented because "
        "this repository does not contain verified endpoint/TR_ID values. "
        "Cancel manually in KIS, then refresh/reconcile."
    )
