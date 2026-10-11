"""Chat-written daily journals, chart packets, and approval of exact drafts."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from src.services.trade_journal import TradeJournalStore, _atomic_text, _json
from src.services.journal_context import journal_context_html, load_journal_context

WRITING_INSTRUCTIONS = (
    "Write one journal entry about this trading day in the user's first-person voice. "
    "Use their simple chat comments and the recorded facts. Use plain sentences and "
    "few adjectives. Do not invent reasons, feelings, lessons, gains, losses, or claims "
    "about the method. Attach a daily and 5-minute chart for every stock bought on "
    "this session. Distinguish recorded ORB/fill facts from the user's interpretation. "
    "Include the recorded daily NAV, cash-flow-adjusted P&L and benchmark/macro "
    "movements when available. Pending or missing financial data is not zero. "
    "Label observed account equity and derived P&L as estimates until reconciled "
    "with broker net assets/closing statements; do not claim verified NAV. "
    "Confirm external cash flows (including an explicit zero) from the user's "
    "comments before calling NAV change P&L. ETF proxies are not index levels; "
    "yield changes use basis points. Do not invent causal macro explanations. "
    "Show the whole draft with its charts for review. Publish only after the user "
    "confirms this exact draft and chooses its social destinations."
)


def _session(value: str) -> str:
    return dt.date.fromisoformat(value).isoformat()


def _day_evidence(store: TradeJournalStore, session_date: str) -> dict:
    session_date = _session(session_date)
    trades = [trade for trade in store.list_trades() if trade["session_date"] == session_date]
    buys, sells, charts, missing = [], [], [], []
    seen_symbols = set()
    for trade in sorted(trades, key=lambda row: (row.get("submitted_at") or "", row["journal_id"])):
        facts = {key: trade[key] for key in (
            "journal_id", "symbol", "side", "quantity", "average_fill_price", "environment",
            "first_fill_observed_at", "broker_execution_at", "entry_plan",
        )}
        if trade["side"] != "BUY":
            sells.append(facts)
            continue
        buys.append(facts)
        # Include one pair of day charts per purchased stock, while retaining all fills.
        key = (trade["environment"], trade["symbol"])
        if key in seen_symbols:
            continue
        seen_symbols.add(key)
        snapshots = store.snapshots(trade["journal_id"])
        directory = store.materialize(trade)
        for interval in ("1d", "5m"):
            available = [row for row in snapshots if row["interval"] == interval]
            if not available:
                missing.append(f"{trade['symbol']} {interval}")
                continue
            snapshot = next((row for row in available if row["stage"] == "end_of_day"), available[0])
            stem = f"{snapshot['stage']}-{interval}"
            path = directory / f"{stem}.png"
            if not path.exists():
                path = directory / f"{stem}.svg"
            charts.append({
                "symbol": trade["symbol"], "interval": interval,
                "stage": snapshot["stage"], "source": snapshot["source"],
                "capture_basis": snapshot["capture_basis"], "captured_at": snapshot["captured_at"],
                "bars_sha256": snapshot["bars_sha256"],
                "path": path.relative_to(store.root).as_posix(),
                "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    return {"session_date": session_date, "buys": buys, "sells": sells,
            "charts": charts, "missing_charts": missing,
            "daily_context": load_journal_context(store, session_date)}


def _evidence_signature(evidence: dict) -> str:
    return hashlib.sha256(_json(evidence).encode()).hexdigest()


def prepare_daily_journal_packet(store: TradeJournalStore, session_date: str) -> Path:
    """Prepare facts and charts for the assistant; this does not write a journal."""
    evidence = _day_evidence(store, session_date)
    path = store.root / "days" / evidence["session_date"] / "packets" / uuid4().hex[:12]
    path.mkdir(parents=True, exist_ok=True)
    _atomic_text(path / "trading_day.json", _json(evidence))
    _atomic_text(path / "writing_instructions.txt", WRITING_INSTRUCTIONS + "\n")
    for index, chart in enumerate(evidence["charts"]):
        source = store.root / chart["path"]
        shutil.copyfile(source, path / f"chart-{index+1}-{chart['interval']}{source.suffix}")
    return path


def load_daily_journal(store: TradeJournalStore, session_date: str) -> dict | None:
    with store.connect() as conn:
        row = conn.execute("SELECT payload FROM daily_journals WHERE session_date=?", (_session(session_date),)).fetchone()
    return json.loads(row["payload"]) if row else None


def daily_journal_review_state(store: TradeJournalStore, session_date: str) -> str:
    entry = load_daily_journal(store, session_date)
    if not entry:
        return "NO_DRAFT"
    if entry["evidence_signature"] != _evidence_signature(_day_evidence(store, session_date)):
        return "DRAFT_NEEDS_UPDATE"
    return entry["state"]


def _save_entry(store: TradeJournalStore, entry: dict) -> None:
    with store.connect() as conn:
        conn.execute("INSERT INTO daily_journals VALUES (?,?) ON CONFLICT(session_date) "
                     "DO UPDATE SET payload=excluded.payload", (entry["session_date"], _json(entry)))
    directory = store.root / "days" / entry["session_date"]
    _atomic_text(directory / "journal.json", _json(entry))
    _atomic_text(directory / "revisions" / f"{entry['revision_id']}.json", _json(entry))
    markdown = [entry["draft"]]
    rendered = ["<!doctype html><html><head><meta charset='utf-8'><title>Trading day journal</title>",
                "<style>body{font:16px Arial;max-width:1000px;margin:32px auto}p{line-height:1.5}svg{width:100%;height:auto}figure{margin:20px 0;break-inside:avoid}figcaption{font-size:12px;color:#555}</style></head><body>",
                f"<h1>{entry['session_date']}</h1>",
                f"<p>{html.escape(entry['draft']).replace(chr(10), '<br>')}</p>",
                journal_context_html(entry["evidence"]["daily_context"])]
    for chart in entry["evidence"]["charts"]:
        source = store.root / chart["path"]
        label = f"{chart['symbol']} — {'Daily' if chart['interval'] == '1d' else '5-minute'} chart"
        relative = Path(os.path.relpath(source, directory)).as_posix()
        markdown.append(f"\n\n![{label}](<{relative}>)\n\n{chart['capture_basis']}")
        svg_path = source.with_suffix(".svg")
        if svg_path.is_file():
            rendered.append(f"<figure>{svg_path.read_text(encoding='utf-8')}<figcaption>{html.escape(label)}. {html.escape(chart['capture_basis'])}</figcaption></figure>")
    if entry["evidence"]["missing_charts"]:
        missing = ", ".join(entry["evidence"]["missing_charts"])
        markdown.append(f"\n\nCharts unavailable: {missing}.")
        rendered.append(f"<p>Charts unavailable: {html.escape(missing)}.</p>")
    rendered.append("</body></html>")
    _atomic_text(directory / "draft.md", "".join(markdown))
    _atomic_text(directory / "journal.html", "".join(rendered))


def save_daily_journal_draft(store: TradeJournalStore, session_date: str, *,
                             comments: str, draft: str) -> dict:
    """The assistant calls this after writing from the user's chat comments."""
    if not str(draft).strip():
        raise ValueError("Journal draft is empty")
    evidence = _day_evidence(store, session_date)
    entry = {
        "session_date": evidence["session_date"], "comments": str(comments),
        "draft": str(draft).strip(), "revision_id": uuid4().hex,
        "state": "AWAITING_CONFIRMATION", "evidence": evidence,
        "evidence_signature": _evidence_signature(evidence),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "approval": None,
    }
    _save_entry(store, entry)
    return entry


