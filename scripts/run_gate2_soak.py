"""Run the standalone, read-only Gate-2 KIS WebSocket soak."""
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.config import install_repository_configuration  # noqa: E402


# Gate-2 imports execution_config, which snapshots its values at import time.
# Install the repository-backed credential/runtime configuration first so the
# standalone runner observes the same fail-closed settings as main.py.
install_repository_configuration()

from gate2.reporting import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
