"""Safely inspect, migrate, and atomically edit KIS WebSocket symbol keys."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.kis_ws_symbol_keys import (  # noqa: E402
    DEFAULT_KIS_WS_SYMBOL_KEYS_FILE,
    LEGACY_SYMBOL_KEYS_ENV,
    KisWsSymbolKeyStore,
    KisWsSymbolKeysError,
    read_symbol_keys_file,
    update_symbol_keys_file,
)
from src.utils.config import get_env_value  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manage the gitignored, hot-reloadable KIS WebSocket symbol-key map. "
            "No command edits .env or calls KIS."
        )
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_KIS_WS_SYMBOL_KEYS_FILE,
        help=f"mapping file (default: {DEFAULT_KIS_WS_SYMBOL_KEYS_FILE})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="print the current normalized mapping")
    commands.add_parser("validate", help="validate the file and print its digest")

    set_parser = commands.add_parser("set", help="add or replace one verified key")
    set_parser.add_argument("symbol")
    set_parser.add_argument("key")

    remove_parser = commands.add_parser("remove", help="remove one symbol mapping")
    remove_parser.add_argument("symbol")

    commands.add_parser(
        "migrate-env",
        help=(
            f"copy {LEGACY_SYMBOL_KEYS_ENV} into the separate file without "
            "modifying either environment file"
        ),
    )
    return parser


def _summary(path: Path, keys: dict[str, str]) -> str:
    snapshot = KisWsSymbolKeyStore(path, legacy_json="{}").snapshot()
    return (
        f"{len(keys)} symbol key(s); file={path.resolve()}; "
        f"sha256={snapshot.sha256}"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = Path(args.file)
    try:
        if args.command == "show":
            keys = read_symbol_keys_file(path)
            print(json.dumps(keys, indent=2, sort_keys=True))
            return 0
        if args.command == "validate":
            keys = read_symbol_keys_file(path)
            print(_summary(path, keys))
            return 0
        if args.command == "set":
            keys = update_symbol_keys_file(
                set_values={args.symbol: args.key},
                path=path,
            )
            print(_summary(path, keys))
            print("The running application will discover an added/missing key on its next cycle.")
            return 0
        if args.command == "remove":
            keys = update_symbol_keys_file(
                remove_symbols=(args.symbol,),
                path=path,
            )
            print(_summary(path, keys))
            print(
                "A currently ACKed channel keeps its last-known-good key until the "
                "symbol leaves the active board."
            )
            return 0
        if args.command == "migrate-env":
            from src.services.kis_ws_symbol_keys import parse_legacy_symbol_keys

            legacy = parse_legacy_symbol_keys(
                str(get_env_value(LEGACY_SYMBOL_KEYS_ENV, "{}") or "{}")
            )
            if not legacy:
                raise KisWsSymbolKeysError(
                    f"{LEGACY_SYMBOL_KEYS_ENV} is empty; nothing to migrate"
                )
            keys = update_symbol_keys_file(
                set_values=legacy,
                path=path,
                refuse_conflicts=True,
            )
            print(_summary(path, keys))
            print(
                f"Migration copied {len(legacy)} key(s). .env and .env.pc were not modified."
            )
            return 0
    except (FileNotFoundError, KisWsSymbolKeysError, OSError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
