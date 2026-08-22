"""Verify that every direct requirement is satisfied by requirements.lock."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


ROOT = Path(__file__).resolve().parents[1]
_LOCKED_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s\\]+)"
)


def _direct_requirements(path: Path) -> tuple[Requirement, ...]:
    requirements: list[Requirement] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            raise ValueError(
                f"{path}:{line_number}: nested requirement files are not supported"
            )
        try:
            requirements.append(Requirement(line))
        except InvalidRequirement as exc:
            raise ValueError(
                f"{path}:{line_number}: invalid requirement {line!r}: {exc}"
            ) from exc
    return tuple(requirements)


def _locked_versions(path: Path) -> dict[str, Version]:
    versions: dict[str, Version] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = _LOCKED_REQUIREMENT.match(raw_line.strip())
        if match is None:
            continue
        name = canonicalize_name(match.group("name"))
        try:
            version = Version(match.group("version"))
        except InvalidVersion as exc:
            raise ValueError(
                f"{path}:{line_number}: invalid locked version for {name}: {exc}"
            ) from exc
        previous = versions.setdefault(name, version)
        if previous != version:
            raise ValueError(
                f"{path}:{line_number}: {name} is locked more than once "
                f"({previous} and {version})"
            )
    return versions


def requirement_lock_errors(requirements_path: Path, lock_path: Path) -> tuple[str, ...]:
    """Return direct-requirement/lock inconsistencies in stable display order."""

    errors: list[str] = []
    locked = _locked_versions(lock_path)
    for requirement in _direct_requirements(requirements_path):
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        locked_version = locked.get(name)
        if locked_version is None:
            errors.append(f"{requirement.name} is missing from {lock_path.name}")
            continue
        if requirement.url is not None:
            errors.append(
                f"{requirement.name} uses a direct URL that this lock check cannot verify"
            )
            continue
        if requirement.specifier and locked_version not in requirement.specifier:
            errors.append(
                f"{requirement.name}{requirement.specifier} does not allow "
                f"locked version {locked_version}"
            )
    return tuple(errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=ROOT / "requirements.txt")
    parser.add_argument("--lock", type=Path, default=ROOT / "requirements.lock")
    args = parser.parse_args(argv)
    try:
        errors = requirement_lock_errors(args.requirements, args.lock)
    except (OSError, ValueError) as exc:
        print(f"requirements lock check failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"requirements lock check failed: {error}", file=sys.stderr)
        return 1
    print(
        f"requirements lock check passed: every direct requirement in "
        f"{args.requirements.name} is satisfied by {args.lock.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
