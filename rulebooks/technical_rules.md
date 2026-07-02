# Trading Rulebook: Qullamaggie Breakouts & Episodic Pivots

## System Overview
- **Trading Style**: Daily swing trading
- **Trade Types**: Breakouts + Episodic Pivots only
- **Market**: US common stocks
- **Workflow**:
  1. Scanner filters the US stock universe
  2. Breakouts are manually reviewed from screener output
  3. Episodic Pivots are identified and researched with AI
  4. Trade selection follows strict risk rules
  5. Rulebook serves as on-demand reference

---

## Part 1: SCANNER & UNIVERSE RULES

### Target Universe
- US common stocks only
- Avoid OTC, ADRs, preferred shares, warrants, and non-common equity symbols
- Focus on liquid, tradable equities with clean market structure

### Daily Filter Set
Apply these filters to every candidate stock in the scanner:

```
- Price history > 1 day
- Daily volume > 40,000 shares
- Daily dollar volume > $35,000
- ADR 20-day > 2.4%
- 1-month price growth rank >= 97.04 vs US stocks
- Trend intensity score > 90
```

Notes:
- Trend intensity must be configurable, so values can be tightened or loosened.
- Price growth rank compares the stock to the US stock universe and keeps only the strongest performers.
- This is the base scanner. Manual review and AI research decide the final trade.

### Result Set
- Output: ranked list of the best daily candidates
- Rank by: trend intensity, price growth rank, ADR, and liquidity
- Use this list to find breakout candidates and possible episodic pivot names

---

## Part 2: BREAKOUT SETUP

### Breakout Definition
A breakout is a stock that has:
- moved higher over recent weeks,
- pulled back into a tight consolidation,
- and is now breaking out on strong volume.

### Role in the system
- Breakout candidates are identified by the scanner
- User manually checks chart structure and volume
- AI is not required for the initial breakout signal, only for verification if desired

### Breakout Entry Rules
Must satisfy all of these:

```
1. Price is above the 50-day EMA
2. Price is above the 20-day moving average
3. ADR 20-day > 2.4%
4. Daily volume > 40,000 shares (scanner baseline)
5. Daily dollar volume > $35,000 (scanner baseline)
6. Stock is ranked in the top 2.96% of US stocks for 1-month price growth
7. Trend intensity score > 90
8. Price is breaking above a recent consolidation/high
9. Breakout occurs with volume expansion versus the recent average
10. No parabolic extension or overly stretched move
```

### Breakout Review Workflow
- Review the screener output and charts
- Confirm the consolidation base and breakout level
- Check for supporting volume on breakout day
- Verify there is no bad news catalyst or other disqualifier
- Add the ticker to the watchlist if the setup is clean

### Breakout Stop Rules
- Initial stop = most recent intraday support low (daily low or consolidation low)
- Do not place stop wider than the 20-day ADR
- If the stop distance is greater than ADR, skip the setup or tighten the entry

### Breakout Target Rules
- Scale out after 3-5 days if the trade is profitable
- Take at least 1/3 to 1/2 off at the first valid resistance or predefined target
- Trail the remainder using a moving average close rule (10-day or 20-day MA)
- Exit on the first close below the chosen trail MA

---

## Part 3: EPISODIC PIVOT SETUP

### Episodic Pivot Definition
A pivot trade is a news-driven reversal or gap-based continuation that is identified through focused AI research rather than only by pattern recognition.

### Role in the system
- Scanner identifies liquid, volatile names with strong relative strength
- AI performs the episodic pivot research and validation
- User uses the AI output to confirm the pivot thesis and risk profile

### Episodic Pivot Entry Rules
Must satisfy all of these:

```
1. Stock remains above the 50-day EMA or at least in a structurally bullish context
2. There is a discrete news catalyst, strong earnings beat, regulatory event, or sector macro trigger
3. The stock is showing a rebound or a controlled gap move with volume support
4. Daily volume > 40,000 shares and dollar volume > $35,000
5. ADR 20-day > 2.4%
6. 1-month price growth rank = 97.04 vs US stocks
7. Trend intensity score > 90
8. AI research confirms the pivot thesis and identifies the key technical support/resistance levels
```

