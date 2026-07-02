# Trading Rulebook: Risk Management & Position Sizing

## Capital Allocation System

### Account Risk Per Trade - Decision Matrix

**Decide BEFORE every trade (this is non-negotiable):**

```
Risk Tier        Account Risk %   Use When
────────────────────────────────────────────────────────
Conservative     0.25%            Testing new setup, uncertain
Standard         0.5% - 1%        Normal trade, confident
Aggressive       2% - 4%          Episodic pivot, high conviction

Example (Account: $10,000):
Conservative: Risk $25 per trade
Standard:     Risk $50-100 per trade
Aggressive:   Risk $200-400 per trade
```

---

## Position Size Calculation - Core Formula

### Step-by-Step Calculation

**Given:**
```
Entry Price: $160
Stop Loss Price: $155 (intraday low, user selects timeframe)
Account: $10,000
Risk Decision: 1% ($100)
```

**Calculation:**
```
Step 1: Calculate risk per share
  Risk per share = Entry - Stop
  Risk per share = $160 - $155 = $5
  
Step 2: Calculate shares to buy
  Shares = Total Risk Amount / Risk per share
  Shares = $100 / $5 = 20 shares
  
Step 3: Calculate position value
  Position value = Shares × Entry price
  Position value = 20 × $160 = $3,200
  
Step 4: Calculate position as % of account
  Position % = Position value / Account
  Position % = $3,200 / $10,000 = 32%
  
Step 5: Verify constraints (see section below)
  Position 32% > 25% max ❌ TOO LARGE
  Action: Reduce to 20 shares or tighter stop
```

---

## Position Size Constraints (All MUST Be Met)

### Constraint 1: Maximum Single Position Size
```
Rule: No single trade can exceed 25% of account

Example:
  Account: $10,000
  Max position size: $2,500 (25%)
  
  If entry $100 and stop $95:
    Max shares = $2,500 / $100 = 25 shares max
    Max risk = 25 × $5 = $125 (1.25% account) ✓
    
  If entry $100 and stop $80:
    Max shares = $2,500 / $100 = 25 shares max
    Max risk = 25 × $20 = $500 (5% account) ❌ TOO MUCH
    Action: Buy fewer shares or use tighter stop
```

### Constraint 2: Account Risk Matches Decision
```
Rule: Actual account risk must equal your pre-decided %

Decision: "I will risk 1%"
Account: $10,000
Risk Amount: 1% × $10,000 = $100

Verification:
  Trade: Buy 20 shares @ $100, stop $95
  Actual risk: 20 × ($100-$95) = $100 ✓
  Actual risk %: $100 / $10,000 = 1% ✓
  ✓ MATCHES decision, proceed
  
Wrong Example:
  Trade: Buy 30 shares @ $100, stop $95  
  Actual risk: 30 × $5 = $150 (not $100!)
  Actual risk %: $150 / $10,000 = 1.5% ❌
  ❌ DOES NOT MATCH, reduce to 20 shares
```

### Constraint 3: Stop Loss Distance vs ADR

**Momentum Trades:**
```
Rule: Stop loss distance % ≤ ADR %

Example:
  ADR = 4%
  Entry = $100
  Stop = $96.50 (from intraday 1h low)
  Stop distance = ($100 - $96.50) / $100 = 3.5%
  
  Check: 3.5% < 4% ADR? ✓ YES, VALID
```

**Episodic Pivot Trades:**
```
Rule: Stop loss distance % ≤ 150% of ADR %

Example:
  ADR = 3.5%
  Entry = $100
  Stop = $95 (news-based, needs wider stop)
  Stop distance = ($100 - $95) / $100 = 5%
  Max allowed = 3.5% × 1.5 = 5.25%
  
  Check: 5% < 5.25%? ✓ YES, VALID
```

### Constraint 4: ADR Requirement

```
Rule: ADR must be > 3.5%

If ADR < 3.5%:
  ❌ SKIP this stock
  ❌ Reason: Not enough intraday volatility for swing trade
  ❌ Will result in tight stops and frequent stop-outs
```

### Constraint 5: Account Risk Cannot Exceed 2% Per Trade

