"""Process-local ordering for durable stop changes and market-data drains.

The database remains authoritative.  This coordinator only closes the small
same-process window where the Qt thread commits a pending stop after the
runtime worker loaded its card snapshot but before that worker drains the
market-data accumulator.
"""
from __future__ import annotations

import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Iterator, Optional

from sqlalchemy.engine import Engine

from src.core.trade_card_state import BoardStatus, StopType, TradeCardState


@dataclass(frozen=True)
class CoordinatedStopChange:
    card_key: str
    command_id: str
    request_card_version: int
    stop_type: Optional[StopType]
    price: float
    quantity: int
    requested_at: datetime

    @classmethod
    def from_card(cls, card: TradeCardState) -> "CoordinatedStopChange":
        if not card.pending_stop_command_id or card.pending_stop_price is None:
            raise ValueError("Card does not contain a pending stop change")
        if card.pending_stop_requested_at is None:
            raise ValueError("Pending stop change has no durable request timestamp")
        return cls(
            card_key=card.card_key,
            command_id=card.pending_stop_command_id,
            request_card_version=int(card.version),
            stop_type=card.pending_stop_type,
            price=float(card.pending_stop_price),
            quantity=int(card.pending_stop_quantity),
            requested_at=card.pending_stop_requested_at,
        )

    def matches_active(self, card: TradeCardState) -> bool:
        return (
            card.stop_type == self.stop_type
            and card.active_stop_price is not None
            and float(card.active_stop_price) == self.price
            and int(card.stop_quantity) == self.quantity
            and not card.pending_stop_command_id
        )

    def apply_to(self, card: TradeCardState) -> None:
        card.pending_stop_type = self.stop_type
        card.pending_stop_price = self.price
        card.pending_stop_quantity = self.quantity
        card.pending_stop_command_id = self.command_id
        card.pending_stop_requested_at = self.requested_at


class StopChangeCoordinator:
    """Serialize stop commits with feed-rule rotation/evaluation."""

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._locks: Dict[str, threading.RLock] = {}
        self._pending: Dict[str, CoordinatedStopChange] = {}

    def _lock_for(self, card_key: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(str(card_key), threading.RLock())

    @contextmanager
    def lock_cards(self, card_keys: Iterable[str]) -> Iterator[None]:
        locks = [self._lock_for(key) for key in sorted(set(card_keys))]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def record_durable(self, card: TradeCardState) -> None:
        change = CoordinatedStopChange.from_card(card)
        with self.lock_cards([change.card_key]):
            with self._guard:
                self._pending[change.card_key] = change

    def overlay_pending(self, cards: Iterable[TradeCardState]) -> None:
        """Overlay a just-committed request onto an older worker snapshot."""

        with self._guard:
            pending = dict(self._pending)
        for card in cards:
            change = pending.get(card.card_key)
            if change is None:
                continue
            if card.pending_stop_command_id == change.command_id:
                change.apply_to(card)
                continue
            if change.matches_active(card):
                continue
            # A local object may already have terminated the lifecycle but
            # not persisted it yet. Never resurrect the request into an
            # ineligible lifecycle; durable reconciliation below owns
            # retiring it after the version advances.
            if card.board_status not in {
                BoardStatus.OPEN_POSITION,
                BoardStatus.PARTIAL_SELL,
            }:
                continue
            # A newer canonical state without this command proves another
            # durable write superseded or terminated the request. The normal
            # persistence paths reconcile eagerly; this is a defensive read
            # path for a coordinator surviving an unusual caller.
            if (
                int(card.version) > change.request_card_version
                and not card.pending_stop_command_id
            ):
                self.reconcile_durable(card)
                continue
            if (
                card.pending_stop_command_id
                and card.pending_stop_command_id != change.command_id
            ):
                continue
            change.apply_to(card)

    def reconcile_durable(self, card: TradeCardState) -> bool:
        """Reconcile one successful canonical card write with pending state.

        The card lock serializes this decision with a concurrent UI stop
        commit. A newer request can therefore never be removed by cleanup
        associated with the prior trade cycle.
        """

        with self.lock_cards([card.card_key]):
            with self._guard:
                change = self._pending.get(card.card_key)
                if change is None:
                    return False
                if card.pending_stop_command_id == change.command_id:
                    return False
                completed = change.matches_active(card)
                superseded = (
                    int(card.version) > change.request_card_version
                    and not card.pending_stop_command_id
                )
                if not completed and not superseded:
                    return False
                # Recheck object identity under the guard: record_durable()
                # may have installed a newer request while this caller was
                # waiting for the card lock.
                if self._pending.get(card.card_key) is not change:
                    return False
                self._pending.pop(card.card_key, None)
                return True

    # Compatibility for callers/tests from the first coordinator revision.
    def complete_if_durable(self, card: TradeCardState) -> bool:
        return self.reconcile_durable(card)

    def pending_for(self, card_key: str) -> Optional[CoordinatedStopChange]:
        with self._guard:
            return self._pending.get(card_key)


_registry_lock = threading.Lock()
_coordinators: "weakref.WeakKeyDictionary[Engine, StopChangeCoordinator]" = (
    weakref.WeakKeyDictionary()
)


def stop_change_coordinator_for(engine: Engine) -> StopChangeCoordinator:
    with _registry_lock:
        coordinator = _coordinators.get(engine)
        if coordinator is None:
            coordinator = StopChangeCoordinator()
            _coordinators[engine] = coordinator
        return coordinator
