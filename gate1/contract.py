"""Frozen Gate-1 inputs and post-failure system-property evaluation.

This module is test/reporting infrastructure.  It has no application startup
hook and cannot enable execution.  Capstone scenarios translate observations
from the real gateway, reconciliation, lease, and market-data components into
this small neutral model so every injected failure is checked against the same
six Workstream 7 properties.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


ACTIVATION_DEFAULTS: Mapping[str, str] = {
    "TRADING_ENABLED": "false",
    "BUYBOARD_ENGINE_ENABLED": "false",
    "KIS_WS_ENABLED": "false",
    "KIS_WS_PROTOCOL_VERIFIED": "false",
    "KIS_MUTATION_BUDGET_VERIFIED": "false",
    "KIS_WS_TRADE_CHANNEL_CAPACITY": "0",
    "KIS_WS_QUOTE_CHANNEL_CAPACITY": "0",
    "KIS_SUBMIT_MUTATION_CAPACITY": "0",
    "KIS_CANCEL_MUTATION_CAPACITY": "0",
    "KIS_REPLACE_MUTATION_CAPACITY": "0",
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
    exact_order_owned: bool = True
    is_new_entry: bool = False
    market_data_fresh: bool = True
    lease_current: bool = True

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
    for mutation in observation.mutations:
        if mutation.action == "SUBMIT":
            submit_counts[mutation.client_order_id] = (
                submit_counts.get(mutation.client_order_id, 0) + 1
            )
            if mutation.is_new_entry and not mutation.market_data_fresh:
                violations.append(
                    {
                        "property": "no_new_entry_from_stale_data",
                        "detail": (
                            f"client_order_id={mutation.client_order_id!r} crossed "
                            "the broker boundary with stale market data"
                        ),
                    }
                )
        if mutation.action == "CANCEL" and not mutation.exact_order_owned:
            violations.append(
                {
                    "property": "no_unowned_cancellation",
                    "detail": (
                        f"broker_order_id={mutation.target_broker_order_id!r} "
                        "was cancelled without exact ownership"
                    ),
                }
            )
        if mutation.action in {"SUBMIT", "CANCEL", "REPLACE"} and not mutation.lease_current:
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

    forgotten = sorted(
        set(observation.broker_open_order_ids)
        - set(observation.remembered_broker_order_ids)
    )
    for broker_order_id in forgotten:
        violations.append(
            {
                "property": "no_open_broker_order_silently_forgotten",
                "detail": f"broker_order_id={broker_order_id!r} has no durable local audit row",
            }
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
