"""``ExecutionOwner`` -- H1's per-``(environment, account_no, symbol)``
persisted execution ownership (Workstream 9).

``docs/kanban_production_readiness.md``, H1: "Every account+symbol has
exactly one ``execution_owner`` (``LEGACY``/``KANBAN``/``MANUAL``) and, for
``KANBAN``, a ``strategy_instance_id``. Enforced at the gateway (B2), not
by convention." H2: "During controlled rollout, Kanban owns only
explicitly assigned symbols; everything else defaults ``LEGACY``."

PR2's second-pass review (finding 5) was explicit that a lighter,
in-process mutual-exclusion registry (which only prevents two *concurrent*
calls from racing the same key within one process) is not equivalent to
this durable, cross-restart, cross-device ownership assignment, and that
substituting one for the other silently narrows Workstream 9/B2's signed
scope without a logged contract revision. This module is the actual H1
value type; :mod:`src.services.execution_ownership_repository` is its
durable persistence, and :mod:`src.services.execution_command_gateway`
enforces it as part of the B2 gate sequence.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ExecutionOwner(str, Enum):
    LEGACY = "LEGACY"
    KANBAN = "KANBAN"
    MANUAL = "MANUAL"


@dataclass
class ExecutionOwnership:
    """One row's worth of H1's assignment. ``owner`` defaults to
    ``LEGACY`` (H2: "everything else defaults LEGACY") -- this default is
    applied by the repository when no row exists yet for a given
    ``(environment, account_no, symbol)``, not encoded as a stored value,
    so an explicit ``LEGACY`` assignment and "never assigned" remain
    distinguishable in the audit trail even though they behave identically
    for gating purposes.
    """

    environment: str
    account_no: str
    symbol: str
    owner: ExecutionOwner = ExecutionOwner.LEGACY
    strategy_instance_id: str = ""
    assigned_by: str = ""
    assigned_at: Optional[str] = None
    # Zero represents H2's virtual LEGACY default when no durable row exists.
    # Persisted assignments start at version 1.
    version: int = 0

    def __post_init__(self) -> None:
        self.environment = str(self.environment or "").upper()
        self.account_no = str(self.account_no or "")
        self.symbol = str(self.symbol or "").upper()
        if isinstance(self.owner, ExecutionOwner):
            pass
        else:
            self.owner = ExecutionOwner(str(self.owner or "LEGACY").upper())
        if self.owner == ExecutionOwner.KANBAN and not str(self.strategy_instance_id or "").strip():
            raise ValueError("KANBAN ownership requires a non-blank strategy_instance_id")
        self.strategy_instance_id = str(self.strategy_instance_id or "")
        self.assigned_by = str(self.assigned_by or "")
        self.version = int(self.version or 0)
        if self.version < 0:
            raise ValueError("ownership version cannot be negative")


@dataclass(frozen=True)
class ExecutionOwnershipProof:
    """Exact healthy-DB ownership evidence carried into an outage journal."""

    environment: str
    account_no: str
    symbol: str
    owner: ExecutionOwner
    strategy_instance_id: str
    version: int

    @classmethod
    def from_ownership(cls, ownership: ExecutionOwnership) -> "ExecutionOwnershipProof":
        return cls(
            environment=ownership.environment,
            account_no=ownership.account_no,
            symbol=ownership.symbol,
            owner=ownership.owner,
            strategy_instance_id=ownership.strategy_instance_id,
            version=int(ownership.version),
        )

    def to_dict(self) -> dict:
        return {
            "environment": self.environment,
            "account_no": self.account_no,
            "symbol": self.symbol,
            "owner": self.owner.value,
            "strategy_instance_id": self.strategy_instance_id,
            "version": self.version,
        }


# H1's own mapping from the application-facing
# src.core.execution_mode.ExecutionSource to the persisted ExecutionOwner
# that source is authorized to act as -- kept here (not in execution_mode,
# which must not depend on this module) since it is specifically about
# ownership authorization, not about the source concept itself.
_SOURCE_TO_OWNER = {
    "LEGACY_BUY_DASHBOARD": ExecutionOwner.LEGACY,
    "KANBAN_BOARD": ExecutionOwner.KANBAN,
}


def owner_for_source(source_value: str) -> Optional[ExecutionOwner]:
    """``None`` for a source (e.g. ``SYSTEM``) with no fixed ownership
    mapping -- callers must decide their own policy for that case rather
    than this function guessing one."""
    return _SOURCE_TO_OWNER.get(str(source_value or "").upper())