def confirm_daily_journal(store: TradeJournalStore, session_date: str, *,
                          revision_id: str, confirmation: str,
                          destinations: list[str]) -> dict:
    """Call only in response to the user's explicit confirmation in chat."""
    entry = load_daily_journal(store, session_date)
    if not entry or entry["revision_id"] != revision_id:
        raise ValueError("The journal draft changed; review the current version")
    if not confirmation.strip() or not destinations or any(not value.strip() for value in destinations):
        raise ValueError("Confirmation and selected social destinations are required")
    if _evidence_signature(_day_evidence(store, session_date)) != entry["evidence_signature"]:
        raise ValueError("The trade facts or charts changed, or financial context changed; update the draft before confirmation")
    entry["state"] = "APPROVED"
    entry["approval"] = {
        "revision_id": revision_id, "confirmation": confirmation,
        "destinations": destinations, "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "draft_sha256": hashlib.sha256(entry["draft"].encode()).hexdigest(),
        "evidence_signature": entry["evidence_signature"],
    }
    _save_entry(store, entry)
    return entry


def approved_daily_publication(store: TradeJournalStore, session_date: str) -> dict:
    """Return the approved content for a publisher, with no posting side effect."""
    entry = load_daily_journal(store, session_date)
    if not entry or entry["state"] != "APPROVED" or not entry["approval"]:
        raise ValueError("The user has not confirmed this journal for publication")
    approval = entry["approval"]
    if approval["draft_sha256"] != hashlib.sha256(entry["draft"].encode()).hexdigest():
        raise ValueError("The approved text changed")
    if approval["evidence_signature"] != _evidence_signature(_day_evidence(store, session_date)):
        raise ValueError("The approved facts or charts changed, or financial context changed")
    return {"draft": entry["draft"], "charts": entry["evidence"]["charts"],
            "destinations": approval["destinations"], "revision_id": entry["revision_id"]}
