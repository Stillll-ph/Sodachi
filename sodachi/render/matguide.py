"""The true-scale mat cutting guide, as vector PDF.

A guide that is subtly wrong wastes real board, so the drawing is assembled as
millimetre primitives first and handed to reportlab second. The primitives are
the part worth testing: there is no rasteriser on this machine, and asserting
that the calibration bar is 100.000mm in the record catches every
unit-conversion regression that measuring a rendered bitmap would.

The opening geometry itself, mirroring included, is shared with the cut-file
writers through ``sodachi.core.mat``, so the guide a person checks and the file
a machine cuts cannot drift apart. The page flip — Sodachi's top-left origin
into PDF's bottom-left — lives here and nowhere else. Mirroring applies to
geometry only: a mirrored label is unreadable, and the guide is for a human.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from reportlab.pdfgen import canvas

from sodachi.core.geometry import Rect
from sodachi.core.mat import MatOpeningError, openings_mm, outer_openings_mm
from sodachi.core.units import mm_to_pt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sodachi.core.layout import Layout
    from sodachi.spec.model import MatSpec, Spec

CALIBRATION_LENGTH_MM = 100.0
CALIBRATION_TICK_STEP_MM = 10.0
CALIBRATION_TEXT = "Print at 100% / Actual Size. This bar must measure exactly 100mm."

_MIRRORED_BANNER = "BACK OF BOARD — MIRRORED"
_UNMIRRORED_BANNER = "FRONT OF BOARD — NOT MIRRORED"

# Stroke width in points and grey level per weight class. Cut lines are the
# ones a blade follows, so they are the only black ones.
_WEIGHTS: dict[str, tuple[float, float]] = {
    "cut": (1.0, 0.0),
    "opening": (0.4, 0.45),
    "annotation": (0.25, 0.35),
}

_TEXT_GREY = 0.20
_TEXT_GREY_BOLD = 0.0

_HEADER_LEFT_MM = 8.0
_BANNER_BASELINE_MM = 11.0
_HEADER_FIRST_BASELINE_MM = 16.5
_HEADER_STEP_MM = 4.2
_TEXT_SLACK_MM = 1.5
"""Descender allowance below a baseline, so a line cannot creep into a window."""

_CAL_LEFT_MM = 10.0
_CAL_BAR_FROM_BOTTOM_MM = 10.0
_CAL_TICK_MM = 3.0
_CAL_LABEL_FROM_BOTTOM_MM = 5.0
_BOARD_CALLOUT_FROM_BOTTOM_MM = 1.6
_BOARD_HEIGHT_CALLOUT_LEFT_MM = 2.0
_BOARD_HEIGHT_CALLOUT_CLEARANCE_MM = 20.0

_DIM_OFFSET_MM = 2.2
_CROSSHAIR_ARM_MM = 6.0
_IMAGE_LABEL_DROP_MM = 4.0
"""Baseline of the image-extent label below the extent's top edge."""


class MatGuideError(ValueError):
    """Raised when a mat guide is asked for that would be wrong or useless."""


@dataclass(frozen=True, slots=True)
class MatPrimitive:
    """One drawing instruction, in millimetres, top-left origin, y down.

    ``points_mm`` is interpreted per ``kind``: ``line`` and ``tick`` take two
    endpoints; ``rect`` takes top-left and bottom-right corners; ``text`` takes
    a single baseline anchor; ``crosshair`` takes the centre followed by the
    four arm endpoints, so the centre is always ``points_mm[0]``.
    """

    kind: Literal["line", "rect", "text", "crosshair", "tick"]
    points_mm: tuple[tuple[float, float], ...]
    text: str | None = None
    weight: Literal["cut", "opening", "annotation"] = "annotation"
    role: str = ""
    size_pt: float = 7.0
    align: Literal["left", "center", "right"] = "left"
    bold: bool = False

    @property
    def length_mm(self) -> float:
        if len(self.points_mm) != 2:
            raise ValueError(
                f"length_mm needs a two-point primitive, but {self.role!r} has "
                f"{len(self.points_mm)} points"
            )
        (x0_mm, y0_mm), (x1_mm, y1_mm) = self.points_mm
        return ((x1_mm - x0_mm) ** 2 + (y1_mm - y0_mm) ** 2) ** 0.5


