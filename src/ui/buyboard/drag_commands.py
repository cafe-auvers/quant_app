"""Compatibility exports for the domain-owned Kanban command contract.

New code should import these types from :mod:`src.core.board_workflow`.  This
module remains so existing UI imports and extensions do not break.
"""
from src.core.board_workflow import (
    ActivateForToday,
    AdoptExternalOrder,
    AnyBoardCommand,
    BoardCommand,
    CancelEntry,
    CancelPartialSell,
    CancelQueuedSellAll,
    MoveToBuylist,
    MoveToWatchlist,
    ReorderCard,
    RequestPartialSell,
    RequestSellAll,
    SetBreakevenStop,
    SetManualStop,
    SetOrbStop,
    new_command_id,
)

__all__ = [
    "ActivateForToday",
    "AdoptExternalOrder",
    "AnyBoardCommand",
    "BoardCommand",
    "CancelEntry",
    "CancelPartialSell",
    "CancelQueuedSellAll",
    "MoveToBuylist",
    "MoveToWatchlist",
    "ReorderCard",
    "RequestPartialSell",
    "RequestSellAll",
    "SetBreakevenStop",
    "SetManualStop",
    "SetOrbStop",
    "new_command_id",
]
