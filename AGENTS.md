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

This workspace does not include Git history, so no existing commit convention is available. Use concise, imperative commit messages, for example `Add latest cache date to dashboard`.

Pull requests should include a short summary, test results such as `pytest -q`, and screenshots for visible PyQt UI changes. Link related issues or notes when a change affects trading rules, risk behavior, or persisted data formats.

## Security & Configuration Tips

Keep secrets out of source control. Store local database credentials in `.env` using keys such as `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DB`. Treat files in `data/` as local state unless intentionally adding sample data.
