from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "web.example.json"
LOCAL_CONFIG = PROJECT_ROOT / "config" / "web.local.json"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "web"


class WebConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WebConfig:
    mode: str = "SANDBOX"
    host: str = "localhost"
    port: int = 8080
    base_url: str = "http://localhost:8080"
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    trusted_origins: tuple[str, ...] = (
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    )
    session_hours: int = 12
    daily_bars: int = 750
    hourly_months: int = 6
    chart_cache_max_symbols: int = 350
    pc_repository_path: str = ""
    local_mirror_path: str = ""
    pc_hourly_reads: bool = False
    watchlist_history_path: str = ""
    canonical_planning_reads: bool = False
    canonical_environment: str = "PROD"
    canonical_account_no: str = ""
    supabase_enabled: bool = False
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_allowed_user_id: str = ""
    canonical_planning_writes: bool = False
    connected_passive_operations: tuple[str, ...] = ()
    connected_operator_operations: tuple[str, ...] = ()
    data_dir: Path = DEFAULT_DATA_DIR

    @property
    def database_path(self) -> Path:
        return self.data_dir / "web_state.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "chart_cache"

    @property
    def secure_cookie(self) -> bool:
        return urlparse(self.base_url).scheme.lower() == "https"

    @property
    def resolved_pc_repository(self) -> Path | None:
        value = str(self.pc_repository_path or "").strip()
        return Path(value).expanduser().resolve() if value else None

    @property
    def resolved_local_mirror_path(self) -> str:
        configured = str(self.local_mirror_path or "").strip()
        if configured:
            return str(Path(configured).expanduser().resolve())
        repository = self.resolved_pc_repository
        if repository is None:
            return ""
        candidate = repository / "data" / "local_mirror.db"
        return str(candidate.resolve()) if candidate.is_file() else ""

    @property
    def data_mode(self) -> str:
        return "PC_MIRROR" if self.resolved_local_mirror_path else "DEMO"


