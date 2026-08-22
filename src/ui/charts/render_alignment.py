"""HTML presentation for the precomputed chart alignment overlay."""

from __future__ import annotations

import datetime as dt
import html
from typing import Mapping, Optional

from src.core.market_alignment import ContextState, MarketAlignmentSnapshot


MARKET_ALIGNMENT_OVERLAY_CSS = """
#market-alignment-overlay {
    position: absolute;
    top: 10px;
    right: 68px;
    left: auto;
    z-index: 8;
    min-width: 190px;
    max-width: min(410px, calc(100% - 24px));
    color: #e5e7eb;
    background: rgba(15, 20, 25, 0.88);
    border-right: 2px solid rgba(96, 165, 250, 0.72);
    border-radius: 2px;
    box-shadow: 0 3px 12px rgba(0, 0, 0, 0.22);
    font-size: 11px;
    line-height: 1.25;
    pointer-events: auto;
    font-variant-numeric: tabular-nums;
}
#market-alignment-compact { padding: 6px 8px 5px; }
.alignment-leadership { display: flex; align-items: baseline; gap: 7px; }
.alignment-score { color: #f9fafb; font-size: 21px; font-weight: 750; }
.alignment-label { color: #dbeafe; font-size: 12px; font-weight: 700; letter-spacing: .04em; }
.alignment-context { margin-top: 1px; color: #cbd5e1; font-weight: 650; }
.alignment-indicators { display: flex; gap: 9px; margin-top: 3px; white-space: nowrap; }
.alignment-indicator { color: #cbd5e1; }
.alignment-dot { margin-right: 3px; font-size: 10px; }
.alignment-indicator[data-state="GREEN"] .alignment-dot { color: #22c55e; }
.alignment-indicator[data-state="YELLOW"] .alignment-dot { color: #f59e0b; }
.alignment-indicator[data-state="RED"] .alignment-dot { color: #ef4444; }
.alignment-indicator[data-state="UNKNOWN"] .alignment-dot { color: #94a3b8; }
.alignment-footer { display: flex; align-items: center; gap: 6px; margin-top: 3px; }
#market-alignment-toggle {
    padding: 0;
    border: 0;
    background: transparent;
    color: #93c5fd;
    cursor: pointer;
    font: inherit;
}
.alignment-stale { color: #fbbf24; font-size: 10px; }
#market-alignment-details {
    display: none;
    max-height: min(64vh, 520px);
    overflow: auto;
    padding: 6px 9px 9px;
    border-top: 1px solid rgba(71, 85, 105, 0.7);
    background: rgba(15, 23, 42, 0.96);
}
#market-alignment-details.open { display: block; }
.alignment-section { margin-top: 7px; }
.alignment-section:first-child { margin-top: 0; }
.alignment-section-title { color: #f8fafc; font-weight: 700; margin-bottom: 2px; }
.alignment-detail-row { display: grid; grid-template-columns: minmax(128px, 1fr) auto; gap: 12px; padding: 1px 0; }
.alignment-detail-key { color: #94a3b8; }
.alignment-detail-value { color: #e2e8f0; text-align: right; }
.alignment-note { margin-top: 4px; color: #fbbf24; }
"""


MARKET_ALIGNMENT_OVERLAY_JS = """
window.initializeMarketAlignmentOverlay = function() {
    const alignmentToggle = document.getElementById('market-alignment-toggle');
    const alignmentDetails = document.getElementById('market-alignment-details');
    if (!alignmentToggle || !alignmentDetails || alignmentToggle.dataset.bound === '1') return;
    alignmentToggle.dataset.bound = '1';
    let alignmentOpen = false;
    try {
        alignmentOpen = sessionStorage.getItem('marketAlignmentDetailsOpen') === '1';
    } catch (_error) {}
    const applyAlignmentDetailsState = () => {
        alignmentDetails.classList.toggle('open', alignmentOpen);
        alignmentDetails.setAttribute('aria-hidden', alignmentOpen ? 'false' : 'true');
        alignmentToggle.setAttribute('aria-expanded', alignmentOpen ? 'true' : 'false');
        alignmentToggle.textContent = alignmentOpen ? 'Hide details ▴' : 'Details ▾';
    };
    alignmentToggle.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        alignmentOpen = !alignmentOpen;
        try {
            sessionStorage.setItem('marketAlignmentDetailsOpen', alignmentOpen ? '1' : '0');
        } catch (_error) {}
        applyAlignmentDetailsState();
    });
    applyAlignmentDetailsState();
};
window.updateMarketAlignmentOverlay = function(markup) {
    const current = document.getElementById('market-alignment-overlay');
    if (!current) return;
    current.outerHTML = String(markup || '');
    window.initializeMarketAlignmentOverlay();
};
window.initializeMarketAlignmentOverlay();
"""


