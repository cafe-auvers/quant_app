# Stock-management tunable-variable audit

Audit date: 2026-08-24
Scope: repository and local effective non-secret configuration present on the audited workstation
Phase: discovery and classification only

This report distinguishes a **code fallback** from the **current effective value**. The latter includes non-secret overrides in the gitignored `.env`, `.env.pc`, `data/settings.json`, and `data/scanner_setups.json`. Secret values were never read into this report. Arbitrary prices, symbols, quantities, and timestamps used only as test scenarios are not tunable defaults; tests that assert a default, boundary, or conflicting semantic are included. Illustrative worked examples in documentation are likewise excluded unless their wording establishes a rule or exposes a conflict with executable behavior. Current per-symbol orders/candidates in `data/execution_queue.json` and other state snapshots are records of user intent/market observations, not global defaults; their schemas/defaulting code is audited, but personal position rows are not reproduced.

## 1. Executive summary

The audit found **99 distinct conceptual candidates**. Section 3 enumerates the semantically relevant definitions, fallbacks, consumers, validations, test assertions, and documentation occurrences separately; adjacent lines implementing one expression are kept together, while definitions in separate files are separate occurrences.

### Counts

| Safety | Count |
|---|---:|
| SAFE | 18 |
| CONDITIONAL | 38 |
| UNSAFE | 28 |
| SECRET | 8 |
| UNCLEAR | 7 |
| **Total** | **99** |

| Preliminary category | Count |
|---|---:|
| 1. Portfolio risk and exposure | 11 |
| 2. Position sizing and capital allocation | 9 |
| 3. Entry and breakout rules | 5 |
| 4. ORB configuration | 4 |
| 5. Stop-loss and breakeven management | 4 |
| 6. Partial exits and full exits | 4 |
| 7. Order execution and price protection | 5 |
| 8. Order lifecycle, retries, and timeouts | 8 |
| 9. Trading sessions and scheduling | 3 |
| 10. Stock eligibility and trade filters | 8 |
| 11. Market data freshness and validation | 7 |
| 12. Kanban and workflow state transitions | 3 |
| 13. Broker or account operational limits | 7 |
| 14. UI-only preferences | 3 |
| 15. Internal engineering constant | 3 |
| 16. Security or credential value | 8 |
| 17. Unclear | 7 |
| **Total** | **99** |

There are **35 duplicated conceptual definitions**, **21 conflicting defaults or behaviors**, **5 ambiguous/inconsistent percentage concepts**, and **31 SAFE/CONDITIONAL candidates with at least one finite hard bound that the repository does not establish**. `null` is used for those bounds; recommendations are not presented as technical limits.

The five percentage hazards counted above are: (1) `risk_percent` fraction versus points; (2) `orb_buffer_percent` points versus `buffer_pct` fraction; (3) `stop_adr` percent-of-ADR versus ATR-price use; (4) `capital_percent`/`position_percent` points versus portfolio `*_fraction` fields; and (5) generic `*_pct` names that are fractions in execution/outage code while scanner returns and thresholds use percentage points.

### Highest-risk findings

1. **`risk_percent` can mean either a fraction or percentage points.** The planning UI converts `1` to `0.01`, `BuylistItem` persists a default of `1.0` as percentage points, but `TradeCardState` also defaults to `1.0` and the Buy Board runtime explicitly treats it as a fraction. Legacy migration copies `1.0` without dividing by 100. A fallback-created or migrated card can therefore carry a 100% risk budget instead of 1% (`src/core/watchlist.py:403,541`; `src/core/trade_card_state.py:174,559`; `src/services/trade_card_repository.py:628`; `src/services/buyboard_runtime.py:1040-1067`). This must be consolidated before it becomes a setting.
2. **The visible position limit disagrees with enforcement.** Current local enforcement is 20 positions (`.env:148`), while the Buy Board always renders and colors against 30 (`src/ui/buyboard/board.py:74,140,420-424`). The code and example fallback are also 30.
3. **The account-equity sizing base has incompatible fallbacks.** The visible planner initializes to $100,000, while scoring and the dashboard's manual fallback use $10,000. Configured production execution correctly replaces both with fresh KIS total equity and fails closed when that snapshot is unavailable (`src/ui/charts/controller_layout.py:148`; `src/core/scoring.py:68`; `src/ui/mixins/dashboard_mixin.py:1161-1199,1219-1238`). Any future setting must stay planning-only and never override broker equity in production.
4. **Gross exposure is intentionally capable of leverage and currently equals 2.0 (200%).** The fallback/dataclass value is 10.0 (1,000%), and validation has no finite maximum (`.env:150`; `src/core/execution_config.py:368-370`; `src/risk/portfolio.py:36,46-66`). A missing local override silently expands the governor fivefold.
5. **ORB settings are already mutable but device-local.** Effective values are capital 10/17.5/28% and stop/ADR 20/65/90%, while code fallbacks are 10/17.5/30 and 15/65/66. `settings` and `scanner_setups` are absent from `SYNCED_STATE_KEYS`, so laptop and PC can rank or reject different plans (`data/settings.json:2-9`; `src/services/state_sync.py:64`).
6. **`stop_adr` has incompatible meanings.** ORB sizing stores stop distance as a percentage of ADR, but outage classification divides a dollar price distance by `card.stop_adr` as if it were a dollar ATR value (`src/risk/orb_position.py:173-177`; `src/services/trading_engine.py:202-205`). The outage tier can be dimensionally wrong.
7. **Protective-exit price floors differ by path.** The shared command path floors a marketable sell at $0.01; the legacy stop path permits $0.0001 (`src/core/exit_execution_command.py:67-80`; `src/ui/buylist/actions.py:665-674`).
8. **Documented and implemented exit choices diverge.** UI/rulebooks say sell 1/3–1/2 and use a selected 10/20 EMA; runtime always calculates one third and prioritizes EMA10 before EMA20 (`src/core/exit_policy.py:52-74`; `src/ui/buylist/view.py:175`; `README.md:33`). Legacy `take_profit`/`target_price` fields also remain even though the active ORB contract explicitly has no fixed profit target.
9. **Environment parsing is permissive.** Malformed values silently fall back and most execution floats/integers receive no range validation at import (`src/core/execution_config.py:22-51`). A dashboard cannot safely write these variables without typed validation and atomic worker reload rules.
10. **Some portfolio controls are implemented but disabled.** Daily loss, drawdown, sector, industry, correlation-group, strategy, and incremental buying-power caps are all zero in the effective environment. Their runtime inputs are not all canonical, so exposing them now could create false confidence (`src/core/execution_config.py:355-393`).
11. **The local live envelope is active.** The audited environment is `CONTROLLED_LIVE`, with one allowlisted symbol and a $0.01 entry-notional cap (`.env:135-137`). Mode, ownership, mutation budgets, and idempotency controls remain operational safety controls, not ordinary dashboard settings.

### Coverage and uncertainty

Inspected areas include all Python under `src/`, repository configuration and environment examples, local non-secret environment overrides, local JSON state, database schema/repositories, tests and fakes, rulebooks, README, and execution/readiness documentation. Searches covered named constants, default parameters, literals in arithmetic/comparisons, state transitions, durations, percentages, limits, and alternative terms (`risk|capital|allocation|exposure|entry|exit|stop|ORB|quantity|retry|timeout|stale|buffer|threshold|session|loss|profit|limit`). Database schema contains market-data/cache defaults but no separate relational source for trade-risk settings.

Coverage remains uncertain for two external facts: measured KIS account/broker limits not committed to the repository, and exceptional NYSE closures outside the recurring calendar implemented here. Three metric-definition candidates are `UNCLEAR` because documentation and runtime formulas disagree and no authoritative product decision identifies the intended formula. No separate machine-enforced minimum-cash-reserve setting, per-symbol daily-loss cap, fixed active profit target, or independent percentage trailing-stop parameter was found. Their nearest implemented controls are fresh buying-power/portfolio gates, per-position protective stops, and the advisory EMA10/EMA20 exit policy; the absence is recorded here rather than inventing candidates.

### Reading the inventory

Bounds use `value (inclusive/exclusive; BASIS)`. `null (UNKNOWN)` means the repository supplies no defensible finite bound. The following compact codes keep the master table readable:

Within bound and basis cells, the compact words `CODE`, `MATH`, `BROKER`, `rulebook`/`business`, `recommended`, and `inferred` mean exactly `CODE_ENFORCED`, `MATHEMATICALLY_REQUIRED`, `BROKER_ENFORCED`, `BUSINESS_RULE`, `RECOMMENDED_SAFETY_BOUND`, and `INFERRED`, respectively. `UI_ONLY`/`UI-only` means `CODE_ENFORCED` by the current frontend validator only and is never treated as a runtime or broker hard bound. `UNKNOWN` is used unchanged. These are aliases for the required basis vocabulary, not additional basis types.

| Code | Meaning |
|---|---|
| `F01` | Fraction: finite; hard 0 inclusive to 1 inclusive (`CODE_ENFORCED` or `MATHEMATICALLY_REQUIRED` as stated); dashboard percent; step 0.01 percentage point, 2 decimals. |
| `F+` | Finite non-negative fraction; hard lower 0 inclusive; hard upper null; dashboard percent; recommended upper only when explicitly stated. |
| `PP100` | Percentage points: finite 0..100 inclusive; step 0.5 or 0.01 as cited. |
| `POS` | Positive finite scalar; hard lower 0 exclusive; hard upper null. |
| `NN` | Non-negative finite scalar; hard lower 0 inclusive; hard upper null. |
| `SEC+` | Positive duration; hard lower 0 exclusive; hard upper null; integer seconds unless source uses float. |
| `COUNT+` | Positive whole number; hard lower 1 inclusive; hard upper null unless specified. |
| `MC` | Market closed and no pending order; restart/rebuild worker snapshot before effect. |
| `NP` | No pending order for affected symbol/account. |
| `NO` | No open position for affected symbol/account. |
| `R` | Process/worker restart required because the authoritative source is read at import/composition time. |
| `LIVE=SAME` | Production and mock use the same value unless a test injects/monkeypatches a scenario value. |
| `MOCK-DIFF` | Test-friendly constructor fallback differs from production composition; details appear in evidence/conflict matrix. |

## 2. Master inventory

The proposed key is only a discovery-phase name. It is not a configuration design commitment.

For every row, the discovery-phase **proposed default is exactly the value in “Current effective default.”** Where that cell is path-specific or “not enforced,” no numeric default is proposed until the conflict is resolved.