```
Rule: Account risk per trade ≤ 2% (max 4% for aggressive)

Example:
  Account: $10,000
  Max account risk: 2% × $10,000 = $200
  
  If entry $100, stop $90:
    Each share risks $10
    Can buy max 20 shares = $200 risk ✓
    Position = 20 × $100 = $2,000 (20% account) ✓
    
  If entry $100, stop $80:
    Each share risks $20
    Can buy max 10 shares = $200 risk ✓
    Position = 10 × $100 = $1,000 (10% account) ✓
```

---

## Position Size Pre-Entry Checklist

Before executing ANY trade, verify ALL of these:

```
┌─────────────────────────────────────────────────────────┐
│ POSITION SIZE VALIDATION CHECKLIST                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│ ✓ ADR > 3.5%? (If no, SKIP)                           │
│   ADR = ___% (REQUIREMENT)                            │
│                                                          │
│ ✓ Stop loss distance < ADR% (momentum)?               │
│   Stop distance = __% (must be < ADR)                 │
│                                                          │
│ ✓ Account risk ≤ 2% per trade?                        │
│   Actual account risk = __% (must be ≤ 2%)           │
│                                                          │
│ ✓ Position size ≤ 25% of account?                     │
│   Position size = __% (must be ≤ 25%)                │
│                                                          │
│ ✓ Breakout level and stop are valid?                          │
│   Breakout is structural; stop risk fits plan                  │
│                                                          │
│ ✓ Matches your risk decision?                          │
│   Decided: ___% | Actual: __% (must match)           │
│                                                          │
│ ✓ No disqualifying factors?                            │
│   ADR ok? Price above 50EMA? No earnings? ✓           │
│                                                          │
│ ✓ AI has approved this setup?                          │
│   ChatGPT response: ___________________            │
│                                                          │
│ ✓✓✓ ALL BOXES CHECKED? → READY TO EXECUTE             │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

---

## Stop Loss Rules (CRITICAL - Never Violate)

### Rule 1: Placement Source
```
Stop loss is placed at the intraday LOW of selected timeframe.

Choose timeframe based on trade type:
  Scalper:      Use 1-min or 5-min low
  Day trader:   Use 15-min or 30-min low
  Swing trader: Use 1-hour low (preferred for momentum)
               OR previous day low (pivot trades)

Example:
  Entry time: 10:30 AM
  1-hour low (9:30-10:30): $155.00 ← Use this as stop
  5-minute low: $155.50
  Intraday low (1 min): $155.25
```

### Rule 2: Never Move Stop Lower
```
❌ VIOLATION: Trying to "recover" by moving stop down
  Example: Stop was $155, moved to $152 to avoid loss
  Result: Blown account

✓ CORRECT: Move stop UP only (lock in gains)
  Example: Stop was $155, moved to $160 after price rises
  Result: Protected profit
```

### Rule 3: Always Use Hard Stops
```
❌ WRONG: "I'll exit manually if it hits stop level"
  Reality: Emotions kick in, you don't exit
  Result: Loss becomes catastrophic

✓ RIGHT: Set stop order immediately upon entry
  Setup: Buy 20 shares @ $160, SELL STOP @ $155
  Execution: Automatic, no emotion, discipline
```

### Rule 4: Exit at Stop Price - No Exceptions
```
❌ WRONG: "I know this will bounce, let me hold"
  Reality: It doesn't bounce, continues lower
  Result: Account blows up

✓ RIGHT: Follow your plan with discipline
  Execution: Hit stop, accept loss, learn lesson
  Psychology: This is what separates winners from losers
```

---

## Risk Validation Without Fixed Profit Targets

### Calculation
```
Risk = Entry - Stop
Account risk = Risk per share x Shares
Stop distance % = Risk per share / Entry

Example:
  Entry: $100
  Stop: $95 (risk = $5)
  Shares: 100

  Account risk = $5 x 100 = $500
  Stop distance = 5%
  Validate this against account risk limit, ADR, liquidity, and setup quality.
```

### Required Validation
```
Do not require a fixed profit target.
Do not use a target price as a take-profit level.

