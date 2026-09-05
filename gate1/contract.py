"""Frozen Gate-1 inputs and post-failure system-property evaluation.

This module is test/reporting infrastructure.  It has no application startup
hook and cannot enable execution.  Capstone scenarios translate observations
from the real gateway, reconciliation, lease, and market-data components into
this small neutral model so every injected failure is checked against the same
six Workstream 7 properties.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

ACTIVATION_DEFAULTS: Mapping[str, str] = {
    "TRADING_ENABLED": "false",
    # Engine availability is not broker-mutation authority.  Keeping the
    # guarded runtime available lets reconciliation, monitoring, and Kanban
    # state continue while the independent live envelope remains DISABLED.
    "BUYBOARD_ENGINE_ENABLED": "true",
    "KIS_WS_ENABLED": "false",
    "KIS_WS_PROTOCOL_VERIFIED": "false",
    "KIS_MUTATION_BUDGET_VERIFIED": "false",
    "KIS_WS_TOTAL_SUBSCRIPTION_CAPACITY": "0",
    "KIS_WS_TRADE_CHANNEL_CAPACITY": "0",
    "KIS_WS_QUOTE_CHANNEL_CAPACITY": "0",
    "KIS_SUBMIT_MUTATION_CAPACITY": "0",
    "KIS_CANCEL_MUTATION_CAPACITY": "0",
    "KIS_REPLACE_MUTATION_CAPACITY": "0",
    "KIS_LIVE_EXECUTION_MODE": "DISABLED",
    "KIS_CONTROLLED_LIVE_MAX_ENTRY_NOTIONAL": "0",
    "KIS_CONTROLLED_LIVE_MAX_ENTRY_EQUITY_FRACTION": "0",
}

REQUIRED_POST_FAILURE_PROPERTIES: tuple[str, ...] = (
    "no_duplicate_order",
    "no_unowned_cancellation",
    "no_position_quantity_below_broker_holdings",
    "no_open_broker_order_silently_forgotten",
    "no_new_entry_from_stale_data",
    "no_destructive_action_after_lease_loss",
)


@dataclass(frozen=True)
class BrokerMutationObservation:
    """One actual broker-boundary mutation observed by a scenario."""

    action: str
    client_order_id: str = ""
    target_broker_order_id: str = ""
    logical_operation_id: tuple[str, int, str, str, str] = ()
    exact_order_owned: Optional[bool] = None
    is_new_entry: bool = False
    market_data_fresh: Optional[bool] = None
    lease_current: Optional[bool] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", str(self.action or "").strip().upper())
        object.__setattr__(
            self, "client_order_id", str(self.client_order_id or "").strip()
        )
        object.__setattr__(
            self,
            "target_broker_order_id",
            str(self.target_broker_order_id or "").strip(),
        )
        if self.logical_operation_id:
            group_id, attempt_number, side, intent, symbol = self.logical_operation_id
            object.__setattr__(
                self,
                "logical_operation_id",
                (
                    str(group_id or "").strip(),
                    int(attempt_number or 0),
                    str(side or "").strip().upper(),
                    str(intent or "").strip().upper(),
                    str(symbol or "").strip().upper(),
                ),
            )


@dataclass(frozen=True)
class Gate1SystemObservation:
    """Authoritative observations captured after a failure/restart boundary."""

    mutations: Sequence[BrokerMutationObservation] = ()
    broker_open_order_ids: frozenset[str] = frozenset()
    remembered_broker_order_ids: frozenset[str] = frozenset()
    broker_holdings: Mapping[str, int] = field(default_factory=dict)
    projected_card_quantities: Mapping[str, int] = field(default_factory=dict)


def evaluate_post_failure_properties(
    observation: Gate1SystemObservation,
) -> tuple[dict[str, str], ...]:
    """Return stable, machine-readable violations of the frozen WS7 rules."""

    violations: list[dict[str, str]] = []
    submit_counts: dict[str, int] = {}
    logical_submit_counts: dict[tuple[str, int, str, str, str], int] = {}
    for mutation in observation.mutations:
        if mutation.action == "SUBMIT":
            submit_counts[mutation.client_order_id] = (
                submit_counts.get(mutation.client_order_id, 0) + 1
            )
            if mutation.logical_operation_id:
                logical_submit_counts[mutation.logical_operation_id] = (
                    logical_submit_counts.get(mutation.logical_operation_id, 0) + 1
                )
            if mutation.is_new_entry and mutation.market_data_fresh is not True:
                violations.append(
                    {
                        "property": "no_new_entry_from_stale_data",
                        "detail": (
                            f"client_order_id={mutation.client_order_id!r} crossed "
                            "the broker boundary with stale market data"
                        ),
                    }
                )
        if mutation.action == "CANCEL" and mutation.exact_order_owned is not True:
            violations.append(
                {
                    "property": "no_unowned_cancellation",
                    "detail": (
                        f"broker_order_id={mutation.target_broker_order_id!r} "
                        "was cancelled without exact ownership"
                    ),
                }
            )
        if (
            mutation.action in {"SUBMIT", "CANCEL", "REPLACE"}
            and mutation.lease_current is not True
        ):
            violations.append(
                {
                    "property": "no_destructive_action_after_lease_loss",
                    "detail": (
                        f"{mutation.action} for client_order_id="
                        f"{mutation.client_order_id!r} crossed after lease loss"
                    ),
                }
            )

    for client_order_id, count in sorted(submit_counts.items()):
        if client_order_id and count > 1:
            violations.append(
                {
                    "property": "no_duplicate_order",
                    "detail": (
                        f"client_order_id={client_order_id!r} reached the broker "
                        f"{count} times"
                    ),
                }
            )

    for logical_operation_id, count in sorted(logical_submit_counts.items()):
        if count > 1:
            violations.append(
                {
                    "property": "no_duplicate_order",
                    "detail": (
                        f"logical_operation_id={logical_operation_id!r} reached "
                        f"the broker {count} times under one or more client IDs"
                    ),
                }
            )

    forgotten = sorted(
        set(observation.broker_open_order_ids)
        - set(observation.remembered_broker_order_ids)
    )
    violations.extend(
        {
            "property": "no_open_broker_order_silently_forgotten",
            "detail": f"broker_order_id={broker_order_id!r} has no durable local audit row",
        }
        for broker_order_id in forgotten
    )

    symbols = sorted(set(observation.broker_holdings))
    for symbol in symbols:
        broker_quantity = max(0, int(observation.broker_holdings.get(symbol, 0)))
        projected_quantity = max(
            0, int(observation.projected_card_quantities.get(symbol, 0))
        )
        if projected_quantity < broker_quantity:
            violations.append(
                {
                    "property": "no_position_quantity_below_broker_holdings",
                    "detail": (
                        f"{symbol}: projected={projected_quantity}, "
                        f"broker={broker_quantity}"
                    ),
                }
            )

    return tuple(
        sorted(violations, key=lambda item: (item["property"], item["detail"]))
    )
