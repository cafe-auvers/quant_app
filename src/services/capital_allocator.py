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

from sqlalchemy.engine import Engine

from src.core.capital_reservation import (
    CapitalReservation,
    available_for_new_entries,
)
from src.services import capital_reservation_repository
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
    engine: Optional[Engine] = None,
) -> Optional[CapitalReservation]:
    """Section 9.3's short account-level transaction.

    Returns the new reservation, or ``None`` when there is not enough
    available capital right now -- the caller (entry-attempt engine) must
    treat that as WAITING_FOR_CAPITAL and retry later; it must never submit
    the order anyway and let KIS reject it for insufficient funds
    (section 391).

    ``engine``, when supplied (code review finding P1-12), makes the shared
    database -- not just this process's local JSON ledger -- authoritative
    for the availability check: any reservation another device made is
    merged in via :func:`src.services.capital_reservation_repository.list_active_reservations`
    before computing what's left, and the new reservation is mirrored to
    the database in addition to the local ledger. ``engine=None`` (the
    default) preserves the original local-JSON-only behavior exactly.
    """
    if requested_notional <= 0:
        raise ValueError("requested_notional must be positive")

    with _account_lock(environment, account_no):
        with exclusive_file_lock(path):
            reservations = _load_unlocked(path)
            active = find_active_reservations(reservations, environment, account_no)
            if engine is not None:
                db_active = capital_reservation_repository.list_active_reservations(
                    engine, environment=environment, account_no=account_no
                )
                # The database is authoritative for duplicate IDs. A stale
                # JSON copy must never shadow a newer cross-device value.
                active_by_id = {
                    reservation.reservation_id: reservation for reservation in active
                }
                active_by_id.update(
                    {reservation.reservation_id: reservation for reservation in db_active}
                )
                active = list(active_by_id.values())
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
            if engine is not None:
                capital_reservation_repository.save_reservation_strict(
                    engine, reservation
                )
            reservations.append(reservation)
            _save_unlocked(reservations, path)
            return reservation


def _upsert_local_reservation(
    reservations: List[CapitalReservation], reservation: CapitalReservation
) -> None:
    for index, current in enumerate(reservations):
        if current.reservation_id == reservation.reservation_id:
            reservations[index] = reservation
            return
    reservations.append(reservation)


def _mutate_reservation(
    reservation_id: str,
    mutation: Callable[[CapitalReservation], None],
    *,
    path: Path,
    engine: Optional[Engine] = None,
) -> Optional[CapitalReservation]:
    with exclusive_file_lock(path):
        reservations = _load_unlocked(path)
        reservation = next(
            (
                item
                for item in reservations
                if item.reservation_id == reservation_id
            ),
            None,
        )
        if reservation is None and engine is not None:
            reservation = capital_reservation_repository.fetch_reservation(
                engine, reservation_id
            )
        if reservation is None:
            return None

        mutation(reservation)
        if engine is not None:
            try:
                capital_reservation_repository.save_reservation_strict(
                    engine, reservation
                )
            except capital_reservation_repository.CapitalReservationVersionConflictError:
                # Another writer won. Repair the local copy from the
                # authoritative row before exposing the conflict, so the
                # next availability calculation cannot reuse stale capital.
                authoritative = capital_reservation_repository.fetch_reservation(
                    engine, reservation_id
                )
                if authoritative is not None:
                    _upsert_local_reservation(reservations, authoritative)
                    _save_unlocked(reservations, path)
                raise

        # The strict CAS advances ``version`` on success. Mirror only the
        # finalized authoritative object.
        _upsert_local_reservation(reservations, reservation)
        _save_unlocked(reservations, path)
        return reservation
    return None


def consume_reservation(
    reservation_id: str,
    notional: float,
    *,
    path: Path = RESERVATIONS_FILE,
    engine: Optional[Engine] = None,
) -> Optional[CapitalReservation]:
    """Apply a fill (section 872-874: "Consume capital corresponding to
    filled quantity")."""
    return _mutate_reservation(
        reservation_id, lambda reservation: reservation.consume(notional), path=path, engine=engine
    )


def release_reservation(
    reservation_id: str,
    *,
    path: Path = RESERVATIONS_FILE,
    engine: Optional[Engine] = None,
) -> Optional[CapitalReservation]:
    """Give back the unfilled remainder (section 876: "Release reservation
    when remainder is cancelled")."""
    return _mutate_reservation(
        reservation_id, lambda reservation: reservation.release(), path=path, engine=engine
    )


def expire_reservation(
    reservation_id: str,
    *,
    path: Path = RESERVATIONS_FILE,
    engine: Optional[Engine] = None,
) -> Optional[CapitalReservation]:
    return _mutate_reservation(
        reservation_id, lambda reservation: reservation.expire(), path=path, engine=engine
    )