def _to_page_pt(point_mm: tuple[float, float], sheet_height_mm: float) -> tuple[float, float]:
    """The only top-left-to-bottom-left flip in the project."""
    x_mm, y_mm = point_mm
    return mm_to_pt(x_mm), mm_to_pt(sheet_height_mm - y_mm)


def _require_printable(spec: Spec) -> None:
    if not spec.mat.enabled:
        raise MatGuideError(
            "mat.enabled is false, so this spec describes no mat; set mat.enabled to true "
            "before asking for a cutting guide"
        )
    if spec.target == "screen":
        raise MatGuideError(
            "target is 'screen', but a mat guide describes board cut for a physical print; "
            "set target to 'print'"
        )


def _source_names(layout: Layout, sources: Sequence[str] | None) -> list[str]:
    if sources is not None:
        return [Path(name).name for name in sources]
    return [Path(slot.source).name for slot in layout.slots if slot.source]


def _openings_mm(layout: Layout, mat: MatSpec) -> list[Rect]:
    """The shared opening geometry, with its errors re-raised in this module's voice."""
    try:
        return openings_mm(
            layout,
            overlap_mm=float(mat.window_overlap_mm),
            reveal_mm=float(mat.reveal_mm),
            mirrored=mat.mirror,
        )
    except MatOpeningError as exc:
        raise MatGuideError(str(exc)) from exc


def _outer_openings_mm(layout: Layout, mat: MatSpec) -> list[Rect]:
    """The top board's openings of a double mat, errors in this module's voice."""
    try:
        return outer_openings_mm(
            layout,
            overlap_mm=float(mat.window_overlap_mm),
            reveal_mm=float(mat.reveal_mm),
            inner_reveal_mm=float(mat.inner_reveal_mm),
            mirrored=mat.mirror,
        )
    except MatOpeningError as exc:
        raise MatGuideError(str(exc)) from exc


def _window_primitives(
    index: int, opening_mm: Rect, overcut_mm: float, reveal_mm: float
) -> list[MatPrimitive]:
    number = index + 1
    left_mm, top_mm = opening_mm.x_mm, opening_mm.y_mm
    right_mm, bottom_mm = opening_mm.right_mm, opening_mm.bottom_mm
    centre = opening_mm.center

    out: list[MatPrimitive] = [
        MatPrimitive(
            kind="rect",
            points_mm=((left_mm, top_mm), (right_mm, bottom_mm)),
            weight="opening",
            role=f"window_{number}",
        )
    ]

    # Each cut line runs past both corners by the overcut. A 45-degree bevel
    # blade travels roughly the board thickness before it clears, and without
    # the overrun the corners tear on release.
    out.extend(
        [
            MatPrimitive(
                kind="line",
                points_mm=((left_mm - overcut_mm, top_mm), (right_mm + overcut_mm, top_mm)),
                weight="cut",
                role=f"cut_top_{number}",
            ),
            MatPrimitive(
                kind="line",
                points_mm=(
                    (right_mm, top_mm - overcut_mm),
                    (right_mm, bottom_mm + overcut_mm),
                ),
                weight="cut",
                role=f"cut_right_{number}",
            ),
            MatPrimitive(
                kind="line",
                points_mm=(
                    (left_mm - overcut_mm, bottom_mm),
                    (right_mm + overcut_mm, bottom_mm),
                ),
                weight="cut",
                role=f"cut_bottom_{number}",
            ),
            MatPrimitive(
                kind="line",
                points_mm=((left_mm, top_mm - overcut_mm), (left_mm, bottom_mm + overcut_mm)),
                weight="cut",
                role=f"cut_left_{number}",
            ),
        ]
    )

    arm_mm = min(_CROSSHAIR_ARM_MM, opening_mm.width_mm / 4, opening_mm.height_mm / 4)
    out.append(
        MatPrimitive(
            kind="crosshair",
            points_mm=(
                (centre.x_mm, centre.y_mm),
                (centre.x_mm - arm_mm, centre.y_mm),
                (centre.x_mm + arm_mm, centre.y_mm),
                (centre.x_mm, centre.y_mm - arm_mm),
                (centre.x_mm, centre.y_mm + arm_mm),
            ),
            weight="annotation",
            role=f"crosshair_{number}",
        )
    )

    # Callouts sit in the overlap band just outside the opening, never inside
    # it, where they would print across the falling waste and confuse the cut.
    out.extend(
        [
            MatPrimitive(
                kind="text",
                points_mm=((centre.x_mm, top_mm - _DIM_OFFSET_MM),),
                text=f"{opening_mm.width_mm:.2f} mm",
                role=f"dim_width_{number}",
                size_pt=6.5,
                align="center",
            ),
            MatPrimitive(
                kind="text",
                points_mm=((left_mm - _DIM_OFFSET_MM, centre.y_mm),),
                text=f"{opening_mm.height_mm:.2f} mm",
                role=f"dim_height_{number}",
                size_pt=6.5,
                align="right",
            ),
        ]
    )

    # A reveal means the print sits behind a hole larger than the image, and a
    # hand cutter placing the print needs to see where the image lands. The
    # extent is drawn inside the opening — on the falling waste — which is
    # exactly right: it exists for placement, not for cutting.
    if reveal_mm > 0:
        image_mm = opening_mm.inset(reveal_mm)
        out.append(
            MatPrimitive(
                kind="rect",
                points_mm=(
                    (image_mm.x_mm, image_mm.y_mm),
                    (image_mm.right_mm, image_mm.bottom_mm),
                ),
                weight="annotation",
                role=f"image_extent_{number}",
            )
        )
        out.append(
            MatPrimitive(
                kind="text",
                points_mm=((centre.x_mm, image_mm.y_mm + _IMAGE_LABEL_DROP_MM),),
                text=f"image {image_mm.width_mm:.2f} × {image_mm.height_mm:.2f} mm",
                role=f"image_extent_label_{number}",
                size_pt=6.5,
                align="center",
            )
        )
    return out


