"""Keep credential files aligned and migrate runtime settings to JSON.

``.env.example`` owns the credential-only schema. Non-secret operational
settings live in tracked ``config/runtime.json`` defaults plus a gitignored
``config/runtime.local.json`` override. Existing runtime values are migrated
out of legacy ``.env`` files without changing their effective values.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


_ASSIGNMENT_RE = re.compile(
    r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*)=(.*)$"
)
_LEGACY_SYMBOL_KEYS_ENV = "KIS_WS_SYMBOL_KEYS_JSON"
_RETIRED_CONTROLLED_LIVE_SYMBOLS_ENV = "KIS_CONTROLLED_LIVE_SYMBOLS"
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_SYMBOL_WRITE_LOCK_TIMEOUT_SECONDS = 5.0
_STALE_SYMBOL_WRITE_LOCK_SECONDS = 30.0

_PC_HEADER = (
    "# Generated from the credential-only .env file.",
    "# Fill blank MYSQL_* credentials manually on the PC.",
    "# Runtime settings are not stored here; see config/runtime.json.",
    "# The file is gitignored and is refreshed automatically when the application starts.",
    "",
)


@dataclass(frozen=True)
class EnvironmentSyncResult:
    """Non-sensitive summary of one synchronization pass."""

    template_key_count: int
    added_env_keys: tuple[str, ...]
    env_changed: bool
    pc_env_changed: bool
    mysql_values_blanked: int
    runtime_local_changed: bool = False
    migrated_runtime_keys: tuple[str, ...] = ()
    symbol_keys_changed: bool = False
    migrated_symbol_keys: tuple[str, ...] = ()
    retired_symbols_archive_changed: bool = False
    archived_retired_symbols: tuple[str, ...] = ()


def _read_text(path: Path) -> str:
    # utf-8-sig accepts both normal UTF-8 and files written with a BOM.
    return path.read_text(encoding="utf-8-sig")


def _newline_for(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _assignment(line: str) -> re.Match[str] | None:
    return _ASSIGNMENT_RE.match(line)


def _assignments(lines: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    """Return last-value-wins right-hand sides and key encounter order."""

    values: dict[str, str] = {}
    order: list[str] = []
    for line in lines:
        match = _assignment(line)
        if match is None:
            continue
        key = match.group(2)
        if key not in values:
            order.append(key)
        values[key] = match.group(4)
    return values, order


def _render_env(
    template_text: str,
    current_text: str | None,
    *,
    runtime_keys: set[str] | None = None,
    legacy_migration_keys: set[str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Render credentials on the latest template and remove runtime keys."""

    template_lines = template_text.splitlines()
    current_lines = [] if current_text is None else current_text.splitlines()
    current_values, current_order = _assignments(current_lines)

    template_keys: set[str] = set()
    added_keys: list[str] = []
    rendered: list[str] = []
    for line in template_lines:
        match = _assignment(line)
        if match is None:
            rendered.append(line)
            continue

        key = match.group(2)
        if key in template_keys:
            raise ValueError(f"Duplicate environment key in template: {key}")
        template_keys.add(key)
        if key in current_values:
            value = current_values[key]
        else:
            value = match.group(4)
            added_keys.append(key)
        rendered.append(f"{match.group(1)}{key}{match.group(3)}={value}")

    migrated_keys = (runtime_keys or set()) | (legacy_migration_keys or set())
    # The credential template and runtime schema jointly own every file-backed
    # key. Refuse an unclassified extra rather than silently leaving an
    # operational setting in .env or deleting a possible secret.
    extra_keys = [
        key
        for key in current_order
        if key not in template_keys and key not in migrated_keys
    ]
    if extra_keys:
        raise ValueError(
            "Unclassified .env key(s); add credentials to .env.example or "
            "runtime settings to config/runtime.json: " + ", ".join(extra_keys)
        )

    newline = _newline_for(template_text)
    return newline.join(rendered) + newline, tuple(added_keys)


def _json_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    payload = json.loads(_read_text(path))
    if not isinstance(payload, dict):
        raise ValueError(f"Runtime configuration must be a JSON object: {path}")
    return {str(key): value for key, value in payload.items()}


