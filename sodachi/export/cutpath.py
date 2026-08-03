"""Cut geometry for a computerised mat cutter: closed polygons, millimetres.

A cutting machine wants the outlines it should follow and nothing else. The
guide in ``render/matguide.py`` is a drawing for a person: it carries dimension
callouts, a calibration bar, crosshairs and cut lines that run past the
corners, and a machine would happily cut every one of them. This module keeps
the geometry the two share -- the window openings and the board -- and drops
the rest.

The openings are the same openings the guide draws, imported from
``sodachi.core.mat`` rather than recomputed: at zero reveal each solved slot
rect inset by ``mat.window_overlap_mm`` on all four sides, above zero the slot
grown by ``mat.reveal_mm``. Sharing the computation is what keeps the printed
guide and the machine file one shape, and core is the one place both can
import from — ``render/matguide.py`` imports reportlab at module scope, and a
DXF writer has no business pulling in a PDF library.

Everything in this package is millimetres, top-left origin, y increasing
downwards -- the layout's own frame. Each writer states the convention its
format uses and does the conversion in one helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sodachi.core.geometry import Rect
from sodachi.core.mat import MatOpeningError, openings_mm, outer_openings_mm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sodachi.core.layout import Layout
    from sodachi.spec.model import Spec

WINDOW_LAYER = "WINDOW"
WINDOW_TOP_LAYER = "WINDOW_TOP"
"""The top board's openings of a double mat. A separate layer because the two
boards are cut separately: the machine cuts WINDOW and BOARD into one board,
WINDOW_TOP and BOARD into the other."""
BOARD_LAYER = "BOARD"

_DECIMALS = 6
"""Nanometre resolution in millimetres, which is below any cutter's step."""


class CutPathError(ValueError):
    """Raised when a spec describes nothing a cutter could usefully cut."""


@dataclass(frozen=True, slots=True)
class CutPath:
    """One closed polygon for a cutter, in millimetres.

    ``points_mm`` is a closed polygon: the edge from the last point back to the
    first is implied and the first point is never repeated. Each writer closes
    the path in whatever way its own format spells closure.
    """

    role: str
    layer: str
    points_mm: tuple[tuple[float, float], ...]

    @property
    def bounds_mm(self) -> tuple[float, float, float, float]:
        """``(min_x, min_y, max_x, max_y)`` in the polygon's own frame."""
        xs = [x for x, _ in self.points_mm]
        ys = [y for _, y in self.points_mm]
        return min(xs), min(ys), max(xs), max(ys)


def format_mm(value_mm: float) -> str:
    """A millimetre value as text, trailing zeros trimmed, no negative zero.

    Shared by all three writers so a coordinate reads identically whichever
    file it lands in, which makes the exports diffable against each other.
    """
    text = f"{value_mm:.{_DECIMALS}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-", "-0") else text


def _require_cuttable(spec: Spec) -> None:
    if not spec.mat.enabled:
        raise CutPathError(
            "mat.enabled is false, so this spec describes no mat; set mat.enabled to true "
            "before asking for cutter paths"
        )
    if spec.target == "screen":
        raise CutPathError(
            "target is 'screen', but cutter paths describe board cut for a physical print; "
            "set target to 'print'"
        )


def _rect_polygon(rect: Rect) -> tuple[tuple[float, float], ...]:
    """Corners in the order top-left, top-right, bottom-right, bottom-left.

    Reflecting the rect before this call rather than reflecting the points
    after it is what keeps a mirrored path wound the same way as an unmirrored
    one, so a cutter that cares about direction sees no difference.
    """
    return (
        (rect.x_mm, rect.y_mm),
        (rect.right_mm, rect.y_mm),
        (rect.right_mm, rect.bottom_mm),
        (rect.x_mm, rect.bottom_mm),
    )


def _openings_mm(layout: Layout, spec: Spec, *, mirrored: bool) -> list[Rect]:
    """The shared opening geometry, with its errors re-raised in this module's voice."""
    try:
        return openings_mm(
            layout,
            overlap_mm=float(spec.mat.window_overlap_mm),
            reveal_mm=float(spec.mat.reveal_mm),
            mirrored=mirrored,
        )
    except MatOpeningError as exc:
        raise CutPathError(str(exc)) from exc


def _outer_openings_mm(layout: Layout, spec: Spec, *, mirrored: bool) -> list[Rect]:
    """The top board's openings of a double mat, errors in this module's voice."""
    try:
        return outer_openings_mm(
            layout,
            overlap_mm=float(spec.mat.window_overlap_mm),
            reveal_mm=float(spec.mat.reveal_mm),
            inner_reveal_mm=float(spec.mat.inner_reveal_mm),
            mirrored=mirrored,
        )
    except MatOpeningError as exc:
        raise CutPathError(str(exc)) from exc


def cut_paths(layout: Layout, spec: Spec, *, mirror: bool = False) -> tuple[CutPath, ...]:
    """The board and its windows as closed polygons, millimetres, y downwards.

    Windows come first and the board outline last, because that is the order
    they have to be cut in: the outline is what holds the board flat while the
    interior falls out.

    ``mirror`` defaults to False and deliberately ignores ``spec.mat.mirror``.
    That flag exists because a mat cut by hand is cut from the back, so the
    printed guide has to be reflected for a person to lay it on the reverse of
    the board. A computerised cutter is told the true geometry and works out
    its own orientation; hand it a reflected path and it cuts a reflected mat,
    which for any layout that is not symmetric is a wasted board. The parameter
    stays for the rare machine that genuinely expects back-side coordinates,
    and it has to be asked for by name.

    No overcut is applied. The guide runs each cut line past both corners
    because a 45-degree bevel blade travels roughly the board thickness before
    it clears; a closed polygon cannot express that overrun, and cutter
    software applies its own lead-in, lead-out and overcut settings, which
    would compound with anything baked in here.
    """
    _require_cuttable(spec)

    sheet = Rect(0.0, 0.0, layout.sheet.width_mm, layout.sheet.height_mm)
    windows_mm = _openings_mm(layout, spec, mirrored=mirror)

    out = [
        CutPath(
            role=f"window_{index + 1}",
            layer=WINDOW_LAYER,
            points_mm=_rect_polygon(opening_mm),
        )
        for index, opening_mm in enumerate(windows_mm)
    ]
    if spec.mat.double:
        # The top board's openings, appended after the bottom board's so a
        # single-mat file is a byte-for-byte prefix of what it always was.
        # Both boards share the one board outline below.
        out.extend(
            CutPath(
                role=f"window_top_{index + 1}",
                layer=WINDOW_TOP_LAYER,
                points_mm=_rect_polygon(outer_mm),
            )
            for index, outer_mm in enumerate(
                _outer_openings_mm(layout, spec, mirrored=mirror)
            )
        )
    out.append(
        CutPath(role="board_outline", layer=BOARD_LAYER, points_mm=_rect_polygon(sheet))
    )
    return tuple(out)


def extents_mm(paths: tuple[CutPath, ...]) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)`` over every path, in layout coordinates."""
    if not paths:
        raise CutPathError("extents_mm needs at least one cut path")
    boxes = [path.bounds_mm for path in paths]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


__all__ = [
    "BOARD_LAYER",
    "WINDOW_LAYER",
    "WINDOW_TOP_LAYER",
    "CutPath",
    "CutPathError",
    "cut_paths",
    "extents_mm",
    "format_mm",
]