def _header_primitives(
    layout: Layout,
    spec: Spec,
    openings_mm: Sequence[Rect],
    names: Sequence[str],
    title: str | None,
    top_clear_mm: float,
) -> list[MatPrimitive]:
    mat = spec.mat
    banner = _MIRRORED_BANNER if mat.mirror else _UNMIRRORED_BANNER

    if _BANNER_BASELINE_MM + _TEXT_SLACK_MM > top_clear_mm:
        raise MatGuideError(
            f"the top margin leaves only {top_clear_mm:.2f}mm above the first window, but the "
            f"orientation banner needs {_BANNER_BASELINE_MM + _TEXT_SLACK_MM:.2f}mm; a guide "
            f"that does not say which face it is must not be printed, so raise "
            f"margins.top_mm"
        )

    out = [
        MatPrimitive(
            kind="text",
            points_mm=((_HEADER_LEFT_MM, _BANNER_BASELINE_MM),),
            text=banner,
            role="header_orientation",
            size_pt=14.0,
            bold=True,
        )
    ]

    lines: list[tuple[str, float, bool]] = [
        (title or f"Sodachi mat guide · {layout.name}", 8.0, True)
    ]
    if names:
        lines.append(("Sources: " + ", ".join(names), 7.0, False))
    lines.append(
        (
            f"Board {layout.sheet.width_mm:.2f} × {layout.sheet.height_mm:.2f} mm · "
            f"overlap {mat.window_overlap_mm:.2f} mm · overcut {mat.resolved_overcut_mm:.2f} mm "
            f"· board {mat.board_thickness_mm:.2f} mm",
            7.0,
            False,
        )
    )
    lines.append(("Window coordinates are top-left origin on this printed face.", 7.0, False))
    for index, opening_mm in enumerate(openings_mm):
        lines.append(
            (
                f"Window {index + 1}: {opening_mm.width_mm:.2f} × {opening_mm.height_mm:.2f} mm "
                f"at ({opening_mm.x_mm:.2f}, {opening_mm.y_mm:.2f}) mm",
                7.0,
                False,
            )
        )

    # Lines are ordered by how much a cutter would miss them, so running out of
    # top margin drops the redundant tail rather than the orientation.
    baseline_mm = _HEADER_FIRST_BASELINE_MM
    for line_index, (text, size_pt, bold) in enumerate(lines):
        if baseline_mm + _TEXT_SLACK_MM > top_clear_mm:
            break
        out.append(
            MatPrimitive(
                kind="text",
                points_mm=((_HEADER_LEFT_MM, baseline_mm),),
                text=text,
                role=f"header_line_{line_index}",
                size_pt=size_pt,
                bold=bold,
            )
        )
        baseline_mm += _HEADER_STEP_MM
    return out


