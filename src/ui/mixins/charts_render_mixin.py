"""Compatibility alias for the P2 chart renderer."""
import sys
from src.ui.charts import renderer as _renderer

sys.modules[__name__] = _renderer
