"""Registration port for the shared public control panel."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


PanelFactory = Callable[[Any], Any]
ViewFactory = Callable[[], Any]

_panel_factory: PanelFactory | None = None
_view_factory: ViewFactory | None = None


def register_public_panel_provider(panel_factory: PanelFactory, view_factory: ViewFactory) -> None:
    global _panel_factory, _view_factory
    _panel_factory = panel_factory
    _view_factory = view_factory


def public_panel_provider() -> tuple[PanelFactory, ViewFactory] | None:
    if _panel_factory is None or _view_factory is None:
        return None
    return _panel_factory, _view_factory