def _calibration_primitives(sheet_width_mm: float, sheet_height_mm: float) -> list[MatPrimitive]:
    if sheet_width_mm < 2 * _CAL_LEFT_MM + CALIBRATION_LENGTH_MM:
        raise MatGuideError(
            f"mat.calibration_bar is true but the board is only {sheet_width_mm:.2f}mm wide, "
            f"which cannot hold a {CALIBRATION_LENGTH_MM:g}mm bar with margins; widen the "
            f"sheet or set mat.calibration_bar to false"
        )

    bar_y_mm = sheet_height_mm - _CAL_BAR_FROM_BOTTOM_MM
    x0_mm = _CAL_LEFT_MM
    x1_mm = x0_mm + CALIBRATION_LENGTH_MM

    out = [
        MatPrimitive(
            kind="line",
            points_mm=((x0_mm, bar_y_mm), (x1_mm, bar_y_mm)),
            weight="cut",
            role="calibration_bar",
        )
    ]
    tick_count = int(round(CALIBRATION_LENGTH_MM / CALIBRATION_TICK_STEP_MM))
    for tick in range(tick_count + 1):
        tick_x_mm = x0_mm + tick * CALIBRATION_TICK_STEP_MM
        out.append(
            MatPrimitive(
                kind="tick",
                points_mm=((tick_x_mm, bar_y_mm), (tick_x_mm, bar_y_mm - _CAL_TICK_MM)),
                weight="cut",
                role=f"calibration_tick_{tick}",
            )
        )
    out.append(
        MatPrimitive(
            kind="text",
            points_mm=((x0_mm, sheet_height_mm - _CAL_LABEL_FROM_BOTTOM_MM),),
            text=CALIBRATION_TEXT,
            role="calibration_label",
            size_pt=8.0,
            bold=True,
        )
    )
    return out


