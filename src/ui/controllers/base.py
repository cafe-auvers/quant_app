from __future__ import annotations

from typing import Any, Type, TypeVar


class WindowController:
    """Forward workflow controller attribute access to the owning MainWindow."""

    def __init__(self, window: Any) -> None:
        object.__setattr__(self, "window", window)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.window, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "window":
            object.__setattr__(self, name, value)
            return
        setattr(self.window, name, value)


ControllerT = TypeVar("ControllerT", bound=WindowController)


def get_controller(window: Any, attribute_name: str, controller_class: Type[ControllerT]) -> ControllerT:
    controller = getattr(window, "__dict__", {}).get(attribute_name)
    if controller is None:
        controller = controller_class(window)
        window.__dict__[attribute_name] = controller
    return controller
