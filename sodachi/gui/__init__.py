"""The PySide6 front end.

Importing this package pulls in no Qt at all. ``main`` is resolved lazily, so
``import sodachi.gui`` stays cheap for anything that only wants to look the
package up — a packaging spec reading entry points, a test collecting modules —
without paying for a widget toolkit it is not going to use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sodachi.gui.app import main


def __getattr__(name: str) -> Any:
    if name == "main":
        from sodachi.gui.app import main as _main

        return _main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["main"]