def _env_value_text(raw: str) -> str:
    value = str(raw).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _normalize_symbol_keys(raw: object, *, source: str) -> dict[str, str]:
    """Validate the legacy map without importing application dependencies."""

    if not isinstance(raw, dict):
        raise ValueError(f"{source} must contain a JSON object")
    normalized: dict[str, str] = {}
    key_owners: dict[str, str] = {}
    for raw_symbol, raw_key in raw.items():
        symbol = str(raw_symbol or "").strip().upper()
        key = str(raw_key or "").strip()
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"{source} contains an invalid symbol")
        if (
            not key
            or len(key) > 128
            or any(char.isspace() or ord(char) < 32 for char in key)
        ):
            raise ValueError(f"{source} contains an invalid key for {symbol}")
        existing = normalized.get(symbol)
        if existing is not None and existing != key:
            raise ValueError(f"{source} contains conflicting values for {symbol}")
        prior_owner = key_owners.get(key)
        if prior_owner is not None and prior_owner != symbol:
            raise ValueError(
                f"{source} assigns one WebSocket key to multiple symbols"
            )
        normalized[symbol] = key
        key_owners[key] = symbol
    return dict(sorted(normalized.items()))


def _legacy_symbol_keys(raw: str) -> dict[str, str]:
    rendered = _env_value_text(raw)
    if not rendered:
        return {}
    try:
        payload = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{_LEGACY_SYMBOL_KEYS_ENV} is not valid JSON; the source .env was left unchanged"
        ) from exc
    return _normalize_symbol_keys(payload, source=_LEGACY_SYMBOL_KEYS_ENV)


def _normalized_symbol_list(raw: object, *, source: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{source} must contain a symbol list")
    normalized: set[str] = set()
    for raw_symbol in raw:
        symbol = str(raw_symbol or "").strip().upper()
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"{source} contains an invalid symbol")
        normalized.add(symbol)
    return tuple(sorted(normalized))


def _retired_controlled_live_symbols(raw: str) -> tuple[str, ...]:
    """Parse the old allowlist only for non-authorizing archival."""

    rendered = _env_value_text(raw)
    if not rendered:
        return ()
    if rendered.lstrip().startswith("["):
        try:
            payload = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{_RETIRED_CONTROLLED_LIVE_SYMBOLS_ENV} is not valid JSON; "
                "the source .env was left unchanged"
            ) from exc
        return _normalized_symbol_list(
            payload,
            source=_RETIRED_CONTROLLED_LIVE_SYMBOLS_ENV,
        )
    return _normalized_symbol_list(
        [item for item in re.split(r"[,;\s]+", rendered) if item],
        source=_RETIRED_CONTROLLED_LIVE_SYMBOLS_ENV,
    )