Entry validation:
  - Breakout price is the user-entered daily structural breakout level.
  - ORB entry is valid only when price clears both ORB high and the buffered breakout price.
  - Stop distance must fit the selected setup and ADR constraint.
  - Position size must fit account risk and max-position limits.

Exit management:
  - First partial exit: sell 1/3 to 1/2 after 3-5 days if the trade has worked.
  - Hold remaining shares while momentum continues.
  - Final exit: sell when price closes below the selected EMA, usually 10 EMA or 20 EMA.
```

---
## Position Size Examples

### Example 1: Standard Momentum Trade
```
Scenario:
  Account: $10,000
  Risk Decision: 1% ($100)
  
Technical Setup:
  Entry: $50
  Stop (1h low): $48
  Breakout Price: $50 structural trigger
  ADR: 4%
  
Calculation:
  Risk/share: $50 - $48 = $2
  Shares: $100 / $2 = 50 shares
  Position value: 50 × $50 = $2,500
  Position %: $2,500 / $10,000 = 25% ⚠️ AT MAX
  Actual risk %: $100 / $10,000 = 1% ✓
  Stop distance %: $2 / $50 = 4% = ADR ✓
  
Verification:
  ✓ ADR 4% > 3.5%
  ✓ Stop 4% ≤ ADR 4%
  ✓ Account risk 1% ≤ 2%
  ✓ Position 25% ≤ 25% (at limit)
  ✓ Breakout trigger and rule-based exit plan are defined
  
Verdict: VALID (though at position size limit)
         Consider using 0.5% risk instead = 25 shares = 12.5%
```

### Example 2: Conservative Episodic Pivot
```
Scenario:
  Account: $25,000
  Risk Decision: 0.5% ($125) - conservative on pivot
  
Technical Setup:
  Entry: $100
  Stop (news low): $92 (wider stop for pivot)
  Breakout Price: $100 structural trigger
  ADR: 3.5%
  
Calculation:
  Risk/share: $100 - $92 = $8
  Shares: $125 / $8 = 15.625 → 15 shares (round down)
  Position value: 15 × $100 = $1,500
  Position %: $1,500 / $25,000 = 6% ✓ (well under 25%)
  Actual risk %: 15 × $8 / $25,000 = 0.48% ✓
  Stop distance %: $8 / $100 = 8%
  Max allowed for pivot: 3.5% × 1.5 = 5.25% ❌
  
Problem: Stop 8% > 150% of ADR 5.25%
Solution: Tighten stop to $95 (3% distance)
          New calc: Shares = $125 / $5 = 25 shares
          Position = 25 × $100 = $2,500 (10%) ✓
  
Verification (Revised):
  ✓ ADR 3.5% > 3.5%
  ✓ Stop 3% ≤ 150% of ADR (5.25%)
  ✓ Account risk 0.5% ≤ 2%
  ✓ Position 10% ≤ 25%
  ✓ Breakout trigger and rule-based exit plan are defined
  
Verdict: VALID after adjustment
```

---

## Quick Position Sizing Reference Card

```
┌───────────────────────────────────────────────────┐
│ POSITION SIZING QUICK REFERENCE                  │
├───────────────────────────────────────────────────┤
│                                                   │
│ FORMULA:                                          │
│ Shares = (Risk % × Account) / (Entry - Stop)    │
│                                                   │
│ CONSTRAINTS (ALL MUST BE TRUE):                  │
│ 1. Position ≤ 25% of account                    │
│ 2. Account risk ≤ 2% per trade                  │
│ 3. Stop distance ≤ ADR (momentum)               │
│ 4. Stop distance ≤ 150% ADR (pivot)             │
│ 5. ADR > 3.5% required                          │
│ 6. Breakout trigger is structural, not target                   │
│ 7. Rule-based exit plan is defined                 │
│                                                   │
│ DECISION MATRIX:                                 │
│ Conservative: 0.25% risk (test setups)          │
│ Standard:     0.5-1% risk (normal)              │
│ Aggressive:   2-4% risk (high conviction)       │
│                                                   │
│ STOP LOSS TIERS:                                │
│ Intraday: 1m/5m low (scalp)                     │
│ Swing: 15m/1h low (day/swing)                   │
│ Multi-day: Previous swing low (position)        │
│                                                   │
└───────────────────────────────────────────────────┘
```

---

## Position Management Rules

### Rule 1: Maximum Concurrent Positions
```
Never hold more than 5 active positions simultaneously

