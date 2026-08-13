"""Compatibility alias for :mod:`src.ui.buylist`."""
import sys
from src.ui import buylist as _buylist

sys.modules[__name__] = _buylist
