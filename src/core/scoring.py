"""Deterministic technical scoring and optional AI review for stock setups."""
from __future__ import annotations

import math
import json
from typing import Dict, Any, List, Optional
import pandas as pd
import requests

from src.utils.config import get_env_value
from src.core.position_sizer import PositionSizer


def calculate_deterministic_scores(
    symbol: str,
    history: pd.DataFrame,
    entry_price: Optional[float] = None,
    target_price: Optional[float] = None,
    breakout_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    account_size: float = 10000.0,
    risk_percent: float = 0.01,
) -> Dict[str, Any]:
    """
    Calculate deterministic technical, setup, risk, and timing scores,
    and identify any hard reject reasons.
    """
    symbol = symbol.strip().upper()
    
    # 1. Fallback / Default prices
    if history.empty:
        return {
            "price": entry_price or 0.0,
            "technical_score": 0.0,
            "setup_score": 0.0,
            "risk_score": 0.0,
            "timing_score": 0.0,
            "rr": 0.0,
            "stop_adr": 0.0,
            "position_percent": 0.0,
            "shares": 0,
            "warnings": ["No price history available"],
            "status": "REJECTED",
        }
        
    latest_bar = history.iloc[-1]
    price = float(latest_bar["Close"])
    volume = float(latest_bar["Volume"])
    dollar_volume = price * volume
    
    if entry_price is None or entry_price <= 0:
        entry_price = price
        
    # Calculate ADR (20-day Average Daily Range %)
    prev_close = history["Close"].astype(float).shift(1)
    high_low_ratio = (history["High"].astype(float) - history["Low"].astype(float)) / prev_close
    adr_percent_series = high_low_ratio.rolling(20, min_periods=5).mean() * 100.0
    adr_percent = float(adr_percent_series.iloc[-1]) if not pd.isna(adr_percent_series.iloc[-1]) else 2.5
    
    # Default Stop Loss (if not provided, default to entry - 0.75 * ADR_percent as price)
    if stop_loss is None or stop_loss <= 0:
        stop_loss = entry_price * (1.0 - (0.75 * adr_percent / 100.0))
        
    risk_per_share = entry_price - stop_loss
    if (breakout_price is None or breakout_price <= 0) and target_price and target_price > 0:
        breakout_price = target_price
        
    # Recalculate risk per share and stop/ADR fit. Profit exits are rule-based,
    # not fixed target or R/R based.
    risk_per_share = max(0.001, entry_price - stop_loss)
    rr = 0.0
    stop_loss_percent = (risk_per_share / entry_price) * 100.0
    stop_adr = stop_loss_percent / adr_percent if adr_percent > 0 else 0.0
    
    # Moving Averages
    close_series = history["Close"].astype(float)
    ema_20 = close_series.ewm(span=20, adjust=False).mean().iloc[-1]
    ema_50 = close_series.ewm(span=50, adjust=False).mean().iloc[-1]
    
    # Trend Intensity (7 SMA / 65 SMA)
    sma_7 = close_series.rolling(7, min_periods=1).mean()
    sma_65 = close_series.rolling(65, min_periods=1).mean()
    ti65 = float(sma_7.iloc[-1] / sma_65.iloc[-1]) if sma_65.iloc[-1] > 0 else 1.0
    
    # Position Sizing
    sizer = PositionSizer(account_size=account_size, max_risk_per_trade=risk_percent)
    sizing = sizer.size_risk_based(entry_price=entry_price, stop_loss_price=stop_loss, risk_percent=risk_percent)
    shares = sizing.shares
    capital_percent = sizing.percent_of_account * 100.0
    
    # 2. Hard reject evaluation
    warnings = []
    
    if price < ema_50:
        warnings.append("Price is below 50-day EMA")
    if adr_percent < 2.4:
        warnings.append(f"ADR 20-day ({adr_percent:.2f}%) is below 2.4% threshold")
    if volume < 40000:
        warnings.append(f"Daily volume ({volume:,.0f}) is below 40,000 shares")
    if dollar_volume < 35000:
        warnings.append(f"Daily dollar volume (${dollar_volume:,.2f}) is below $35,000")
    if stop_loss_percent >= adr_percent:
        warnings.append(f"Stop loss % ({stop_loss_percent:.2f}%) is wider than ADR 20-day ({adr_percent:.2f}%)")
    if capital_percent >= 30.0:
        warnings.append(f"Capital allocation ({capital_percent:.2f}%) exceeds hard limit of 30%")
    if shares < 1:
        warnings.append("Position size calculation resulted in 0 shares")
        


    # 3. Component Scores
    # Technical Score (out of 100)
    tech_score = 0.0
    if price > ema_20:
        tech_score += 30.0
    if price > ema_50:
        tech_score += 30.0
    if ti65 >= 1.05:
        tech_score += 40.0
    elif ti65 >= 1.0:
        tech_score += 20.0
        
    # Setup Score (out of 100)
    setup_score = 0.0
    if adr_percent >= 2.4:
        setup_score += 30.0
        
    # 1-month growth proxy
    if len(close_series) >= 22:
        month_growth = (close_series.iloc[-1] / close_series.iloc[-22] - 1.0) * 100.0
    else:
        month_growth = 0.0
    if month_growth >= 15.0:
        setup_score += 35.0
    elif month_growth >= 5.0:
        setup_score += 15.0
        
    # Tight consolidation proxy (10-day range <= 1.5 * ADR)
    if len(close_series) >= 10:
        range_10d = (close_series.iloc[-10:].max() - close_series.iloc[-10:].min()) / price * 100.0
        if range_10d <= 1.5 * adr_percent:
            setup_score += 35.0
        elif range_10d <= 2.5 * adr_percent:
            setup_score += 15.0
    else:
        setup_score += 15.0
        
    # Risk Score (out of 100)
    risk_score = 0.0
    if stop_loss_percent < adr_percent:
        risk_score += 45.0
        
    # Capital allocation close to 17.5%
    capital_score = max(0.0, 40.0 - abs(capital_percent - 17.5) * 2.0)
    risk_score += capital_score
    if shares >= 1:
        risk_score += 15.0
    
    # Timing Score (out of 100)
    timing_score = 0.0
    if len(close_series) >= 10:
        high_10d = close_series.iloc[-10:].max()
        if price >= high_10d * 0.97:
            timing_score += 50.0
            
    # Rising 7 SMA
    if len(sma_7) >= 2 and sma_7.iloc[-1] > sma_7.iloc[-2]:
        timing_score += 50.0
        
    return {
        "price": price,
        "technical_score": round(tech_score, 1),
        "setup_score": round(setup_score, 1),
        "risk_score": round(risk_score, 1),
        "timing_score": round(timing_score, 1),
        "rr": round(rr, 2),
        "stop_adr": round(stop_adr, 2),
        "position_percent": round(capital_percent, 1),
        "shares": shares,
        "warnings": warnings,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target_price": 0.0,
        "breakout_price": breakout_price,
        "adr_percent": adr_percent,
        "volume": volume,
        "above_20_ema": bool(price > ema_20),
        "above_50_ema": bool(price > ema_50),
        "risk_percent": round((shares * risk_per_share / account_size) * 100.0, 2) if account_size > 0 else 0.0,
        "trade_plan": f"Buy {shares:,.0f} shares @ ${entry_price:.2f}" if shares > 0 else "No shares (0 size)",
    }