def build_market_alignment_overlay(
    snapshot: Optional[MarketAlignmentSnapshot],
) -> str:
    if snapshot is None:
        return _overlay_shell(
            score="—",
            leadership_label="N/A",
            context_label="UNKNOWN",
            states={key: ContextState.UNKNOWN for key in ("MKT", "SEG", "SEC", "IND")},
            details=_missing_details(),
            stale=False,
        )

    details = snapshot.calculation_details or {}
    sections = [
        _leadership_section(snapshot, details.get("leadership", {})),
        _market_section(snapshot, details.get("market", {})),
        _segment_section(snapshot, details.get("segment", {})),
        _sector_section(snapshot, details.get("sector", {})),
        _industry_section(snapshot, details.get("industry", {})),
        _metadata_section(snapshot, details.get("metadata", {})),
    ]
    score = snapshot.displayed_score
    return _overlay_shell(
        score=str(score) if score is not None else "—",
        leadership_label=snapshot.leadership_label,
        context_label=snapshot.context_label,
        states={
            "MKT": snapshot.market_state,
            "SEG": snapshot.segment_state,
            "SEC": snapshot.sector_state,
            "IND": snapshot.industry_state,
        },
        details="".join(sections),
        stale=snapshot.is_stale,
    )


def _overlay_shell(
    *,
    score: str,
    leadership_label: str,
    context_label: str,
    states: Mapping[str, ContextState],
    details: str,
    stale: bool,
) -> str:
    indicators = []
    for label in ("MKT", "SEG", "SEC", "IND"):
        state = states.get(label, ContextState.UNKNOWN)
        state_text = state.value.title()
        dot = "○" if state is ContextState.UNKNOWN else "●"
        indicators.append(
            f'<span class="alignment-indicator" data-state="{state.value}" '
            f'title="{label}: {state_text}" aria-label="{label}: {state_text}">'
            f'<span class="alignment-dot" aria-hidden="true">{dot}</span>{label}</span>'
        )
    stale_html = (
        '<span class="alignment-stale" title="Snapshot is older than the expected completed market session">STALE</span>'
        if stale
        else ""
    )
    return (
        '<aside id="market-alignment-overlay" aria-label="Leadership and Market Context">'
        '<div id="market-alignment-compact">'
        f'<div class="alignment-leadership"><span class="alignment-score">{html.escape(score)}</span>'
        f'<span class="alignment-label">{html.escape(leadership_label)}</span></div>'
        f'<div class="alignment-context">CONTEXT: {html.escape(context_label)}</div>'
        f'<div class="alignment-indicators">{"".join(indicators)}</div>'
        '<div class="alignment-footer">'
        '<button id="market-alignment-toggle" type="button" aria-controls="market-alignment-details" aria-expanded="false">Details ▾</button>'
        f'{stale_html}</div></div>'
        f'<div id="market-alignment-details" aria-hidden="true">{details}</div>'
        '</aside>'
    )


def _missing_details() -> str:
    return _section(
        "Data",
        (
            ("Leadership Score", "N/A"),
            ("Market Context", "UNKNOWN"),
            ("Status", "No published EOD snapshot"),
        ),
    )


def _leadership_section(snapshot, values) -> str:
    basis = {
        "industry": "Industry",
        "sector_fallback": "Sector fallback",
    }.get(snapshot.peer_basis, "N/A")
    return _section(
        "Leadership",
        (
            (
                "Leadership Score",
                (
                    f"{snapshot.displayed_score} / 100"
                    if snapshot.displayed_score is not None
                    else "N/A"
                ),
            ),
            ("Market RS", _number(snapshot.market_rs)),
            ("Market RS source", _text(snapshot.market_rs_source)),
            ("Industry Peer RS", _number(snapshot.industry_peer_rs)),
            ("Peer Group", _text(snapshot.peer_group_name)),
            ("Peer Count", str(snapshot.peer_count) if snapshot.peer_count else "N/A"),
            ("Peer Basis", basis),
        ),
    )


