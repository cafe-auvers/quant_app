"""Composite chart controller assembled from focused workflow mixins."""

from .controller_data_flow import ChartsDataFlowMixin
from .controller_drawing import ChartsDrawingMixin
from .controller_layout import ChartsLayoutMixin
from .controller_navigation import ChartsNavigationMixin
from .controller_plotting import ChartsPlottingMixin


class ChartsControllerMixin(
    ChartsLayoutMixin,
    ChartsNavigationMixin,
    ChartsDataFlowMixin,
    ChartsDrawingMixin,
    ChartsPlottingMixin,
):
    """Compatibility surface for the existing MainWindow inheritance graph."""