def fetch_recent_news_headlines(symbol: str) -> list:
    """Fetch the latest 2 stock news headlines from Google News RSS feed."""
    import sys
    if "pytest" in sys.modules:
        return []
    import requests
    import xml.etree.ElementTree as ET
    try:
        url = f"https://news.google.com/rss/search?q={symbol}+stock"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            root = ET.fromstring(response.text)
            titles = []
            for item in root.findall(".//item")[:2]:
                title = item.find("title").text
                if " - " in title:
                    title = title.rsplit(" - ", 1)[0]
                titles.append(title.strip())
            return titles
    except Exception:
        pass
    return []


def run_ai_review(
    symbol: str,
    metrics: Dict[str, Any] = None,
    reasoning: str = "",
    company_name: str = "",
    as_of_date: str = "",
    current_price: float = 0.0,
    scanner_metrics_json: str = "",
    technical_indicators_json: str = "",
    chart_notes: str = "",
    trade_plan_json: str = "",
    account_risk_json: str = "",
    recent_news_json: str = "",
    fundamental_summary_json: str = "",
    market_context_json: str = "",
    user_notes: str = "",
) -> Dict[str, Any]:
    """
    Query OpenAI to get a quantitative swing-trading analyst report.
    Falls back to a rulebook-based summary if no API key is configured or request fails.
    """
    import datetime as dt
    if not company_name:
        company_name = symbol
    if not as_of_date:
        as_of_date = dt.date.today().isoformat()
    if current_price <= 0 and metrics:
        current_price = metrics.get("price", 0.0)
        
    if not scanner_metrics_json:
        if metrics:
            scanner_metrics_json = json.dumps({
                "volume": metrics.get("volume"),
                "adr_20_pct": metrics.get("adr_percent"),
                "dollar_volume": metrics.get("price", 0.0) * metrics.get("volume", 0.0) if metrics.get("volume") else None
            }, indent=2)
        else:
            scanner_metrics_json = "{}"
            
    if not technical_indicators_json:
        if metrics:
            technical_indicators_json = json.dumps({
                "above_20_ema": metrics.get("above_20_ema"),
                "above_50_ema": metrics.get("above_50_ema"),
                "ema_20": metrics.get("ema_20"),
                "ema_50": metrics.get("ema_50")
            }, indent=2)
        else:
            technical_indicators_json = "{}"
            
    if not recent_news_json:
        headlines = fetch_recent_news_headlines(symbol)
        recent_news_json = json.dumps(headlines, indent=2)
    else:
        try:
            headlines = json.loads(recent_news_json)
        except Exception:
            headlines = [recent_news_json]
        
    if not user_notes:
        user_notes = reasoning

    api_key = get_env_value("OPENAI_API_KEY")
    if not api_key:
        return _generate_fallback_ai_review(symbol, metrics or {}, headlines)
        
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    prompt = f"""
You are a senior quantitative swing-trading analyst embedded inside a rule-based stock dashboard.

Your job is to review watchlist candidates and decide whether each stock should be:
1. BUYLIST_READY
2. WATCH_ONLY
3. REJECT
4. NEEDS_MORE_DATA

You are not giving investment advice. You are producing a structured trade-readiness assessment based only on the data provided.

==================================================
STRATEGY CONTEXT
==================================================

The trading system is daily swing trading for US common stocks.

Valid setup types:
- BREAKOUT
- EPISODIC_PIVOT
- UNKNOWN / NOT_CLASSIFIED

General universe preference:
- US common stocks only
- Avoid OTC, ADRs, preferred shares, warrants, illiquid names, bankruptcy/delisting risk, reverse split risk, and structurally weak stocks.

Scanner baseline:
- Price history > 1 day
- Daily volume > 40,000 shares
- Daily dollar volume > $35,000
- ADR 20-day > 2.4%
- 1-month price growth rank >= 97.04
- Trend intensity score > 90

Breakout setup requirements:
- Price above 20-day moving average
- Price above 50-day EMA
- Strong recent momentum
- Recent consolidation or tight pullback
- Breakout above recent high/consolidation level
- Volume expansion versus recent average
- No parabolic extension
- Stop location must be technically logical and not too wide

Episodic pivot requirements:
- A clear discrete catalyst, such as earnings surprise, guidance raise, regulatory event, sector macro trigger, or other material news
- Catalyst must be recent, specific, and strong enough to explain institutional interest
- Price remains in a constructive bullish context
- Volume supports the move
- Support, entry zone, stop, and rule-based exit plan must be identifiable
- If no current news/catalyst data is provided, do not invent one

Risk and exit rules:
- Maximum position size: 25% of account value
- Standard account risk: 0.25% to 2%
- Aggressive episodic pivot risk may reach 4% only if thesis, liquidity, catalyst, and execution quality are exceptional
- Stop must be defined before entry
- Stop should not be moved lower after entry
- For momentum/breakout trades, stop distance should generally be <= ADR 20-day
- For episodic pivots, wider stops require explicit structural justification
- No fixed profit target is used. First partial exit is 1/3 to 1/2 after 3-5 days if the trade has worked.
- Remaining position is held while momentum continues. Final exit is a close below the selected 10 EMA or 20 EMA.

==================================================
INPUT DATA
==================================================

Analyze the following stock candidate data.

Ticker:
{symbol}

Company name:
{company_name}

As-of date:
{as_of_date}

Current price:
{current_price}

Scanner metrics:
{scanner_metrics_json}

Technical indicators:
{technical_indicators_json}

Chart/context notes from user or dashboard:
{chart_notes}

Trade plan, if available:
{trade_plan_json}

Account/risk data, if available:
{account_risk_json}

Recent news/catalyst data, if available:
{recent_news_json}

Fundamental summary, if available:
{fundamental_summary_json}

Market/sector context, if available:
{market_context_json}

Existing user notes:
{user_notes}

==================================================
ANALYSIS TASK
==================================================

Analyze the candidate in this order:

1. Data sufficiency
   - Identify missing fields that materially affect the decision.
   - If current price, volume, ADR, trend, stop, breakout level, or catalyst data is missing, flag it.

2. Setup classification
   - Classify as BREAKOUT, EPISODIC_PIVOT, UNKNOWN, or INVALID.
   - Explain why.

3. Technical quality
   - Evaluate trend, relative strength, price versus moving averages, consolidation quality, breakout quality, volume confirmation, extension risk, and support/resistance.
   - Distinguish between:
     - filter passed
     - setup forming
     - trigger confirmed
     - entry valid now

4. Catalyst/news quality
   - If news is provided, summarize the catalyst and judge whether it is strong, moderate, weak, or irrelevant.
   - If no news is provided, state “No verified catalyst data provided.”
   - Do not fabricate news, earnings results, analyst ratings, FDA events, guidance, or filings.

5. Risk and execution
   - Evaluate entry trigger, structural breakout level, stop, stop distance %, ADR fit, position size %, and account risk %.
   - If the stop is too tight, too wide, or technically invalid, explain.
   - Evaluate the rule-based exit plan: partial after 3-5 working days if the trade has worked, final exit below selected EMA.

6. Disqualifiers
   - Identify hard disqualifiers.
   - Hard disqualifiers include:
     - insufficient liquidity
     - price below key trend structure
     - no valid setup
     - parabolic extension without consolidation
     - stop distance invalid
     - missing catalyst for episodic pivot
     - unclear support/stop
     - severe negative news
     - earnings gap risk without plan

7. Score the stock
   Use the following scoring model:
   - Technical setup quality: 0-35
   - Relative strength / momentum: 0-20
   - Volume / liquidity / execution quality: 0-15
   - Catalyst / fundamental support: 0-15
   - Risk, stop, and exit-plan quality: 0-15

   Total score must be 0-100.

8. Final decision
   Use these decision rules:
   - BUYLIST_READY only if:
     - setup is valid
     - no hard disqualifier exists
     - stop is technically valid
     - total score >= 75
     - entry trigger is confirmed or clearly defined
   - WATCH_ONLY if:
     - setup is promising but entry trigger, volume confirmation, catalyst, or risk location is not ready
   - REJECT if:
     - hard disqualifier exists or setup is invalid
   - NEEDS_MORE_DATA if:
     - important data is missing and decision cannot be made safely

==================================================
OUTPUT FORMAT
==================================================

Return only valid JSON.

Use this exact schema:

{{
  "symbol": "string",
  "as_of_date": "string",
  "decision": "BUYLIST_READY | WATCH_ONLY | REJECT | NEEDS_MORE_DATA",
  "setup_type": "BREAKOUT | EPISODIC_PIVOT | UNKNOWN | INVALID",
  "total_score": 0,
  "score_breakdown": {{
    "technical_setup_quality": 0,
    "relative_strength_momentum": 0,
    "volume_liquidity_execution": 0,
    "catalyst_fundamental_support": 0,
    "risk_stop_exit_quality": 0
  }},
  "confidence": 0.0,
  "summary": "One concise paragraph explaining the decision.",
  "technical_assessment": {{
    "trend_status": "string",
    "relative_strength_status": "string",
    "volume_status": "string",
    "setup_structure": "string",
    "entry_trigger_status": "string",
    "extension_risk": "LOW | MODERATE | HIGH | UNKNOWN"
  }},
  "risk_assessment": {{
    "entry_price": null,
    "stop_loss": null,
    "take_profit": null,
    "stop_distance_pct": null,
    "adr_20_pct": null,
    "position_size_pct": null,
    "account_risk_pct": null,
    "risk_valid": true,
    "risk_comments": "string"
  }},
  "catalyst_assessment": {{
    "has_verified_catalyst": true,
    "catalyst_strength": "STRONG | MODERATE | WEAK | NONE | UNKNOWN",
    "catalyst_summary": "string",
    "source_quality": "PRIMARY | SECONDARY | USER_PROVIDED | NONE | UNKNOWN"
  }},
  "hard_disqualifiers": [
    "string"
  ],
  "soft_warnings": [
    "string"
  ],
  "missing_data": [
    "string"
  ],
  "recommended_action": {{
    "action": "ADD_TO_BUYLIST | KEEP_ON_WATCHLIST | REMOVE_FROM_WATCHLIST | REQUEST_MORE_DATA",
    "entry_condition": "string",
    "stop_condition": "string",
    "exit_logic": "string",
    "next_check": "string"
  }},
  "ai_notes_for_user": [
    "string"
  ]
}}
"""
    
    data = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "You are a professional stock trading reviewer following Qullamaggie style breakout rules."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }
    
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data, timeout=10)
        if response.status_code == 200:
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            
            decision = parsed.get("decision", "WATCH_ONLY")
            summary_paragraph = parsed.get("summary", "")
            action = parsed.get("recommended_action", {}).get("action", "")
            entry_cond = parsed.get("recommended_action", {}).get("entry_condition", "")
            
            status_map = {
                "BUYLIST_READY": "BUY_READY",
                "WATCH_ONLY": "WATCHING",
                "REJECT": "REJECTED",
                "NEEDS_MORE_DATA": "WATCHING"
            }
            status = status_map.get(decision, "WATCHING")
            
            summary_text = f"Decision: {decision} ({status})\nAction: {action}\nEntry: {entry_cond}\nSummary: {summary_paragraph}"
            
            cat_summary = parsed.get("catalyst_assessment", {}).get("catalyst_summary", "")
            has_cat = parsed.get("catalyst_assessment", {}).get("has_verified_catalyst", False)
            cat_text = f"- {cat_summary}" if has_cat and cat_summary else "- No verified catalyst data provided."
            
            news_score = float(parsed.get("score_breakdown", {}).get("catalyst_fundamental_support", 10.0)) * (100.0 / 15.0) if parsed.get("score_breakdown") else 80.0
            
            return {
                "summary": summary_text,
                "catalyst": cat_text,
                "news_score": news_score,
                "status": status,
                "total_score": parsed.get("total_score", 50),
                "full_json": parsed
            }
        else:
            return _generate_fallback_ai_review(symbol, metrics or {}, headlines, prefix=f"[API Error {response.status_code}] ")
    except Exception as e:
        return _generate_fallback_ai_review(symbol, metrics or {}, headlines, prefix=f"[Request Fail] ")