### Episodic Pivot Workflow
- Feed candidate ticker and context into the AI research engine
- Confirm the event type, expected path, and target levels
- Use AI to identify the correct entry zone, support level, and stop placement
- Only take the trade if AI confirms the pivot is valid and the risk reward is acceptable

### Episodic Pivot Stop Rules
- Stop at the nearest technical support or intraday low
- Never use a stop wider than the 20-day ADR unless AI provides a strong structural justification
- Maximum account risk per episodic pivot may be higher than standard, but still limited to 4% in aggressive cases

### Episodic Pivot Target Rules
- Target the next major resistance or the measured move from the pivot base
- Scale out as the trade confirms price strength
- Trail the remainder until price closes below the 10-day or 20-day MA, depending on volatility

---

## Part 4: AI PROCESS

### Breakouts
- AI is optional for breakout validation
- Use AI to confirm the breakout pattern, volume quality, and risk/reward
- The final decision remains manual for breakout trades

### Episodic Pivots
- AI is mandatory for episodic pivot validation in this system
- The AI should research:
  - the catalyst type and strength
  - whether the event is genuine and sustainable
  - the appropriate support level and stop range
  - whether the trade still fits the Qullamaggie pivot profile

---

## Part 5: POSITION SIZING & RISK MANAGEMENT

### Risk Rules
- Target risk per trade: 0.25% to 2% of account
- Aggressive episodic pivots may use up to 4% account risk only when thesis and liquidity justify it
- Maximum position size: 25% of account value

### Stop Loss Constraints
- Stop loss distance should generally be less than ADR 20-day
- If stop distance is greater than ADR, do not take the trade
- Stop loss must be set before entry and never moved lower

### Position Size Example
```
Account size: $100,000
Trade risk decision: 1% = $1,000
Entry: $50
Stop: $48
Risk per share: $2
Shares = $1,000 / $2 = 500
Position value = $25,000 (25% max)
```

### Risk Validation
- Do not require a fixed profit target or R/R-based take-profit level.
- Validate risk from entry, stop distance, ADR, liquidity, and position size.
- The breakout level is a structural trigger, not a profit target.
- Profit management is rule based: take the first partial exit after 3-5 days if the trade has worked, then hold remaining shares while momentum continues.

---

## Part 6: DISQUALIFIERS

Reject any stock with one or more of the following:

```
? ADR 20-day = 2.4%
? Daily volume = 40,000 shares
? Daily dollar volume = $35,000
? Price below 50-day EMA
? Trend intensity = 90 unless intentionally widened
? Stock in parabolic extension
? Price below $5 or above $500 (optional universe constraint)
? Earnings within 7 days without a clear catalyst plan
? Market-wide selloff or severe sector weakness
? Low liquidity, high spread, or poor execution risk
? Unclear or invalid AI pivot thesis
```

---

## Part 7: DAILY CHECKLIST

### Scanner Setup
- [ ] US common stock universe selected
- [ ] Minimum 1 day price history
- [ ] Volume > 40,000 shares
- [ ] Dollar volume > $35,000
- [ ] ADR 20-day > 2.4%
- [ ] 1-month price growth rank = 97.04
- [ ] Trend intensity > 90

### Breakout Review
- [ ] Price above 50-day EMA
- [ ] Consolidation + breakout present
- [ ] Volume expanding on breakout
- [ ] Stop distance < ADR
- [ ] Breakout level defined for setup validation
- [ ] Rule-based exit plan defined
- [ ] Manual chart check complete

### Episodic Pivot Review
- [ ] Catalyst confirmed by AI
- [ ] Price remains in a bullish structural context
- [ ] Support and stop defined
- [ ] AI verifies pivot thesis
- [ ] Rule-based exit plan defined
- [ ] Position size = 25% account

### Execution
- [ ] Stop set before order entry
- [ ] Position size calculated
- [ ] Entry thesis documented
- [ ] Trade logged for review
