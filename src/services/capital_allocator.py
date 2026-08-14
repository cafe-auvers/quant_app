"""Account-level capital reservation transactions.

``buydashboard_to_kanban.md`` section 9.3:

    Acquire account capital lock
        v
    Refresh usable buying power
        v
    Subtract existing reservations
        v
    Reserve capital for the candidate
        v
    Persist reservation
        v
    Release account lock

The lock is held only for this short transaction, never until the resulting
order fills (section 372). Persistence reuses
:func:`src.utils.file_lock.exclusive_file_lock` -- the same cross-process
file-lock primitive :mod:`src.services.order_ledger` uses for the order
ledger -- rather than inventing a second locking scheme.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from src.core.capital_reservation import (
    CapitalReservation,
    available_for_new_entries,
)
from src.utils.config import DATA_DIR
from src.utils.file_lock import exclusive_file_lock
from src.utils.storage import load_json, save_json

logger = logging.getLogger(__name__)

RESERVATIONS_FILE = DATA_DIR / "capital_reservations.json"

# One in-process lock per (environment, account_no). This only serializes
# concurrent reservation attempts *within this process* -- the
# exclusive_file_lock taken around every read-modify-write below is what
# actually protects the persisted reservation ledger across processes/
# devices, exactly as the order ledger does for orders.
_account_locks: Dict[Tuple[str, str], threading.Lock] = {}
_account_locks_guard = threading.Lock()


def _account_lock(environment: str, account_no: str) -> threading.Lock:
    key = (str(environment or "").upper(), str(account_no or ""))
    with _account_locks_guard:
        lock = _account_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _account_locks[key] = lock
        return lock


def _load_unlocked(path: Path) -> List[CapitalReservation]:
    data = load_json(path, {"reservations": []})
    reservations: List[CapitalReservation] = []
    for raw in data.get("reservations", []):
        try:
            reservations.append(CapitalReservation.from_dict(raw))
        except (TypeError, ValueError) as exc:
            logger.warning("Rejected capital-reservation record: %s", exc)
    return reservations


def _save_unlocked(reservations: Iterable[CapitalReservation], path: Path) -> None:
    save_json(path, {"reservations": [r.to_dict() for r in reservations]})


def load_reservations(path: Path = RESERVATIONS_FILE) -> List[CapitalReservation]:
    with exclusive_file_lock(path):
        return _load_unlocked(path)


def find_active_reservations(
    reservations: Iterable[CapitalReservation], environment: str, account_no: str
) -> List[CapitalReservation]:
    environment = str(environment or "").upper()
    account_no = str(account_no or "")
    return [
        reservation
        for reservation in reservations
        if reservation.is_open()
        and reservation.environment == environment
        and reservation.account_no == account_no
    ]


def reserve_capital_for_entry(
    *,
    environment: str,
    account_no: str,
    symbol: str,
    attempt_group_id: str,
    requested_notional: float,
    buying_power_provider: Callable[[], float],
    path: Path = RESERVATIONS_FILE,
) -> Optional[CapitalReservation]:
    """Section 9.3's short account-level transaction.

    Returns the new reservation, or ``None`` when there is not enough
    available capital right now -- the caller (entry-attempt engine) must
    treat that as WAITING_FOR_CAPITAL and retry later; it must never submit
    the order anyway and let KIS reject it for insufficient funds
    (section 391).
    """
    if requested_notional <= 0:
        raise ValueError("requested_notional must be positive")

    with _account_lock(environment, account_no):
        with exclusive_file_lock(path):
            reservations = _load_unlocked(path)
            active = find_active_reservations(reservations, environment, account_no)
            buying_power = float(buying_power_provider() or 0.0)
            available = available_for_new_entries(buying_power, active)
            if available < requested_notional:
                logger.info(
                    "Capital reservation denied for %s %s: requested %.2f, "
                    "available %.2f (buying_power=%.2f, %d active reservation(s))",
                    environment,
                    symbol,
                    requested_notional,
                    available,
                    buying_power,
                    len(active),
                )
                return None
            reservation = CapitalReservation.create(
                environment=environment,
                account_no=account_no,
                symbol=symbol,
                attempt_group_id=attempt_group_id,
                requested_notional=requested_notional,
            )
            reservations.append(reservation)
            _save_unlocked(reservations, path)
            return reservation


def _mutate_reservation(
    reservation_id: str, mutation: Callable[[CapitalReservation], None], *, path: Path
) -> Optional[CapitalReservation]:
    with exclusive_file_lock(path):
        reservations = _load_unlocked(path)
        for reservation in reservations:
            if reservation.reservation_id != reservation_id:
                continue
            mutation(reservation)
            _save_unlocked(reservations, path)
            return reservation
    return None


def consume_reservation(
    reservation_id: str, notional: float, *, path: Path = RESERVATIONS_FILE
) -> Optional[CapitalReservation]:
    """Apply a fill (section 872-874: "Consume capital corresponding to
    filled quantity")."""
    return _mutate_reservation(
        reservation_id, lambda reservation: reservation.consume(notional), path=path
    )


def release_reservation(
    reservation_id: str, *, path: Path = RESERVATIONS_FILE
) -> Optional[CapitalReservation]:
    """Give back the unfilled remainder (section 876: "Release reservation
    when remainder is cancelled")."""
    return _mutate_reservation(
        reservation_id, lambda reservation: reservation.release(), path=path
    )


def expire_reservation(
    reservation_id: str, *, path: Path = RESERVATIONS_FILE
) -> Optional[CapitalReservation]:
    return _mutate_reservation(
        reservation_id, lambda reservation: reservation.expire(), path=path
    )
