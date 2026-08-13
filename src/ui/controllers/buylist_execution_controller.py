"""Compatibility alias for the P2 buylist execution controller."""
import sys

from src.ui.buylist import execution_controller as _execution_controller

sys.modules[__name__] = _execution_controller
