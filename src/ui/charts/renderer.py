"""Composite chart renderer assembled from focused rendering mixins."""

from .render_lightweight import ChartLightweightRenderMixin
from .render_local import ChartLocalRenderMixin
from .render_metrics import ChartRenderMetricsMixin
from .render_primitives import ChartRenderPrimitivesMixin


class ChartsRenderMixin(
    ChartRenderPrimitivesMixin,
    ChartLightweightRenderMixin,
    ChartLocalRenderMixin,
    ChartRenderMetricsMixin,
):
    """Compatibility surface for chart rendering helpers."""
