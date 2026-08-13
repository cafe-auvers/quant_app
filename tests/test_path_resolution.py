from src.api.kis_account_snapshot_dual import KisEnvironment
from src.services import app_state
from src.services.order_ledger import ORDERS_FILE
from src.ui.buylist.constants import EXECUTION_QUEUE_FILE
from src.utils import data_loader
from src.utils.config import (DATA_DIR, DEFAULT_KIS_TOKEN_CACHE, ROOT_DIR,
                              RULEBOOK_DIR, resolve_repo_path)


def test_application_owned_paths_are_repository_anchored(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    assert DATA_DIR == ROOT_DIR / "data"
    assert app_state.WATCHLIST_FILE == DATA_DIR / "watchlist.json"
    assert app_state.BUYLIST_FILE == DATA_DIR / "buylist.json"
    assert ORDERS_FILE == DATA_DIR / "orders.json"
    assert EXECUTION_QUEUE_FILE == DATA_DIR / "execution_queue.json"
    assert data_loader.DEFAULT_UNIVERSE_CACHE == DATA_DIR / "us_kis_tickers.csv"
    assert RULEBOOK_DIR == ROOT_DIR / "rulebooks"


def test_relative_configured_paths_resolve_from_repository_root(tmp_path):
    absolute = tmp_path / "token.json"

    assert resolve_repo_path("custom/token.json") == ROOT_DIR / "custom/token.json"
    assert resolve_repo_path(absolute) == absolute
    assert KisEnvironment.PROD.default_token_cache == DEFAULT_KIS_TOKEN_CACHE