@contextmanager
def _exclusive_symbol_write_lock(path: Path) -> Iterator[None]:
    """Serialize the one-time migration with normal symbol-file writers."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _SYMBOL_WRITE_LOCK_TIMEOUT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()} {time.time()}".encode("ascii"))
        except FileExistsError:
            try:
                stale = (
                    time.time() - lock_path.stat().st_mtime
                    > _STALE_SYMBOL_WRITE_LOCK_SECONDS
                )
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for symbol-key migration lock: {lock_path}"
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except OSError:
            pass


def _migrate_legacy_symbol_keys(
    current_env_values: dict[str, str],
    path: Path,
) -> tuple[bool, tuple[str, ...]]:
    """Move the retired environment map into its canonical local JSON file."""

    if _LEGACY_SYMBOL_KEYS_ENV not in current_env_values:
        return False, ()
    legacy = _legacy_symbol_keys(current_env_values[_LEGACY_SYMBOL_KEYS_ENV])
    with _exclusive_symbol_write_lock(path):
        if path.is_file():
            try:
                existing_payload = json.loads(_read_text(path))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Existing symbol-key file is not valid JSON: {path}"
                ) from exc
            existing = _normalize_symbol_keys(
                existing_payload,
                source="Existing symbol-key file",
            )
        else:
            existing = {}
        conflicts = sorted(
            symbol
            for symbol, key in legacy.items()
            if symbol in existing and existing[symbol] != key
        )
        if conflicts:
            raise ValueError(
                "Legacy symbol-key migration refuses to overwrite conflicting "
                "reviewed value(s): " + ", ".join(conflicts)
            )
        merged = dict(existing)
        merged.update(legacy)
        rendered = json.dumps(dict(sorted(merged.items())), indent=2) + "\n"
        encoded = rendered.encode("utf-8")
        if path.is_file() and path.read_bytes() == encoded:
            return False, tuple(sorted(legacy))
        if path.is_file():
            _write_if_changed(
                path.with_suffix(path.suffix + ".bak"),
                _read_text(path),
            )
        changed = _write_if_changed(path, rendered)
    return changed, tuple(sorted(legacy))


def _archive_retired_controlled_live_symbols(
    current_env_values: dict[str, str],
    path: Path,
) -> tuple[bool, tuple[str, ...]]:
    """Preserve the retired allowlist as audit-only, never authorization."""

    if _RETIRED_CONTROLLED_LIVE_SYMBOLS_ENV not in current_env_values:
        return False, ()
    retired = _retired_controlled_live_symbols(
        current_env_values[_RETIRED_CONTROLLED_LIVE_SYMBOLS_ENV]
    )
    if not retired:
        return False, ()
    with _exclusive_symbol_write_lock(path):
        existing_symbols: tuple[str, ...] = ()
        if path.is_file():
            try:
                existing_payload = json.loads(_read_text(path))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Retired-symbol archive is not valid JSON: {path}"
                ) from exc
            if (
                not isinstance(existing_payload, dict)
                or existing_payload.get("schema_version") != 1
                or existing_payload.get("source")
                != _RETIRED_CONTROLLED_LIVE_SYMBOLS_ENV
                or existing_payload.get("retired") is not True
                or existing_payload.get("authorization_effect") is not False
            ):
                raise ValueError(
                    f"Retired-symbol archive has an unexpected schema: {path}"
                )
            existing_symbols = _normalized_symbol_list(
                existing_payload.get("symbols"),
                source="Retired-symbol archive",
            )
        merged = tuple(sorted(set(existing_symbols) | set(retired)))
        archive = {
            "schema_version": 1,
            "source": _RETIRED_CONTROLLED_LIVE_SYMBOLS_ENV,
            "retired": True,
            "authorization_effect": False,
            "symbols": list(merged),
        }
        rendered = json.dumps(archive, indent=2) + "\n"
        encoded = rendered.encode("utf-8")
        if path.is_file() and path.read_bytes() == encoded:
            return False, retired
        if path.is_file():
            _write_if_changed(
                path.with_suffix(path.suffix + ".bak"),
                _read_text(path),
            )
        changed = _write_if_changed(path, rendered)
    return changed, retired


def _render_runtime_local(
    defaults: dict[str, object],
    existing: dict[str, object],
    current_env_values: dict[str, str],
) -> tuple[str | None, tuple[str, ...]]:
    unknown = sorted(set(existing) - set(defaults))
    if unknown:
        raise ValueError(
            "Unknown local runtime configuration key(s): " + ", ".join(unknown)
        )
    merged = dict(existing)
    migrated: list[str] = []
    for key in defaults:
        if key not in current_env_values:
            continue
        merged[key] = _env_value_text(current_env_values[key])
        migrated.append(key)
    if not merged:
        return None, tuple(migrated)
    return json.dumps(merged, indent=2, sort_keys=True) + "\n", tuple(migrated)


def _render_pc_env(env_text: str) -> tuple[str, int]:
    """Copy ``.env`` while blanking all PC-specific MySQL assignments."""

    redacted_count = 0
    body: list[str] = []
    for line in env_text.splitlines():
        match = _assignment(line)
        if match is not None and match.group(2).upper().startswith("MYSQL_"):
            redacted_count += 1
            line = f"{match.group(1)}{match.group(2)}{match.group(3)}="
        body.append(line)

    newline = _newline_for(env_text)
    return newline.join((*_PC_HEADER, *body)) + newline, redacted_count


def _write_if_changed(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        elif os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def synchronize_environment_files(
    template_path: str | Path,
    env_path: str | Path,
    pc_env_path: str | Path,
    runtime_defaults_path: str | Path | None = None,
    runtime_local_path: str | Path | None = None,
    symbol_keys_path: str | Path | None = None,
    retired_symbols_archive_path: str | Path | None = None,
) -> EnvironmentSyncResult:
    """Synchronize secret files and migrate known runtime keys atomically."""

    template = Path(template_path).resolve()
    env = Path(env_path).resolve()
    pc_env = Path(pc_env_path).resolve()
    runtime_defaults = Path(
        runtime_defaults_path
        or template.parent / "config" / "runtime.json"
    ).resolve()
    runtime_local = Path(
        runtime_local_path
        or runtime_defaults.with_name("runtime.local.json")
    ).resolve()
    symbol_keys = Path(
        symbol_keys_path
        or template.parent / "data" / "kis_ws_symbol_keys.json"
    ).resolve()
    retired_symbols_archive = Path(
        retired_symbols_archive_path
        or template.parent / "data" / "retired_controlled_live_symbols.json"
    ).resolve()
    if not template.is_file():
        raise FileNotFoundError(f"Environment template does not exist: {template}")
    if not runtime_defaults.is_file():
        raise FileNotFoundError(
            f"Runtime configuration defaults do not exist: {runtime_defaults}"
        )
    if env == pc_env:
        raise ValueError("env_path and pc_env_path must be different files")

    template_text = _read_text(template)
    current_text = _read_text(env) if env.is_file() else None
    current_values, _ = _assignments(
        [] if current_text is None else current_text.splitlines()
    )
    runtime_values = _json_object(runtime_defaults)
    runtime_local_values = _json_object(runtime_local)
    secret_values, _ = _assignments(template_text.splitlines())
    overlap = sorted(set(runtime_values) & set(secret_values))
    if overlap:
        raise ValueError(
            "Configuration keys cannot be both secret and runtime settings: "
            + ", ".join(overlap)
        )
    rendered_runtime, migrated_keys = _render_runtime_local(
        runtime_values,
        runtime_local_values,
        current_values,
    )
    rendered_env, added_keys = _render_env(
        template_text,
        current_text,
        runtime_keys=set(runtime_values),
        legacy_migration_keys={
            _LEGACY_SYMBOL_KEYS_ENV,
            _RETIRED_CONTROLLED_LIVE_SYMBOLS_ENV,
        },
    )
    # Persist every migration destination first. If either write fails, leave
    # the legacy source values in .env so operational state is never lost.
    retired_symbols_archive_changed, archived_retired_symbols = (
        _archive_retired_controlled_live_symbols(
            current_values,
            retired_symbols_archive,
        )
    )
    symbol_keys_changed, migrated_symbol_keys = _migrate_legacy_symbol_keys(
        current_values,
        symbol_keys,
    )
    runtime_local_changed = (
        _write_if_changed(runtime_local, rendered_runtime)
        if rendered_runtime is not None
        else False
    )
    env_changed = _write_if_changed(env, rendered_env)

    rendered_pc_env, redacted_count = _render_pc_env(rendered_env)
    pc_env_changed = _write_if_changed(pc_env, rendered_pc_env)
    template_values, _ = _assignments(template_text.splitlines())
    return EnvironmentSyncResult(
        template_key_count=len(template_values),
        added_env_keys=added_keys,
        env_changed=env_changed,
        pc_env_changed=pc_env_changed,
        mysql_values_blanked=redacted_count,
        runtime_local_changed=runtime_local_changed,
        migrated_runtime_keys=migrated_keys,
        symbol_keys_changed=symbol_keys_changed,
        migrated_symbol_keys=migrated_symbol_keys,
        retired_symbols_archive_changed=retired_symbols_archive_changed,
        archived_retired_symbols=archived_retired_symbols,
    )
