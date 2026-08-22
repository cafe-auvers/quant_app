"""Keep private environment files aligned with the tracked template.

The template owns the set and order of supported settings.  Existing values
in the gitignored ``.env`` remain machine-local and always win over template
defaults.  ``.env.pc`` is then regenerated from ``.env`` with every
``MYSQL_*`` value blanked so it is safe to use as the PC setup copy.
"""
from __future__ import annotations

import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_ASSIGNMENT_RE = re.compile(
    r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*)=(.*)$"
)

_PC_HEADER = (
    "# Generated from .env by the repository environment synchronizer.",
    "# All non-MySQL values match .env; fill the blank MYSQL_* values manually on the PC.",
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


def _render_env(template_text: str, current_text: str | None) -> tuple[str, tuple[str, ...]]:
    """Render the current file on the latest template without changing values."""

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

    # Never delete a machine-local setting merely because it is not (or is no
    # longer) documented by the template.  Keeping it visible at the end also
    # makes cleanup a deliberate operator action.
    extra_keys = [key for key in current_order if key not in template_keys]
    if extra_keys:
        if rendered and rendered[-1] != "":
            rendered.append("")
        rendered.extend(
            (
                "# --- Machine-local settings not present in .env.example ---",
                *[f"{key}={current_values[key]}" for key in extra_keys],
            )
        )

    newline = _newline_for(template_text)
    return newline.join(rendered) + newline, tuple(added_keys)


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
) -> EnvironmentSyncResult:
    """Synchronize ``.env`` and regenerate ``.env.pc`` atomically.

    Existing values in ``env_path`` are never replaced by template defaults.
    The template controls comments and ordering, and undocumented local keys
    are retained in a clearly marked final section.
    """

    template = Path(template_path).resolve()
    env = Path(env_path).resolve()
    pc_env = Path(pc_env_path).resolve()
    if not template.is_file():
        raise FileNotFoundError(f"Environment template does not exist: {template}")
    if env == pc_env:
        raise ValueError("env_path and pc_env_path must be different files")

    template_text = _read_text(template)
    current_text = _read_text(env) if env.is_file() else None
    rendered_env, added_keys = _render_env(template_text, current_text)
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
    )