def _generate_fallback_ai_review(symbol: str, metrics: Dict[str, Any], headlines: list = None, prefix: str = "") -> Dict[str, Any]:
    """Generates a rule-based fallback review when AI is not available or fails."""
    import datetime as dt
    rejections = metrics.get("warnings", [])
    
    if not headlines:
        if rejections:
            summary = f"{prefix}Setup has active violations: {rejections[0]}."
            news_score = 50.0
        else:
            summary = f"{prefix}Clean bullish setup above 50 EMA with rule-based exit plan."
            news_score = 80.0
        return {
            "summary": summary,
            "catalyst": "",
            "news_score": news_score,
            "status": "REJECTED" if rejections else "BUY_READY",
            "total_score": int(metrics.get("total_score", 70.0)),
            "full_json": {}
        }
    
    # 1. Determine decision
    ema_20 = metrics.get("ema_20", 0.0)
    ema_50 = metrics.get("ema_50", 0.0)
    price = metrics.get("price", 0.0)
    
    is_extended = False
    if ema_20 > 0:
        if (price / ema_20 - 1.0) > 0.08:
            is_extended = True
    stop_adr = metrics.get("stop_adr", 0.0)
    if stop_adr >= 1.0:
        is_extended = True
        
    if rejections:
        if is_extended:
            decision = "REJECT"
            status = "REJECTED"
            reason = f"Setup is too extended or stop is too wide ({rejections[0]})."
        else:
            decision = "REJECT"
            status = "REJECTED"
            reason = f"Fails basic guidelines: {rejections[0]}."
    elif price < ema_50:
        decision = "REJECT"
        status = "REJECTED"
        reason = "Trading below the 50 EMA (bearish trend)."
    elif is_extended:
        decision = "WATCH_ONLY"
        status = "WATCHING"
        reason = "Setup is too extended from the 20 EMA."
    else:
        decision = "BUYLIST_READY"
        status = "BUY_READY"
        reason = "Clean consolidation setup above key EMAs."
        
    suggested_entry = metrics.get("entry_price", price)
    if suggested_entry <= 0:
        suggested_entry = price
        
    # Standard values
    adr = metrics.get("adr_percent", 2.5)
    total_score = metrics.get("total_score", 70.0)
    setup_type = "BREAKOUT" if not rejections else "INVALID"
    
    # Format fallback JSON matching the schema
    fallback_json = {
        "symbol": symbol,
        "as_of_date": dt.date.today().isoformat(),
        "decision": decision,
        "setup_type": setup_type,
        "total_score": int(total_score),
        "score_breakdown": {
            "technical_setup_quality": 20 if decision == "BUYLIST_READY" else 10,
            "relative_strength_momentum": 15 if decision == "BUYLIST_READY" else 8,
            "volume_liquidity_execution": 12 if decision == "BUYLIST_READY" else 5,
            "catalyst_fundamental_support": 12 if headlines else 5,
            "risk_stop_exit_quality": 12 if decision == "BUYLIST_READY" else 5
        },
        "confidence": 0.85 if decision == "BUYLIST_READY" else 0.50,
        "summary": f"{prefix}{reason}",
        "technical_assessment": {
            "trend_status": "UPTREND" if price > ema_50 else "DOWNTREND",
            "relative_strength_status": "STRONG" if price > ema_20 else "MODERATE",
            "volume_status": "NORMAL",
            "setup_structure": "CONSOLIDATION" if is_extended else "TIGHT",
            "entry_trigger_status": "WAITING" if is_extended else "CONFIRMED",
            "extension_risk": "HIGH" if is_extended else "LOW"
        },
        "risk_assessment": {
            "entry_price": suggested_entry,
            "stop_loss": metrics.get("stop_loss", 0.0),
            "take_profit": None,
            "stop_distance_pct": metrics.get("stop_loss_percent", 0.0),
            "adr_20_pct": adr,
            "position_size_pct": metrics.get("position_percent", 0.0),
            "account_risk_pct": metrics.get("risk_percent", 0.0),
            "risk_valid": not bool(rejections),
            "risk_comments": ", ".join(rejections) if rejections else "Risk profile acceptable."
        },
        "catalyst_assessment": {
            "has_verified_catalyst": bool(headlines),
            "catalyst_strength": "MODERATE" if headlines else "NONE",
            "catalyst_summary": headlines[0] if headlines else "No verified catalyst data provided.",
            "source_quality": "SECONDARY" if headlines else "NONE"
        },
        "hard_disqualifiers": rejections,
        "soft_warnings": [],
        "missing_data": [],
        "recommended_action": {
            "action": "ADD_TO_BUYLIST" if decision == "BUYLIST_READY" else "KEEP_ON_WATCHLIST",
            "entry_condition": f"Breakout above recent consolidation level near ${suggested_entry:.2f}.",
            "stop_condition": f"Close below ${metrics.get('stop_loss', 0.0):.2f}.",
            "exit_logic": "No fixed target. Sell 1/3 to 1/2 after 3-5 working days if the trade has worked; exit the rest on a close below the selected 10 EMA or 20 EMA.",
            "next_check": "Next daily close."
        },
        "ai_notes_for_user": [
            "Generated via rulebook-based fallback scoring model.",
            "Please verify trend intensity and volume breakout metrics on daily chart."
        ]
    }
    
    summary_text = f"Decision: {decision} ({status})\nAction: {fallback_json['recommended_action']['action']}\nEntry: {fallback_json['recommended_action']['entry_condition']}\nSummary: {fallback_json['summary']}"
    cat_text = f"- {fallback_json['catalyst_assessment']['catalyst_summary']}"
    
    return {
        "summary": summary_text,
        "catalyst": cat_text,
        "news_score": 50.0 if rejections else 80.0,
        "status": status,
        "total_score": int(total_score),
        "full_json": fallback_json
    }