def _market_section(snapshot, values) -> str:
    return _section(
        "Broad market",
        (
            ("Benchmark", _text(values.get("benchmark") or "SPY")),
            ("Close above SMA20", _condition(values, "close_above_sma20")),
            ("Close above SMA50", _condition(values, "close_above_sma50")),
            ("Five-day return", _percent(values.get("return_5d"))),
            ("State", snapshot.market_state.value.title()),
        ),
    )


def _segment_section(snapshot, values) -> str:
    return _section(
        "Market segment",
        (
            ("Segment", _text(snapshot.segment_name)),
            ("Proxy", _text(snapshot.segment_proxy)),
            ("Five-day return", _percent(values.get("return_5d"))),
            ("SPY five-day return", _percent(values.get("spy_return_5d"))),
            ("Close above SMA20", _condition(values, "close_above_sma20")),
            ("State", snapshot.segment_state.value.title()),
        ),
    )


def _sector_section(snapshot, values) -> str:
    return _section(
        "Sector",
        (
            ("Sector", _text(snapshot.sector_name)),
            ("Proxy", _text(snapshot.sector_proxy)),
            ("Five-day return", _percent(values.get("return_5d"))),
            ("Twenty-day percentile", _number(values.get("performance_percentile_20d"))),
            ("Outperforming SPY", _condition(values, "outperforming_spy_5d")),
            ("State", snapshot.sector_state.value.title()),
        ),
    )


def _industry_section(snapshot, values) -> str:
    return _section(
        "Industry",
        (
            ("Industry", _text(snapshot.industry_name)),
            ("Proxy/Index", _text(snapshot.industry_proxy_or_index)),
            ("Five-day return", _percent(values.get("return_5d"))),
            ("Sector five-day return", _percent(values.get("sector_return_5d"))),
            ("Twenty-day percentile", _number(values.get("performance_percentile_20d"))),
            ("State", snapshot.industry_state.value.title()),
        ),
    )


def _metadata_section(snapshot, values) -> str:
    if snapshot.is_stale:
        status = "Stale"
    elif snapshot.is_provisional:
        status = "Provisional (incomplete data)"
    else:
        status = "Complete"
    rows = [
        ("EOD as of", snapshot.as_of_date.isoformat()),
        ("Calculated at", _datetime(snapshot.calculated_at)),
        ("Feature version", snapshot.feature_version),
        ("Classification source", snapshot.classification_source),
        ("Data status", status),
    ]
    themes = values.get("themes") if isinstance(values, Mapping) else None
    if isinstance(themes, (list, tuple)) and themes:
        rows.append(("Themes", ", ".join(str(value) for value in themes)))
    note = (
        '<div class="alignment-note">Result is normalized from the available components and is not fully evaluated.</div>'
        if snapshot.is_provisional
        else ""
    )
    return _section("Metadata", rows, note=note)


def _section(title: str, rows, *, note: str = "") -> str:
    rendered = []
    for key, value in rows:
        rendered.append(
            '<div class="alignment-detail-row">'
            f'<span class="alignment-detail-key">{html.escape(str(key))}</span>'
            f'<span class="alignment-detail-value">{html.escape(str(value))}</span>'
            '</div>'
        )
    return (
        '<section class="alignment-section">'
        f'<div class="alignment-section-title">{html.escape(title)}</div>'
        f'{"".join(rendered)}{note}</section>'
    )


def _condition(values: Mapping, name: str) -> str:
    conditions = values.get("conditions", []) if isinstance(values, Mapping) else []
    for condition in conditions if isinstance(conditions, list) else []:
        if isinstance(condition, Mapping) and condition.get("name") == name:
            value = condition.get("result")
            return "Yes" if value is True else "No" if value is False else "N/A"
    return "N/A"


def _score(value: object, suffix: str = "") -> str:
    try:
        return f"{int(round(float(value)))}{suffix}"
    except (TypeError, ValueError, OverflowError):
        return "N/A"


def _number(value: object) -> str:
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError, OverflowError):
        return "N/A"


def _percent(value: object) -> str:
    try:
        return f"{float(value) * 100:+.1f}%"
    except (TypeError, ValueError, OverflowError):
        return "N/A"


def _text(value: object) -> str:
    text = str(value or "").strip()
    return text or "N/A"


def _datetime(value: object) -> str:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    return _text(value)
