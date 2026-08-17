"""Frontend-neutral command for enabling an entry-monitoring lifecycle."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntryMonitoringCommand:
    environment: str
    account_no: str
    symbol: str
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", str(self.environment or "").upper())
        object.__setattr__(self, "account_no", str(self.account_no or ""))
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(self, "enabled", bool(self.enabled))
        if not self.account_no or not self.symbol:
            raise ValueError("EntryMonitoringCommand requires account and symbol")


def build_entry_monitoring_command(
    *, environment: str, account_no: str, symbol: str, enabled: bool = True
) -> EntryMonitoringCommand:
    return EntryMonitoringCommand(
        environment=environment,
        account_no=account_no,
        symbol=symbol,
        enabled=enabled,
    )
