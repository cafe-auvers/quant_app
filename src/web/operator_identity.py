"""Stable identities used by browser-based operator controls."""
from __future__ import annotations

from src.services.state_sync import LocalDeviceRole, mobile_web_operator_role

from .config import WebConfig


def mobile_web_role(config: WebConfig) -> LocalDeviceRole:
    """Return the durable, non-executor identity for this private web app."""

    return mobile_web_operator_role(
        config.canonical_environment,
        config.canonical_account_no,
    )


__all__ = ["mobile_web_role"]