def _read_json(path: Path, *, required: bool = False) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise WebConfigError(f"Web configuration does not exist: {path}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WebConfigError(f"Invalid web configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WebConfigError(f"Web configuration must be a JSON object: {path}")
    return value


def _tuple_of_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise WebConfigError(f"{name} must be a list of strings")
    result = tuple(str(item).strip() for item in value if str(item).strip())
    if not result:
        raise WebConfigError(f"{name} must not be empty")
    return result


def _from_mapping(values: Mapping[str, Any]) -> WebConfig:
    config = WebConfig(
        mode=str(values.get("mode", "SANDBOX")).strip().upper(),
        host=str(values.get("host", "localhost")).strip(),
        port=int(values.get("port", 8080)),
        base_url=str(values.get("base_url", "http://localhost:8080")).rstrip("/"),
        trusted_hosts=_tuple_of_strings(
            values.get("trusted_hosts", ["localhost", "127.0.0.1"]),
            "trusted_hosts",
        ),
        trusted_origins=_tuple_of_strings(
            values.get(
                "trusted_origins",
                ["http://localhost:8080", "http://127.0.0.1:8080"],
            ),
            "trusted_origins",
        ),
        session_hours=int(values.get("session_hours", 12)),
        daily_bars=int(values.get("daily_bars", 750)),
        hourly_months=int(values.get("hourly_months", 6)),
        chart_cache_max_symbols=int(values.get("chart_cache_max_symbols", 350)),
        pc_repository_path=str(values.get("pc_repository_path", "")).strip(),
        local_mirror_path=str(values.get("local_mirror_path", "")).strip(),
        pc_hourly_reads=bool(values.get("pc_hourly_reads", False)),
        watchlist_history_path=str(values.get("watchlist_history_path", "")).strip(),
        canonical_planning_reads=bool(
            values.get("canonical_planning_reads", False)
        ),
        canonical_environment=str(
            values.get("canonical_environment", "PROD")
        ).strip().upper(),
        canonical_account_no=str(values.get("canonical_account_no", "")).strip(),
        supabase_enabled=bool(values.get("supabase_enabled", False)),
        supabase_url=str(values.get("supabase_url", "")).strip().rstrip("/"),
        supabase_publishable_key=str(
            values.get("supabase_publishable_key", "")
        ).strip(),
        supabase_allowed_user_id=str(
            values.get("supabase_allowed_user_id", "")
        ).strip(),
        canonical_planning_writes=bool(
            values.get("canonical_planning_writes", False)
        ),
        connected_passive_operations=tuple(
            str(item).strip()
            for item in values.get("connected_passive_operations", [])
            if str(item).strip()
        ),
        connected_operator_operations=tuple(
            str(item).strip()
            for item in values.get("connected_operator_operations", [])
            if str(item).strip()
        ),
        data_dir=Path(values.get("data_dir", DEFAULT_DATA_DIR)).expanduser().resolve(),
    )
    _validate(config)
    return config


def _validate(config: WebConfig) -> None:
    if config.mode not in {"SANDBOX", "CONNECTED"}:
        raise WebConfigError("mode must be SANDBOX or CONNECTED")
    if config.host not in {"127.0.0.1", "localhost", "::1"}:
        raise WebConfigError(
            "The web process must stay loopback-bound; use Tailscale Serve for phone access"
        )
    if not 1 <= config.port <= 65535:
        raise WebConfigError("port must be between 1 and 65535")
    parsed = urlparse(config.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebConfigError("base_url must be an absolute http(s) URL")
    if not 1 <= config.session_hours <= 168:
        raise WebConfigError("session_hours must be between 1 and 168")
    if not 50 <= config.daily_bars <= 2000:
        raise WebConfigError("daily_bars must be between 50 and 2000")
    if not 1 <= config.hourly_months <= 24:
        raise WebConfigError("hourly_months must be between 1 and 24")
    if not 25 <= config.chart_cache_max_symbols <= 2000:
        raise WebConfigError("chart_cache_max_symbols must be between 25 and 2000")
    if config.canonical_environment != "PROD":
        raise WebConfigError("canonical_environment must be PROD")
    if config.pc_hourly_reads and (config.mode != "CONNECTED" or config.resolved_pc_repository is None):
        raise WebConfigError("PC hourly reads require CONNECTED mode and pc_repository_path")
    if config.canonical_planning_reads:
        if config.mode != "CONNECTED":
            raise WebConfigError("canonical planning reads require CONNECTED mode")
        if config.resolved_pc_repository is None:
            raise WebConfigError(
                "canonical planning reads require pc_repository_path"
            )
    if config.canonical_planning_writes:
        if config.mode != "CONNECTED":
            raise WebConfigError("canonical planning writes require CONNECTED mode")
        if not config.canonical_planning_reads:
            raise WebConfigError("canonical planning writes require canonical planning reads")
        if not config.connected_passive_operations:
            raise WebConfigError(
                "canonical planning writes require an explicit passive-operation allowlist"
            )
        from .connected_planning import ALLOWED_PASSIVE_OPERATIONS

        unknown = set(config.connected_passive_operations) - ALLOWED_PASSIVE_OPERATIONS
        if unknown:
            raise WebConfigError(
                "Unsupported connected passive operations: " + ", ".join(sorted(unknown))
            )
    elif config.connected_passive_operations:
        raise WebConfigError(
            "connected_passive_operations require canonical_planning_writes"
        )
    allowed_operator_operations = {
        "move_watchlist",
        "remove_buylist",
        "move_buylist",
        "activate_buy_today",
        "deactivate_buy_today",
        "cancel_entry",
        "request_partial_sell",
        "cancel_partial_sell",
        "request_sell_all",
        "cancel_sell_all",
        "set_orb_stop",
        "set_breakeven_stop",
        "set_manual_stop",
        "reorder_card",
        "publish_today_plan",
        "update_orb_settings",
    }
    unknown_operator_operations = (
        set(config.connected_operator_operations) - allowed_operator_operations
    )
    if unknown_operator_operations:
        raise WebConfigError(
            "Unsupported connected operator operations: "
            + ", ".join(sorted(unknown_operator_operations))
        )
    if config.connected_operator_operations:
        if config.mode != "CONNECTED":
            raise WebConfigError(
                "connected_operator_operations require CONNECTED mode"
            )
        if not config.canonical_planning_writes:
            raise WebConfigError(
                "connected_operator_operations require canonical planning writes"
            )
    if config.supabase_enabled:
        if config.mode != "CONNECTED":
            raise WebConfigError("Supabase requires CONNECTED mode")
        if not (
            config.supabase_url
            and config.supabase_publishable_key
            and config.supabase_allowed_user_id
        ):
            raise WebConfigError(
                "Supabase requires URL, publishable key, and one allowlisted user UUID"
            )


def load_web_config(
    path: str | Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> WebConfig:
    """Load tracked defaults, then local settings, then explicit CLI overrides."""

    values: dict[str, Any] = _read_json(DEFAULT_CONFIG, required=True)
    selected = Path(path).expanduser().resolve() if path else LOCAL_CONFIG
    values.update(_read_json(selected, required=path is not None))
    if overrides:
        values.update({key: value for key, value in overrides.items() if value is not None})
    return _from_mapping(values)


def ensure_runtime_directories(config: WebConfig) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.cache_dir.mkdir(parents=True, exist_ok=True)


def with_runtime_overrides(
    config: WebConfig, *, host: str | None = None, port: int | None = None
) -> WebConfig:
    updated = replace(
        config,
        host=host if host is not None else config.host,
        port=port if port is not None else config.port,
    )
    _validate(updated)
    return updated
