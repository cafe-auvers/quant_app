from __future__ import annotations

from typing import Any, Type, TypeVar


class WindowController:
    """Base for workflows that are explicitly bound to a UI host.

    Controllers may call the host through :attr:`window`, but they never proxy
    arbitrary attribute reads or writes.  Keeping that dependency visible is
    important: a controller must not silently turn into another view of the
    ``MainWindow`` god object.
    """

    def __init__(self, window: Any) -> None:
        self.window = window


ControllerT = TypeVar("ControllerT", bound=WindowController)


def get_controller(
    window: Any, attribute_name: str, controller_class: Type[ControllerT]
) -> ControllerT:
    controller = getattr(window, "__dict__", {}).get(attribute_name)
    if controller is None:
        controller = controller_class(window)
        window.__dict__[attribute_name] = controller
    return controller
