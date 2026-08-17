"""Run PR8's deterministic Workstream 7 capstone and emit its JSON report."""
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gate1.reporting import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
