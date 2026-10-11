"""Capture private trade evidence once, or run an unattended journal loop."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from src.infrastructure.database.coordination_engine import (  # noqa: E402
    coordination_database_configured, init_coordination_engine,
)
from src.infrastructure.database.engine import init_mysql_engine  # noqa: E402
from src.services.trade_journal import TradeJournalStore, export_journal_html, sync_trade_journal  # noqa: E402
from src.utils.redaction import scrub_sensitive_text  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="Capture until stopped; default runs once")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between captures (minimum 15)")
    parser.add_argument("--archive", type=Path, help="Private archive directory")
    parser.add_argument("--source-db", type=Path, help="Explicit SQLite recovery/test source instead of shared store")
    parser.add_argument("--no-network", action="store_true", help="Use cached chart data only")
    parser.add_argument("--export", type=Path, help="Write a standalone printable HTML journal")
    parser.add_argument("--export-only", action="store_true", help="Export existing private archive without database or market calls")
    parser.add_argument("--session", help="Optional YYYY-MM-DD filter for export (New York session)")
    args = parser.parse_args(argv)
    if args.session:
        import datetime as dt
        dt.date.fromisoformat(args.session)
    if args.export_only and (not args.export or args.watch):
        parser.error("--export-only requires --export and cannot be used with --watch")
    if args.no_network:
        os.environ["TRADE_JOURNAL_CHART_NETWORK_ENABLED"] = "false"
    store = TradeJournalStore(args.archive)
    if args.export_only:
        export_journal_html(store, args.export, args.session)
        print(f"Journal exported: {args.export}")
        return 0
    # The unattended helper can render shareable PNGs without a visible window.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtGui import QFontDatabase, QGuiApplication
    qt_app = QGuiApplication.instance() or QGuiApplication([])
    qt_app.setApplicationName("Private trade journal")
    if sys.platform == "win32":
        # The Windows offscreen plugin has no system font enumeration.
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    engine = market_engine = None
    try:
        if args.source_db:
            if not args.source_db.is_file():
                parser.error("--source-db must identify an existing SQLite database")
            engine = create_engine(f"sqlite+pysqlite:///{args.source_db.resolve().as_posix()}")
        else:
            engine = (init_coordination_engine(ensure_schema=False) if coordination_database_configured()
                      else init_mysql_engine(ensure_schema=False))
            market_engine = init_mysql_engine(log_unavailable=False, ensure_schema=False)
        if engine is None:
            print("Shared trading database unavailable; the private archive is preserved.", file=sys.stderr)
            return 1
        while True:
            try:
                result = sync_trade_journal(engine, store=store, market_engine=market_engine)
                print(f"Journal: {len(result['trades'])} orders; {result['changed']} fill updates; {len(result['errors'])} capture errors", flush=True)
                for error in result["errors"]:
                    print(f"Capture issue: {error}", file=sys.stderr)
                if args.export:
                    export_journal_html(store, args.export, args.session)
                if not args.watch:
                    return 1 if result["errors"] else 0
            except Exception as exc:
                print(f"Journal capture failed: {scrub_sensitive_text(str(exc))}", file=sys.stderr, flush=True)
                if not args.watch:
                    return 1
            time.sleep(max(15, args.interval))
    except KeyboardInterrupt:
        return 0
    finally:
        if market_engine is not None:
            market_engine.dispose()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
