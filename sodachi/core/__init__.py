"""Millimetre-native geometry and the layout solver.

Nothing in this package may import pyvips, reportlab or PySide6. The rule is
enforced by ``tests/test_import_graph.py``; it is what stops the physical and
digital renderers from drifting apart.
"""

from sodachi.core.geometry import Point, Rect, Size
from sodachi.core.layout import Layout, Margins, Sheet, Slot
from sodachi.core.solver import LayoutError, solve

__all__ = [
    "Point",
    "Rect",
    "Size",
    "Layout",
    "Margins",
    "Sheet",
    "Slot",
    "LayoutError",
    "solve",
]
