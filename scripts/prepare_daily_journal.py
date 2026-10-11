"""Prepare a trading day's facts/charts or save a journal written in chat."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.daily_journal import prepare_daily_journal_packet, save_daily_journal_draft  # noqa: E402
from src.services.trade_journal import TradeJournalStore  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="New York trading day, YYYY-MM-DD")
    parser.add_argument("--archive", type=Path, help="Explicit private archive")
    parser.add_argument("--draft-file", type=Path, help="Finished journal prose written by the assistant")
    parser.add_argument("--comments-file", type=Path, help="The user's original chat comments")
    args = parser.parse_args(argv)
    if bool(args.draft_file) != bool(args.comments_file):
        parser.error("Saving a draft requires both --draft-file and --comments-file")
    store = TradeJournalStore(args.archive)
    if args.draft_file:
        entry = save_daily_journal_draft(
            store, args.session, draft=args.draft_file.read_text(encoding="utf-8"),
            comments=args.comments_file.read_text(encoding="utf-8"),
        )
        print(f"Journal draft saved; awaiting confirmation. Revision: {entry['revision_id']}")
        print(store.root / "days" / entry["session_date"] / "journal.html")
    else:
        print(prepare_daily_journal_packet(store, args.session))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
