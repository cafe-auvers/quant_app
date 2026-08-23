"""Reject tracked local runtime state and credential-shaped files."""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_EXACT = {
    ".env",
    ".env.local",
    ".env.pc",
}
_FORBIDDEN_SUFFIXES = {
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".token",
}


def forbidden_tracked_paths(paths: list[str]) -> tuple[str, ...]:
    """Return tracked paths that must remain workstation-local."""

    forbidden: list[str] = []
    for raw_path in paths:
        normalized = raw_path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        path = PurePosixPath(normalized)
        lowered = normalized.lower()
        if lowered in _FORBIDDEN_EXACT:
            forbidden.append(normalized)
        elif path.suffix.lower() in _FORBIDDEN_SUFFIXES:
            forbidden.append(normalized)
        elif lowered.startswith(".secrets/"):
            forbidden.append(normalized)
        elif any(
            part.lower().startswith("pre_restore_backup_") for part in path.parts
        ):
            forbidden.append(normalized)
    return tuple(sorted(set(forbidden)))


def _tracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        value.decode("utf-8", errors="surrogateescape")
        for value in result.stdout.split(b"\0")
        if value
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        errors = forbidden_tracked_paths(_tracked_paths(args.root.resolve()))
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"repository hygiene check failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("repository hygiene check failed; local-only files are tracked:", file=sys.stderr)
        for path in errors:
            print(f"  - {path}", file=sys.stderr)
        return 1
    print("repository hygiene check passed: no forbidden local runtime files are tracked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