def _page_primitives(
    layout: Layout,
    spec: Spec,
    windows_mm: list[Rect],
    names: Sequence[str],
    title: str | None,
    *,
    reveal_mm: float,
    board_label: str | None = None,
    outer_windows_mm: list[Rect] | None = None,
) -> tuple[MatPrimitive, ...]:
    """One board's page as millimetre records.

    ``windows_mm`` are the openings this page's board is cut with. On a double
    mat's bottom page ``outer_windows_mm`` carries the top board's openings,
    drawn as light annotation so the cutter sees where the band will fall, and
    ``board_label`` names the board — single-mat pages carry neither, which is
    what keeps them identical to the pages this module has always drawn.
    """
    mat = spec.mat
    sheet_width_mm = layout.sheet.width_mm
    sheet_height_mm = layout.sheet.height_mm
    overcut_mm = float(mat.resolved_overcut_mm)

    top_clear_mm = min((o.y_mm for o in windows_mm), default=sheet_height_mm)
    bottom_clear_mm = sheet_height_mm - max((o.bottom_mm for o in windows_mm), default=0.0)
    left_clear_mm = min((o.x_mm for o in windows_mm), default=sheet_width_mm)

    out: list[MatPrimitive] = [
        MatPrimitive(
            kind="rect",
            points_mm=((0.0, 0.0), (sheet_width_mm, sheet_height_mm)),
            weight="annotation",
            role="board_outline",
        )
    ]

    for index, opening_mm in enumerate(windows_mm):
        out.extend(_window_primitives(index, opening_mm, overcut_mm, reveal_mm))

    if outer_windows_mm is not None:
        # The top board's openings on the bottom board's page: annotation
        # weight, no cut lines, so a blade has nothing to follow but the
        # cutter sees the band of this board the top window will expose.
        for index, outer_mm in enumerate(outer_windows_mm):
            out.append(
                MatPrimitive(
                    kind="rect",
                    points_mm=(
                        (outer_mm.x_mm, outer_mm.y_mm),
                        (outer_mm.right_mm, outer_mm.bottom_mm),
                    ),
                    weight="annotation",
                    role=f"outer_window_{index + 1}",
                )
            )

    out.extend(_header_primitives(layout, spec, windows_mm, names, title, top_clear_mm))

    if board_label is not None:
        out.append(
            MatPrimitive(
                kind="text",
                points_mm=((sheet_width_mm - _HEADER_LEFT_MM, _BANNER_BASELINE_MM),),
                text=board_label,
                role="board_label",
                size_pt=14.0,
                align="right",
                bold=True,
            )
        )

    if mat.calibration_bar:
        needed_mm = _CAL_BAR_FROM_BOTTOM_MM + _CAL_TICK_MM + _TEXT_SLACK_MM
        if bottom_clear_mm < needed_mm:
            raise MatGuideError(
                f"mat.calibration_bar is true but the bottom margin leaves only "
                f"{bottom_clear_mm:.2f}mm below the last window, and the bar needs "
                f"{needed_mm:.2f}mm; raise the bottom margin or set mat.calibration_bar "
                f"to false"
            )
        out.extend(_calibration_primitives(sheet_width_mm, sheet_height_mm))

    if bottom_clear_mm >= _BOARD_CALLOUT_FROM_BOTTOM_MM + _TEXT_SLACK_MM:
        out.append(
            MatPrimitive(
                kind="text",
                points_mm=(
                    (sheet_width_mm / 2, sheet_height_mm - _BOARD_CALLOUT_FROM_BOTTOM_MM),
                ),
                text=f"board width {sheet_width_mm:.2f} mm",
                role="board_width",
                size_pt=6.5,
                align="center",
            )
        )
    if left_clear_mm >= _BOARD_HEIGHT_CALLOUT_CLEARANCE_MM:
        out.append(
            MatPrimitive(
                kind="text",
                points_mm=((_BOARD_HEIGHT_CALLOUT_LEFT_MM, sheet_height_mm / 2),),
                text=f"board height {sheet_height_mm:.2f} mm",
                role="board_height",
                size_pt=6.5,
                align="left",
            )
        )

    return tuple(out)


def build_primitive_pages(
    layout: Layout,
    spec: Spec,
    *,
    sources: Sequence[str] | None = None,
    title: str | None = None,
) -> tuple[tuple[MatPrimitive, ...], ...]:
    """Every page of the guide as millimetre records, one tuple per board.

    A single mat is one unlabelled page, exactly the page this module has
    always produced. A double mat is two: the bottom board first, labelled
    BOTTOM BOARD and annotated with the top board's openings, then the top
    board, labelled TOP BOARD, cut with the openings grown by the inner
    reveal. The top page passes a reveal of zero to the window primitives:
    the print is placed against the bottom board, so the image-extent
    placement aid belongs on the bottom page alone.
    """
    _require_printable(spec)

    mat = spec.mat
    names = _source_names(layout, sources)
    windows_mm = _openings_mm(layout, mat)
    reveal_mm = float(mat.reveal_mm)

    if not mat.double:
        return (
            _page_primitives(layout, spec, windows_mm, names, title, reveal_mm=reveal_mm),
        )

    outer_mm = _outer_openings_mm(layout, mat)
    bottom_page = _page_primitives(
        layout,
        spec,
        windows_mm,
        names,
        title,
        reveal_mm=reveal_mm,
        board_label="BOTTOM BOARD",
        outer_windows_mm=outer_mm,
    )
    top_page = _page_primitives(
        layout,
        spec,
        outer_mm,
        names,
        title,
        reveal_mm=0.0,
        board_label="TOP BOARD",
    )
    return (bottom_page, top_page)