| Candidate ID | Proposed key | Display name | Preliminary category | Safety level | Current effective default | Unit | Internal representation | Dashboard representation | Hard minimum | Hard maximum | Recommended UI minimum | Recommended UI maximum | Bounds basis | Step and precision | Validation | Cross-field constraints | Change timing | Runtime consumers | Authoritative source | Duplicate locations | Live/mock behavior | Risk if misconfigured | Evidence | Confidence | Recommendation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RISK-001 | `risk.trade_fraction` | Account risk per trade | 2 | CONDITIONAL | UI/new plan `0.01`; card fallback/migration `1.0` (conflict) | fraction | mixed fraction and percentage points | percent | `>0 (CODE)` | `1 inclusive (CODE)` | `0.25% (BUSINESS_RULE)` | `2% normal; 4% exceptional (BUSINESS_RULE)` | F01; rulebook | 0.05 pp, 2 dp | finite, `0<x<=1`; normalize once | must fit ORB capital bounds, total-open-risk cap, buying power; stop `<` entry | MC+NP+NO; rebuild plans | scoring, queue, card bridge, pre-trade sizing | no single source; UI `1` -> `0.01` for new plan | watchlist, Buylist, card, repository, tests, dead template | LIVE=SAME; tests use both 1.0 and 0.01 | 100x over-sizing | `src/ui/charts/controller_layout.py:152,170`; `src/ui/buylist/view.py:741-748`; `src/core/trade_card_state.py:174`; `src/services/buyboard_runtime.py:1040-1067` | HIGH | consolidate first; do not expose yet |
| RISK-002 | `risk.max_positions` | Maximum simultaneous positions | 1 | CONDITIONAL | `20` local; fallback/UI `30` | positions | integer | integer | `1 inclusive (CODE)` | `30 inclusive (CODE)` | `1 (CODE)` | `30 (CODE)` | COUNT+; code cap | 1, 0 dp | integer 1..30 | existing positions must not exceed new limit | MC+NP; R | portfolio governor, Health, Buy Board label | `.env:148` -> execution config | portfolio dataclass, example, UI label | LIVE=SAME | label may understate blocking; lowering below holdings freezes entries | `.env:148`; `src/risk/portfolio.py:16,34,48-51`; `src/ui/buyboard/board.py:74` | HIGH | expose after UI uses authority |
| RISK-003 | `risk.max_total_open_risk_fraction` | Maximum total open risk | 1 | CONDITIONAL | `0.10` | fraction of equity | float | percent | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0%` | `100% (RECOMMENDED_SAFETY_BOUND)` | F+ | 0.1 pp, 1 dp | finite non-negative | `trade risk <= cap`; canonical stops/equity required | MC+NP; R | `PortfolioRiskManager` | `.env:149` | code/dataclass/example all 0.10 | LIVE=SAME | excessive aggregate loss; zero blocks risky entries | `.env:149`; `src/risk/portfolio.py:35,361-390`; `src/services/buyboard_runtime.py:478-508` | HIGH | expert-only after canonical risk audit |
| RISK-004 | `risk.max_gross_notional_fraction` | Maximum gross exposure | 1 | CONDITIONAL | `2.0` (200%); fallback `10.0` | fraction of equity | float | percent / leverage | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0%` | `100% without margin; broker/account-specific otherwise (BROKER)` | F+ | 1 pp, 0 dp | finite non-negative | cannot exceed broker buying power/margin; interacts with incremental BP | MC+NP; R | portfolio governor | `.env:150` | code/dataclass/example 10.0 | LIVE=SAME | unintended leverage; missing env permits 1,000% | `.env:150`; `src/core/execution_config.py:368-370`; `src/risk/portfolio.py:36` | HIGH | expert-only; consolidate fallback first |
| RISK-005 | `risk.max_incremental_buying_power_fraction` | Maximum buying power per new entry | 1 | CONDITIONAL | `0` (disabled) | fraction of usable buying power | float; `0` disables | percent plus Disabled | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0%` | `100% (RECOMMENDED)` | F+ | 1 pp | finite non-negative | projected notional <= buying power and gross cap | MC+NP; R | portfolio governor | `.env:151` | code/dataclass/example | LIVE=SAME | false protection while disabled; too low blocks entries | `src/risk/portfolio.py:37,361-390`; `.env:151` | MEDIUM | investigate canonical semantics first |
| RISK-006 | `risk.max_daily_loss_fraction` | Daily loss lockout | 1 | CONDITIONAL | `0` (disabled) | fraction of equity | float; strict loss `< -equity*x` | percent plus Disabled | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0%` | `null (UNKNOWN)` | F+ | 0.1 pp | finite non-negative | requires canonical realized+unrealized daily P&L | MC; R | portfolio governor | `.env:152` | code/dataclass/example | LIVE=SAME | false confidence because runtime provider may omit P&L | `src/risk/portfolio.py:38,392-402`; `src/core/execution_config.py:355-360` | HIGH | investigate; not first wave |
| RISK-007 | `risk.max_drawdown_fraction` | Equity drawdown lockout | 1 | CONDITIONAL | `0` (disabled) | fraction | float; triggers `>=` | percent plus Disabled | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0%` | `null (UNKNOWN)` | F+ | 0.1 pp | finite non-negative | high-water equity must be canonical and reset policy defined | MC; R | portfolio governor | `.env:153` | code/dataclass/example | LIVE=SAME | stale high-water mark blocks/permits entries | `src/risk/portfolio.py:39,404-411`; `.env:153` | HIGH | investigate |
| RISK-008 | `risk.max_sector_notional_fraction` | Maximum sector exposure | 1 | CONDITIONAL | `0` (disabled) | fraction of equity | float | percent plus Disabled | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0%` | `100% (RECOMMENDED)` | F+ | 1 pp | finite non-negative | every position/proposal needs sector or fails closed when enabled | MC+NP; R | portfolio governor | `.env:154` | code/dataclass/example | LIVE=SAME | missing classification blocks all entries or concentration grows unchecked | `src/risk/portfolio.py:40,413-448`; `.env:154` | HIGH | expert-only after classifications canonical |
| RISK-009 | `risk.max_industry_notional_fraction` | Maximum industry exposure | 1 | CONDITIONAL | `0` (disabled) | fraction | float | percent plus Disabled | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0%` | `100% (RECOMMENDED)` | F+ | 1 pp | finite non-negative | industry required when enabled | MC+NP; R | portfolio governor | `.env:155` | code/dataclass/example | LIVE=SAME | same as sector | `src/risk/portfolio.py:41,413-448`; `.env:155` | HIGH | expert-only |
| RISK-010 | `risk.max_correlation_group_fraction` | Maximum correlation-group exposure | 1 | CONDITIONAL | `0` (disabled) | fraction | float | percent plus Disabled | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0%` | `100% (RECOMMENDED)` | F+ | 1 pp | finite non-negative | correlation group required when enabled | MC+NP; R | portfolio governor | `.env:156` | code/dataclass/example | LIVE=SAME | correlated loss concentration | `src/risk/portfolio.py:42,413-448`; `.env:156` | HIGH | expert-only |
| RISK-011 | `risk.max_strategy_notional_fraction` | Maximum strategy exposure | 1 | CONDITIONAL | `0` (disabled) | fraction | float | percent plus Disabled | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0%` | `100% (RECOMMENDED)` | F+ | 1 pp | finite non-negative | strategy ID required when enabled | MC+NP; R | portfolio governor | `.env:157` | code/dataclass/example | LIVE=SAME | strategy concentration or blanket blocks | `src/risk/portfolio.py:43,413-448`; `.env:157` | HIGH | expert-only |
| RISK-012 | `risk.max_fx_age_seconds` | Maximum FX quote age | 1 | CONDITIONAL | `300` | seconds | float/timedelta | minutes | `>0 (CODE)` | `null (UNKNOWN)` | `1s` | `null (UNKNOWN)` | SEC+ | 1 s | finite positive | only applies to non-USD equity; source timestamp required | MC; R | portfolio governor | `.env:158` | dataclass `5 minutes`, example | LIVE=SAME | stale conversion misstates every cap | `src/risk/portfolio.py:44,450-463`; `.env:158` | HIGH | expert-only |
| SIZE-001 | `orb.capital_min_percent` | ORB minimum capital allocation | 2 | SAFE | `10.0` | percentage points | float points | percent | `0 inclusive (CODE)` | `< capital_max (CODE)` | `0%` | `100%` | PP100 + cross-field | 0.5 pp, 2 dp | finite | min <= ideal <= max; plan uses lower inclusive | MC+NP; queue refresh | ORB validator/scorer/dialog | `data/settings.json:4` | code fallback 10 | LIVE=SAME; device-local | rejecting small valid plans | `src/risk/orb_position.py:18,29-46,202-204`; `src/ui/orb_settings_dialog.py:63-80` | HIGH | expose; add sync |
| SIZE-002 | `orb.capital_ideal_percent` | ORB ideal capital allocation | 2 | SAFE | `17.5` | percentage points | float points | percent | `capital_min inclusive (CODE)` | `capital_max inclusive (CODE)` | same | same | CODE_ENFORCED | 0.5 pp, 2 dp | finite | between bounds | MC+NP; queue refresh | ORB scorer | `data/settings.json:5` | code fallback 17.5 | LIVE=SAME; device-local | changes ranking only | `src/risk/orb_position.py:19,33-38,283-290` | HIGH | expose; add sync |
| SIZE-003 | `orb.capital_max_percent` | ORB maximum capital allocation | 2 | CONDITIONAL | `28.0`; code fallback `30.0` | percentage points | float points; plan rejects `>= max` | percent | `> capital_min (CODE)` | `100 inclusive (CODE)` | `10%` | `30% (BUSINESS_RULE)` | PP100 + cross-field | 0.5 pp, 2 dp | finite | ideal <= max; also portfolio exposure/buying power | MC+NP+NO; queue refresh | ORB validator/scorer/pre-trade | `data/settings.json:6` | code fallback and rulebook 30 | LIVE=SAME; device-local | permits oversized single name | `src/risk/orb_position.py:20,29-46,202-204`; `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md:336-341` | HIGH | expose with warning; add sync |
| SIZE-004 | `orb.stop_adr_min_percent` | ORB minimum stop/ADR | 2 | SAFE | `20.0`; code fallback `15.0` | percent of ADR | float points | percent of ADR | `0 inclusive (CODE)` | `< max (CODE)` | `0%` | `1000%` | CODE_ENFORCED/UI | 0.5 pp, 2 dp | finite | min <= ideal <= max | MC+NP; queue refresh | ORB validator/scorer | `data/settings.json:7` | code fallback 15 | LIVE=SAME; device-local | rejects tight stops | `src/risk/orb_position.py:21,31-48,213-217` | HIGH | expose; add sync |
| SIZE-005 | `orb.stop_adr_ideal_percent` | ORB ideal stop/ADR | 2 | SAFE | `65.0` | percent of ADR | float points | percent of ADR | `min inclusive (CODE)` | `max inclusive (CODE)` | same | same | CODE_ENFORCED | 0.5 pp, 2 dp | finite | between bounds | MC+NP; queue refresh | ORB scorer | `data/settings.json:8` | code fallback 65 | LIVE=SAME; device-local | ranking drift | `src/risk/orb_position.py:22,39-44,278-290` | HIGH | expose; add sync |
| SIZE-006 | `orb.stop_adr_max_percent` | ORB maximum stop/ADR | 2 | CONDITIONAL | `90.0`; code fallback `66.0` | percent of ADR | float points; inclusive | percent of ADR | `> min (CODE)` | `null (CODE only UI=1000)` | `min` | `100% (BUSINESS_RULE unresolved)` | lower CODE; upper UNKNOWN | 0.5 pp, 2 dp | finite | ideal <= max; stop-loss% must remain `< ADR%` independently | MC+NP+NO; queue refresh | ORB validator/pre-trade | `data/settings.json:9` | code fallback 66 | LIVE=SAME; device-local | permits structurally wide stop | `src/risk/orb_position.py:23,47-48,206-217`; `src/ui/orb_settings_dialog.py:32-36` | HIGH | expose with expert warning |
| SIZE-007 | `sizing.share_rounding_policy` | Position share rounding | 2 | UNSAFE | risk sizing `ceil`; fixed allocation `floor` | shares | integer whole shares | none | n/a | n/a | n/a | n/a | broker/invariant | n/a | whole positive shares | ceil may exceed risk budget by <1 share; must still pass notional/risk governor | internal | position sizers/ORB | code formula | three sizing paths | LIVE=SAME | changing can over-risk or create zero quantity | `src/risk/position_sizer.py:101-105,159-179`; `src/risk/orb_position.py:167-173` | HIGH | keep internal; document one-share overshoot |
| SIZE-008 | `sizing.legacy_models` | Legacy fixed/ATR/Kelly sizing model parameters | 17 | UNCLEAR | fixed 1%; ATR 2.0x; quarter-Kelly 0.25; max-risk fallback 2% | mixed | floats | none yet | model-dependent | model-dependent | null | null | UNKNOWN | null | individual methods validate positivity/0..1 | these paths are not wired to the Buy Board execution plan | NO; redesign required | `PositionSizer` only; main scoring uses risk-based path | code defaults | dead template repeats 2% | LIVE=SAME | exposing dead paths creates false controls | `src/risk/position_sizer.py:52,74-110,182-208,249-256`; `config/template_config.py:18` | MEDIUM | investigate usage before any UI |
| SIZE-009 | `sizing.planning_account_equity_usd` | Planning account-equity base | 2 | CONDITIONAL | configured PROD: fresh KIS total equity; visible initial `$100,000`; no-profile/scoring fallback `$10,000` | USD | positive finite float | read-only broker equity in PROD; currency input only for offline planning | `>0 (CODE downstream)` | `$1e12 inclusive (UI_ONLY)` | `$0.01` | `null (BROKER/ACCOUNT)` | lower CODE; upper UI_ONLY | $0.01, 2 dp | finite positive; configured live profile fails closed without a fresh snapshot | FX must be valid for KRW conversion; same exact-account equity must feed risk sizing, queue and portfolio governor | broker refresh anytime; manual planning change NP; never a live execution override | scoring, planning, execution queue, portfolio projection | fresh KIS total equity for configured PROD; manual cache only without profile | UI initializer100k, scoring/dashboard fallback10k, dead KRW template | LIVE uses exact-account snapshot/fails closed; lightweight tests inject sizes | 10x sizing drift or unsafe trust in a manual balance | `src/ui/charts/controller_layout.py:148,165-169`; `src/core/scoring.py:68,182-207`; `src/ui/mixins/dashboard_mixin.py:1161-1199,1219-1254` | HIGH | keep PROD read-only; consolidate offline-planning fallback before exposure |
| ENTRY-001 | `entry.breakout_price` | Daily breakout price | 3 | SAFE | no global default; required per symbol | USD/share | optional positive float persisted per plan/card | price | `>0 (CODE)` | `null (UNKNOWN)` | `0.01` | `null` | POS | broker tick; up to 4 dp below $1 | finite positive when armed | buffered breakout must remain below ORB high; immutable once published/locked | anytime while passive; NP+NO once Buy Today | watchlist, queue, ORB strategy, card | per-symbol persisted plan | watchlist/buylist/card | LIVE=SAME | invalid value prevents entry or shifts trigger | `src/strategy/orb/strategy.py:94-166`; `src/core/execution_queue.py:474-477,654-663` | HIGH | expose as per-trade field (already present) |
| ENTRY-002 | `entry.orb_buffer_percent` | Breakout buffer | 3 | SAFE | `0.1` dashboard points = `0.001` fraction | percent/fraction | `orb_buffer_percent` points; `buffer_pct` fraction | percent | `0 inclusive (CODE)` | `100 inclusive (CODE)` | `0%` | `5% (RECOMMENDED_SAFETY_BOUND)` | CODE, UI max inferred safer | 0.01 pp, 2 dp | finite; divide by 100 exactly once | `breakout*(1+buffer) < orb_high`; existing published plans retain saved buffer | anytime for future plans; NP for queued plan | Buy Board header, strategy, queue/card | `data/settings.json:2` -> UI conversion | code default 0.001 in 7 paths | LIVE=SAME; device-local | points/fraction mix can shift trigger 100x | `src/ui/buyboard/board.py:104-128,154-175`; `src/strategy/orb/config.py:21`; `src/core/execution_queue.py:42` | HIGH | expose; add sync and unit-tagged type |
| ENTRY-003 | `entry.orb_confirmation_invariants` | ORB confirmation rule | 3 | UNSAFE | complete range required; `orb_high > buffered breakout`; price `> orb_high` | Boolean/comparisons | strict inequalities | read-only explanation | n/a | n/a | n/a | n/a | execution invariant | n/a | fail closed on incomplete/invalid bars | range end after start; breakout below high; trigger exactly high | internal; immutable after plan publish | ORB strategy, trading engine precondition | strategy/execution code | README describes a different `max()` trigger | LIVE=SAME | relaxing submits entries without a valid range | `src/strategy/orb/strategy.py:55-89,94-211`; `src/services/trading_engine.py:140-161` | HIGH | keep internal |
| ENTRY-004 | `entry.candidate_upgrade_margin` | ORB plan upgrade score margin | 3 | CONDITIONAL | `0.0` | score points | non-negative float via `max(0, value)` at comparison | score margin | `0 inclusive (CODE)` | `null (UNKNOWN)` | `0` | `null` | NN | 0.1, 1 dp | finite; non-negative | new candidate replaces only if score >= old + margin; locked plans immutable | pre-market; NP | execution queue manager | persisted manager fallback | UI resets manager to 0 | LIVE=SAME | churn at zero; excessive margin preserves inferior plan | `src/core/execution_queue.py:921-948,1004-1005,1328-1340`; `src/ui/buylist/view.py:619` | HIGH | expert-only after sync |
| ENTRY-005 | `entry.orb_risk_case_menu` | ORB comparison risk cases | 3 | SAFE | 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2% | fractions | tuple `0.0025..0.02` | percent multi-select/preset | `>0 (CODE downstream)` | `1 inclusive (CODE downstream)` | `0.25%` | `2% (BUSINESS_RULE)` | F01/rulebook | 0.25 pp | unique finite fractions | each case must satisfy capital/stop/portfolio caps | pre-market; read-only comparison | ORB combinations/planning mixin | `src/core/orb_combinations.py` | duplicated inline in planning mixin and queue | LIVE=SAME | list drift yields different recommendations | `src/core/orb_combinations.py:27-36`; `src/ui/mixins/planning_support_mixin.py:113-119`; `src/core/execution_queue.py:763-777` | HIGH | expose presets after consolidation |
| ORB-001 | `orb.windows` | Enabled ORB windows | 4 | CONDITIONAL | execution: 1m, 5m, 30m; strategy also declares 1h | minutes/enums | tuple/map | multi-select | `1 minute (current code)` | `30 minutes execution; 60 declared only` | current set | current set | CODE_ENFORCED | discrete | value in supported tuple | data source must supply complete bars; window end before session close | MC+NP+NO; rebuild queue | queue, strategy, combinations | execution tuple wins | strategy map includes 1h | LIVE=SAME | enabling unsupported 1h creates plans execution cannot consume | `src/core/execution_queue.py:41,395`; `src/strategy/orb/config.py:9-14` | HIGH | consolidate first; expert-only |
| ORB-002 | `orb.default_window` | Default ORB window | 4 | SAFE | `5m` | enum | string | dropdown | allowed set only | allowed set only | `5m` | `5m` | CODE_ENFORCED | discrete | member of enabled windows | must have matching complete bar data | MC+NP | standalone ORB strategy; queue evaluates all windows | strategy config | tests assume 5m in many fixtures | LIVE=SAME | selects different stop/range | `src/strategy/orb/config.py:20`; `tests/test_buyboard_runtime.py:47` | HIGH | expose after window consolidation |
| ORB-003 | `orb.market_open` | ORB session anchor | 4 | UNSAFE | 09:30 America/New_York | local time | `datetime.time` | read-only | exchange schedule | exchange schedule | n/a | n/a | EXCHANGE/BUSINESS_RULE | n/a | calendar-derived only | timezone and DST required | internal | range calculator | strategy config/calendar | repeated in market helpers/UI | LIVE=SAME | wrong anchor invalidates every range | `src/strategy/orb/config.py:22`; `src/strategy/orb/strategy.py:38-49,55-89` | HIGH | keep internal; derive from calendar |
| ORB-004 | `orb.probe_mode` | Pre-confirmation probe entries | 4 | UNSAFE | disabled; dormant size multiplier `0.5` | Boolean/fraction | bool + fraction | none | n/a | n/a | n/a | n/a | safety invariant until designed | n/a | requires confirmation price and `price <= confirmation` | portfolio risk and partial-fill semantics not specified | internal; restart | ORB strategy only; execution config does not enable | strategy config | no production activation path found | LIVE=SAME | could authorize early half-size entries | `src/strategy/orb/config.py:24-25`; `src/strategy/orb/strategy.py:184-203` | HIGH | keep internal |
| STOP-001 | `stop.initial_type` | Initial protective stop | 5 | CONDITIONAL | selected entry ORB low | USD/share / enum | immutable persisted `ORB_LOW` price | price plus policy label | `>0 (CODE upstream)` | `< entry (CODE)` | broker tick | entry minus one tick | POS/cross-field | tick precision | finite positive, stop < entry | selected ORB low must match approved plan; cannot change while order pending | set at first fill; later only tighten; NP | position manager, pre-trade plan | persisted selected plan | queue/scoring calculate same | LIVE=SAME | missing/wrong stop invalidates sizing or leaves exposure | `src/services/position_manager.py:259-273`; `src/risk/orb_position.py:153-164` | HIGH | expose only as per-trade review, not free global default |
| STOP-002 | `stop.legacy_default_adr_multiple` | Legacy default stop distance | 5 | CONDITIONAL | `0.75 * ADR%` below entry when no stop is supplied | ADR multiple | float formula | ADR multiple | `>0 (MATH)` | constrained so stop >0 and <entry | `0.75` | `null (UNKNOWN)` | math/cross-field | 0.05, 2 dp | finite positive | result must be positive, below entry, and stop loss < ADR for ORB | passive planning only; NO | scoring/trade-plan helper, not selected ORB live stop | inline scoring fallback | no config source | LIVE=SAME | hidden fallback changes risk per share | `src/core/scoring.py:168-175` | HIGH | expert-only or remove from active terminology later |
| STOP-003 | `stop.breakeven_buffer_bps` | Breakeven fee/slippage buffer | 5 | CONDITIONAL | `15` fallback (not set locally) | basis points | float / 10,000 | bps | `null (UNKNOWN; negative currently accepted)` | `null (UNKNOWN)` | `0 bps (RECOMMENDED)` | account-specific | UNKNOWN; comment says account-specific | 1 bp, 0 dp | finite and account fee reviewed; current parser lacks checks | rounded up to valid tick; must not lower active stop | MC+NP; R; positions require controlled migration | position manager | execution config fallback | `.env.example` omits it | LIVE=SAME | negative/wrong fee estimate widens risk or triggers premature exit | `src/core/execution_config.py:401-407`; `src/services/position_manager.py:136-158` | HIGH | expert-only after account-fee decision |
| STOP-004 | `stop.invariants` | Manual-stop and trigger invariants | 5 | UNSAFE | manual stop >= max(breakeven,current); trigger at price <= stop; trigger sticky | comparisons | exact invariant | read-only | n/a | n/a | n/a | n/a | execution safety | n/a | never widen a long stop; trigger cannot be undone by rebound | requested stop should also be below market/entry, but code lacks that check | internal | position manager/trading engine | code | legacy stop path duplicates trigger | LIVE=SAME | changing can widen loss or cancel liquidation | `src/services/position_manager.py:149-174,281-310`; `src/ui/buylist/monitoring.py:520-537` | HIGH | keep internal; add missing upper validation later |
| EXIT-001 | `exit.first_partial_fraction` | First partial exit size | 6 | SAFE | exactly one third, `max(1, shares//3)` | fraction/shares | integer floor with min one | percent or shares | `1 share (CODE)` | `< remaining shares for partial; otherwise full exit` | `33.33%` | `50% (DOCUMENTED BUSINESS_RULE)` | CODE/cross-field | 1 share or 1% | positive whole shares | <= orderable; request >= orderable becomes Sell All | anytime as explicit command; NP for another exit | exit policy/UI/workflow | code formula | docs/UI say 1/3–1/2 | LIVE=SAME | selling wrong portion; one-share positions become full exit | `src/core/exit_policy.py:52-59`; `src/ui/buylist/view.py:175`; `src/services/execution_workflow_service.py:977-1013` | HIGH | expose explicit partial quantity; clarify default |
| EXIT-002 | `exit.partial_review_days` | Partial-exit review window | 6 | SAFE | days held 3 through 5 inclusive | trading days | integer comparison | day range | `0 inclusive (MATH)` | `null (UNKNOWN)` | `3` | `5` | BUSINESS_RULE | 1 day | integer lower <= upper | only when shares>0 and first partial not done | anytime; affects alerts only | legacy monitor/view | inline UI logic | docs/rulebooks repeat | LIVE=SAME | early/late alert; no automatic sell | `src/ui/buylist/monitoring.py:608-625`; `src/ui/buylist/view.py:493`; `README.md:33` | HIGH | expose alert window after defining trading-day count |
| EXIT-003 | `exit.momentum_ema_policy` | Remaining-position EMA exit | 6 | CONDITIONAL | EMA10 checked first, then EMA20; alert only after partial | daily bars | periods 10/20 and priority | dropdown intended but absent | `period >=1 (MATH)` | `null (UNKNOWN)` | 10 | 20 | BUSINESS_RULE | 1 bar | positive integer; completed daily close only | selected EMA must be persisted per trade; current code has no selection field | NO for policy change; daily-close evaluation | exit policy/monitor | inline code | docs say selected EMA | LIVE=SAME | EMA20 is often unreachable when price is below both; path mismatch | `src/core/exit_policy.py:62-75,101-118`; `src/ui/buylist/monitoring.py:634-645,827-838` | HIGH | consolidate and persist selection first |
| EXIT-004 | `exit.partial_quantity` | Manual partial-sell quantity | 6 | SAFE | operator-supplied; no default | shares | positive int | shares | `1 inclusive (CODE)` | current orderable quantity inclusive; equality becomes full exit | `1` | current orderable | CODE/BROKER | 1 share | whole positive | `< orderable` for partial; `>=` becomes Sell All; clamp on submission | NP; position open | workflow and engine | command | shared exit command validates positive | LIVE=SAME | oversell prevented; stale orderable may change outcome | `src/services/execution_workflow_service.py:977-1013`; `src/services/trading_engine.py:1286-1314` | HIGH | expose (already command field) |
| EXIT-005 | `exit.legacy_profit_target` | Legacy take-profit/target field | 17 | UNCLEAR | active ORB: none/disabled; legacy UI blank, persisted fallback `0.0`/`None` | USD/share | optional/required legacy floats; positive `target_price` migrates to `breakout_price` | none; compatibility field only | `0 inclusive (UI_ONLY)` | `$1e9 inclusive (UI_ONLY)` | `null` | `null` | UNKNOWN/legacy UI validator | null | active scoring emits target0; legacy positive target is treated as structural breakout, not profit objective | must never coexist ambiguously with breakout or imply an automatic exit | legacy load/review only; remove only with versioned data migration | compatibility persistence, old review model, chart input | README and active scoring: no fixed profit target | `take_profit` and `target_price` across UI/watchlist/reviewer/scoring | LIVE=SAME; tests assert migration/no R/R target | exposing it can reintroduce a false exit control or overwrite breakout meaning | `README.md:28-33`; `src/ui/charts/controller_layout.py:146,153-162`; `src/core/watchlist.py:61-64,128-140,257-272,515-525`; `src/core/scoring.py:211-216,362` | HIGH | keep hidden; inventory for compatibility cleanup only |
| ORD-001 | `execution.sell_discount_fraction` | Marketable sell discount | 7 | CONDITIONAL | `0.005` (0.5%) | fraction | float multiplier | percent | `0 inclusive (MATH)` | `<1 exclusive (MATH)` | `0%` | `5% (existing emergency hard cap)` | math/code cap | 0.05 pp, 2 dp | finite `0<=x<1` (not currently enforced) | broker tick/floor; emergency scaling | MC+NP; R | shared exit builder and legacy path | execution config fallback | UI constant duplicates 0.005 | LIVE=SAME | too low leaves order; too high increases slippage | `src/core/execution_config.py:80-86`; `src/core/exit_execution_command.py:67-80`; `src/ui/buylist/constants.py:10` | HIGH | expert-only; unify paths |
| ORD-002 | `execution.emergency_collar_policy` | Emergency sell collar widening | 7 | UNSAFE | discount × attempt number; capped 5%; max 3 reprices | fraction/count | formula | read-only | n/a | n/a | n/a | n/a | execution safety | n/a | bounded and fresh-id/cancel-confirm lifecycle required | no replacement before cancel confirmation | internal | exit command/runtime | code | tests assert cap consumption | LIVE=SAME | excessive slippage or duplicate orders | `src/core/exit_execution_command.py:72-80`; `src/core/execution_config.py:294-295`; `src/services/buyboard_runtime.py:1394-1403` | HIGH | keep internal |
| ORD-003 | `execution.legacy_stop_reprice_drop_fraction` | Legacy stop reprice minimum drop | 7 | CONDITIONAL | `0.002` (0.2%) | fraction | float comparison | percent | `0 inclusive (MATH)` | `<1 exclusive (MATH)` | `0%` | `null (UNKNOWN)` | math | 0.01 pp | finite 0<=x<1 | new price also uses 0.5% discount; only after cancellable working sell | NP; R | legacy buylist actions | UI constant | no core equivalent | LIVE=SAME | too high leaves stale sell; zero causes churn | `src/ui/buylist/constants.py:11`; `src/ui/buylist/actions.py:676-725` | HIGH | expert-only if legacy path remains |
| ORD-004 | `execution.us_equity_tick_policy` | US equity price ticks and floor | 7 | UNSAFE | tick $0.0001 below $1, $0.01 at/above; core sell floor $0.01, legacy floor $0.0001 | USD/share | decimal round-down/up | read-only | broker rules | broker rules | n/a | n/a | BROKER_ENFORCED | 4/2 dp | use broker-specific tick formatter | entry/exit rounding direction differs by purpose | internal | KIS formatter, stop/breakeven, exit builder | broker adapter | core/legacy floor conflict | LIVE=SAME | broker rejection or materially wrong sell price | `src/api/kis_order.py:290-297`; `src/services/position_manager.py:119-133`; `src/core/exit_execution_command.py:70,80`; `src/ui/buylist/actions.py:672-674` | HIGH | keep internal; resolve floor conflict |
| ORD-005 | `execution.quantity_invariants` | Order quantity limits | 7 | UNSAFE | positive whole shares; sell <= refreshed orderable; one card/account/symbol | shares | int | read-only | `1 inclusive (CODE/BROKER)` | available/orderable quantity for sell | n/a | n/a | CODE/BROKER | 1 | finite whole, non-Boolean | reservation, position, and command quantities must agree | internal | pre-trade, workflow, broker adapter | broker/runtime truth | tests cover fractional/zero rejection | LIVE=SAME | zero, negative, fractional or oversell order | `src/risk/pre_trade.py:21-31`; `src/core/exit_execution_command.py:96-118`; `src/services/execution_workflow_service.py:983-1013` | HIGH | keep internal |
| LIFE-001 | `orders.entry_ttl_seconds` | Entry order lifetime | 8 | CONDITIONAL | `15` | seconds | int | seconds | `>0 (MATH)` | `null (UNKNOWN)` | `1` | `null` | SEC+ | 1 s | positive integer; current env parser does not enforce | cancel then confirm before reprice; market/session dependent | MC+NP; R | entry attempt manager | code fallback (not in local env) | comments/tests | LIVE=SAME | too short churns; too long leaves stale entry | `src/core/execution_config.py:54-59`; `src/services/entry_attempt_manager.py:390` | HIGH | expert-only |
| LIFE-002 | `orders.entry_retry_cooldown_seconds` | Entry retry cooldown | 8 | CONDITIONAL | `3` | seconds | int | seconds | `>=0 (MATH)` | `null (UNKNOWN)` | `0` | `null` | inferred/SEC+ | 1 s | non-negative integer | rate limit and max-attempt window | MC+NP; R | entry attempt manager | code fallback | repeated consumers | LIVE=SAME | retry burst or missed entry | `src/core/execution_config.py:55-56`; `src/services/entry_attempt_manager.py:372,459,477,502,575` | HIGH | expert-only |
| LIFE-003 | `orders.max_entry_attempts_per_symbol_minute` | Entry attempt rate fence | 8 | UNSAFE | `4` | attempts/min/symbol | int/sliding timestamps | read-only | n/a | n/a | n/a | n/a | idempotency/rate safety | n/a | positive and coupled to broker budget | deterministic identities and reconciliation mandatory | internal | entry attempt manager | execution config | no local override | LIVE=SAME | duplicate entry orders | `src/core/execution_config.py:57-59`; `src/services/entry_attempt_manager.py:329-333` | HIGH | keep internal |
| LIFE-004 | `orders.exit_retry_cooldown_seconds` | Exit retry cooldown | 8 | CONDITIONAL | `5` | seconds | int | seconds | `>=0 (MATH)` | `null (UNKNOWN)` | `1` | `null` | inferred | 1 s | non-negative integer | protective exit urgency vs rate limits; no retry before reconciliation | MC+NP; R | trading engine/runtime worker | execution config | no example entry | LIVE=SAME | rapid duplicate attempts or prolonged exposure | `src/core/execution_config.py:61-65`; `src/ui/buyboard/runtime_worker.py:2808` | HIGH | expert-only |
| LIFE-005 | `orders.exit_ttl_seconds` | Partial/Sell-All order lifetimes | 8 | CONDITIONAL | partial `10`; Sell All `5` | seconds | two ints | seconds by intent | `>0 (MATH)` | `null (UNKNOWN)` | `1` | `null` | SEC+ | 1 s | positive integer | cancel-confirm must complete before replacement | MC+NP; R | trading engine/runtime | execution config | no env example | LIVE=SAME | stale exit or excessive cancel/reprice | `src/core/execution_config.py:67-78`; `src/services/buyboard_runtime.py:1489` | HIGH | expert-only |
| LIFE-006 | `orders.exit_cancel_confirmation_timeout_seconds` | Exit cancel confirmation timeout | 8 | UNSAFE | `10` | seconds | int | read-only | n/a | n/a | n/a | n/a | idempotency invariant | n/a | positive; must exceed realistic broker propagation | replacement forbidden until terminal evidence | internal | runtime alerts/reconciliation | execution config | alert type/docs | LIVE=SAME | duplicate sell or unresolved liquidation | `src/core/execution_config.py:76-78`; `src/ui/buyboard/runtime_worker.py:1910-1951` | HIGH | keep internal |
| LIFE-007 | `orders.pretrade_approval_ttl_seconds` | Pre-trade approval lifetime | 8 | UNSAFE | `30` | seconds | timedelta | read-only | n/a | n/a | n/a | n/a | safety/idempotency | n/a | exact command fingerprint; aware timestamps; TTL <=30 | must be re-evaluated after delays or changed plan/price/quantity | internal | pre-trade/gateway | code | tests use decision timestamps | LIVE=SAME | stale approval authorizes changed order | `src/risk/pre_trade.py:14,61-88,182-198` | HIGH | keep internal |
| LIFE-008 | `orders.reconciliation_timing_profile` | Order/account reconciliation profile | 8 | UNSAFE | pending 2s; unknown 1s; active account 5s; idle 20s; full 60s; ambiguous candidate 60s; absence confirmation 60s/two generations; durable observation 3600s | seconds/generations | coupled timers | read-only | n/a | n/a | n/a | n/a | reconciliation correctness | n/a | existing floors: pending>=2, unknown>=1, durable>=3600 | cannot disable absence confirmation, unknown-state discovery, or two-generation proof | internal; R | runtime worker/account reconciliation | execution config | env/example for first two | MOCK-DIFF only injected clocks | orphan/duplicate orders, false flat state | `src/core/execution_config.py:182-212`; `src/services/account_reconciliation.py:778,826,1147,1155` | HIGH | keep internal |
| SESSION-001 | `session.nyse_regular_calendar` | NYSE regular session calendar | 9 | UNSAFE | 09:30–16:00 ET; recurring early close 13:00; recurring holidays | exchange time/calendar | timezone-aware functions | read-only | exchange-defined | exchange-defined | n/a | n/a | EXCHANGE/BUSINESS_RULE | n/a | derive, do not free-type | ORB anchor, entry gate, EOD, MOO policy must share calendar | internal | market calendar, runtime, legacy UI | `src/utils/market_calendar.py` for production runtime | many UI modules duplicate hours without holiday/early-close awareness | MOCK-DIFF: TradingEngine constructor defaults always-open unless production injects calendar | orders outside session or missed EOD | `src/utils/market_calendar.py:9-15,56-151`; `src/services/trading_engine.py:278-283,1704-1712` | HIGH | keep internal and remove duplicated clocks later |
| SESSION-002 | `session.manual_exit_outside_regular` | Outside-session manual exit policy | 9 | UNSAFE | PROD partial/manual exits reserve MOO; other exits use regular limit/queued-at-open logic | enum | intent/session policy | read-only | n/a | n/a | n/a | n/a | BROKER/SAFETY | n/a | exact intent, environment, session | reserved MOO requires limit price 0; stop/Sell All lifecycle differs | internal | shared exit command, legacy orders | code | README documents behavior | LIVE differs from non-PROD by design | wrong type can reject or execute at unintended time | `src/core/exit_execution_command.py:21-41,96-118`; `README.md:124-125` | HIGH | keep internal |
| SESSION-003 | `session.eod_entry_cleanup_seconds_before_close` | Entry cleanup window before close | 9 | CONDITIONAL | `60` | seconds before close | int comparison; remains active post-close | seconds | `>=0 (MATH)` | session length (MATH) | `0` | `null (UNKNOWN)` | math; upper repository-unknown | 1 s | non-negative integer | close must come from early-close-aware calendar; cleanup semantics depend on state/order truth | MC; R | runtime/EOD service | code fallback | no local/example value | LIVE=SAME; injected false in tests unless composed | stale Buy Today/Entry Pending or premature cancellation | `src/core/execution_config.py:396-399`; `src/services/buyboard_runtime.py:248-253`; `src/services/eod_trading_service.py:109-142` | HIGH | expert-only |
| FILTER-001 | `scanner.setups.rules` | Scanner rule set | 10 | SAFE | two local setups; arbitrary attribute/operator/threshold list | mixed | persisted JSON list | rule builder | operator/metric-specific | operator/metric-specific | current values | metric-specific | CODE/MATH per metric | metric-specific | known attribute/operator; finite compatible threshold | implicit history rule is always added in DB path | anytime; rescan required | scanner/UI/DB query builder | `data/scanner_setups.json` | default setup catalog duplicates summary fields/rules | LIVE=SAME; device-local | eligibility differs across devices | `data/scanner_setups.json:2-81`; `src/infrastructure/database/repositories/scanner.py:464-523`; `src/ui/main_window.py:3474-3554` | HIGH | expose (already UI); add sync and schema |
| FILTER-002 | `scanner.minimum_history_days` | Minimum price history | 10 | CONDITIONAL | runtime implicit `>=1` and compute needs `min+1` rows; docs say >1 or >=65 | days/bars | integer | days | `1 inclusive (CODE query)` | `null (UNKNOWN)` | `65 (DOCUMENTED RECOMMENDATION)` | `252 (DOCUMENTED IDEAL)` | CODE vs BUSINESS_RULE conflict | 1 | positive integer | lookback must retrieve at least min+1 rows | anytime; metrics rebuild/rescan | metric computation and DB base condition | DB query hard-code wins | scanner method default 1, docs/rulebooks differ | LIVE=SAME | insufficient history gives unstable 200-day/RS metrics | `src/utils/data_loader.py:869-878`; `src/infrastructure/database/repositories/scanner.py:120-143,492`; `src/ui/filter_catalog.py:122` | HIGH | consolidate first |
| FILTER-003 | `scanner.setup1` | Scanner Setup 1 thresholds | 10 | SAFE | volume>=40,000; dollar volume>=35,000; ADR20>=2.4 pp; growth rank1m>=97.04; trend>=90; price>=5; return1m<1500 | mixed | JSON thresholds; numeric strings accepted | per-rule controls | metric-specific (volume/$/price >=0; ranks 0..100) | volume/$/price/return upper null; ranks/trend 100 | current | current until product review | MATH/CODE | volume 1; money .01; percentages .01 | finite; operator compatible | ADR/growth/trend metrics must use same definitions; implicit history applies | anytime; rescan; device sync | scanner/DB | local JSON | default catalog lacks last two rules and duplicates first five | LIVE=SAME; device-local | major universe change; 1500% cap is effectively inert | `data/scanner_setups.json:3-45`; `src/ui/filter_catalog.py:21-35` | HIGH | expose after sync |
| FILTER-004 | `scanner.setup2` | Scanner Setup 2 thresholds | 10 | SAFE | volume>=250,000; dollar volume>=5,000,000; ADR20>=3; growth rank1m>=95; trend>=80 | mixed | JSON | per-rule controls | metric-specific | as FILTER-003 | current | current | MATH/CODE | metric-specific | finite/operator-compatible | implicit history applies | anytime; rescan; device sync | scanner/DB | local JSON | default catalog same values | LIVE=SAME; device-local | eligibility drift | `data/scanner_setups.json:47-80`; `src/ui/filter_catalog.py:36-49` | HIGH | expose after sync |
| FILTER-005 | `scanner.metric_lookback_profile` | Scanner metric lookbacks | 10 | CONDITIONAL | averages 20; ADR20 min 5; ATR14; returns 5/21/63/126 sessions; MAs 10/20/50/200; highs 20/50/252; consolidation 10; RS 252/50/20 | bars | inline rolling windows/index offsets | advanced profile | `>=1 bar (MATH)` | `null (UNKNOWN)` | current | current | MATH/BUSINESS_RULE | 1 bar | positive integers; offsets require one extra row | changing invalidates scanner cache/version and all threshold meanings | MC; cache rebuild; R | metric computation/database cache | inline code | UI labels/docs repeat some windows | LIVE=SAME | thresholds cease to be comparable; stale cache mixes formulas | `src/utils/data_loader.py:892-1020`; `src/infrastructure/database/schema.py:307-362` | HIGH | expert-only as a versioned profile, not individual live knobs |
| FILTER-006 | `scanner.trend_formula` | Trend intensity/scoring formula | 10 | CONDITIONAL | `100*tanh((price/EMA50-1)*20)`, clamped 0..100; trend score adds ±10/±10/+20 | score points | inline formula | advanced formula/profile | `0 inclusive (CODE output)` | `100 inclusive for intensity; trend score can exceed range` | current | current | CODE output | null | finite inputs | scanner thresholds 80/90 assume exact formula | MC; rebuild cache | metric computation/scanner | code formula | UI describes scaled score but not coefficients | LIVE=SAME | changes eligibility across whole universe | `src/utils/data_loader.py:933-940`; `src/ui/filter_catalog.py:140-141` | HIGH | expert-only/versioned |
| FILTER-007 | `scanner.volume_dryup_formula` | Volume dry-up definition | 17 | UNCLEAR | runtime minimum one-day volume in last 10 / average20; docs say average5 / average20 | ratio | inline formula | none until resolved | `0 inclusive (MATH)` | `null (UNKNOWN)` | null | null | UNKNOWN | null | finite non-negative | thresholds 0.8/0.6 in docs refer to different formula | rebuild cache | metric computation | runtime code wins | filter catalog conflict | LIVE=SAME | misleading scanner results | `src/utils/data_loader.py:942-947`; `src/ui/filter_catalog.py:144` | HIGH | product decision required |
| FILTER-008 | `scanner.consolidation_tightness_formula` | Consolidation tightness definition | 17 | UNCLEAR | runtime `100/(10d_range_pct+1)` (higher=tighter); docs say `range10/ADR20` (lower=tighter) | score/ratio | inline formula | none until resolved | formula-dependent | formula-dependent | null | null | UNKNOWN | null | finite inputs; division protected by +1 only in runtime formula | operator direction and thresholds reverse between definitions | rebuild cache | metric computation | runtime code wins | filter catalog conflict | LIVE=SAME | rule can select the opposite stocks | `src/utils/data_loader.py:960-964`; `src/ui/filter_catalog.py:152-154` | HIGH | product decision required |
| FILTER-009 | `scanner.parabolic_thresholds` | Parabolic extension thresholds | 10 | CONDITIONAL | extension above SMA10 >15 pp OR 5-day return >25 pp | percentage points | strict comparisons | percent | metric lower unbounded for return; threshold finite | `null (UNKNOWN)` | `15%`, `25%` | current | BUSINESS_RULE | 0.5 pp | finite | relies on SMA10 and 5-day definition | MC; rebuild cache/rescan | metric computation | inline code | docs recommend different warning bands 15–20 and 30–40 | LIVE=SAME | chase-risk flag too lax/strict | `src/utils/data_loader.py:966-981`; `src/ui/filter_catalog.py:155-161` | HIGH | expert-only after rule decision |
| FILTER-010 | `scanner.relative_strength_profile` | Relative-strength defaults and windows | 10 | CONDITIONAL | missing SPY -> score90, aboveSMA false, slope0; otherwise 252-rank, SMA50, 20-day slope | score/bars | inline favorable score fallback | advanced | score 0..100 when calculated | 100 | null | null | CODE output; fallback BUSINESS_RULE unknown | 1 score/bar | aligned positive closes | missing reference should not look strong without explicit policy | MC; rebuild cache | metric computation/scanner | inline code | filter catalog expects score threshold 70/80 | LIVE=SAME | missing SPY can pass rank filters with 90 | `src/utils/data_loader.py:983-1020`; `src/ui/filter_catalog.py:162-164` | HIGH | make missing explicit before exposing |
| DATA-001 | `market_data.quote_stale_seconds` | Fallback quote maximum age | 11 | CONDITIONAL | `3` | seconds | int | seconds | `>0 (MATH)` | `null (UNKNOWN)` | `1` | `null` | SEC+ | 0.1 s, 1 dp | finite positive | must exceed feed cadence/queue delay and align WS age checks | MC; R | realtime market-data service | execution config fallback | no local env line; docs architecture 3 | LIVE=SAME | stale price used or healthy feed blocked | `src/core/execution_config.py:209-212`; `src/services/realtime_market_data.py:121-130` | HIGH | expert-only |
| DATA-002 | `market_data.ws_freshness_profile` | WebSocket freshness/skew envelope | 11 | UNSAFE | broker age3s; local receive3s; queue delay1s; clock skew5s; future event1s | seconds | coupled float thresholds | read-only | n/a | n/a | n/a | n/a | protocol safety | n/a | non-negative and jointly verified against timestamp semantics | mode/protocol manifest and broker clock required | internal; R | KIS realtime/runtime worker | local `.env:100-104` | `.env.example`/architecture=3; Gate2 checklist says 2 for first two | LIVE only; mocks inject clocks | stale/future ticks authorize orders | `src/services/kis_realtime_market_data.py:1148-1152,1523-1525,1660-1661`; `.env:100-104` | HIGH | keep internal |
| DATA-003 | `market_data.buying_power_max_age_seconds` | Buying-power snapshot maximum age | 11 | UNSAFE | `15` | seconds | float; stale returns zero | read-only | n/a | n/a | n/a | n/a | safety boundary | n/a | positive; account-scoped; fail closed | refresh cadence must be faster; no manual account figure | internal | buying-power/equity providers | code constant | doc says 10–15 example | LIVE=SAME | stale capital creates oversizing; too short blocks all entries | `src/services/buying_power_cache.py:29-32,93-153` | HIGH | keep internal; surface status only |
| DATA-004 | `ui.buyboard_broker_snapshot_warning_seconds` | Buy Board broker snapshot warning age | 11 | SAFE | `120` | seconds | float UI age check | seconds | `>=0 (MATH)` | `null (UNKNOWN)` | `0` | `120` | UI-only | 1 s | finite non-negative | must not be mistaken for execution-grade 15s/3s gates | anytime | Buy Board controller display | UI constant | none | LIVE=SAME | stale-looking UI or false confidence | `src/ui/buyboard/controller.py:540,832` | HIGH | expose as UI preference only |
| DATA-005 | `market_data.outage_timing` | Existing-position outage grace and ceiling | 11 | CONDITIONAL | high-risk grace15s; all-position max hold120s; 0 disables ceiling in tests | seconds | ints | seconds | `>=0 (CODE permits 0 ceiling)` | `null (UNKNOWN)` | current | current | CODE/BUSINESS_RULE | 1 s | non-negative integers | high-tier grace <= hard ceiling; supervised flag cannot disable unattended ceiling | MC+NP; R; open positions require migration plan | trading engine | local `.env:110-111` | config/example same; tests inject 1/2/5/10/0 | LIVE=SAME | forced sale too soon or unprotected position too long | `src/services/trading_engine.py:1959-1979`; `.env:110-111` | HIGH | expert-only |
| DATA-006 | `market_data.outage_risk_profile` | Outage high-risk classification thresholds | 11 | CONDITIONAL | stop buffer1%; loss2%; account risk1%; concentration20%; stop distance0.5 ATR; supervised-hold false | mixed fractions/ATR/Boolean | coupled env floats/bool | expert profile | fractions logically >=0; stop multiple >=0 | finite uppers not established | current | current | MATH/BUSINESS_RULE/UNKNOWN | 0.1 pp; 0.1 ATR | finite/non-negative; current parser lacks checks | `stop_adr` unit must be corrected; equity/quantity/stop required | MC+NP; R | outage classifier | local `.env:112-117` | example/config same | LIVE=SAME | wrong tier changes forced-liquidation timing | `src/services/trading_engine.py:165-210`; `.env:112-117` | HIGH | investigate unit bug before expert UI |
| DATA-007 | `market_data.high_spread_fraction` | High-spread outage threshold | 11 | CONDITIONAL | bid/ask spread `>=0.02` (2%) | fraction | inline comparison | percent | `0 inclusive (MATH)` | `null (UNKNOWN; mathematically ratio can exceed1)` | `0%` | `null` | MATH/UNKNOWN | 0.1 pp | finite non-negative bid/ask | liquidity-tier label can independently force high risk | MC+NP; R | outage classifier | inline code | not in env/example | LIVE=SAME | unnecessary forced exit or ignored illiquidity | `src/services/trading_engine.py:206-209` | HIGH | expert-only after liquidity policy review |
| WF-001 | `workflow.board_transition_graph` | Buy Board transition graph | 12 | UNSAFE | fixed legal graph from Watchlist through Sell All/Closed | enum graph | set mapping plus system-only bypasses | read-only diagram | n/a | n/a | n/a | n/a | state-machine safety | n/a | only enumerated user edges; broker-owned system edges separately verified | pending/ambiguous order forbids leaving owning state | internal | command workflow, EOD, engine | `ALLOWED_BOARD_TRANSITIONS` | docs/tests repeat | LIVE=SAME | orphan order, duplicate position, false Closed state | `src/core/kanban_transitions.py:26-98`; `src/services/execution_workflow_service.py:977-1044` | HIGH | keep internal |
| WF-002 | `workflow.card_ownership_invariants` | One-card and execution ownership invariants | 12 | UNSAFE | one card per environment/account/symbol; one owner/strategy identity | key/enum/string | identity and registry checks | read-only status | n/a | n/a | n/a | n/a | idempotency/ownership | n/a | exact normalized key; ownership proof before mutation | account number and device/lease identity must never be exposed as stock knobs | internal | transitions, ownership, gateway | code and coordination store | README/docs | LIVE stricter than simple mocks | two devices or strategies trade same symbol | `src/core/kanban_transitions.py:111-136`; `README.md:123`; `src/core/execution_ownership.py:110-120` | HIGH | keep internal |
| WF-003 | `workflow.eod_state_semantics` | EOD state-reset rules | 12 | UNSAFE | Buy Today without order -> Buylist only when due/closed; Entry Pending reconciled; open completion stopped; prior-session intent repaired/expired | state rules | direct verified system transitions | read-only | n/a | n/a | n/a | n/a | reconciliation/state safety | n/a | broker terminal evidence controls transitions | scheduled session, holiday repair, fills, open order correlation | internal | EOD service/engine | code | tests cover each state | MOCK-DIFF timing gate injected | lost order tracking or unintended next-day entry | `src/services/eod_trading_service.py:109-211,213-408`; `src/services/trading_engine.py:1714-1754` | HIGH | keep internal |
| BROKER-001 | `broker.buyboard_engine_enabled` | Buy Board engine availability | 13 | UNSAFE | `true` | Boolean | environment read on call | operator status only | n/a | n/a | n/a | n/a | safety architecture | n/a | Boolean; not trading authorization | independent live mode, switch, lease, reconciliation, market data, budgets, capital and risk gates remain mandatory | operational change; R/controlled recovery | Buy Board composition | local `.env:142` | code fallback/example true | LIVE only meaningful | disabling removes protection lifecycle; enabling does not authorize trading | `src/core/execution_config.py:409-421`; `.env:142` | HIGH | keep operational, not stock setting |
| BROKER-002 | `broker.live_execution_envelope` | Live execution mode and symbol allowlist | 13 | UNSAFE | `CONTROLLED_LIVE`; one allowlisted symbol (STIM) | enum/list | env string/tuple | separate privileged operations surface only | n/a | n/a | n/a | n/a | live-trading safety | n/a | mode in DISABLED/CONTROLLED_LIVE/FULL_LIVE; controlled list nonempty | requires verified WS, mutation budgets/spacing, one attempt, cap, lease/readiness | market closed, explicit runbook, R | controlled-live policy/gateway | local `.env:135-136` | code fallback DISABLED/empty; runbook | PROD-only | unauthorized production mutation | `src/core/execution_config.py:335-349`; `src/services/controlled_live_policy.py:77-111`; `.env:135-136` | HIGH | keep privileged/internal |
| BROKER-003 | `broker.controlled_live_entry_cap_usd` | Controlled-live maximum entry notional | 13 | CONDITIONAL | `$0.01` | USD/order | positive float | USD with expert warning | `>0 in CONTROLLED_LIVE (CODE)` | `null (BROKER/UNKNOWN)` | `$0.01` | broker/account-approved | POS | $0.01, 2 dp | finite positive | allowlisted symbol, mode, portfolio/buying power caps; only BUY entry | market closed, no pending orders, runbook, R | controlled-live policy | local `.env:137` | code fallback/example 0 disabled | PROD-only | mistaken cap blocks pilot or allows large live entry | `src/core/execution_config.py:351-353`; `src/services/controlled_live_policy.py:85-95,167`; `.env:137` | HIGH | privileged expert-only, not ordinary dashboard |
| BROKER-004 | `broker.mutation_budget_profile` | KIS mutation budgets and spacing | 13 | UNSAFE | verified flag/environment-specific capacities; 1s window; current spacing1.1s; max confirmed attempts1 | operations/window | token buckets, priority, typed rejection | read-only readiness | n/a | n/a | n/a | n/a | broker/idempotency safety | n/a | controlled mode requires positive measured capacities, spacing>=0.1, attempts exactly1 | one process-wide scheduler; protective reserve; only explicit pre-acceptance refusal retryable | internal/runbook; R | scheduler/runtime/control policy | env + measured capability | scheduler constructor fallback 5/s and 2 attempts overridden at production composition | LIVE stricter; tests inject policies | throttling, rejection, duplicate mutation | `src/core/execution_config.py:304-332`; `src/services/kis_request_scheduler.py:118-145`; `src/services/controlled_live_policy.py:60-76,96-111` | HIGH | keep internal |
| BROKER-005 | `broker.read_retry_transport_profile` | KIS request timeout/retry/pagination profile | 13 | UNSAFE | order HTTP 15s; scheduler reads 3 attempts, 0.25s exponential backoff, 20 reads/s; account retries3 with 1/2/4s; page caps order10/account20/intraday<=50 | mixed | adapter/scheduler constants | read-only health | n/a | n/a | n/a | n/a | broker/data-integrity | n/a | bounded; partial pagination raises; mutations have separate rules | endpoint rate limits, snapshot completeness, token refresh | internal | KIS adapters/scheduler | code | tests/fakes inject sleepers | MOCK-DIFF sleepers/network | partial account truth or overload | `src/api/kis_order.py:73,591-633`; `src/api/kis_account_snapshot_dual.py:113-117,391-444,730-780`; `src/api/kis_intraday.py:34-35,257-264`; `src/services/kis_request_scheduler.py:121-127` | HIGH | keep internal |
| BROKER-006 | `broker.websocket_profile` | KIS WebSocket activation/capacity/reconnect profile | 13 | UNSAFE | WS enabled/protocol verified/mode from env; approval TTL82800s; auth retry3; reconnect1..30s ±0.5; ACK5s; configured capacity0 until measured; verified hard limit41 | mixed | env + verified constant | read-only readiness | n/a | n/a | n/a | n/a | broker protocol | n/a | fail closed; total capacity <=41; manifest verification | live mode requires WEBSOCKET; fallback is display-only | internal/runbook; R | KIS realtime service | env/config + measured limit | deprecated per-channel capacities0 | LIVE only | unverified feed authorizes entries or subscriptions rejected | `src/core/execution_config.py:214-260`; `src/services/kis_realtime_market_data.py:53,564-567,1583,1882-1892` | HIGH | keep internal |
| BROKER-007 | `broker.orb_formation_source` | Execution ORB data source | 13 | UNSAFE | `KIS_MINUTE_BARS` | enum | env uppercase string | read-only | n/a | n/a | n/a | n/a | execution-data provenance | n/a | reviewed source only | must provide complete, timezone-correct minute bars; display feed cannot silently substitute | internal; R | execution config/intraday provider | code/env example | standalone strategy accepts DataFrame | LIVE may use KIS; tests use frames | display-quality bars produce bad entries/stops | `src/core/execution_config.py:224-226`; `.env.example:93` | HIGH | keep internal |
| UI-001 | `ui.buyboard_position_limit_display` | Buy Board position-limit display | 14 | SAFE | fixed `30`, but enforcement `20` | positions | UI integer | read-only label/color | `1` | `30` | authoritative limit | authoritative limit | UI must derive | 1 | equal runtime governor | none; display only | anytime | Buy Board summary | UI constant (incorrect) | risk config | same display in live/mock | misleading operator status | `src/ui/buyboard/board.py:74,140,420-424`; `.env:148` | HIGH | consolidate first; no independent setting |
| UI-002 | `ui.legacy_monitor_interval_seconds` | Legacy Buylist monitor interval | 14 | SAFE | `60` | seconds | QTimer milliseconds60000 | seconds | `>0 (MATH)` | `null (UNKNOWN)` | `1` | `300 (RECOMMENDED)` | MATH/recommended | 1 s | positive integer | execution-grade Buy Board heartbeat is independent | anytime | legacy monitoring UI | inline UI | displayed as 60s | LIVE=SAME | slow alerts; too fast API load | `src/ui/buylist/monitoring.py:190-220` | HIGH | UI-only if legacy monitor retained |
| UI-003 | `ui.chart_refresh_profile` | Chart/display refresh profile | 14 | SAFE | live intraday5min; TradingView5min; daily-fetch failure cooldown30min; market-data ready07:00 KST | durations/time | duplicated UI constants | minutes/time | positive durations | `null (UNKNOWN)` | current | current | UI/business | 1 min | positive; market-ready time valid | must not be used for execution readiness | anytime | chart/scanner/sidebar mixins | duplicated UI code | at least 8 modules | LIVE=SAME | display staleness/API load only | `src/ui/charts/controller_data_flow.py:47-50`; `src/ui/mixins/scanner_mixin.py:76-79`; `src/utils/market_calendar.py:10,228-240` | HIGH | UI-only; centralize later |
| INT-001 | `internal.coordination_poll_profile` | Cross-device coordination cadence profile | 15 | UNSAFE | active180s; standby300s; DB probe180s; lease20s; heartbeat240s/max age>=300s; ownership proof30s; alert90s; operator20s; off-hours300s; state/board180s; remote fallback3600s; recon cache900s | seconds | env values with hard floors/coupling | read-only Health | n/a | n/a | n/a | n/a | ownership/data-integrity | n/a | retain code floors; max age >= heartbeat+60 | local pulse, DB lease, watchdog and failover jointly designed | internal; R | state sync/runtime repository/watchdog | execution config | `.env.example` | LIVE coordination absent from most unit mocks | split-brain, stale owner, unsafe failover | `src/core/execution_config.py:89-201`; `src/services/state_sync.py:1075-1334` | HIGH | keep internal |
| INT-002 | `internal.protocol_identity` | Coordination/strategy protocol identities | 15 | UNSAFE | RU profile `typed-change-pulse-v6`; strategy instance `buyboard-orb-v1` | strings | protocol/ownership IDs | read-only | n/a | n/a | n/a | n/a | compatibility/idempotency | n/a | exact versioned values | every active peer must agree; strategy ownership persisted | deployment-only; R | coordination/ownership | code/env | docs/readiness | LIVE-specific | incompatible peers share store or ownership | `src/core/execution_config.py:89-93,261-266` | HIGH | keep internal |
| INT-003 | `internal.settings_sync_scope` | Cross-device settings synchronization scope | 15 | UNSAFE | only watchlist, buylist, trade plans, execution queue; not settings/scanner setups | key set | tuple of state keys | read-only status | n/a | n/a | n/a | n/a | data integrity | n/a | publish requires exact key set | future config needs versioning, atomicity, conflict resolution | architecture change/restart | app state/state sync | `SYNCED_STATE_KEYS` | cloud backup includes local files but is not live sync | LIVE/SIM share local files per device | divergent strategy/risk behavior | `src/services/state_sync.py:59-64,902-905`; `src/services/app_state.py:61-65,565,722-723` | HIGH | keep internal; extend deliberately before first wave |
| SEC-001 | `security.kis_app_key` | KIS application key | 16 | SECRET | redacted/not audited | secret | environment string | never display | n/a | n/a | n/a | n/a | credential | n/a | required; redact logs/backups | per environment | secure env/secret store only | KIS adapters | environment | template placeholders | live/sim separate | account/API compromise | `.env.example:62,68`; `src/api/kis_config.py:41` | HIGH | never dashboard setting |
| SEC-002 | `security.kis_app_secret` | KIS application secret | 16 | SECRET | redacted/not audited | secret | environment string | never display | n/a | n/a | n/a | n/a | credential | n/a | required; redact/encrypt | paired with app key/environment | secure env/secret store only | KIS adapters | environment | template placeholders | live/sim separate | account/API compromise | `.env.example:63,69`; `src/api/kis_config.py:42` | HIGH | never dashboard setting |
| SEC-003 | `security.account_identifiers` | Broker account/product identifiers | 16 | SECRET | redacted/not audited | identifier | environment/profile strings | masked status only | n/a | n/a | n/a | n/a | sensitive identity | n/a | broker format/account scope | every order/risk snapshot exact-account scoped | secure profile only | KIS, cards, reconciliation | environment | numbered account profiles supported | live/sim separate | orders sent to wrong account/privacy leak | `.env.example:64,70-71`; `src/api/kis_account_snapshot_dual.py:1096-1111` | HIGH | never stock setting |
| SEC-004 | `security.kis_tokens` | KIS access/approval tokens and caches | 16 | SECRET | redacted/not audited | token/path | memory/cache files | never display token | n/a | n/a | n/a | n/a | credential | n/a | expiry/locking/redaction | cache lock/refresh and environment isolation | secure internal only | KIS HTTP/WS | broker adapter/cache | approval TTL is operational, not token value | live/sim separate | account compromise | `src/api/kis_account_snapshot_dual.py:80,116-197,333-371`; `src/services/kis_realtime_market_data.py:1858-1883` | HIGH | never dashboard setting |
| SEC-005 | `security.database_credentials` | MySQL/coordination database credentials | 16 | SECRET | redacted/not audited | credential | environment strings | never display; connectivity only | n/a | n/a | n/a | n/a | credential | n/a | TLS/least privilege | coordination and cache DB roles differ | secure env only | database engines | environment | example blanks | per device | data exfiltration/coordination takeover | `.env.example:5-18` | HIGH | never dashboard setting |
| SEC-006 | `security.alert_webhook_token` | External alert webhook token | 16 | SECRET | redacted/not audited | token | environment string | never display | n/a | n/a | n/a | n/a | credential | n/a | redact | alert endpoint/transport | secure env only | external alerting | environment | example blank | live operations | forged/leaked alerts | `.env.example:164`; `src/services/external_alerting.py:1239` | HIGH | never dashboard setting |
| SEC-007 | `security.remote_control_token` | PC remote-control token | 16 | SECRET | redacted/not audited | token | environment string | never display | n/a | n/a | n/a | n/a | credential | n/a | both devices must match; redact | remote host/port/Tailscale | secure env only | remote control | environment | example blank | device-specific | unauthorized shutdown/control | `.env.example:183`; `src/services/pc_remote_control.py:91,180-193` | HIGH | never dashboard setting |
| SEC-008 | `security.backup_and_device_identity` | Backup passphrase, encryption and device/ownership identities | 16 | SECRET | values redacted; PBKDF2 600,000 iterations; recommended passphrase>=12 | mixed | secrets/IDs/crypto metadata | never stock dashboard | n/a | n/a | n/a | n/a | credential/crypto/ownership | n/a | cryptographic/versioned rules | device IDs and leases are execution controls | secure internal/operations only | env backup/device state/ownership | code and local state | cloud-backup docs | device-specific | secret exposure, undecryptable backup, split brain | `src/services/env_backup.py:48-52,130,168-173`; `docs/cloud_backup.md:153-156,210-213` | HIGH | never dashboard setting |
| FILTER-011 | `scanner.optional_universe_guards` | Documented optional price, earnings, and float filters | 17 | UNCLEAR | not enforced globally; local Setup1 only has price>=5 | USD/days/shares | documentation values: price below5 or above500, earnings within7 days, float below10M | none until decided | metric-specific | metric-specific | null | null | UNKNOWN/BUSINESS_RULE | null | data availability and exact operators unresolved | earnings/catalyst exception and liquidity exception required | product decision; metrics/cache work | rulebooks only except local minimum-price rule | no runtime authority | technical/fundamental rulebooks | LIVE=SAME | presenting as active would create false confidence | `rulebooks/fundamental_rules.md:54-57,134-138`; `rulebooks/technical_rules.md:197-198`; `data/scanner_setups.json:35-39` | HIGH | investigate/product decision |
| FILTER-012 | `scanner.episodic_gap_profile` | Documented episodic-pivot gap rules | 17 | UNCLEAR | not machine-enforced; rulebook says gap>=10%, first15–30min volume, first red5m candle | percent/minutes/bars | prose-only thresholds | none until decided | null | null | null | null | UNKNOWN/BUSINESS_RULE | null | requires gap, catalyst and intraday-volume definitions | cannot share ordinary breakout rules without setup type | product decision; backtest/version | rulebook only | no consumer found | exact-setup rulebook | LIVE=SAME | false eligibility control or overfit rule | `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md:140-152,188,252-270` | HIGH | investigate; do not expose |
| FILTER-013 | `scanner.momentum_setup_profile` | Documented momentum/parabolic setup thresholds | 17 | UNCLEAR | not machine-enforced; 30–100% over1–3 months, top1–2%, stair-step20–50%, parabolic50–100%/300–1000%, 3–5 up days | percent/days/months/rank | prose-only ranges | none until decided | null | null | null | null | UNKNOWN/BUSINESS_RULE | null | setup/cap-size/timeframe definitions unresolved | overlaps runtime parabolic flag but uses different semantics | product decision; backtest/version | rulebook only | no consumer found | exact-setup rulebook | LIVE=SAME | operator may assume unsupported strategy automation | `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md:12-18,101-117,252-258,310-311` | HIGH | investigate; do not expose |

## 3. Occurrence-level appendix

`Role` is one of authoritative (`AUTH`), duplicated (`DUP`), derived (`DERIVED`), fallback-only (`FALLBACK`), test-only (`TEST`), mock-only (`MOCK`), dead code (`DEAD`), documentation-only (`DOC`), or current local override (`LOCAL`). “Reached” names the production path; test-only scenario data that does not define or assert behavior is deliberately not listed because it cannot control runtime.

| Candidate | File:line | Symbol/context | Literal or expression | Role | Reached during runtime |
|---|---|---|---|---|---|
| RISK-001 | `src/ui/charts/controller_layout.py:152,170` | trade-plan form | `QLineEdit("1")`; validator 0..100 | DUP | user planning input |
| RISK-001 | `src/ui/buylist/view.py:741-748` | `_buylist_risk_fraction` | fallback `0.01`; `raw/100` | AUTH | queue refresh converts UI points to fraction |
| RISK-001 | `src/core/scoring.py:69,193-200` | `calculate_stock_scores` | default `0.01`; `0<x<=1` | DUP | scoring/trade plan |
| RISK-001 | `src/risk/position_sizer.py:52,113-165` | `PositionSizer` | fallback max `0.02`; risk fraction <=1 | DUP | scoring explicitly passes selected fraction |
| RISK-001 | `src/core/watchlist.py:140,368` | `TradePlan` | `0.01` fraction | DUP | JSON trade-plan load |
| RISK-001 | `src/core/watchlist.py:403,541` | `BuylistItem` | `1.0` points | DUP | legacy Buylist persistence/display |
| RISK-001 | `src/core/trade_card_state.py:174,313-314,559` | `TradeCardState` | `1.0` fallback | FALLBACK | card construction/deserialization |
| RISK-001 | `src/services/planning_membership_service.py:169` | `_buylist_from_watchlist` | `risk_percent=1.0` | DUP | passive membership conversion |
| RISK-001 | `src/services/trade_card_repository.py:612-632` | legacy migration | copy item risk or `1.0` without `/100` | AUTH | migration to executable card |
| RISK-001 | `src/services/buyboard_runtime.py:1040-1067` | entry sizing | comment and use as fraction | AUTH | planned quantity fallback sizing |
| RISK-001 | `src/core/execution_queue.py:520-536` | queue display | fraction `*100` | DERIVED | Buylist display projection |
| RISK-001 | `config/template_config.py:18` | old template | `0.02` | DEAD | not imported by runtime |
| RISK-001 | `tests/test_buyboard_runtime.py:37-51` | `_card` fixture | `1.0` | TEST | permissive legacy fixture, not production default proof |
| RISK-002 | `.env:148` | local override | `20` | LOCAL | read at execution-config import |
| RISK-002 | `src/core/execution_config.py:362-364` | env fallback | `30` | FALLBACK | portfolio manager composition |
| RISK-002 | `src/risk/portfolio.py:16,34,48-51` | model cap | `MAX_PORTFOLIO_POSITIONS=30`; valid 1..30 | AUTH | risk-limit construction/validation |
| RISK-002 | `src/services/buyboard_runtime.py:474-508` | manager factory | execution-config value | AUTH | production entry governor |
| RISK-002 | `src/services/health.py:767` | Health | execution-config value | DERIVED | status display |
| RISK-002 | `src/ui/buyboard/board.py:74,140,420-424` | board label | fixed `30` | DUP | UI render |
| RISK-002 | `.env.example:148` | example | `30` | DOC | environment setup |
| RISK-002 | `tests/test_portfolio_risk.py:62` | assertion | constant equals 30 | TEST | validates code ceiling, not local effective value |
| RISK-003 | `.env:149` | local override | `0.10` | LOCAL | config import |
| RISK-003 | `src/core/execution_config.py:365-367` | env fallback | `0.10` | FALLBACK | runtime composition |
| RISK-003 | `src/risk/portfolio.py:35,361-390` | limits/evaluation | `0.10`; projected `>` cap | AUTH | pre-entry portfolio evaluation |
| RISK-004 | `.env:150` | local override | `2.0` | LOCAL | config import |
| RISK-004 | `src/core/execution_config.py:368-370` | env fallback | `10.0` | FALLBACK | runtime if override absent |
| RISK-004 | `src/risk/portfolio.py:36,361-390` | limits/evaluation | `10.0`; projected `>` cap | AUTH | pre-entry portfolio evaluation |
| RISK-005 | `.env:151` | local override | `0` | LOCAL | disables cap |
| RISK-005 | `src/core/execution_config.py:371-373` | fallback | `0.0` | FALLBACK | runtime composition |
| RISK-005 | `src/risk/portfolio.py:37,361-390` | limit | enabled only >0 | AUTH | projected exposure evaluation |
| RISK-006 | `.env:152` | local override | `0` | LOCAL | disabled |
| RISK-006 | `src/core/execution_config.py:374-376` | fallback | `0.0` | FALLBACK | composition |
| RISK-006 | `src/risk/portfolio.py:38,392-402` | daily-loss gate | strict `< -(equity*fraction)` | AUTH | entry evaluation when P&L present |
| RISK-006 | `rulebooks/risk_management.md:417-426,454-474` | daily-loss rule | stop trading at -4% | DOC | manual rulebook only; runtime cap is disabled |
| RISK-007 | `.env:153` | local override | `0` | LOCAL | disabled |
| RISK-007 | `src/core/execution_config.py:377-379` | fallback | `0.0` | FALLBACK | composition |
| RISK-007 | `src/risk/portfolio.py:39,404-411` | drawdown gate | `>=` threshold | AUTH | entry evaluation when high-water present |
| RISK-008 | `.env:154` | local override | `0` | LOCAL | disabled |
| RISK-008 | `src/core/execution_config.py:380-382` | fallback | `0.0` | FALLBACK | composition |
| RISK-008 | `src/risk/portfolio.py:40,413-448` | sector gate | classification required when enabled | AUTH | entry evaluation |
| RISK-009 | `.env:155` | local override | `0` | LOCAL | disabled |
| RISK-009 | `src/core/execution_config.py:383-385` | fallback | `0.0` | FALLBACK | composition |
| RISK-009 | `src/risk/portfolio.py:41,413-448` | industry gate | classification required | AUTH | entry evaluation |
| RISK-010 | `.env:156` | local override | `0` | LOCAL | disabled |
| RISK-010 | `src/core/execution_config.py:386-388` | fallback | `0.0` | FALLBACK | composition |
| RISK-010 | `src/risk/portfolio.py:42,413-448` | correlation gate | classification required | AUTH | entry evaluation |
| RISK-011 | `.env:157` | local override | `0` | LOCAL | disabled |
| RISK-011 | `src/core/execution_config.py:389-391` | fallback | `0.0` | FALLBACK | composition |
| RISK-011 | `src/risk/portfolio.py:43,505-530` | strategy gate | strategy ID required | AUTH | entry evaluation |
| RISK-012 | `.env:158` | local override | `300` | LOCAL | config import |
| RISK-012 | `src/core/execution_config.py:392-394` | fallback | `300.0` | FALLBACK | composition |
| RISK-012 | `src/risk/portfolio.py:44,450-463` | FX gate | `timedelta(minutes=5)`; stale `>` max | AUTH | non-USD risk evaluation |
| SIZE-001 | `data/settings.json:4` | local settings | `10.0` | LOCAL | loaded at MainWindow startup |
| SIZE-001 | `src/risk/orb_position.py:18,29-46,202-204` | settings/validator | `10.0`; lower inclusive | AUTH | all ORB validation |
| SIZE-001 | `src/ui/orb_settings_dialog.py:23-80` | dialog | 0..100, step0.5 | DUP | user edits |
| SIZE-002 | `data/settings.json:5` | local settings | `17.5` | LOCAL | startup configuration |
| SIZE-002 | `src/risk/orb_position.py:19,33-38,283-290` | scorer | `17.5`; distance weight4 | AUTH | candidate ranking |
| SIZE-003 | `data/settings.json:6` | local settings | `28.0` | LOCAL | startup configuration |
| SIZE-003 | `src/risk/orb_position.py:20,29-46,202-204` | fallback/validator | `30.0`; upper exclusive | AUTH | ORB plan validity |
| SIZE-003 | `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md:336-341` | position guidance | overnight max30%; typical10–20% | DOC | review/rulebook only |
| SIZE-003 | `rulebooks/risk_management.md:55-77,376-377` | position guidance | maximum25% | DOC | manual rulebook only |
| SIZE-003 | `rulebooks/technical_rules.md:158-175` | position guidance | maximum25% | DOC | AI/review rule only |
| SIZE-004 | `data/settings.json:7` | local settings | `20.0` | LOCAL | startup configuration |
| SIZE-004 | `src/risk/orb_position.py:21,31-48,213-217` | fallback/validator | `15.0`; lower inclusive | AUTH | ORB plan validity |
| SIZE-005 | `data/settings.json:8` | local settings | `65.0` | LOCAL | startup configuration |
| SIZE-005 | `src/risk/orb_position.py:22,278-290` | fallback/scorer | `65.0`; distance weight3 | AUTH | candidate ranking |
| SIZE-006 | `data/settings.json:9` | local settings | `90.0` | LOCAL | startup configuration |
| SIZE-006 | `src/risk/orb_position.py:23,47-48,213-217` | fallback/validator | `66.0`; upper inclusive | AUTH | plan validity |
| SIZE-006 | `src/ui/orb_settings_dialog.py:31-37,63-80` | dialog | UI permits up to1000 | DUP | user edits |
| SIZE-006 | `rulebooks/risk_management.md:109-145,376-387` | stop/ADR guidance | normal <=100% ADR; pivot <=150% ADR | DOC | manual rulebook only |
| SIZE-007 | `src/risk/orb_position.py:167-173` | ORB sizing | `ceil(raw_shares)` | AUTH | executable candidate size |
| SIZE-007 | `src/risk/position_sizer.py:159-179` | risk sizing | `ceil(raw_shares)` | DUP | scoring position size |
| SIZE-007 | `src/risk/position_sizer.py:98-110` | fixed allocation | `int(raw_shares)` floor | DUP | legacy sizing method |
| SIZE-007 | `rulebooks/risk_management.md:339-344` | documented risk sizing | fractional result rounded down | DOC | conflicts with active risk-size ceiling |
| SIZE-008 | `src/risk/position_sizer.py:52` | constructor | max risk `0.02` | FALLBACK | only if caller omits explicit max |
| SIZE-008 | `src/risk/position_sizer.py:74-110` | fixed model | allocation `0.01` | FALLBACK | direct method caller only |
| SIZE-008 | `src/risk/position_sizer.py:182-208` | volatility model | ATR multiplier `2.0` | FALLBACK | direct method caller only |
| SIZE-008 | `src/risk/position_sizer.py:249-256` | Kelly model | multiplier `0.25` | AUTH | direct Kelly caller only |
| SIZE-008 | `config/template_config.py:15-20` | old portfolio template | capital10m KRW, position10%, risk2% | DEAD | no imports found |
| SIZE-009 | `src/ui/charts/controller_layout.py:148,165-169,197` | visible planner input | initializes100000; validator0..1e12 | FALLBACK | replaced by account refresh when available |
| SIZE-009 | `src/ui/mixins/dashboard_mixin.py:1161-1199` | account projection | KIS total equity converted to USD | AUTH | configured production account |
| SIZE-009 | `src/ui/mixins/dashboard_mixin.py:1219-1254` | fail-closed/manual path | configured profile clears field; no-profile fallback10000 | AUTH/FALLBACK | live safety versus offline planning |
| SIZE-009 | `src/core/scoring.py:68,182-207` | scoring default/validation | default10000; finite positive required | FALLBACK | direct callers omitting size only |
| SIZE-009 | `src/services/buyboard_runtime.py:375-402,1040-1080` | execution sizing | exact-account fresh equity | AUTH | broker-facing Buy Board path |
| SIZE-009 | `src/ui/buylist/execution_controller.py:109-174` | account resolution | missing/nonpositive exact-account size rejects | AUTH | entry execution request |
| ENTRY-001 | `src/core/watchlist.py:60,386` | plan models | optional/required entry price | AUTH | persisted user plan |
| ENTRY-001 | `src/core/execution_queue.py:474-477,654-663` | candidate builder | fallback order candidate->queue->buylist | AUTH | queue refresh |
| ENTRY-001 | `src/strategy/orb/strategy.py:94-166` | signal evaluator | breakout positive required | AUTH | ORB signal |
| ENTRY-002 | `data/settings.json:2` | local settings | `0.1` points | LOCAL | Buy Board header load |
| ENTRY-002 | `src/ui/filter_catalog.py:5-7` | settings fallback | `0.10` points | FALLBACK | missing settings JSON |
| ENTRY-002 | `src/ui/buyboard/board.py:104-128` | converter | points `/100` | AUTH | new plan buffer |
| ENTRY-002 | `src/strategy/orb/config.py:21` | strategy fallback | `0.001` fraction | DUP | direct strategy construction |
| ENTRY-002 | `src/core/execution_queue.py:42,634` | queue fallback | `0.001` | DUP | candidate build |
| ENTRY-002 | `src/core/watchlist.py:423,566` | Buylist persistence | `0.001` | DUP | legacy load |
| ENTRY-002 | `src/core/trade_card_state.py:173,558` | card persistence | `0.001` | DUP | card load |
| ENTRY-002 | `src/ui/buylist/execution_controller.py:24-29,134-145` | request validation | `0.001`; invalid -> fallback | DUP | refresh/submission planning |
| ENTRY-003 | `src/strategy/orb/strategy.py:55-89` | `calculate_orb_range` | end exclusive; complete required | AUTH | intraday range build |
| ENTRY-003 | `src/strategy/orb/strategy.py:167-211` | signal | high>buffered breakout; price>high | AUTH | entry signal |
| ENTRY-003 | `src/services/trading_engine.py:140-161` | persisted plan check | trigger≈high; high>buffered breakout | DUP | pre-entry gate |
| ENTRY-003 | `README.md:30-33` | terminology | trigger described as `max(...)` | DOC | operator documentation only |
| ENTRY-004 | `src/core/execution_queue.py:43,921-948` | replacement rule | `DEFAULT_UPGRADE_MARGIN=0.0` | AUTH | queue candidate replacement |
| ENTRY-004 | `src/ui/buylist/view.py:619` | manager | resets `upgrade_margin=0.0` | DUP | UI manager creation |
| ENTRY-005 | `src/core/orb_combinations.py:27-36,162-163` | comparison grid | eight fractions | AUTH | 24-row dialog |
| ENTRY-005 | `src/ui/mixins/planning_support_mixin.py:113-119` | helper | same eight cases inline | DUP | legacy UI planning |
| ENTRY-005 | `src/core/execution_queue.py:763-777` | optimizer | same cases inline | DUP | candidate optimization |
| ORB-001 | `src/core/execution_queue.py:41,395` | execution support | 1m/5m/30m | AUTH | queue validation/execution |
| ORB-001 | `src/strategy/orb/config.py:9-14` | strategy map | adds1h=60 | DUP | standalone strategy can calculate 1h |
| ORB-002 | `src/strategy/orb/config.py:20` | strategy config | default5m | AUTH | direct construction |
| ORB-002 | `tests/test_buyboard_runtime.py:47` | card fixture | selected5m | TEST | common scenario, not authority |
| ORB-003 | `src/strategy/orb/config.py:22` | config | 09:30 | AUTH | range anchor |
| ORB-003 | `src/strategy/orb/strategy.py:38-49` | naive timestamp heuristic | 09:30, UTC13:30/14:30 | DERIVED | timezone normalization |
| ORB-004 | `src/strategy/orb/config.py:24-25` | config | confirmation None, probe false | AUTH | default signal |
| ORB-004 | `src/strategy/orb/strategy.py:184-203` | probe branch | multiplier0.5 | DERIVED | only explicit config enables |
| STOP-001 | `src/services/position_manager.py:259-273` | first-fill stop | `ORB_LOW` | AUTH | confirmed first fill |
| STOP-001 | `src/risk/orb_position.py:153-164` | sizing validation | 0<stop<entry | DUP | candidate sizing |
| STOP-002 | `src/core/scoring.py:168-175` | missing-stop fallback | `entry*(1-.75*ADR/100)` | AUTH | legacy scoring only |
| STOP-003 | `src/core/execution_config.py:401-407` | env fallback | 15bps placeholder | AUTH | position manager default |
| STOP-003 | `src/services/position_manager.py:136-146` | formula | avg*(1+bps/10000), tick up | DERIVED | after partial/manual minimum |
| STOP-004 | `src/services/position_manager.py:149-174` | floor/trigger | max(breakeven,current); price<=stop sticky | AUTH | stop management |
| STOP-004 | `src/services/position_manager.py:281-290` | manual stop | reject below minimum | AUTH | command application |
| STOP-004 | `src/ui/buylist/monitoring.py:520-537` | legacy trigger | current<=stop, Sell All | DUP | 60s legacy monitor |
| EXIT-001 | `src/core/exit_policy.py:52-59` | partial helper | `max(1, shares//3)` | AUTH | legacy/controller button |
| EXIT-001 | `src/ui/buylist/view.py:175` | label | Sell1/3–1/2 | DOC | button text |
| EXIT-001 | `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md:77-85` | guidance | 1/3 to1/2 | DOC | review only |
| EXIT-002 | `src/ui/buylist/monitoring.py:608-625` | alert | 3<=days<=5 | AUTH | monitor cycle |
| EXIT-002 | `src/ui/buylist/view.py:493` | display | same | DUP | row render |
| EXIT-002 | `README.md:33` | guidance | 3–5 days | DOC | operator docs |
| EXIT-003 | `src/core/exit_policy.py:62-75` | signal | EMA10 then EMA20 | AUTH | monitor alert |
| EXIT-003 | `src/core/exit_policy.py:111-118` | EMA | period parameter | DERIVED | daily closes |
| EXIT-003 | `src/ui/buylist/monitoring.py:827-838` | fetch/compute | periods10,20 | DUP | legacy monitor |
| EXIT-003 | `src/core/scoring.py:532-533,610` | AI instructions | selected10 or20 | DOC | AI analysis text only |
| EXIT-004 | `src/services/execution_workflow_service.py:977-1013` | partial command | >0; >=orderable -> Sell All | AUTH | board command |
| EXIT-004 | `src/services/trading_engine.py:1286-1314` | submission | min(request, refreshed remaining) | AUTH | broker-boundary exit |
| EXIT-005 | `README.md:28-33` | active strategy contract | no fixed target; legacy target migrates to breakout | DOC | intended active semantics |
| EXIT-005 | `src/ui/charts/controller_layout.py:146,153-162` | legacy UI field | blank; price validator0..1e9 | DUP | field is not placed in the visible form |
| EXIT-005 | `src/core/watchlist.py:61-64,257-272` | watchlist compatibility | optional target migrated to breakout | LEGACY | persisted JSON load |
| EXIT-005 | `src/core/watchlist.py:128-140,350-368` | old TradePlan | required take-profit; load fallback0 | LEGACY | old plan manager only |
| EXIT-005 | `src/core/watchlist.py:387,515-525` | Buylist compatibility | target0; positive target becomes breakout | LEGACY | backward-compatible JSON/tests |
| EXIT-005 | `src/core/scoring.py:211-216,350-369` | scoring migration/output | positive target aliases breakout; emitted target0 | AUTH | active scoring contract |
| EXIT-005 | `src/core/trade_reviewer.py:10-19` | review DTO | required take-profit float | LEGACY | rulebook review input |
| EXIT-005 | `tests/test_buylist_and_scoring.py:202-235,238-252` | asserted semantics | no R/R target; migration to breakout | TEST | regression contract |
| ORD-001 | `src/core/execution_config.py:80-86` | env fallback | 0.005 | AUTH | shared exit price |
| ORD-001 | `src/core/exit_execution_command.py:67-80` | price builder | bid/reference*(1-discount) | DERIVED | Sell command |
| ORD-001 | `src/ui/buylist/constants.py:10` | legacy constant | 0.005 | DUP | legacy stop sell |
| ORD-002 | `src/core/exit_execution_command.py:77-80` | emergency formula | ×(attempt+1), min with0.05 | AUTH | unavailable bid fallback |
| ORD-002 | `src/core/execution_config.py:294-295` | max reprices | 3 | AUTH | runtime cap |
| ORD-002 | `tests/test_buyboard_runtime_guarded_composition.py:1308-1312` | cap test | 3 | TEST | asserts production behavior |
| ORD-003 | `src/ui/buylist/constants.py:11` | legacy constant | 0.002 | AUTH | legacy path only |
| ORD-003 | `src/ui/buylist/actions.py:717-725` | reprice gate | new>=old*(1-.002) -> no cancel | DERIVED | stop-hit monitoring |
| ORD-004 | `src/api/kis_order.py:290-297` | formatter | .0001 below$1, .01 otherwise, round down | AUTH | outbound KIS price |
| ORD-004 | `src/services/position_manager.py:119-133` | breakeven ticks | same split, round up | DUP | stop computation |
| ORD-004 | `src/core/exit_execution_command.py:70,80` | core floor | max(.01,price) | DUP | shared exit path |
| ORD-004 | `src/ui/buylist/actions.py:672-674` | legacy floor | invalid ->.01; valid min.0001 | DUP | legacy stop path |
| ORD-005 | `src/risk/pre_trade.py:21-31` | quantity normalizer | positive whole, not Boolean | AUTH | approval boundary |
| ORD-005 | `src/core/exit_execution_command.py:96-118` | exit command | quantity>0, regular price>0 | AUTH | shared command |
| ORD-005 | `src/services/execution_workflow_service.py:983-1013` | sell bound | orderable quantity | AUTH | board workflow |
| LIFE-001 | `src/core/execution_config.py:55` | fallback | 15 | AUTH | entry manager |
| LIFE-001 | `src/services/entry_attempt_manager.py:390` | deadline | now+15s | DERIVED | each attempt |
| LIFE-002 | `src/core/execution_config.py:56` | fallback | 3 | AUTH | entry retry |
| LIFE-002 | `src/services/entry_attempt_manager.py:372,459,477,502,575` | consumers | timedelta(cooldown) | DERIVED | rejection/cancel paths |
| LIFE-003 | `src/core/execution_config.py:57-59` | fallback | 4/min | AUTH | attempt manager |
| LIFE-003 | `src/services/entry_attempt_manager.py:329-333` | rate gate | len timestamps>=4 | DERIVED | pre-submit |
| LIFE-004 | `src/core/execution_config.py:65` | fallback | 5 | AUTH | exit retry |
| LIFE-004 | `src/ui/buyboard/runtime_worker.py:2808` | consumer | next retry timedelta | DERIVED | runtime error path |
| LIFE-005 | `src/core/execution_config.py:74-75` | fallbacks | partial10, all5 | AUTH | exit attempt deadline |
| LIFE-005 | `src/services/buyboard_runtime.py:1489` | Sell All consumer | timedelta(config) | DERIVED | broker submit |
| LIFE-006 | `src/core/execution_config.py:76-78` | fallback | 10 | AUTH | cancel-confirm monitor |
| LIFE-006 | `src/ui/buyboard/runtime_worker.py:1910-1951` | alert | timeout critical alert | DERIVED | unresolved cancel |
| LIFE-007 | `src/risk/pre_trade.py:14,85` | approval creation | 30s | AUTH | every exposure-increasing command |
| LIFE-007 | `src/risk/pre_trade.py:182-198` | approval validation | exact price ±1e-9 and TTL | AUTH | gateway boundary |
| LIFE-008 | `src/core/execution_config.py:182-190` | recon cadence | 2,1,5,20,60 | AUTH | runtime worker |
| LIFE-008 | `src/core/execution_config.py:200-212` | evidence cadence | 3600,60,60,2,3 | AUTH | reconciliation/data fallback |
| LIFE-008 | `src/services/account_reconciliation.py:778,826` | candidate/absence | configured windows | DERIVED | order discovery |
| LIFE-008 | `src/services/account_reconciliation.py:1147-1155` | release proof | interval + two generations | DERIVED | reservation release |
| SESSION-001 | `src/utils/market_calendar.py:9-15` | calendar constants | 07:00 KST, 09:30/16:00/13:00 ET | AUTH | production runtime/calendar |
| SESSION-001 | `src/utils/market_calendar.py:56-91` | holidays | recurring NYSE closure set | AUTH | session gate/cache date |
| SESSION-001 | `src/utils/market_calendar.py:116-151` | close/open | early-close logic; open<=t<close | AUTH | production entry/EOD |
| SESSION-001 | `src/core/exit_policy.py:9,101` | daily completion | fixed16:00, no early close | DUP | legacy EMA data completion |
| SESSION-001 | `src/ui/buylist/constants.py:12-13` | legacy hours | 09:30/16:00 | DUP | manual session policy |
| SESSION-001 | `src/ui/buylist/orders.py:257-269` | UI gate | weekday and fixed hours only | DUP | legacy orders |
| SESSION-001 | `src/ui/mixins/planning_support_mixin.py:26-27,110` | planning gate | fixed hours | DUP | UI planning |
| SESSION-001 | `src/ui/charts/controller_data_flow.py:56-57` | chart gate | fixed hours | DUP | chart refresh |
| SESSION-001 | `src/ui/charts/controller_drawing.py:30-31` | chart constants | fixed hours | DUP | inherited UI code |
| SESSION-001 | `src/ui/charts/controller_layout.py:41-42` | chart constants | fixed hours | DUP | inherited UI code |
| SESSION-001 | `src/ui/charts/render_local.py:29-30` | chart constants | fixed hours | DUP | local chart |
| SESSION-001 | `src/ui/charts/render_metrics.py:30-31` | chart constants | fixed hours | DUP | chart metrics |
| SESSION-001 | `src/ui/charts/render_primitives.py:32-33` | chart constants | fixed hours | DUP | chart primitives |
| SESSION-001 | `src/ui/main_window.py:148-149` | UI constants | fixed hours | DUP | legacy helpers |
| SESSION-001 | `src/ui/mixins/scanner_mixin.py:81-82` | scanner UI | fixed hours | DUP | intraday refresh |
| SESSION-001 | `src/ui/mixins/sidebar_mixin.py:83-84` | sidebar UI | fixed hours | DUP | intraday refresh |
| SESSION-001 | `src/services/trading_engine.py:278-283` | constructor defaults | always open/never EOD when uninjected | MOCK | tests/simple callers only; production injects calendar |
| SESSION-002 | `src/core/exit_execution_command.py:21-41` | policy | manual PROD outside session -> MOO | AUTH | command builder |
| SESSION-002 | `src/core/exit_execution_command.py:96-118` | validation | MOO price0; regular positive | AUTH | broker boundary |
| SESSION-002 | `README.md:124-125` | documentation | outside-session PROD manual exits use MOO | DOC | operator guidance |
| SESSION-003 | `src/core/execution_config.py:397-399` | fallback | 60 | AUTH | EOD predicate |
| SESSION-003 | `src/services/buyboard_runtime.py:248-253` | predicate | seconds_left<=config | DERIVED | engine composition |
| SESSION-003 | `src/services/trading_engine.py:1714-1754` | EOD call | due gate + closed state | DERIVED | heartbeat |
| FILTER-001 | `data/scanner_setups.json:2-81` | local state | two rule arrays | LOCAL | scanner load |
| FILTER-001 | `src/ui/main_window.py:3474-3554` | normalization/load | rule schema/fallback | AUTH | startup/UI |
| FILTER-001 | `src/infrastructure/database/repositories/scanner.py:464-523` | SQL builder | cumulative expressions | AUTH | DB-backed scan |
| FILTER-001 | `src/services/app_state.py:62,565,722-723` | persistence | scanner JSON local | AUTH | save/load |
| FILTER-002 | `src/core/scanner.py:127-141` | threshold helper | default1 and >= | DUP | direct scanner setup |
| FILTER-002 | `src/utils/data_loader.py:869-878` | metric guard | len<min+1 rejects | AUTH | metric compute |
| FILTER-002 | `src/infrastructure/database/repositories/scanner.py:117-143` | repository | default1, lookback380 | AUTH | metric refresh |
| FILTER-002 | `src/infrastructure/database/repositories/scanner.py:489-493` | SQL base filter | history>=1 always | AUTH | every DB scan |
| FILTER-002 | `src/ui/filter_catalog.py:122` | recommendation | min65, ideal252 | DOC | rule-builder help |
| FILTER-002 | `rulebooks/fundamental_rules.md:9,73` | guidance | >1 day | DOC | review only |
| FILTER-003 | `data/scanner_setups.json:3-45` | Setup1 | 7 effective rules | LOCAL | scan |
| FILTER-003 | `src/ui/filter_catalog.py:21-35` | Setup1 fallback | first5 rules | FALLBACK | missing local state |
| FILTER-003 | `src/core/scanner.py:127-170` | threshold helper | 40k/35k/2.4/97.04/90 | DUP | direct caller |
| FILTER-003 | `rulebooks/fundamental_rules.md:8-15` | documented rules | mostly `>` wording | DOC | review only |
| FILTER-004 | `data/scanner_setups.json:47-80` | Setup2 | 250k/$5m/3/95/80 | LOCAL | scan |
| FILTER-004 | `src/ui/filter_catalog.py:36-49` | Setup2 fallback | same | FALLBACK | missing local state |
| FILTER-005 | `src/utils/data_loader.py:892-908` | volume/volatility | 20,20 min5,14 | AUTH | metrics |
| FILTER-005 | `src/utils/data_loader.py:915-924` | returns/MAs | offsets6/22/64/127;20/50/200 | AUTH | metrics |
| FILTER-005 | `src/utils/data_loader.py:949-973` | highs/consolidation/returns | 20/50/252,10,10,3/5 | AUTH | metrics |
| FILTER-005 | `src/utils/data_loader.py:983-1020` | RS | 252/50/20 | AUTH | metrics |
| FILTER-005 | `src/infrastructure/database/schema.py:307-362` | cache schema | materialized metric columns | DERIVED | scanner cache |
| FILTER-006 | `src/utils/data_loader.py:933-940` | trend formula | tanh scale20 and score weights | AUTH | metrics |
| FILTER-006 | `src/ui/filter_catalog.py:140-141` | help | threshold suggestions80/90,70/75–80 | DOC | rule builder |
| FILTER-007 | `src/utils/data_loader.py:942-947` | dry-up metric | min10/avg20 | AUTH | metrics |
| FILTER-007 | `src/ui/filter_catalog.py:144` | help | avg5/avg20 | DOC | rule builder |
| FILTER-008 | `src/utils/data_loader.py:960-964` | tightness | 100/(range+1) | AUTH | metrics |
| FILTER-008 | `src/ui/filter_catalog.py:152-154` | help | range/ADR; lower better | DOC | rule builder |
| FILTER-009 | `src/utils/data_loader.py:966-981` | parabolic flag | >15 or >25 | AUTH | metrics |
| FILTER-009 | `src/ui/filter_catalog.py:155-161` | help | different bands | DOC | rule builder |
| FILTER-010 | `src/utils/data_loader.py:983-986` | missing-reference defaults | 90/false/0 | FALLBACK | metrics without SPY |
| FILTER-010 | `src/utils/data_loader.py:998-1020` | calculated RS | 252 rank/50 SMA/20 slope | AUTH | metrics with SPY |
| FILTER-010 | `src/ui/filter_catalog.py:162-164` | help | threshold70/80 | DOC | rule builder |
| DATA-001 | `src/core/execution_config.py:209-212` | quote fallback | poll2, stale3 | AUTH | REST/fallback quote service |
| DATA-001 | `src/services/realtime_market_data.py:121-130` | service default | config value | DERIVED | quote readiness |
| DATA-002 | `.env:100-104` | local values | 3,3,1,5,1 | LOCAL | config import |
| DATA-002 | `src/core/execution_config.py:241-248` | fallbacks | 3,3,1,5,1 | AUTH | realtime composition |
| DATA-002 | `src/services/kis_realtime_market_data.py:1148-1152` | stale checks | broker-event age | DERIVED | tick ingestion |
| DATA-002 | `src/services/kis_realtime_market_data.py:1523-1525` | clock checks | future/skew bounds | DERIVED | tick normalization |
| DATA-002 | `src/services/kis_realtime_market_data.py:1660-1661` | readiness | both ages <= thresholds | DERIVED | execution-grade quote |
| DATA-002 | `docs/gate2_readiness_checklist.md:83-84` | checklist | first two values2 | DOC | stale deployment guidance |
| DATA-002 | `docs/kanban_architecture.md:201-202` | architecture | values3 | DOC | current docs |
| DATA-003 | `src/services/buying_power_cache.py:29-32` | max age | 15 | AUTH | provider construction |
| DATA-003 | `src/services/buying_power_cache.py:93-111` | stale rule | age>max -> None | DERIVED | entry risk/capital |
| DATA-003 | `src/services/buying_power_cache.py:114-153` | providers | stale ->0 | DERIVED | Buy Board runtime |
| DATA-004 | `src/ui/buyboard/controller.py:540,832` | UI age | 120 | AUTH | board projection only |
| DATA-005 | `.env:110-111` | local values | 15,120 | LOCAL | config import |
| DATA-005 | `src/core/execution_config.py:270-275` | fallbacks | 15,120 | AUTH | outage policy |
| DATA-005 | `src/services/trading_engine.py:1959-1979` | liquidation timing | grace/high or ceiling/all | DERIVED | heartbeat during disconnect |
| DATA-005 | `tests/test_trading_engine.py:1896-1936` | scenarios | 5; 2/10; max0 | TEST | boundary behavior only |
| DATA-006 | `.env:112-117` | local values | .01,.02,false,.01,.20,.5 | LOCAL | config import |
| DATA-006 | `src/core/execution_config.py:276-293` | fallbacks | same | AUTH | outage classifier |
| DATA-006 | `src/services/trading_engine.py:165-205` | classifier | stop/loss/concentration/risk/ATR tests | DERIVED | disconnect snapshot |
| DATA-006 | `src/risk/orb_position.py:173-177` | `sl_adr` source | percent of ADR | DUP | card plan source showing unit conflict |
| DATA-007 | `src/services/trading_engine.py:206-209` | spread check | `(ask-bid)/bid>=.02` | AUTH | outage classification |
| WF-001 | `src/core/kanban_transitions.py:47-82` | graph | fixed edge sets | AUTH | user board commands |
| WF-001 | `src/core/kanban_transitions.py:86-98` | validator | target must be allowed | DERIVED | workflow command |
| WF-001 | `src/services/execution_workflow_service.py:977-1044` | partial transitions | additional live-order guards | AUTH | board command |
| WF-002 | `src/core/kanban_transitions.py:111-136` | duplicate card check | exact card key | AUTH | load/command validation |
| WF-002 | `src/core/execution_ownership.py:110-120` | owner mapping | fixed source-owner map | AUTH | broker gateway |
| WF-002 | `README.md:123` | operator rule | one owner per env/account/symbol | DOC | operations |
| WF-003 | `src/services/eod_trading_service.py:109-211` | EOD state decisions | session/order/state conditions | AUTH | EOD window/startup |
| WF-003 | `src/services/eod_trading_service.py:213-408` | reset/reconcile | broker evidence before transition | AUTH | EOD cleanup |
| WF-003 | `tests/test_eod_trading_service.py:109-157` | boundary tests | before-close/no-order cases | TEST | behavior verification |
| BROKER-001 | `.env:142` | local value | true | LOCAL | `is_buyboard_engine_enabled` |
| BROKER-001 | `src/core/execution_config.py:409-421` | fallback | true; independent gates | AUTH | runtime composition |
| BROKER-002 | `.env:135-136` | local envelope | CONTROLLED_LIVE; STIM | LOCAL | config import |
| BROKER-002 | `src/core/execution_config.py:335-349` | fallback | DISABLED; empty symbols | AUTH | policy |
| BROKER-002 | `src/services/controlled_live_policy.py:77-111` | readiness | verified WS/budgets/attempts | AUTH | each PROD mutation |
| BROKER-003 | `.env:137` | local cap | 0.01 | LOCAL | config import |
| BROKER-003 | `src/core/execution_config.py:351-353` | fallback | 0.0 | FALLBACK | controlled mode invalid until set |
| BROKER-003 | `src/services/controlled_live_policy.py:90-95,167` | validator | positive; notional<=cap | AUTH | PROD entry |
| BROKER-004 | `.env:127,130` | local values | spacing1.1, attempts1 | LOCAL | scheduler composition |
| BROKER-004 | `src/core/execution_config.py:304-332` | budget config | verified/capacities/window/spacing/attempts | AUTH | runtime worker |
| BROKER-004 | `src/services/kis_request_scheduler.py:118-145` | scheduler fallbacks | reads20/s, mutate5/s, reserve2, attempts3/2, backoff.25 | FALLBACK | composition overrides mutation attempts/spacing only |
| BROKER-004 | `src/services/controlled_live_policy.py:60-76,96-111` | controlled constraints | capacities>0, spacing>=.1, attempts1 | AUTH | readiness |
| BROKER-005 | `src/api/kis_order.py:73` | HTTP request | timeout15 | AUTH | KIS order/account reads |
| BROKER-005 | `src/api/kis_order.py:591-633` | pagination | max10, sleep.2 | AUTH | order discovery |
| BROKER-005 | `src/api/kis_account_snapshot_dual.py:113-117` | account constants | retries3, pages20, lock90/180 | AUTH | account snapshot/token cache |
| BROKER-005 | `src/api/kis_account_snapshot_dual.py:730-780` | retry | backoff vector | DERIVED | rate/network errors |
| BROKER-005 | `src/api/kis_intraday.py:34-35,257-264` | page cap | max50; default days*6 | AUTH | ORB minute-bar fetch |
| BROKER-006 | `src/core/execution_config.py:214-260` | WS config | switches/timings/capacity0 | AUTH | realtime composition |
| BROKER-006 | `src/services/kis_realtime_market_data.py:53,564-567` | verified cap | 41 | AUTH | subscription validation |
| BROKER-006 | `.env.example:77-109` | example | matching fallbacks | DOC | environment setup |
| BROKER-007 | `src/core/execution_config.py:224-226` | formation source | KIS_MINUTE_BARS | AUTH | ORB provider |
| BROKER-007 | `.env.example:93` | example | same | DOC | setup |
| UI-001 | `src/ui/buyboard/board.py:74` | label constant | 30 | AUTH for UI only | render |
| UI-001 | `src/ui/buyboard/board.py:140,420-424` | label/color | positions/30; red at>=30 | DERIVED | render |
| UI-001 | `.env:148` | enforcement | 20 | LOCAL | shows conflict |
| UI-002 | `src/ui/buylist/monitoring.py:210-218` | QTimer | 60000 and 60s text | AUTH | legacy monitor toggle |
| UI-003 | `src/ui/charts/controller_data_flow.py:47-50` | chart constants | 07:00,5m,5m,30m | AUTH | chart refresh |
| UI-003 | `src/ui/charts/controller_drawing.py:26-29` | copied constants | same | DUP | inherited UI |
| UI-003 | `src/ui/charts/controller_layout.py:37-40` | copied constants | same | DUP | inherited UI |
| UI-003 | `src/ui/charts/render_local.py:25-28` | copied constants | same | DUP | local chart |
| UI-003 | `src/ui/charts/render_metrics.py:26-29` | copied constants | same | DUP | chart metrics |
| UI-003 | `src/ui/charts/render_primitives.py:28-31` | copied constants | same | DUP | chart primitives |
| UI-003 | `src/ui/mixins/scanner_mixin.py:76-79` | copied constants | same | DUP | scanner UI |
| UI-003 | `src/ui/mixins/sidebar_mixin.py:79-82` | copied constants | same | DUP | sidebar UI |
| UI-003 | `src/utils/market_calendar.py:10,228-240` | ready time | 07:00 KST | AUTH | expected cache date |
| INT-001 | `src/core/execution_config.py:96-180` | coordination timers | floors/defaults listed in master | AUTH | runtime/state sync |
| INT-001 | `src/core/execution_config.py:194-201` | recon cache/audit | 900 floor300; observation3600 | AUTH | coordination/reconciliation |
| INT-001 | `src/services/state_sync.py:1075-1334` | consumers | heartbeat max age | DERIVED | ownership/failover |
| INT-002 | `src/core/execution_config.py:89-93` | RU profile | typed-change-pulse-v6 | AUTH | peer readiness |
| INT-002 | `src/core/execution_config.py:261-266` | strategy ID | buyboard-orb-v1 | AUTH | ownership |
| INT-003 | `src/services/state_sync.py:59-64` | sync keys | four keys only | AUTH | live cross-device state |
| INT-003 | `src/services/state_sync.py:902-905` | publish validation | exact key set | DERIVED | full plan publish |
| INT-003 | `src/services/app_state.py:61-65,565,722-723` | local files | settings/scanner files outside sync | AUTH | local save/load |
| SEC-001 | `.env.example:62,68` | credential names | PROD/SIM app key blank | DOC | env schema only |
| SEC-001 | `src/api/kis_config.py:41` | loader | prod key/legacy fallback | AUTH | KIS client |
| SEC-002 | `.env.example:63,69` | credential names | secrets blank | DOC | env schema only |
| SEC-002 | `src/api/kis_config.py:42` | loader | prod secret | AUTH | KIS client |
| SEC-003 | `.env.example:64,70-71` | identifiers | account/product fields blank | DOC | env schema only |
| SEC-003 | `src/api/kis_config.py:43` | loader | account number | AUTH | legacy KIS clients |
| SEC-003 | `src/api/kis_account_snapshot_dual.py:1096-1111` | profiles | single/numbered accounts | AUTH | account discovery |
| SEC-004 | `src/api/kis_account_snapshot_dual.py:80,116-197` | token/cache lock | endpoint and lock rules | AUTH | authentication |
| SEC-004 | `src/services/kis_realtime_market_data.py:1858-1883` | WS approval | app credentials -> approval key | AUTH | realtime auth |
| SEC-005 | `.env.example:5-18` | DB env schema | MySQL/coordination credentials | DOC | setup only |
| SEC-006 | `.env.example:164` | webhook token | blank | DOC | setup only |
| SEC-006 | `src/services/external_alerting.py:1239` | loader | bearer token | AUTH | alert delivery |
| SEC-007 | `.env.example:183` | remote token | blank | DOC | setup only |
| SEC-007 | `src/services/pc_remote_control.py:91,180-193` | remote auth | token required/matched | AUTH | shutdown request |
| SEC-008 | `src/services/env_backup.py:48-52` | crypto constants | secrets dir,600k,12 chars | AUTH | encrypted env backup |
| SEC-008 | `src/services/env_backup.py:130,168-173` | validation | exact iterations/min length warning | DERIVED | restore/backup |
| SEC-008 | `docs/cloud_backup.md:153-156,210-213` | handling | device state not copied; env encrypted | DOC | operations |
| FILTER-011 | `rulebooks/fundamental_rules.md:54-57,134-138` | optional guards | price5/500, earnings7d, float10M | DOC | no global runtime consumer |
| FILTER-011 | `rulebooks/technical_rules.md:197-198` | optional guards | price5/500, earnings7d | DOC | no global runtime consumer |
| FILTER-011 | `data/scanner_setups.json:35-39` | local price rule | price>=5 | LOCAL | Setup1 scan only |
| FILTER-012 | `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md:140-152,188` | episodic gap | >=10%; 15–30min volume | DOC | rulebook review only |
| FILTER-012 | `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md:252-270` | parabolic gap entry | first red5m candle | DOC | rulebook review only |
| FILTER-013 | `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md:12-18,101-117` | momentum setup | 30–100%/1–3mo; top1–2%;20–50% | DOC | rulebook review only |
| FILTER-013 | `rulebooks/QULLAMAGGIE_EXACT_SETUPS.md:252-258,310-311` | parabolic setup | 50–100%,300–1000%,3–5days/crash50–60% | DOC | rulebook review only |
| RISK-002 | `.env.pc:152` | generated PC copy | `20` | DUP | same effective non-secret override on PC |
| RISK-003 | `.env.pc:153` | generated PC copy | `0.10` | DUP | same PC override |
| RISK-003 | `.env.example:149` | environment example | `0.10` | DOC | setup fallback schema |
| RISK-004 | `.env.pc:154` | generated PC copy | `2.0` | DUP | same PC override |
| RISK-004 | `.env.example:150` | environment example | `10.0` | DOC | differs from current local override |
| RISK-005 | `.env.pc:155` | generated PC copy | `0` | DUP | same PC override |
| RISK-005 | `.env.example:151` | environment example | `0` | DOC | setup schema |
| RISK-006 | `.env.pc:156` | generated PC copy | `0` | DUP | same PC override |
| RISK-006 | `.env.example:152` | environment example | `0` | DOC | setup schema |
| RISK-007 | `.env.pc:157` | generated PC copy | `0` | DUP | same PC override |
| RISK-007 | `.env.example:153` | environment example | `0` | DOC | setup schema |
| RISK-008 | `.env.pc:158` | generated PC copy | `0` | DUP | same PC override |
| RISK-008 | `.env.example:154` | environment example | `0` | DOC | setup schema |
| RISK-009 | `.env.pc:159` | generated PC copy | `0` | DUP | same PC override |
| RISK-009 | `.env.example:155` | environment example | `0` | DOC | setup schema |
| RISK-010 | `.env.pc:160` | generated PC copy | `0` | DUP | same PC override |
| RISK-010 | `.env.example:156` | environment example | `0` | DOC | setup schema |
| RISK-011 | `.env.pc:161` | generated PC copy | `0` | DUP | same PC override |
| RISK-011 | `.env.example:157` | environment example | `0` | DOC | setup schema |
| RISK-012 | `.env.pc:162` | generated PC copy | `300` | DUP | same PC override |
| RISK-012 | `.env.example:158` | environment example | `300` | DOC | setup schema |
| DATA-002 | `.env.pc:104-108` | generated PC copy | 3,3,1,5,1 | DUP | same PC freshness envelope |
| DATA-005 | `.env.pc:114-115` | generated PC copy | 15,120 | DUP | same PC outage timing |
| DATA-006 | `.env.pc:116-121` | generated PC copy | .01,.02,false,.01,.20,.5 | DUP | same PC outage-risk profile |
| BROKER-001 | `.env.pc:146` | generated PC copy | true | DUP | same PC engine availability |
| BROKER-002 | `.env.pc:139-140` | generated PC copy | CONTROLLED_LIVE; STIM | DUP | same PC live envelope |
| BROKER-003 | `.env.pc:141` | generated PC copy | 0.01 | DUP | same PC entry cap |
| BROKER-004 | `.env.pc:131,134` | generated PC copy | spacing1.1, attempts1 | DUP | same PC mutation policy |

## 4. Duplicate and conflict matrix

### Same-concept duplicate definitions (35 groups)

| ID | Candidate(s) | Duplicate locations | Same value/meaning | Runtime authority |
|---|---|---|---|---|
| D01 | RISK-001 | UI input conversion, scoring, `TradePlan`, execution request | 1% becomes fraction0.01 on the normal new-plan path | UI conversion then persisted candidate/card |
| D02 | RISK-002 | execution fallback, portfolio dataclass/cap, `.env.example`, UI label, tests | 30 | local env overrides enforcement; UI does not |
| D03 | RISK-003 | env fallback, dataclass, example | 0.10 | local env/config object |
| D04 | RISK-004 | env fallback, dataclass, example | 10.0 fallback | local env overrides to2.0 |
| D05 | RISK-005 | env fallback, dataclass, example | 0 disabled | local env |
| D06 | RISK-006/RISK-007 | env fallback, dataclass, example | 0 disabled | local env |
| D07 | RISK-008..011 | env fallback, dataclass, example | 0 disabled | local env |
| D08 | RISK-012 | 300 seconds / five minutes in config and dataclass | same duration | local env |
| D09 | SIZE-001 | local and fallback | 10% | local settings |
| D10 | SIZE-002 | local and fallback | 17.5% | local settings |
| D11 | SIZE-005 | local and fallback | 65% ADR | local settings |
| D12 | SIZE-007 | ORB and risk-based sizers | ceiling to whole share | ORB/risk method used by caller |
| D13 | ENTRY-002 | strategy, queue, Buylist, card, controller | buffer fraction0.001 | local `0.1` points converted to0.001 for new plans |
| D14 | ENTRY-005 | ORB combinations, planning mixin, queue optimizer | eight0.25–2% cases | queue/combinations implementation by path |
| D15 | ORB-001 | strategy and queue | 1m/5m/30m common subset | execution tuple |
| D16 | STOP-004 | core and legacy monitors | stop triggers at `current<=stop` | core for Buy Board; legacy for Buylist |
| D17 | EXIT-002 | monitor, view, README/rulebook | 3–5 day review | monitor logic |
| D18 | EXIT-003 | exit policy and monitor | EMA periods10/20 | exit policy helper |
| D19 | ORD-001 | execution config and UI constant | 0.5% sell discount | config for shared path; UI constant for legacy |
| D20 | ORD-004 | KIS formatter and breakeven rounding | $1 tick boundary, .0001/.01 ticks | KIS formatter at broker boundary |
| D21 | LIFE-001/LIFE-002 | config and attempt manager | 15s TTL,3s cooldown | execution config |
| D22 | LIFE-004/LIFE-005 | config and runtime consumers | 5s cooldown,10s/5s TTLs | execution config |
| D23 | LIFE-008 | config, `.env`, `.env.pc`, example | pending2s, unknown1s | local env |
| D24 | SESSION-001 | calendar plus 11 UI modules | 09:30/16:00 fixed-hour subset | holiday-aware calendar in production engine |
| D25 | FILTER-002 | scanner helper, loader, DB repository/query | minimum history1 | DB base condition plus loader |
| D26 | FILTER-003 | local first five rules, catalog, scanner helper, rulebook | 40k/$35k/2.4/97.04/90 | local rules |
| D27 | FILTER-004 | local state and catalog | 250k/$5m/3/95/80 | local rules |
| D28 | DATA-002 | local env, example, architecture | 3/3/1/5/1 seconds | local env |
| D29 | DATA-005/DATA-006 | local env, `.env.pc`, config/example | outage timing/risk profile | local env |
| D30 | BROKER-001 | local env, `.env.pc`, fallback/example | engine enabled true | environment read on call |
| D31 | BROKER-004 | local `.env` and `.env.pc` | spacing1.1, attempts1 | process environment |
| D32 | UI-003 | chart/controller/render/mixin modules | 07:00,5m,5m,30m | each copied module independently |
| D33 | WF-002/SESSION-002 | code and README/docs | one-owner rule; outside-session MOO rule | gateway/command code |
| D34 | SIZE-009 | planner initializer, scoring default, dashboard manual cache, broker account projection | same account-equity sizing concept, but100k/10k/live values differ | fresh exact-account KIS total equity for configured PROD; otherwise manual planning fallback |
| D35 | EXIT-005 | `take_profit` and `target_price` fields in UI, persistence, reviewer and scoring | all are legacy compatibility names; active target is0/none | active ORB contract/scoring migration to breakout |

### Conflicting defaults or behavior (21 groups)

| ID | Candidate | Conflict | Current runtime winner and reason | Consequence |
|---|---|---|---|---|
| C01 | RISK-001 | `0.01` fraction vs `1.0` percentage points vs card/runtime `1.0` fraction; old template/PositionSizer also default2% | Path-specific: new UI divides by100; persisted/migrated card value is consumed as a fraction | potential100x risk budget; no single safe setting authority |
| C02 | RISK-002/UI-001 | local enforcement20 vs UI/fallback/test30 | `.env:148` wins risk enforcement; UI still shows30 | misleading capacity and late entry rejection |
| C03 | RISK-004 | local gross cap2.0 vs fallback/dataclass/example10.0 | local env wins until missing/malformed | missing override permits fivefold more leverage |
| C04 | SIZE-003 | local ORB max28 vs code/default restore30 vs rulebook maximum25 (another rulebook says overnight30) | local JSON wins at startup; “Restore Defaults” writes30; rulebooks do not execute | user/reset/review surfaces describe different single-position limits |
| C05 | SIZE-004 | local stop/ADR min20 vs fallback/default restore15 | local JSON wins | device/reset changes plan validity |
| C06 | SIZE-006 | local stop/ADR max90 vs fallback/default restore66 vs rulebook normal100/pivot150 | local JSON wins; rulebooks do not execute | large plan-validity swing and inconsistent manual review |
| C07 | ORB-001 | strategy declares1h; execution accepts only1m/5m/30m | execution tuple wins for managed workflow | a direct strategy plan may be unexecutable |
| C08 | EXIT-001 | button/docs say1/3–1/2; helper always returns one third | code helper wins | operator cannot select documented half |
| C09 | EXIT-003 | docs say selected EMA10 or20; no selection is persisted and code checks10 first | code priority wins | 20EMA choice is not implemented |
| C10 | ORD-004 | shared sell floor$0.01 vs legacy stop floor$0.0001 | frontend path determines winner | same intent can use materially different collar below$1 |
| C11 | DATA-002 | Gate2 checklist says broker/local stale2s; runtime/example/architecture/local env say3s | env/config3s wins | stale rollout checklist |
| C12 | FILTER-002 | runtime history1 (and min+1 rows) vs UI help65/ideal252 vs rulebook “>1” | DB/query1 wins | long-window metrics can be based on very short histories |
| C13 | FILTER-003 | Setup1 dollar volume$35k vs filter help minimum$500k; local adds price>=5 and return1m<1500 absent from fallback | local JSON wins | new/reset device selects a different universe |
| C14 | FILTER-007 | runtime min-volume10/avg20 vs docs avg5/avg20 | runtime formula wins | documented thresholds do not mean what UI says |
| C15 | FILTER-008 | runtime `100/(range+1)` higher-is-tighter vs docs `range/ADR` lower-is-tighter | runtime formula wins | operator direction/threshold can invert selection |
| C16 | FILTER-003 | runtime operators `>=`; rulebook frequently says strict `>` | persisted rules/SQL `>=` win | boundary stocks included despite wording |
| C17 | ENTRY-003 | README says trigger=max(ORB high, buffered breakout); runtime rejects when ORB high <= buffered breakout and then triggers only at ORB high | strategy/runtime win | documentation suggests entries runtime forbids |
| C18 | SESSION-001 | production engine uses holiday/early-close-aware calendar; legacy UI/exit completion use weekday and fixed16:00 | production Buy Board calendar wins for engine, legacy path remains different | early-close/holiday behavior differs by frontend |
| C19 | RISK-006 | portfolio daily-loss cap is0/disabled; rulebook says stop trading at -4% | runtime governor is disabled because local env/config0 wins; rulebook is manual only | operator may assume a lockout that does not exist |
| C20 | SIZE-007 | active ORB/risk sizing uses ceiling while the risk rulebook explicitly rounds a fractional share result down; the legacy fixed-allocation method also floors | caller/path wins: executable ORB and risk-based scoring use ceiling | code may exceed the nominal risk budget by less than one share of risk; manual review expects never to exceed it |
| C21 | SIZE-009 | visible planner initializes100000 while scoring and dashboard manual fallback use10000; dead template is KRW-denominated | fresh KIS total equity wins for configured PROD and missing snapshots fail closed; offline/no-profile paths remain inconsistent | offline plan sizes can differ10x and a displayed fallback can be mistaken for broker equity |

Malformed environment values create an additional **fallback hazard**, not a separate default conflict: `_env_int`, `_env_float`, and `_env_bool` silently return code fallbacks and do not report range errors (`src/core/execution_config.py:22-51`).

## 5. Dependency and constraint matrix

These are relationships proved by the current code, not proposed policy.

| Inputs/settings | Required relationship | Enforcement/evidence | Failure behavior or gap |
|---|---|---|---|
| RISK-001, entry, stop | `0 < risk_fraction <= 1`; `0 < stop < entry` | `src/risk/orb_position.py:153-164`; `src/risk/position_sizer.py:133-143` | zero position values |
| RISK-001, stop distance, quantity | `shares = ceil(account_equity*risk/(entry-stop))` | `src/risk/orb_position.py:167-173` | ceiling can exceed requested risk by less than one share of risk; portfolio governor still evaluates projected open risk |
| RISK-001, RISK-003 | proposed and aggregate open risk must not exceed total-open-risk cap | `src/risk/portfolio.py:361-390` | entry rejected; direct relationship is not encoded as `risk_per_trade<=cap`, but a proposal above cap cannot pass |
| RISK-002 | configured max positions must be integer1..30 | `src/risk/portfolio.py:46-51,257-260` | invalid limits raise at composition; UI currently does not show effective value |
| RISK-004, RISK-005 | projected gross notional/equity and proposed notional/buying power must each pass enabled cap | `src/risk/portfolio.py:361-390` | entry rejected; zero disables incremental-BP fraction |
| RISK-006 | daily P&L must exist and be `< -(equity*fraction)` | `src/risk/portfolio.py:392-402` | enabled limit fails closed if required inputs absent; current runtime comments say source is not yet canonical |
| RISK-007 | drawdown `(high_water-equity)/high_water >= fraction` | `src/risk/portfolio.py:404-411` | same canonical-input limitation |
| RISK-008..011 | exposure classification/strategy ID must be present when its cap is >0 | `src/risk/portfolio.py:413-448,505-530` | missing classification rejects entry |
| RISK-012 | non-USD evaluation requires FX rate and age `<=max_fx_age` | `src/risk/portfolio.py:450-463` | missing/stale FX rejects entry |
| SIZE-001..003 | `0 <= capital_min <= capital_ideal <= capital_max <=100`, and `min<max` | `src/risk/orb_position.py:25-46` | invalid settings rejected/fallback mapping can restore all code defaults |
| SIZE-001/SIZE-003 | valid plan requires `capital>=min` and `capital<max` | `src/risk/orb_position.py:198-205` | lower is inclusive, upper is exclusive |
| SIZE-004..006 | `0<=stopADR_min<=ideal<=max`, `min<max` | `src/risk/orb_position.py:31-48` | invalid settings rejected |
| SIZE-004/SIZE-006, ADR | valid plan requires stop/ADR within inclusive bounds and stop-loss% `< ADR%` | `src/risk/orb_position.py:206-217` | a stop at exactly ADR is rejected even if stop/ADR maximum would otherwise allow it |
| SIZE-009, RISK-001, RISK-003/RISK-004 | one fresh exact-account total equity value must be the common denominator for trade risk, aggregate risk and gross exposure | `src/ui/mixins/dashboard_mixin.py:1161-1199`; `src/services/buyboard_runtime.py:375-402,1040-1080` | configured PROD fails closed without broker truth; manual100k/10k fallbacks are planning-only and inconsistent |
| ENTRY-001/ENTRY-002, ORB high | `buffered_breakout=breakout*(1+buffer)` and `ORB high > buffered_breakout` | `src/strategy/orb/strategy.py:145-178`; `src/services/trading_engine.py:149-160` | plan not executable; contrary README `max()` wording |
| ENTRY-003/ORB-001 | opening range contains bars `start<=t<start+window` and, when required, session data reaches end | `src/strategy/orb/strategy.py:55-89` | range unavailable/fail closed |
| ORB-001/SESSION-001 | window must be supported and anchored at NYSE09:30 in America/New_York | `src/strategy/orb/config.py:9-22`; `src/strategy/orb/strategy.py:38-49` | unsupported window rejected; naive timestamp heuristic may infer UTC |
| STOP-001/RISK-001 | selected ORB low becomes both risk-sizing stop and first-fill active stop | `src/core/execution_queue.py:679-810`; `src/services/position_manager.py:259-273` | changed/missing selected plan invalidates approval fingerprint |
| STOP-003/STOP-004 | `breakeven=round_up(avg*(1+bps/10000))`; manual stop `>=max(breakeven,current)` | `src/services/position_manager.py:119-158,281-290` | below-min manual stop rejected; code does not enforce manual stop below current price/entry |
| STOP-004/current price | trigger at `current<=active_stop`; once `exit_all_required`, recovery cannot clear it | `src/services/position_manager.py:161-174` | sticky liquidation |
| EXIT-001/EXIT-004 | partial request must be positive and `<orderable`; `>=orderable` becomes Sell All | `src/services/execution_workflow_service.py:977-1013` | oversell converted to full liquidation, not partial |
| EXIT-002/EXIT-003 | partial alert only days3..5 and before first partial; EMA alert only after partial | `src/ui/buylist/monitoring.py:608-645` | alerts are manual suggestions, not automatic sells |
| EXIT-005, ENTRY-001 | a positive legacy `target_price` is migrated to `breakout_price`; active ORB must not interpret it as a profit exit | `src/core/watchlist.py:257-272,515-525`; `src/core/scoring.py:211-216` | an unversioned editable target field can overwrite structural-entry meaning or falsely imply an exit order |
| ORD-001/ORD-002 | normal discount0.5%; emergency reference retries multiply by attempt and cap at5% | `src/core/exit_execution_command.py:67-80` | no trusted price -> no command; emergency attempts capped elsewhere at3 |
| ORD-003 | legacy stop replacement only after new limit falls by at least0.2% | `src/ui/buylist/actions.py:717-725` | smaller move leaves old order working |
| ORD-004 | broker tick depends on price `<$1`; MOO commands must use price0 | `src/api/kis_order.py:290-297`; `src/core/exit_execution_command.py:110-118` | invalid command rejected/broker formatter changes precision |
| LIFE-001/LIFE-006 | TTL expiry requests cancel; replacement must wait for broker-confirmed cancel/terminal state | `src/core/execution_config.py:67-78`; `src/services/trading_engine.py` exit reconciliation | timeout creates critical alert, not permission to duplicate |
| LIFE-003/BROKER-004 | per-symbol entry attempt cap is in addition to process-wide mutation budget/spacing | `src/services/entry_attempt_manager.py:329-333`; `src/services/kis_request_scheduler.py:361-492` | whichever fence is stricter blocks/waits |
| LIFE-007 | approval command fields and price must exactly match; age must be within30s | `src/risk/pre_trade.py:160-198` | exposure-increasing order rejected before ledger reservation |
| LIFE-008/WF-002 | reservation/order absence needs configured interval and two complete generations | `src/services/account_reconciliation.py:1147-1155` | prevents release on one incomplete snapshot |
| SESSION-001/SESSION-003 | EOD predicate uses early-close-aware seconds-to-close; session must be a trading day | `src/services/buyboard_runtime.py:248-253`; `src/utils/market_calendar.py:116-151` | EOD not run on full-day closure; active through post-close |
| FILTER-001/FILTER-002 | every DB scanner query adds history>=1 before user rules | `src/infrastructure/database/repositories/scanner.py:489-523` | user cannot remove implicit base condition |
| FILTER-003..010/FILTER-005 | persisted thresholds depend on exact cached metric definitions/version | `src/utils/data_loader.py:869-1020`; `src/infrastructure/database/schema.py:307-362` | formula/window change requires full cache rebuild/versioning |
| DATA-001/DATA-002 | execution readiness needs both broker and receive ages within bounds plus queue/skew checks | `src/services/kis_realtime_market_data.py:1523-1525,1660-1661` | quote is display-only/not execution-ready |
| DATA-003/BROKER-003 | account snapshot must be fresh and cap/buying power must cover entry | `src/services/buying_power_cache.py:93-153`; `src/services/controlled_live_policy.py:167` | entry rejected/fails closed |
| DATA-005/DATA-006 | high-risk outage exits after grace; every tier exits after positive hard ceiling; supervised hold cannot suppress unattended ceiling | `src/services/trading_engine.py:1959-1979`; tests at `2157-2168` | queues at market open if session closed |
| DATA-006 | current `card.stop_adr` must have ATR-dollar units for `(price-stop)/ATR`; actual ORB field is percent-of-ADR | `src/services/trading_engine.py:202-205`; `src/risk/orb_position.py:173-177` | unresolved dimensional defect; do not configure threshold yet |
| WF-001/WF-002 | user transition graph cannot bypass unresolved order; one card key only | `src/core/kanban_transitions.py:26-136` | invalid command/duplicate card error |
| BROKER-001/BROKER-002/BROKER-004/BROKER-006 | engine true is insufficient; live mode, switch, lease, reconciliation, verified WS, budget, capital and risk gates all required | `src/core/execution_config.py:409-421`; `src/services/controlled_live_policy.py:49-111` | fail closed before broker mutation |
| INT-001 | heartbeat max age `>= heartbeat cadence+60`; several poll values have non-overridable floors | `src/core/execution_config.py:96-180` | protects failover/RU budget; not independently tunable |
| INT-003 | synchronized config must be versioned/atomic with active plan state; current key set excludes settings/scanner | `src/services/state_sync.py:59-64,902-905` | today, changing one device does not update the other |

## 6. Recommended first-wave settings

These ten are the strongest initial dashboard candidates. They already have clear units, runtime consumers, validation, and behavior-preserving defaults. The required synchronization strategy for all ten is a **single versioned, canonical settings payload in the coordination/state store**, atomically published with a revision and pulled by both PC and laptop. Apply a new revision only while the market is closed and no affected entry is pending; persist the revision used to build each queue candidate so an old plan cannot be silently reinterpreted. Local JSON may remain a cache, not the authority. This is a recommendation only—no sync/config change was made.

| Rank | Candidate/field | Current effective default | Why first wave | Required guard before implementation |
|---:|---|---:|---|---|
| 1 | ENTRY-002 — Breakout buffer | 0.1% dashboard /0.001 fraction | Already visible, persisted per plan, clear formula and 0..100 code bound | Replace points/fraction ambiguity with unit-tagged schema; sync revision |
| 2 | SIZE-001 — ORB minimum capital | 10% | Existing validated dialog and authoritative runtime consumer | Apply only to new/refreshed plans; sync revision |
| 3 | SIZE-002 — ORB ideal capital | 17.5% | Ranking-only, bounded by min/max | Enforce `min<=ideal<=max` atomically |
| 4 | SIZE-003 — ORB maximum capital | 28% | Clear rejection boundary and maximum100% code bound | Expert warning; respect portfolio/buying power; preserve upper-exclusive semantics |
| 5 | SIZE-004 — ORB minimum stop/ADR | 20% of ADR | Existing validation and clear unit | Store explicitly as percent-of-ADR, not ATR dollars |
| 6 | SIZE-005 — ORB ideal stop/ADR | 65% of ADR | Ranking-only and cross-field bounded | Atomic three-field validation |
| 7 | SIZE-006 — ORB maximum stop/ADR | 90% of ADR | Existing validator and clear current behavior | Expert warning; keep independent `stop_loss%<ADR%` check |
| 8 | FILTER-003 — Setup1 minimum daily volume | 40,000 shares | Existing editable scanner rule, direct eligibility effect | Version metric/rule schema and sync scanner setups |
| 9 | FILTER-003 — Setup1 minimum daily dollar volume | $35,000/day | Existing editable rule and clear unit | Resolve help-text $500k conflict before label/help release |
| 10 | FILTER-003 — Setup1 minimum ADR20 | 2.4 percentage points | Existing editable rule, metric window identified | Bind to metric definition/version; retain percentage-points representation |

Notably absent are account risk per trade (RISK-001), maximum positions (RISK-002), gross exposure (RISK-004), and planning account equity (SIZE-009). They are valuable future controls or readouts, but the current risk-unit conflict, stale UI limit, leverage/fallback conflict, and live-versus-manual equity semantics violate the first-wave requirement for one unambiguous authority.

## 7. Values that must remain internal

### UNSAFE execution and state invariants

| Candidates | Why they must not be ordinary dashboard settings |
|---|---|
| SIZE-007 | Whole-share rounding is part of risk arithmetic and broker validity. A UI knob could create zero/fractional orders or deliberate risk-budget overshoot. |
| ENTRY-003, ORB-003, ORB-004 | Range completion, strict trigger inequalities, exchange anchor, and dormant probe behavior define whether an order is authorized. They need strategy versioning/backtesting, not live free-form changes. |
| STOP-004 | “Never widen,” sticky stop trigger, and minimum manual stop are loss-containment invariants. |
| ORD-002, ORD-004, ORD-005 | Emergency collar lifecycle, broker tick rules, and quantity/orderable limits prevent rejection, oversell, and duplicate liquidation. |
| LIFE-003, LIFE-006, LIFE-007, LIFE-008 | Attempt caps, cancel-confirm proof, exact short-lived approval, and reconciliation intervals protect idempotency and order ownership. |
| SESSION-001, SESSION-002 | Exchange calendar and session/order-type mapping must track broker/exchange rules. User editing could submit an invalid order or execute at the wrong session. |
| DATA-002, DATA-003 | Timestamp/freshness and capital snapshot fences determine whether data is execution-grade; loosening them can authorize orders from stale truth. |
| WF-001..003 | State graph, one-card/owner rule, and EOD system transitions protect broker-order correlation and canonical state. |
| BROKER-001, BROKER-002, BROKER-004..007 | Engine availability, live envelope, mutation budgets, transport/retries, verified WS capacity, and formation source are privileged rollout/readiness controls. They must stay in runbooks/secure operations with fail-closed composition. |
| INT-001..003 | Coordination cadences, protocol identities, and sync key set jointly prevent split brain and incompatible peers. They require deployment/version migrations. |

`CONDITIONAL` does not imply a normal setting. RISK-003..012, STOP-003, order timing/collar settings, outage settings, and BROKER-003 should remain expert-only until their stated canonical-input, cross-field, broker, change-timing, and synchronization prerequisites are implemented. SIZE-009 must remain a read-only broker projection in configured production accounts; only an explicitly offline planning value could ever be editable. EXIT-005 is not an active strategy setting and should remain hidden while its legacy migration/removal is decided.

### SECRET values

SEC-001 through SEC-008—KIS application key/secret, account/product identifiers, tokens/caches, database credentials, webhook token, remote-control token, backup passphrase/crypto metadata, and device/ownership identities—must never appear as stock-management settings. At most, the dashboard may show redacted presence/connectivity status. No secret value was copied into this audit.

## 8. Unresolved questions

1. Is `TradeCardState.risk_percent=1.0` intended to represent 1 percentage point or a fraction of1.0? Repository consumers prove both interpretations. A versioned migration rule and one canonical type are required.
2. Should legacy Buylist migration divide `BuylistItem.risk_percent` by100 before constructing a card? No migration-version evidence answers this.
3. Is gross-notional exposure intentionally allowed to 200% in the current account, and should the fallback remain1,000%? Only local configuration and comments are available; broker margin policy is external.
4. Which ORB local values are deliberate product defaults: capital max28 vs30, stop/ADR min20 vs15, and max90 vs66? Local state proves current behavior but not intended default for a new device.
5. What unit should `TradeCardState.stop_adr` have during outage classification? ORB code produces percentage-of-ADR, while outage code expects an ATR price amount.
6. Is the intended ORB trigger `max(orb_high, buffered_breakout)` as README says, or the runtime rule that requires ORB high already exceed buffered breakout?
7. Should the first partial be fixed at one third, selectable one third to one half, or an arbitrary quantity? UI/rulebook and execution helper disagree.
8. Where should a per-position EMA10/EMA20 selection be stored, and should the exit remain advisory or become automatic? No persisted selection exists.
9. Which scanner history requirement is intended: one/two usable rows or65/252? This decision controls whether long-window indicators are meaningful.
10. Which volume-dry-up and consolidation-tightness formulas are intended? Current runtime and UI descriptions are mathematically different, not merely different thresholds.
11. Is the Setup1 $35,000 dollar-volume threshold intentional despite the catalog’s $500,000 minimum guidance? The local extra `price>=5` and `return_1m<1500` rules also need a new-device policy.
12. What are the measured KIS mutation/subscription/order limits for this account, and are they stable across PROD/SIM? The repository deliberately refuses to guess several capacities.
13. What finite upper bounds and permitted change windows should apply to portfolio percentages, retries, TTLs, outage times, and slippage? The repository does not establish them; broker/account/runbook decisions are needed.
14. Should exceptional NYSE closures be obtained from a maintained exchange calendar? The current recurring-calendar implementation explicitly does not cover one-off closures.
15. Should settings/scanner state join `SYNCED_STATE_KEYS`, or should a separate versioned configuration record be introduced? Atomicity with queue/card plan revisions must be decided before dashboard rollout.
16. Should risk-based share sizing continue to round up, as executable code does, or round down, as the risk rulebook requires? This decision must explicitly address the permitted one-share risk-budget overshoot.
17. Should the offline planning account-size fallback be $10,000, $100,000, or unset? Configured production accounts already have the safer answer: broker-reported exact-account equity only.
18. Can the legacy `take_profit`/`target_price` fields be removed after a versioned migration, or must an old trade-review workflow continue to carry them? They must not be relabeled as an active ORB profit target.

### Verification record

- Re-ran alternative named searches and literal searches over `src`, `tests`, `config`, `data`, `docs`, `rulebooks`, `.env.example`, and the non-secret whitelist of local `.env`/`.env.pc` settings.
- Inspected API adapters, dataclasses, enums, UI validators, workers/schedulers, state transitions, database schema/repositories, JSON persistence, environment parsing, tests/fakes, and documented values.
- Confirmed every master candidate has at least one exact file/line evidence reference and occurrence entry.
- Confirmed every SAFE/CONDITIONAL row states units, internal/dashboard representation, validation, bounds (or `null` with basis), cross-field constraints, timing, restart/open-order/open-position implications, and an unchanged proposed default.
- Compared production composition with test-friendly fallbacks: notably TradingEngine’s always-open/never-EOD constructor default, injected clocks/sleepers, permissive risk fixtures, and controlled-live mutation policy.
- Confirmed local `settings.json` and `scanner_setups.json` are not in live `SYNCED_STATE_KEYS`; `.env` and `.env.pc` currently agree for the non-secret audited overrides.
- Confirmed database schema contains scanner/cache fields but no separate relational defaults/constraints for these trade-risk settings.
- Confirmed only this Markdown audit was added. No Python source, runtime JSON, environment file, database schema, test, or production behavior was changed.
