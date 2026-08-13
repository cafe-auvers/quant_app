"""Compatibility alias for the P2 chart controller."""
import sys
from src.ui.charts import controller as _controller

sys.modules[__name__] = _controller
