"""Unit conversion, and nothing else.

Every crossing between millimetres and some other unit goes through this
module, so a regression in the PDF path and a regression in the raster path
cannot silently disagree with each other.
"""

from __future__ import annotations

import math

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0

NOMINAL_SCREEN_DPI = 300.0
"""The DPI that gives a pixel-specified sheet a physical size.

A social post has no physical size, so its sheet is naturally specified in
pixels. That is converted to millimetres exactly once, at spec-load time, at
this DPI — the solver never learns that screen output exists. The raster
renderer converts back at the same DPI and must land on the requested pixel
count exactly; ``tests/test_screen.py`` treats the round trip as a test case
rather than an assumption.
"""


def round_half_up(value: float) -> int:
    """Round halves away from zero.

    Python's ``round`` is banker's rounding, so ``round(0.5) == 0``. That is
    defensible statistically and wrong here: a half-pixel is a visible seam,
    and identical geometry either side of a gutter should round the same way.
    """
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


def mm_to_px(mm: float, dpi: float) -> int:
    """Millimetres to whole pixels at ``dpi``. The one conversion in §1."""
    return round_half_up(mm * dpi / MM_PER_INCH)


def mm_to_px_exact(mm: float, dpi: float) -> float:
    """Millimetres to fractional pixels, for measurement and assertions."""
    return mm * dpi / MM_PER_INCH


def mm_to_px_floor(mm: float, dpi: float) -> int:
    """Millimetres to pixels, rounded down.

    Slot origins use this so that two slots sharing a gutter can never round
    towards each other and overlap by a pixel.
    """
    return int(math.floor(mm * dpi / MM_PER_INCH))


def px_to_mm(px: float, dpi: float) -> float:
    return px * MM_PER_INCH / dpi


def mm_to_pt(mm: float) -> float:
    """Millimetres to PostScript points. The one conversion in §7."""
    return mm * PT_PER_INCH / MM_PER_INCH


def pt_to_mm(pt: float) -> float:
    return pt * MM_PER_INCH / PT_PER_INCH


def inch_to_mm(inch: float) -> float:
    return inch * MM_PER_INCH


def mm_to_inch(mm: float) -> float:
    return mm / MM_PER_INCH


__all__ = [
    "MM_PER_INCH",
    "PT_PER_INCH",
    "NOMINAL_SCREEN_DPI",
    "round_half_up",
    "mm_to_px",
    "mm_to_px_exact",
    "mm_to_px_floor",
    "px_to_mm",
    "mm_to_pt",
    "pt_to_mm",
    "inch_to_mm",
    "mm_to_inch",
]
