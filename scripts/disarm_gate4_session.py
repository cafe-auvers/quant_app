#!/usr/bin/env python3
"""Explicitly disarm a supervised Gate-4 session and record its probe.

The script may live outside the repository evidence bundle. Pass the exact
release checkout with ``--repository`` so imports and configuration always
come from the runtime being qualified.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--session-date", type=date.fromisoformat)
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="Commit/read back OFF without appending session evidence.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = args.repository.expanduser().resolve()
    if not (repository / "main.py").is_file():
        raise RuntimeError(f"Not a quant_app repository: {repository}")
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))

    from src.utils.config import install_repository_configuration

    install_repository_configuration()

    from gate4.runtime_observer import configured_collector, observe_gate4_event
    from src.core.exit_policy import market_session_date
    from src.infrastructure.database.coordination_engine import (
        init_coordination_engine,
    )
    from src.services import trading_state
    from src.services.state_sync import (
        get_live_trading_control,
        live_trading_control_is_effective,
        load_local_device_role,
        set_live_trading_control,
    )
    from src.services.trading_state import TradingDisabledError

    session_date = args.session_date or market_session_date()
    if session_date != market_session_date():
        raise RuntimeError(
            "Gate-4 disarm evidence must use the current NYSE calendar date"
        )

    collector = None if args.no_evidence else configured_collector()
    dated_events = []
    if collector is not None:
        dated_events = [
            event
            for event in collector.journal.read_all()
            if event.payload.get("session_date") == session_date.isoformat()
        ]
        if any(event.event_type == "SESSION_ENDED" for event in dated_events):
            print(f"Gate 4 session {session_date.isoformat()} is already closed.")
            return 0
        starts = sum(event.event_type == "SESSION_STARTED" for event in dated_events)
        if starts != 1:
            raise RuntimeError(
                "Gate-4 disarm evidence requires exactly one open supervised session"
            )

    engine = init_coordination_engine(ensure_schema=False, raise_on_error=True)
    try:
        result = set_live_trading_control(
            engine,
            load_local_device_role(),
            False,
        )
        if not result.success or result.control is None:
            raise RuntimeError(result.error or "Could not disable shared live trading")
        verified = get_live_trading_control(engine)
        if (
            not verified.success
            or verified.control is None
            or verified.control.enabled
            or live_trading_control_is_effective(verified.control)
        ):
            raise RuntimeError(
                verified.error or "Shared live-trading OFF read-back failed"
            )
    finally:
        engine.dispose()

    trading_state.set_trading_enabled(False)
    if collector is not None:
        if not any(event.event_type == "DISARMED" for event in dated_events):
            observe_gate4_event("DISARMED", source="OWNER_AUTHORIZED_OPERATION")
        if not any(event.event_type == "DISARM_PROBE" for event in dated_events):
            blocked = False
            try:
                trading_state.require_trading_enabled(
                    environment="PROD",
                    symbol="GATE4-DISARM-PROBE",
                )
            except TradingDisabledError:
                blocked = True
            if not blocked:
                raise RuntimeError("Gate-4 disarm probe did not block the mutation")
            observe_gate4_event(
                "DISARM_PROBE",
                next_mutation_blocked=True,
                broker_called=False,
            )

    print(
        f"Gate 4 shared trading is OFF for {session_date.isoformat()} "
        f"(revision {verified.control.revision})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
