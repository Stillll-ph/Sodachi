"""SVG holding cut geometry only, at true millimetre scale.

Cutter software and drawing programs both read SVG, and both of them will cut
or print whatever is in the file, so this carries the windows and the board
outline and nothing else: no labels, no calibration bar, no dimension
callouts. The document is sized in real millimetres with a matching viewBox, so
it opens at 1:1 in anything that honours physical units, and every path is
stroked hairline with ``fill="none"`` because a filled window would be read as
a region to engrave rather than an outline to follow.

SVG user space already runs y downwards from a top-left origin, which is the
layout's own convention, so this writer emits the layout coordinates unchanged.
``_to_svg`` is where that claim lives; it is the counterpart of the flip the
DXF writer has to do.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sodachi.export.cutpath import (
    BOARD_LAYER,
    WINDOW_LAYER,
    WINDOW_TOP_LAYER,
    CutPath,
    cut_paths,
    format_mm,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sodachi.core.layout import Layout
    from sodachi.spec.model import Spec

HAIRLINE_MM = 0.1
"""Stroke width in user units, which are millimetres here. Thin enough that
software keying off stroke width treats it as a cut, not a filled shape."""

# Colours match the DXF layer colours (1 red, 3 green, 5 blue), because a good
# deal of cutter software maps colour to tool and the two exports should not
# disagree about which line is which.
_LAYER_STROKES: dict[str, str] = {
    WINDOW_LAYER: "#FF0000",
    WINDOW_TOP_LAYER: "#00FF00",
    BOARD_LAYER: "#0000FF",
}
_DEFAULT_STROKE = "#000000"


def _to_svg(point_mm: tuple[float, float]) -> tuple[float, float]:
    """Layout millimetres to SVG user units: identity, y stays downwards.

    Kept as a function rather than inlined so there is one place to look for
    this writer's coordinate convention, and one place to change if the
    viewBox ever stops being 1 unit to 1 millimetre.
    """
    return point_mm


def _path_data(path: CutPath) -> str:
    """The ``d`` attribute: move, lines, close. Z supplies the implied edge."""
    parts: list[str] = []
    for index, point_mm in enumerate(path.points_mm):
        x, y = _to_svg(point_mm)
        parts.append(f"{'M' if index == 0 else 'L'} {format_mm(x)} {format_mm(y)}")
    parts.append("Z")
    return " ".join(parts)


def svg_document(
    paths: tuple[CutPath, ...], sheet_width_mm: float, sheet_height_mm: float
) -> str:
    """The whole SVG as text, so it can be asserted on without a temp file."""
    width = format_mm(sheet_width_mm)
    height = format_mm(sheet_height_mm)

    layers: list[str] = []
    for path in paths:
        if path.layer not in layers:
            layers.append(path.layer)

    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        (
            '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{width}mm" height="{height}mm" '
            f'viewBox="0 0 {width} {height}">'
        ),
        "  <title>Sodachi mat cut paths</title>",
        f"  <desc>Cut geometry only. User units are millimetres, {width} × {height} mm.</desc>",
    ]

    for layer in layers:
        stroke = _LAYER_STROKES.get(layer, _DEFAULT_STROKE)
        out.append(
            f'  <g id="{layer}" fill="none" stroke="{stroke}" '
            f'stroke-width="{format_mm(HAIRLINE_MM)}">'
        )
        for path in paths:
            if path.layer != layer:
                continue
            out.append(f'    <path id="{path.role}" d="{_path_data(path)}"/>')
        out.append("  </g>")

    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_svg(
    layout: Layout,
    spec: Spec,
    out_path: str | Path,
    *,
    mirror: bool = False,
) -> Path:
    """Write the cut paths as SVG. See ``cut_paths`` on ``mirror``."""
    paths = cut_paths(layout, spec, mirror=mirror)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        svg_document(paths, layout.sheet.width_mm, layout.sheet.height_mm),
        encoding="utf-8",
        newline="\n",
    )
    return out_path


__all__ = ["HAIRLINE_MM", "svg_document", "write_svg"]
