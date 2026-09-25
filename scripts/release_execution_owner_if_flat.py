#!/usr/bin/env python3
"""Release this PC's execution lease only after a strict flat-state proof."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Prove the state is flat without releasing execution ownership.",
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

    from src.core.execution_order_record import TERMINAL_EXECUTION_ORDER_STATUSES
    from src.infrastructure.database.coordination_engine import (
        init_coordination_engine,
    )
    from src.services.app_state import release_main_device_and_demote
    from src.services.execution_order_repository import list_execution_orders
    from src.services.state_sync import (
        get_live_trading_control,
        get_main_device,
        load_local_device_role,
    )
    from src.services.trade_card_repository import list_trade_cards

    engine = init_coordination_engine(ensure_schema=False, raise_on_error=True)
    try:
        control = get_live_trading_control(engine)
        if (
            not control.success
            or control.control is None
            or control.control.enabled
        ):
            raise RuntimeError(
                control.error
                or "Refusing execution-lease release while live control is not OFF"
            )

        cards = list_trade_cards(engine, environment="PROD", raise_on_error=True)
        exposure = sorted(
            card.symbol
            for card in cards
            if int(card.broker_quantity or 0) > 0
            or int(card.orderable_quantity or 0) > 0
        )
        active_orders = [
            order
            for order in list_execution_orders(engine, environment="PROD")
            if order.status not in TERMINAL_EXECUTION_ORDER_STATUSES
        ]
        if exposure or active_orders:
            raise RuntimeError(
                "Refusing execution-lease release with "
                f"exposure={exposure} active_orders={len(active_orders)}"
            )

        role = load_local_device_role()
        ownership = get_main_device(engine)
        if not ownership.success:
            raise RuntimeError(ownership.error or "Could not read execution ownership")
        owner = ownership.main_device
        if owner is None:
            released = False
            already_unclaimed = True
        else:
            if owner.device_id != role.device_id:
                raise RuntimeError("Execution lease belongs to another device")
            if args.verify_only:
                released = False
            else:
                released, _role, error = release_main_device_and_demote(
                    engine,
                    role,
                    expected_lease_token=owner.lease_token,
                    expected_lease_epoch=owner.lease_epoch,
                )
                if not released:
                    raise RuntimeError(error or "Could not release execution ownership")
            already_unclaimed = False
        if not args.verify_only and not already_unclaimed:
            verified = get_main_device(engine)
            if not verified.success or verified.main_device is not None:
                raise RuntimeError(verified.error or "Lease release read-back failed")
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "active_orders": 0,
                "already_unclaimed": already_unclaimed,
                "control_off": True,
                "exposure_cards": 0,
                "flat_verified": True,
                "released": released,
                "verify_only": args.verify_only,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
