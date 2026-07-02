# Trading Rulebook: Stock Scanning Filters & Universe Rules

## Universe Filtration Rules

Apply these filters during the daily scanning process for US common stocks.

### Scanner Baseline Filters
```
- Price history > 1 day
- Daily volume > 40,000 shares
- Daily dollar volume > $35,000
- ADR 20-day > 2.4%
- 1-month price growth rank >= 97.04 vs US stocks
- Trend intensity score > 90 (configurable)
```

Notes:
- These are scanner baseline thresholds, not final trade criteria.
- The scanner should rank and surface the strongest names in the US common stock universe.
- Breakouts are manually reviewed from the scanner output.
- Episodic pivots are validated by AI after the scanner produces the candidate list.

### Price & Trend
```
- Price ABOVE 50-day EMA
  Reason: Keep trades in the broader uptrend

- Price ABOVE 20-day MA
  Reason: Confirm momentum while still allowing breakouts
```

### Liquidity
```
- Daily volume > 40,000 shares
- Daily dollar volume > $35,000
  Reason: Ensure the name is tradable for entry/exit execution
```

### Volatility
```
- ADR 20-day > 2.4%
  Reason: Provide enough daily movement for valid swing setups
```

### Relative Strength
```
- 1-month price growth rank >= 97.04 vs US stocks
  Reason: Keep only the strongest US performers
```

### Disqualifiers
```
- OTC or non-common-stock listings
- Stock price below $5 (optional for quality)
- Stock price above $500 (optional for size control)
- Recent parabolic extension without a valid consolidation
- Earnings due within 7 days unless using an episodic catalyst plan
- Illiquid issues or unusually wide spreads
- News events that create unpredictable gap risk
```

---

## Daily Scanner Output Processing

### Step 1: Load Universe
- Universe: US common stocks only
- Prefer large, liquid, tradable symbols
- Exclude OTC, ADRs, warrants, and non-common equity share classes

### Step 2: Apply Baseline Filters
```
Filter 1: Price history > 1 day
Filter 2: Daily volume > 40,000 shares
Filter 3: Daily dollar volume > $35,000
Filter 4: ADR 20-day > 2.4%
Filter 5: 1-month price growth rank = 97.04
Filter 6: Trend intensity > 90
```

### Step 3: Rank Filtered Results
Rank by priority:
1. Trend intensity
2. 1-month price growth rank
3. ADR 20-day
4. Volume and dollar volume
5. Distance above 50-day EMA

The output should be a ranked watchlist of candidate tickers for manual breakout review and AI episodic pivot research.

### Step 4: Manual or AI Review
- Breakout candidates: manually check the top ranked names
- Episodic pivot candidates: research with AI using the top ranked names

---

## Momentum / Breakout Filter

Use this when deciding if a candidate can become a breakout trade:

```
? Price > 20-day MA
? Price > 50-day EMA
- ADR 20-day > 2.4%
? Volume > 40,000 shares
? Dollar volume > $35,000
- 1-month price growth rank >= 97.04
? Trend intensity > 90
? Recent consolidation followed by breakout action
? Volume expansion on breakout day
```

---

## Episodic Pivot Filter

Use this when the stock has a news-based event or gap that could be a pivot opportunity:

```
- Stock remains in a constructive trend context
- Volume meets scanner minimums
- ADR 20-day > 2.4%
- 1-month price growth rank >= 97.04
? Trend intensity > 90
- AI confirms the event is a valid episodic pivot
- Support and stop location are well-defined
```

---

## Permanent Blacklist (Always Avoid)

```
? Penny stocks (< $5) unless exceptional liquidity and structure
? Illiquid names with wide spreads
? Stocks in bankruptcy or delisting processes
? Reverse split candidates
? Very low float stocks (< 10M shares) when they are not clearly volume-supported
? Macro or sector collapse names without a structural bullish context
? Issues with uncertain share class or corporate action risk
```

---

## Notes for Implementation
- The scanner must be able to adjust `trend intensity` and other thresholds in the UI.
- The scanner should output a ranked list, not a final trade decision.
- Breakouts remain manual decisions from the screener output.
- Episodic pivots are validated by AI after the scanner produces candidate names.

