# Component Breakdown & Responsibilities

## 📱 UI Components (6 Tabs)

| Tab | Component | Key Responsibilities | Data Source |
|-----|-----------|---------------------|-------------|
| **1. Portfolio** | PortfolioTab | Display holdings, balance, P&L | KIS API |
| **2. Scanner** | ScannerTab | Filter stocks, apply rules, rank results | Market Data APIs |
| **3. Watchlist** | WatchlistTab | Manage selected stocks, targets, notes | Local JSON |
| **4. Trade Setup** | TradeSetupTab | Input entry/stop/TP, calc position size, send to AI | User Input + Calculations |
| **5. Charts** | ChartsTab | Display OHLCV, technicals, support/resistance | TradingView or fallback |
| **6. Rules** | RulesTab | Show rulebook markdown, trading checklists | Markdown files |

**Top Bar Component** | StatusBar | Market status (Open/Closed), Time, Account summary | Market Status + KIS API |

---

## 🔧 Core Engine Modules

| Module | File | Responsibility | Input | Output |
|--------|------|-----------------|-------|--------|
| **Stock Scanner** | `core/scanner.py` | Apply rule filters, rank by score | Rules + Stock data | Filtered list (scored) |
| **Position Sizer** | `core/position_sizer.py` | Calculate shares from entry/stop prices | Entry, Stop, Account size | Shares, Position %, Risk amount |
| **Trade Reviewer** | `core/trade_reviewer.py` | Send to ChatGPT + rulebook for approval | Trade setup + Rulebook | Approved/Rejected + Analysis |
| **Watchlist Manager** | `core/watchlist.py` | Store/manage stock selections | Symbol, target, notes | JSON persistence |

---

## 🌐 External API Modules

| Module | File | API Used | Purpose | Fallback |
|--------|------|----------|---------|----------|
| **KIS Account Snapshot** | `src/api/kis_account_snapshot_dual.py` | KIS (Korean Investment) | Portfolio holdings, balance | None (specific to your account) |
| **Market Data** | `api/market_data.py` | Yahoo Finance API | Quotes, OHLCV daily candles | IEX Cloud, Alpha Vantage |
| **Trading View** | `api/tradingview.py` | TradingView API/Embed | Charts, technical analysis | mplfinance (Python charting) |
| **AI Reviewer** | `api/openai_client.py` | OpenAI ChatGPT | Trade approval analysis | Local rule validation (fallback) |

---

## 💾 Data Storage (Local JSON)

| File | Schema | Purpose |
|------|--------|---------|
| `data/portfolio.json` | `{holdings: [{sym, shares, avg_price}], cash, total_value}` | Current portfolio snapshot |
| `data/watchlist.json` | `{watchlist: [{sym, entry_price, target_price, notes, added_date}]}` | Watched stocks |
| `data/trade_log.json` | `{trades: [{sym, entry, stop, tp, shares, ai_approved, timestamp}]}` | Trade history & audit |
| `data/settings.json` | `{account_risk_pct, min_rr_ratio, max_position_pct, ...}` | User preferences |
| `data/cache/quotes.json` | `{SMCI: {price, volume, updated_at}, ...}` | Latest quotes (1-min cache) |
| `data/cache/ohlcv_cache/` | Folder with `{SYM}.json` | Daily OHLCV by symbol |

---

## 📚 Trading Rulebooks (Markdown)

| File | Purpose | Used By |
|------|---------|---------|
| `rulebooks/qullamaggie_momentum.md` | Momentum trade entry/exit rules | Scanner presets + ChatGPT review |
| `rulebooks/pivot_reversals.md` | Pivot trade entry rules | Scanner presets + ChatGPT review |
| `rulebooks/risk_management.md` | Position sizing & risk limits | Position sizer validation |

Example rulebook content:
```markdown
## Momentum Entry Criteria ✓
- Price above 20-day moving average
- Higher lows and higher highs pattern
- Volume > 1.5x 30-day average
- NO parabolic extension (risk too high)
- Risk/Reward ratio ≥ 1.5:1

## Risk Management
- Max account risk per trade: 2%
- Min Risk/Reward: 1.5:1
- Max position size: 5% of portfolio
- Max concurrent positions: 5
```

---

## ⚙️ Configuration Parameters

```python
# Risk Management (Qullamaggie Method)
ACCOUNT_RISK_PER_TRADE = 0.01  # 1%
MIN_RISK_REWARD_RATIO = 1.5    # 1.5:1 minimum
MAX_POSITION_SIZE_PCT = 0.05   # 5% of account
MAX_CONCURRENT_POSITIONS = 5

# Scanner Settings
MIN_PRICE = 5.00
MAX_PRICE = 500.00
MIN_DAILY_VOLUME = 500_000  # shares
STOCK_UNIVERSE_SIZE = 3_000  # Top US stocks by volume

# Data Refresh
QUOTE_REFRESH_INTERVAL = 60  # seconds
SCANNER_REFRESH_INTERVAL = 300  # seconds (5 min)

# Market Hours
US_MARKET_OPEN = "09:30"  # EST
US_MARKET_CLOSE = "16:00"  # EST

# AI Review
CHATGPT_MODEL = "gpt-4" or "gpt-3.5-turbo"
CHATGPT_TEMPERATURE = 0.3  # Conservative
MAX_TOKENS = 1500
```

---

## 🚀 Development Roadmap

### Phase 1: Foundation (Weeks 1-2)
- [ ] Project setup & Python environment
- [ ] Module stubs with function signatures
- [ ] Configuration system
- [ ] Local data structures (JSON schemas)

### Phase 2: Core Logic (Weeks 3-4)
- [ ] Stock scanner implementation
- [ ] Position sizer calculations
- [ ] Watchlist manager
- [ ] Market status checker

### Phase 3: APIs (Weeks 5-6)
- [ ] KIS API authentication & portfolio sync
- [ ] Market data fetching (Yahoo Finance)
- [ ] ChatGPT API integration
- [ ] TradingView chart embedding (or alternative)

### Phase 4: UI Implementation (Weeks 7-8)
- [ ] PyQt5 main window & tabs
- [ ] Portfolio tab (real-time holdings)
- [ ] Scanner tab (filter interface)
- [ ] Watchlist tab (stock list editor)
- [ ] Trade Setup tab (risk calculations + AI)
- [ ] Charts tab (technical analysis)
- [ ] Rules tab (rulebook display)

### Phase 5: Integration & Polish (Week 9)
- [ ] Connect all components
- [ ] Background data polling (threads/async)
- [ ] Error handling & logging
- [ ] Testing & bug fixes
- [ ] User documentation

---

## 🎯 Success Criteria

- ✅ Dashboard displays real-time KIS portfolio on startup
- ✅ Scanner filters stocks by Momentum criteria, returns ranked results
- ✅ Can add stocks to watchlist with target prices
- ✅ Trade Setup calculates safe position sizes (< 2% account risk)
- ✅ ChatGPT validates trades against rulebook, returns approval
- ✅ Charts display technical analysis (TradingView or alternative)
- ✅ Market status shows open/closed with countdown timer
- ✅ All data persists across sessions in JSON files
- ✅ Trade decisions logged with timestamp + AI analysis

---

## 📝 Next Step?

This architecture is ready for implementation. Would you like me to:

1. **Start coding Phase 1** (project setup + module stubs)
2. **Create detailed rulebook examples** (specific momentum/pivot rules)
3. **Design database schema** (detailed JSON structure)
4. **Create UI mockups** (Figma-style wireframes in ASCII)
5. **Refine any specific area** (ask questions first)

What would you prefer?
