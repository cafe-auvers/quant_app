"""Non-blocking adapter for passive Watchlist membership changes."""

from __future__ import annotations

from dataclasses import dataclass

from PyQt5.QtCore import QThread, pyqtSignal


@dataclass(frozen=True)
class PlanningMembershipRequest:
    operation: str
    symbol: str
    watchlist: object
    buylist_manager: object
    engine: object
    default_account_no: str = ""
    buffer_pct: float = 0.001
    name: str = ""
    entry_price: object = None
    source: str = ""


@dataclass(frozen=True)
class PlanningMembershipOutcome:
    request: PlanningMembershipRequest
    result: object = None
    error: str = ""


class PlanningMembershipWorker(QThread):
    """Perform the canonical SQL transition without blocking the Qt thread."""

    completed = pyqtSignal(object)

    def __init__(self, request: PlanningMembershipRequest) -> None:
        super().__init__()
        self.request = request

    def run(self) -> None:
        request = self.request
        try:
            from src.services import planning_membership_service

            if request.operation == "add":
                result = planning_membership_service.add_watchlist_candidate(
                    request.watchlist,
                    symbol=request.symbol,
                    name=request.name,
                    entry_price=request.entry_price,
                    engine=request.engine,
                    default_account_no=request.default_account_no,
                    buffer_pct=request.buffer_pct,
                )
            elif request.operation == "promote":
                result = planning_membership_service.promote_watchlist_to_buylist(
                    request.watchlist,
                    request.buylist_manager,
                    request.symbol,
                    engine=request.engine,
                    default_account_no=request.default_account_no,
                    buffer_pct=request.buffer_pct,
                )
            elif request.operation == "remove":
                result = planning_membership_service.remove_watchlist_candidate(
                    request.watchlist,
                    request.symbol,
                    engine=request.engine,
                    default_account_no=request.default_account_no,
                )
            else:
                raise ValueError(
                    f"Unsupported planning membership operation: {request.operation}"
                )
            outcome = PlanningMembershipOutcome(request=request, result=result)
        except Exception as exc:
            outcome = PlanningMembershipOutcome(request=request, error=str(exc))
        self.completed.emit(outcome)