Reason:
  - Capital diversification (don't concentrate)
  - Risk management (don't correlate all positions)
  - Attention span (can't monitor 10+ trades well)
  - Psychology (too many trades = emotional trading)
  
If already 5 positions open:
  STOP taking new trades
  Wait for one to exit (profit or stop)
  Then take next trade
```

### Rule 2: Daily Loss Limit
```
If you lose 4% of account in single day: STOP TRADING

Example:
  Account: $10,000
  Daily loss limit: -$400 (4%)
  
  If your 3 trades lose: -$50, -$75, -$300 = -$425
  Total: -$425 exceeds -$400 limit
  Action: STOP trading for the day
  Reason: Prevents emotional revenge trading
  
Next day: Start fresh with clear head
```

### Rule 3: Never Average Down
```
❌ WRONG: "Stock down to $95, I'll buy more to average down"
  Psychology: Turning 1 position loss into 2
  Math: Doubles loss if stop hits
  Result: Blown account

✓ RIGHT: Accept initial loss, exit at stop, learn lesson
  Psychology: Discipline
  Math: Limited risk
  Result: Controlled losses
```

---

## Daily Risk Management Ritual

### Before Market Open
```
□ Review yesterday's trades
□ Check overnight news
□ Determine today's risk tolerance (0.25%, 0.5%, 1%)
□ Set daily loss limit (-4%)
□ Prepare watchlist
□ Have scanning filters ready
```

### During Trading Hours
```
□ Scan for opportunities
□ For each potential trade:
  □ Verify all entry criteria
  □ Calculate position size
  □ Validate breakout level, stop risk, and position size
  □ Send to AI verification
  □ Execute if approved
  □ Set hard stop order immediately
  □ Define partial-exit and EMA-close final-exit rules
  □ Log the trade
□ Monitor open positions
□ Check daily loss total
  - If -4% hit: STOP for day
```

### After Market Close
```
□ Close all day trades
□ Calculate daily P&L
□ Log all trades in trade_log.json
□ Review performance
□ Note lessons learned
□ Update watchlist
```

---

## Trader's Accountability Checklist

Every trade gets logged. Every decision gets audited.

```
┌─────────────────────────────────────────────────────────┐
│ TRADE LOG ENTRY (Copy this for each trade)             │
├─────────────────────────────────────────────────────────┤
│                                                          │
│ Trade ID: 20260623_SMCI_001                            │
│ Date: 2026-06-23                                       │
│ Symbol: SMCI                                           │
│ Type: Momentum / Pivot (circle one)                    │
│                                                          │
│ ENTRY                                                   │
│ Entry Price: $160.00                                   │
│ Entry Time: 10:30 AM                                   │
│ Entry Thesis: "Above 20-day MA, volume spike"         │
│ Shares: 1000                                           │
│ Position Size: 0.32% of account                        │
│                                                          │
│ RISK                                                    │
│ Stop Price: $155.00                                    │
│ Account Risk: 1% ($5,000)                              │
│ Breakout Price: $160.00 structural trigger                                  │
│ Exit Model: partial after 3-5 days; final close below EMA                                       │
│                                                          │
│ AI REVIEW                                              │
│ ChatGPT Approved: YES / NO                             │
│ Confidence: 85%                                        │
│ Analysis: "Setup valid, momentum confirmed"           │
│                                                          │
│ EXECUTION                                              │
│ Exit 50% Price: $168.50                                │
│ Exit 50% Date: 3 days later                            │
│ Final Exit: $175.00                                    │
│ Profit/Loss: +$1,500 (0.3% of account)               │
│ Win/Loss: WIN                                          │
│                                                          │
│ LESSONS LEARNED:                                       │
│ ________________________________                       │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