def build_primitives(
    layout: Layout,
    spec: Spec,
    *,
    sources: Sequence[str] | None = None,
    title: str | None = None,
) -> tuple[MatPrimitive, ...]:
    """The first page of the guide — the only page, for a single mat.

    Kept as the module's original entry point so single-mat callers and their
    assertions are untouched; a double mat's further pages come from
    :func:`build_primitive_pages`.
    """
    return build_primitive_pages(layout, spec, sources=sources, title=title)[0]


def _emit(pdf: canvas.Canvas, primitive: MatPrimitive, sheet_height_mm: float) -> None:
    if primitive.kind == "text":
        pdf.setFont(
            "Helvetica-Bold" if primitive.bold else "Helvetica", primitive.size_pt
        )
        grey = _TEXT_GREY_BOLD if primitive.bold else _TEXT_GREY
        pdf.setFillColorRGB(grey, grey, grey)
        x_pt, y_pt = _to_page_pt(primitive.points_mm[0], sheet_height_mm)
        text = primitive.text or ""
        if primitive.align == "center":
            pdf.drawCentredString(x_pt, y_pt, text)
        elif primitive.align == "right":
            pdf.drawRightString(x_pt, y_pt, text)
        else:
            pdf.drawString(x_pt, y_pt, text)
        return

    width_pt, grey = _WEIGHTS[primitive.weight]
    pdf.setLineWidth(width_pt)
    pdf.setStrokeColorRGB(grey, grey, grey)

    if primitive.kind in ("line", "tick"):
        (x0_pt, y0_pt) = _to_page_pt(primitive.points_mm[0], sheet_height_mm)
        (x1_pt, y1_pt) = _to_page_pt(primitive.points_mm[1], sheet_height_mm)
        pdf.line(x0_pt, y0_pt, x1_pt, y1_pt)
    elif primitive.kind == "rect":
        (x0_pt, y0_pt) = _to_page_pt(primitive.points_mm[0], sheet_height_mm)
        (x1_pt, y1_pt) = _to_page_pt(primitive.points_mm[1], sheet_height_mm)
        pdf.rect(x0_pt, y1_pt, x1_pt - x0_pt, y0_pt - y1_pt, stroke=1, fill=0)
    elif primitive.kind == "crosshair":
        for start_mm, end_mm in (primitive.points_mm[1:3], primitive.points_mm[3:5]):
            (x0_pt, y0_pt) = _to_page_pt(start_mm, sheet_height_mm)
            (x1_pt, y1_pt) = _to_page_pt(end_mm, sheet_height_mm)
            pdf.line(x0_pt, y0_pt, x1_pt, y1_pt)
    else:  # pragma: no cover - the Literal makes this unreachable
        raise MatGuideError(f"unknown mat primitive kind {primitive.kind!r}")


def render_mat_guide(
    layout: Layout,
    spec: Spec,
    out_path: str | Path,
    *,
    sources: Sequence[str] | None = None,
    title: str | None = None,
) -> Path:
    """Write the guide as a vector PDF whose pages are exactly the board.

    One page per board: a single mat is one page, a double mat two, bottom
    board first.
    """
    pages = build_primitive_pages(layout, spec, sources=sources, title=title)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sheet_height_mm = layout.sheet.height_mm
    page_pt = (mm_to_pt(layout.sheet.width_mm), mm_to_pt(sheet_height_mm))

    pdf = canvas.Canvas(str(out_path), pagesize=page_pt)
    pdf.setTitle(title or f"Sodachi mat guide - {layout.name}")
    pdf.setAuthor("Sodachi")
    pdf.setSubject(
        f"{layout.sheet.width_mm:.2f} x {layout.sheet.height_mm:.2f} mm board, "
        f"{len(layout.slots)} window(s), print at 100 percent"
    )
    # Butt caps, so an overcut line is exactly as long as the record says.
    pdf.setLineCap(0)
    pdf.setLineJoin(0)

    for primitives in pages:
        for primitive in primitives:
            _emit(pdf, primitive, sheet_height_mm)
        pdf.showPage()

    pdf.save()
    return out_path


__all__ = [
    "CALIBRATION_LENGTH_MM",
    "CALIBRATION_TICK_STEP_MM",
    "CALIBRATION_TEXT",
    "MatGuideError",
    "MatPrimitive",
    "build_primitive_pages",
    "build_primitives",
    "render_mat_guide",
]
