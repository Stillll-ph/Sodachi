"""DXF R12 ASCII, written by hand.

R12 is the version every piece of cutter software reads, including the ones
shipped on a CD in 2004, and it is simple enough that generating it by hand is
sounder than taking on a dependency that speaks a dialect the machine may not.
R12 predates LWPOLYLINE, so a closed rectangle is a POLYLINE followed by four
VERTEX entities and a SEQEND, with the closed bit set in group 70.

This writer emits DXF's own convention: y increases upwards, origin at the
bottom-left corner of the board. The flip out of the layout's top-left frame
happens in ``_to_dxf`` and nowhere else in this module.
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
    extents_mm,
    format_mm,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sodachi.core.layout import Layout
    from sodachi.spec.model import Spec

INSUNITS_MILLIMETRES = 4
"""$INSUNITS value 4. R12 predates the variable, but readers that know it get
the units right without asking, and readers that do not simply skip it."""

_LINE_ENDING = "\r\n"
"""ASCII DXF is a CRLF format. Some old readers treat a bare LF as corruption."""

_LAYER_COLOURS: dict[str, int] = {WINDOW_LAYER: 1, WINDOW_TOP_LAYER: 3, BOARD_LAYER: 5}
_DEFAULT_COLOUR = 7

_CONTINUOUS = "CONTINUOUS"


def _tag(out: list[str], code: int, value: str) -> None:
    """One group-code pair: the code on its own line, then its value."""
    out.append(str(code))
    out.append(value)


def _to_dxf(point_mm: tuple[float, float], sheet_height_mm: float) -> tuple[float, float]:
    """The only top-left-to-bottom-left flip in this module."""
    x_mm, y_mm = point_mm
    return x_mm, sheet_height_mm - y_mm


def _header(out: list[str], min_x: float, min_y: float, max_x: float, max_y: float) -> None:
    _tag(out, 0, "SECTION")
    _tag(out, 2, "HEADER")

    _tag(out, 9, "$ACADVER")
    _tag(out, 1, "AC1009")

    _tag(out, 9, "$INSUNITS")
    _tag(out, 70, str(INSUNITS_MILLIMETRES))

    _tag(out, 9, "$EXTMIN")
    _tag(out, 10, format_mm(min_x))
    _tag(out, 20, format_mm(min_y))
    _tag(out, 30, "0")

    _tag(out, 9, "$EXTMAX")
    _tag(out, 10, format_mm(max_x))
    _tag(out, 20, format_mm(max_y))
    _tag(out, 30, "0")

    _tag(out, 0, "ENDSEC")


def _tables(out: list[str], layers: list[str]) -> None:
    _tag(out, 0, "SECTION")
    _tag(out, 2, "TABLES")
    _tag(out, 0, "TABLE")
    _tag(out, 2, "LAYER")
    _tag(out, 70, str(len(layers)))
    for layer in layers:
        _tag(out, 0, "LAYER")
        _tag(out, 2, layer)
        _tag(out, 70, "0")
        _tag(out, 62, str(_LAYER_COLOURS.get(layer, _DEFAULT_COLOUR)))
        _tag(out, 6, _CONTINUOUS)
    _tag(out, 0, "ENDTAB")
    _tag(out, 0, "ENDSEC")


def _polyline(out: list[str], path: CutPath, sheet_height_mm: float) -> None:
    _tag(out, 0, "POLYLINE")
    _tag(out, 8, path.layer)
    _tag(out, 66, "1")  # vertices follow, up to the SEQEND
    _tag(out, 70, "1")  # closed: the last vertex joins back to the first
    # R12 wants a dummy location on the POLYLINE header itself; the vertices
    # carry the real coordinates.
    _tag(out, 10, "0")
    _tag(out, 20, "0")
    _tag(out, 30, "0")

    for point_mm in path.points_mm:
        x, y = _to_dxf(point_mm, sheet_height_mm)
        _tag(out, 0, "VERTEX")
        _tag(out, 8, path.layer)
        _tag(out, 10, format_mm(x))
        _tag(out, 20, format_mm(y))
        _tag(out, 30, "0")

    _tag(out, 0, "SEQEND")
    _tag(out, 8, path.layer)


def dxf_document(paths: tuple[CutPath, ...], sheet_height_mm: float) -> str:
    """The whole R12 file as text, so it can be asserted on without a temp file."""
    min_x, min_y, max_x, max_y = extents_mm(paths)
    # Extents are stated in the file's own frame, so the y bounds flip too and
    # swap places: the layout's largest y is the drawing's smallest.
    _, dxf_max_y = _to_dxf((min_x, min_y), sheet_height_mm)
    _, dxf_min_y = _to_dxf((max_x, max_y), sheet_height_mm)

    layers: list[str] = []
    for path in paths:
        if path.layer not in layers:
            layers.append(path.layer)

    out: list[str] = []
    _header(out, min_x, dxf_min_y, max_x, dxf_max_y)
    _tables(out, layers)

    _tag(out, 0, "SECTION")
    _tag(out, 2, "ENTITIES")
    for path in paths:
        _polyline(out, path, sheet_height_mm)
    _tag(out, 0, "ENDSEC")

    _tag(out, 0, "EOF")
    return _LINE_ENDING.join(out) + _LINE_ENDING


def write_dxf(
    layout: Layout,
    spec: Spec,
    out_path: str | Path,
    *,
    mirror: bool = False,
) -> Path:
    """Write the cut paths as DXF R12 ASCII. See ``cut_paths`` on ``mirror``."""
    paths = cut_paths(layout, spec, mirror=mirror)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps Python from turning the CRLFs into CRCRLF on Windows.
    out_path.write_text(
        dxf_document(paths, layout.sheet.height_mm), encoding="ascii", newline=""
    )
    return out_path


__all__ = ["INSUNITS_MILLIMETRES", "dxf_document", "write_dxf"]
