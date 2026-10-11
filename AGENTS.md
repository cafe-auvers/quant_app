# Repository Guidelines

## Project Structure & Module Organization

This is a Python/PyQt5 desktop trading dashboard. The entry point is `main.py`, which launches `src/ui/main_window.py`.

- `src/ui/`: PyQt5 window, tab, chart, and interaction code.
- `src/core/`: business logic for scanning, watchlists, trade plans, position sizing, and reviews.
- `src/utils/`: configuration, storage, data loading, and MySQL cache helpers.
- `src/api/`: external API clients, including KIS integration.
- `tests/`: pytest coverage for core behavior and selected UI helpers.
- `data/`: local JSON state and ticker universe files.
- `rulebooks/`: markdown trading rules used by the review workflow.
- `config/`: templates and non-secret configuration examples.

## Build, Test, and Development Commands

- `pip install -r requirements.txt`: install runtime and test dependencies.
- `python main.py`: start the desktop dashboard.
- `pytest -q`: run the full test suite with compact output.
- `pytest tests/test_core_behaviour.py -q`: run the main behavior regression tests.

The app can run without MySQL, but database-backed scanning and cache freshness features require valid MySQL settings.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation. Keep modules focused by layer: UI orchestration in `src/ui`, trading logic in `src/core`, persistence and external data helpers in `src/utils`. Use `snake_case` for functions, variables, and files; use `PascalCase` for classes such as `MainWindow` and `TradePlan`.

Prefer small helper functions over duplicating logic in UI handlers. Keep comments short and only where they clarify non-obvious behavior.

## Testing Guidelines

Tests use `pytest`. Add tests for changes in scanner rules, watchlist persistence, local JSON persistence/shutdown flushing, trade planning, database helpers, and pure UI formatting helpers. Name tests with `test_...` and place them under `tests/`.

For database-related helpers, prefer isolated in-memory or temporary test fixtures when possible. Do not require a developer's local MySQL instance for normal unit tests.

## Commit & Pull Request Guidelines

Use concise, imperative commit messages consistent with the repository history, for example `Add latest cache date to dashboard`.

Pull requests should include a short summary, test results such as `pytest -q`, and screenshots for visible PyQt UI changes. Link related issues or notes when a change affects trading rules, risk behavior, or persisted data formats.

## Security & Configuration Tips

Keep secrets out of source control. Store credentials and private tokens only in `.env`; non-secret hosts, ports, feature flags, limits, and timing settings belong in `config/runtime.json` or the gitignored `config/runtime.local.json` override. Treat files in `data/` as local state unless intentionally adding sample data. Live symbol lists belong only in their dedicated `data/` files, never in an environment file.

Application startup and the PC morning routine synchronize the credential-only `.env.example` schema, migrate recognized legacy runtime keys out of `.env` into `config/runtime.local.json`, and regenerate the gitignored credential-only `.env.pc` copy. Run `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\sync_pc_env.ps1` when an immediate manual sync is needed. Preserve credential values, preserve migrated runtime overrides, and keep generated `MYSQL_*` credentials blank in `.env.pc`; never commit `.env`, `.env.pc`, or `config/runtime.local.json`.

Current release reference: [Opening liquidity and mobile workflow (2026-10-08)](docs/opening_liquidity_mobile_release_2026-10-08.md). Dated reports and archived plans retain their original scope.

## Trading Day Journals

The user shares simple comments about the trading day **in chat**. The assistant
writes one journal entry in the user's first-person voice using plain language
and few adjectives. Do not ask the user to fill structured trade-writing forms,
invent motivations or emotions, or turn the entry into promotional prose.

Use the recorded facts and attach daily and 5-minute graphs for stocks bought on
the chosen New York trading session. Read
[the journal workflow](docs/trading_journey_journal.md) for chart packets and
draft persistence. Show the finished draft and its graphs for review. Publish
only after the user confirms that exact draft and chooses the destinations;
an earlier approval does not cover changed text or charts. Social platforms will
be chosen after the first draft. Preparing an entry or approving a draft is not
evidence that it has been posted.

Include daily observed NAV/account equity, cash-flow-adjusted daily P&L and
market context in the journal evidence. Russell 1000 is the user's requested
"1000 index"; label IWB, SPY and QQQ as ETF proxies. Use basis points for yield
changes. Do not treat cumulative Health-tab P&L as daily P&L, assume unknown
cash flows are zero, or infer a causal macro explanation from price movements.