def render_quant_analysis_html(data: dict) -> str:
    """Generates a premium dark-themed HTML report for the quant AI sidebar."""
    symbol = data.get("symbol", "N/A")
    date = data.get("as_of_date", "N/A")
    decision = data.get("decision", "UNKNOWN")
    setup = data.get("setup_type", "UNKNOWN")
    score = data.get("total_score", 0)
    summary = data.get("summary", "")
    
    # Badge color selection
    bg_color = "#333333"
    if decision == "BUYLIST_READY":
        bg_color = "#2e7d32" # Green
    elif decision == "WATCH_ONLY":
        bg_color = "#1565c0" # Blue
    elif decision == "REJECT":
        bg_color = "#c62828" # Red
    elif decision == "NEEDS_MORE_DATA":
        bg_color = "#ef6c00" # Orange
        
    breakdown = data.get("score_breakdown", {})
    tech = data.get("technical_assessment", {})
    risk = data.get("risk_assessment", {})
    cat = data.get("catalyst_assessment", {})
    disq = data.get("hard_disqualifiers", [])
    warn = data.get("soft_warnings", [])
    missing = data.get("missing_data", [])
    rec = data.get("recommended_action", {})
    notes = data.get("ai_notes_for_user", [])
    
    # Technical quality row mapping
    tech_rows = ""
    for k, v in tech.items():
        tech_rows += f"<tr><td style='padding: 4px 0;'><b>{k.replace('_', ' ').title()}:</b></td><td style='text-align: right; color: #4fc3f7; padding: 4px 0;'>{v}</td></tr>"
        
    # Risk quality row mapping
    risk_rows = ""
    for k, v in risk.items():
        if k in ("risk_valid", "risk_comments", "risk_reward_ratio", "take_profit"):
            continue
        v_str = f"${v:.2f}" if isinstance(v, (int, float)) and ("price" in k or k == "stop_loss" or k == "take_profit") else str(v)
        if isinstance(v, (int, float)) and "pct" in k:
            v_str = f"{v:.2f}%"
        risk_rows += f"<tr><td style='padding: 4px 0;'><b>{k.replace('_', ' ').title()}:</b></td><td style='text-align: right; color: #a5d6a7; padding: 4px 0;'>{v_str}</td></tr>"

    disq_section = ""
    if disq:
        disq_items = "".join(f"<li style='color: #ef9a9a; margin-bottom: 3px;'>{d}</li>" for d in disq)
        disq_section = f"<h4>Hard Disqualifiers:</h4><ul>{disq_items}</ul>"
        
    warn_section = ""
    if warn:
        warn_items = "".join(f"<li style='color: #ffe082; margin-bottom: 3px;'>{w}</li>" for w in warn)
        warn_section = f"<h4>Soft Warnings:</h4><ul>{warn_items}</ul>"
        
    missing_section = ""
    if missing:
        missing_items = "".join(f"<li style='color: #ffcc80; margin-bottom: 3px;'>{m}</li>" for m in missing)
        missing_section = f"<h4>Missing Data:</h4><ul>{missing_items}</ul>"

    notes_items = "".join(f"<li style='margin-bottom: 4px;'>{n}</li>" for n in notes)

    html = f"""
    <html>
    <body style="background-color: #1e1e1e; color: #dcdcdc; font-family: 'Segoe UI', Arial, sans-serif; font-size: 13px; line-height: 1.4; padding: 5px; margin: 0;">
        <div style="border-bottom: 2px solid #333333; padding-bottom: 10px; margin-bottom: 12px;">
            <div style="font-size: 22px; font-weight: bold; color: #ffffff; margin-bottom: 2px;">{symbol}</div>
            <div style="font-size: 11px; color: #888888; margin-bottom: 8px;">As-of Date: {date}</div>
            <div>
                <span style="background-color: {bg_color}; color: #ffffff; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 11px; display: inline-block;">{decision}</span>
                <span style="background-color: #424242; color: #ffffff; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 11px; margin-left: 5px; display: inline-block;">{setup}</span>
            </div>
        </div>
        
        <div style="background-color: #2a2a2a; border-radius: 6px; padding: 12px; margin-bottom: 12px; border: 1px solid #3c3c3c;">
            <table style="width: 100%;">
                <tr>
                    <td><b style="font-size: 14px; color: #ffffff;">Overall Quant Score:</b></td>
                    <td style="text-align: right;"><b style="font-size: 22px; color: #a5d6a7;">{score}</b><span style="font-size: 11px; color: #888888;"> / 100</span></td>
                </tr>
            </table>
            <div style="font-size: 11px; color: #aaaaaa; margin-top: 8px; border-top: 1px solid #444444; padding-top: 6px; line-height: 1.5;">
                <b>Score Breakdown:</b><br/>
                - Technical setup quality: {breakdown.get('technical_setup_quality', 0)}/35<br/>
                - Relative strength / momentum: {breakdown.get('relative_strength_momentum', 0)}/20<br/>
                - Volume / liquidity / execution: {breakdown.get('volume_liquidity_execution', 0)}/15<br/>
                - Catalyst / fundamental support: {breakdown.get('catalyst_fundamental_support', 0)}/15<br/>
                - Risk, stop, and exit-plan quality: {breakdown.get('risk_stop_exit_quality', breakdown.get('risk_exit_stop_quality', breakdown.get('risk_reward_stop_quality', 0)))}/15
            </div>
        </div>
        
        <h4 style="color: #ffffff; border-bottom: 1px solid #333333; padding-bottom: 3px; margin: 12px 0 6px 0;">Summary:</h4>
        <p style="background-color: #252525; padding: 8px; border-left: 3px solid #81c784; border-radius: 3px; margin: 4px 0 12px 0; color: #efefef;">{summary}</p>
        
        <h4 style="color: #ffffff; border-bottom: 1px solid #333333; padding-bottom: 3px; margin: 12px 0 6px 0;">Technical Assessment:</h4>
        <table style="width: 100%; border-collapse: collapse; margin-bottom: 12px;">
            {tech_rows}
        </table>
        
        <h4 style="color: #ffffff; border-bottom: 1px solid #333333; padding-bottom: 3px; margin: 12px 0 6px 0;">Risk & Sizing:</h4>
        <table style="width: 100%; border-collapse: collapse; margin-bottom: 6px;">
            {risk_rows}
        </table>
        <p style="font-size: 11px; color: #bbbbbb; font-style: italic; margin-top: 2px; margin-bottom: 12px;">{risk.get('risk_comments', '')}</p>
        
        <h4 style="color: #ffffff; border-bottom: 1px solid #333333; padding-bottom: 3px; margin: 12px 0 6px 0;">Catalyst & News:</h4>
        <div style="background-color: #252525; padding: 8px; border-radius: 3px; margin-bottom: 12px; border: 1px solid #333333;">
            <div style="margin-bottom: 4px;"><b>Strength:</b> <span style="color: #fffbbf;">{cat.get('catalyst_strength', 'UNKNOWN')}</span></div>
            <div><b>Summary:</b> {cat.get('catalyst_summary', 'N/A')}</div>
        </div>
        
        {disq_section}
        {warn_section}
        {missing_section}
        
        <h4 style="color: #ffffff; border-bottom: 1px solid #333333; padding-bottom: 3px; margin: 12px 0 6px 0;">Recommended Action:</h4>
        <div style="background-color: #1b5e20; padding: 10px; border-radius: 4px; color: #ffffff; margin-bottom: 12px; border: 1px solid #2e7d32;">
            <div style="margin-bottom: 3px;"><b>Action:</b> {rec.get('action', 'N/A')}</div>
            <div style="margin-bottom: 3px;"><b>Entry Trigger:</b> {rec.get('entry_condition', 'N/A')}</div>
            <div style="margin-bottom: 3px;"><b>Stop Logic:</b> {rec.get('stop_condition', 'N/A')}</div>
            <div style="margin-bottom: 3px;"><b>Exit Logic:</b> {rec.get('exit_logic', rec.get('target_logic', 'N/A'))}</div>
            <div><b>Next Check:</b> {rec.get('next_check', 'N/A')}
        </div>
        
        <h4 style="color: #ffffff; border-bottom: 1px solid #333333; padding-bottom: 3px; margin: 16px 0 6px 0;">AI Analyst Notes:</h4>
        <ul style="padding-left: 18px; margin: 4px 0 15px 0;">
            {notes_items}
        </ul>
    </body>
    </html>
    """
    return html
