"""The exploded stack: paper, print, mat and top mat as boards in space.

The flat preview answers "where does everything land"; this pane answers "what
is physically on top of what" — the question a framer asks before cutting a
second board. It draws the same solved ``Layout`` and the same openings from
:mod:`sodachi.core.mat` as every other consumer, lifted apart vertically so the
sandwich reads: BACKING, then the PRINT, then the MAT, then the TOP MAT when
the spec doubles it.

The projection is 2:1 dimetric rather than 30-degree isometric, on purpose:
``(x_mm, y_mm, lift) -> ((x - y) * c, (x + y) * c / 2 - lift)`` sends every
receding edge exactly two pixels across per one down, so with antialiasing off
the edges land on the pixel grid as clean staircases instead of the grey smear
a 30-degree slope produces. Antialiasing comes back on only for the sheared
image draw, where the content is continuous-tone anyway.

Thumbnails do not land on the print board photographically. This pane is a
diagram drawn in the skin's own inks, and a full-colour photograph inside it
reads as a hole into a different program; :func:`riso_duotone` reprints each
thumbnail as one ink on the palette's paper first. The flat preview keeps the
true pixels on purpose — that view is for judging the actual print, this one
for seeing the sandwich.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
    qRgb,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from sodachi.core.geometry import Rect
from sodachi.core.layout import Layout
from sodachi.core.mat import MatOpeningError, openings_mm, outer_openings_mm
# The stack's empty slots must be pixel-for-pixel the flat preview's, or a
# switch between the two views would read as a content change; importing the
# helpers keeps one definition of the wash and of colour mixing.
from sodachi.gui.preview import _mix, _placeholder_gradient
from sodachi.gui.theme import PALETTE, draw_dotted_line, draw_micro_label, mono_font

MARGIN_PX = 26.0
"""Room around the drawing, matching the flat pane's breathing space."""

LABEL_GUTTER_PX = 76.0
"""Reserved on the left for the layer callouts and their leaders."""

CAPTION_H_PX = 16.0
"""Room under the stack for the sheet-size caption."""

DEFAULT_EXPLODE = 1.0
"""Far enough apart that every band reads, close enough to stay one object."""

BOARD_THICKNESS_MM = 1.4
"""Drawn thickness of each board; the spec's mat.board_thickness_mm default.
A constant rather than a fed value because at this scale every board clamps
into the same 2-6px band anyway, and the pane has no opinion about paper
calipers."""

THICKNESS_MIN_PX = 2.0
THICKNESS_MAX_PX = 6.0

LIFT_FACTOR = 0.30
"""Explode lift per layer at factor 1, as a share of the sheet's short edge."""

FRONT_SHADE = 0.22
RIGHT_SHADE = 0.40
"""How far each visible side face mixes toward the palette ink. One light
source, fixed: the right side is always the darker."""

MIN_LEADER_GAP_PX = 4.0
"""Below this open gap between boards, the corner leaders are noise."""

LABEL_ROW_PX = 13.0
"""Minimum vertical spacing between layer callouts when the stack is closed."""

RISO_LEVELS = 6
"""Tone steps in the duotone. Six is where a face still reads as a face but
every band is an event: at eight the output was a photograph accepting a
tint, and the point is a printed separation, not a photo."""

RISO_INK_ACCENT = 0.45
"""How far the shadow ink leans from ``ink_strong`` toward the accent. A riso
ink is never a neutral black; a near-half blend commits the shadows to the
drum's colour rather than hinting at it, which is what makes the print read
as one ink on paper instead of a toned grey."""

RISO_MIDTONE_ACCENT = 0.28
"""Peak accent pull in the midtones, fading to nothing at either end so the
ramp's endpoints stay exactly the palette's own paper and ink. Deep enough
mid-ramp that the flood of the print is unmistakably the ink's own colour,
not a grey passing through it."""

RISO_GRAIN = 20
"""Dither amplitude in grey levels. Kept under half a tone step (256 /
RISO_LEVELS / 2 ≈ 21), so grain can only soften a band edge, never move a
solid black or white off its own band — but pushed close against that bound,
so nearly half of every band carries the screen's texture. A riso edge is a
dissolve of dots, and at 12 the dissolve was too narrow to see."""

_BAYER8 = (
    (0, 32, 8, 40, 2, 34, 10, 42),
    (48, 16, 56, 24, 50, 18, 58, 26),
    (12, 44, 4, 36, 14, 46, 6, 38),
    (60, 28, 52, 20, 62, 30, 54, 22),
    (3, 35, 11, 43, 1, 33, 9, 41),
    (51, 19, 59, 27, 49, 17, 57, 25),
    (15, 47, 7, 39, 13, 45, 5, 37),
    (63, 31, 55, 23, 61, 29, 53, 21),
)
"""The standard 8x8 Bayer thresholds, the ordered screen of cheap print."""

_grain_tile: QImage | None = None


def _grain() -> QImage:
    """The Bayer matrix as a tileable brush, built once.

    64 pixels of Python loop at first use, then never again; the tile carries
    no palette colour, so it survives every switch.
    """
    global _grain_tile
    if _grain_tile is None:
        tile = QImage(8, 8, QImage.Format.Format_RGB32)
        for y, row in enumerate(_BAYER8):
            for x, threshold in enumerate(row):
                g = threshold * RISO_GRAIN // 63
                tile.setPixel(x, y, qRgb(g, g, g))
        _grain_tile = tile
    return _grain_tile


def riso_duotone(image: QImage) -> QImage:
    """``image`` reprinted as a risograph: one ink, the palette's paper.

    A risograph drum holds a single saturated ink and pushes it through a
    screen onto uncoated stock, so a riso print is not a photograph — it is a
    luminance map quantised into a handful of tones of one colour. That is
    exactly the treatment that makes a thumbnail belong on the stack's print
    board: highlights become ``PALETTE.paper``, shadows a deep ink leaned
    toward the accent, midtones tinted a step further toward it, and the whole
    ramp posterised to ``RISO_LEVELS`` steps with an ordered-dither grain at
    the band edges.

    Everything per-pixel happens inside Qt: the image is collapsed to
    Format_Grayscale8, the grain is one composited fill, and the recolouring
    is a 256-entry colour table on a Format_Indexed8 copy — the grey bytes
    become the indices, so the duotone costs format conversions and a table,
    never a Python loop over pixels.

    Reads the palette at call time; the caller owns any caching and must
    treat a palette switch as staleness (see ``StackPane._riso_thumbnail``).
    """
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)

    # The grain rides on the luminance before quantisation, so it dithers the
    # band boundaries rather than speckling over the finished print — and the
    # output still holds no colour outside the ramp. RGB32 as the working
    # surface because it is the format every composition mode is safe on.
    canvas = gray.convertToFormat(QImage.Format.Format_RGB32)
    painter = QPainter(canvas)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
    painter.fillRect(canvas.rect(), QBrush(_grain()))
    painter.end()
    gray = canvas.convertToFormat(QImage.Format.Format_Grayscale8)

    paper = PALETTE.paper
    accent = PALETTE.accent
    ink = _mix(PALETTE.ink_strong, accent, RISO_INK_ACCENT)
    table = []
    for g in range(256):
        level = min(g * RISO_LEVELS // 256, RISO_LEVELS - 1)
        t = level / (RISO_LEVELS - 1)
        tone = _mix(ink, paper, t)
        # The accent pull peaks mid-ramp and vanishes at both ends: 4t(1-t)
        # is 0 at 0 and 1, and 1 at a half.
        tone = _mix(tone, accent, RISO_MIDTONE_ACCENT * 4.0 * t * (1.0 - t))
        table.append(qRgb(tone.red(), tone.green(), tone.blue()))
    out = gray.convertToFormat(QImage.Format.Format_Indexed8)
    out.setColorTable(table)
    return out


def _board_faces(
    double: bool, colour: QColor, inner_colour: QColor
) -> tuple[QColor, QColor | None]:
    """(MAT board face, TOP MAT face or None) for the current mat.

    The mapping is stated once, here: ``mat.color`` is the board you look at
    first — the only board when single, the TOP board when double — and
    ``mat.inner_color`` is the BOTTOM board of a double mat, the band revealed
    inside the top opening. So the MAT layer (the lower board) wears the inner
    colour exactly when a top board exists above it.
    """
    if double:
        return inner_colour, colour
    return colour, None


@dataclass(frozen=True, slots=True)
class _Projection:
    """Millimetres on the sheet plane, plus a pixel lift, to widget pixels."""

    c: float
    x0: float
    y0: float

    def point(self, x_mm: float, y_mm: float, lift_px: float = 0.0) -> QPointF:
        return QPointF(
            self.x0 + (x_mm - y_mm) * self.c,
            self.y0 + (x_mm + y_mm) * self.c / 2.0 - lift_px,
        )

    def shear(self, lift_px: float) -> QTransform:
        """The same mapping as a QTransform, for painting onto a top face."""
        return QTransform(
            self.c, self.c / 2.0, -self.c, self.c / 2.0, self.x0, self.y0 - lift_px
        )


@dataclass(frozen=True, slots=True)
class _Board:
    """One layer of the sandwich: a label, a face colour, and its holes."""

    label: str
    face: QColor
    openings: tuple[Rect, ...] = ()
    slots: bool = False
    """True only for the PRINT layer, whose face carries the images."""


class StackPane(QWidget):
    """The solved sheet as an exploded pile of boards, in 2:1 dimetric."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout: Layout | None = None
        self._warning: str | None = None
        self._thumbnails: dict[int, QImage] = {}
        # Converted duotones by source QImage.cacheKey(). The ramp signature
        # records which paper/ink/accent the cache was built under, because a
        # palette switch repaints without a new set_thumbnails call — the only
        # place staleness can be caught is at paint time.
        self._riso_cache: dict[int, QImage] = {}
        self._riso_ramp: tuple[int, int, int] | None = None
        self._units = "mm"
        self._mat_enabled = False
        self._mat_overlap_mm = 3.0
        self._mat_reveal_mm = 0.0
        self._mat_double = False
        self._mat_inner_reveal_mm = 0.0
        self._mat_color = "#F6F1EA"
        self._mat_inner_color = "#F6F1EA"
        self._explode = DEFAULT_EXPLODE
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(QSize(200, 220))

    # ------------------------------------------------------------------- api

    def units(self) -> str:
        return self._units

    def set_units(self, units: str) -> None:
        """The unit the caption speaks: "mm", "in" or "px", as the flat pane."""
        if units not in ("mm", "in", "px"):
            raise ValueError(f"units is {units!r}; the stack speaks mm, in or px")
        if units != self._units:
            self._units = units
            self.update()

    def layoutResult(self) -> Layout | None:  # noqa: N802 - Qt naming
        return self._layout

    def set_layout_result(self, layout: Layout | None, warning: str | None = None) -> None:
        self._layout = layout
        self._warning = warning
        self.update()

    def set_thumbnails(self, images: dict[int, QImage]) -> None:
        self._thumbnails = dict(images)
        # Keep the duotones whose sources survived the change; a queue edit
        # that replaces one slot should not cost re-converting the others.
        keep = {image.cacheKey() for image in self._thumbnails.values()}
        self._riso_cache = {k: v for k, v in self._riso_cache.items() if k in keep}
        self.update()

    def set_mat(
        self,
        enabled: bool,
        overlap_mm: float,
        reveal_mm: float,
        double: bool = False,
        inner_reveal_mm: float = 0.0,
        color: str | None = None,
        inner_color: str | None = None,
    ) -> None:
        """The mat boards to stack, cut with the same openings the cutter gets.

        ``color`` and ``inner_color`` are the spec's hex fields; None keeps the
        spec's warm board-white default, so a caller that only knows the flat
        pane's three-argument form still draws a truthful board.
        """
        state = (
            bool(enabled),
            float(overlap_mm),
            float(reveal_mm),
            bool(double),
            float(inner_reveal_mm),
            str(color) if color else "#F6F1EA",
            str(inner_color) if inner_color else "#F6F1EA",
        )
        if state != (
            self._mat_enabled,
            self._mat_overlap_mm,
            self._mat_reveal_mm,
            self._mat_double,
            self._mat_inner_reveal_mm,
            self._mat_color,
            self._mat_inner_color,
        ):
            (
                self._mat_enabled,
                self._mat_overlap_mm,
                self._mat_reveal_mm,
                self._mat_double,
                self._mat_inner_reveal_mm,
                self._mat_color,
                self._mat_inner_color,
            ) = state
            self.update()

    def explode(self) -> float:
        return self._explode

    def set_explode(self, factor: float) -> None:
        """How far apart the layers lift, 0 (resting) to 1 (fully apart)."""
        factor = min(max(float(factor), 0.0), 1.0)
        if abs(factor - self._explode) > 1e-9:
            self._explode = factor
            self.update()

    def _fmt(self, mm: float) -> str:
        if self._units == "in":
            return f"{mm / 25.4:.2f}"
        if self._units == "px":
            dpi = self._layout.sheet.dpi if self._layout else 300.0
            return f"{round(mm * dpi / 25.4)}"
        return f"{mm:.1f}"

    # ---------------------------------------------------------------- layers

    def _boards(self, layout: Layout) -> list[_Board]:
        """Bottom to top. The mat layers appear only when their holes compute:
        a combination the margins cannot hold mid-edit drops the boards rather
        than raising mid-paint, exactly as the flat pane drops its wash."""
        backing = _Board(
            "backing",
            # Toward paper so it reads as material behind the print rather
            # than as another patch of the palette's chrome.
            _mix(PALETTE.fill, PALETTE.paper, 0.45),
        )
        print_board = _Board(
            "print", QColor(layout.sheet.background_hex), slots=True
        )
        out = [backing, print_board]
        if not self._mat_enabled:
            return out

        try:
            inner = openings_mm(
                layout,
                overlap_mm=self._mat_overlap_mm,
                reveal_mm=self._mat_reveal_mm,
            )
            outer: list[Rect] | None = None
            if self._mat_double and self._mat_inner_reveal_mm > 0:
                outer = outer_openings_mm(
                    layout,
                    overlap_mm=self._mat_overlap_mm,
                    reveal_mm=self._mat_reveal_mm,
                    inner_reveal_mm=self._mat_inner_reveal_mm,
                )
        except MatOpeningError:
            return out

        double = outer is not None
        mat_face, top_face = _board_faces(
            double, QColor(self._mat_color), QColor(self._mat_inner_color)
        )
        out.append(_Board("mat", mat_face, tuple(inner)))
        if double and top_face is not None:
            out.append(_Board("top mat", top_face, tuple(outer)))
        return out

    # -------------------------------------------------------------- geometry

    def _geometry(
        self, layout: Layout, count: int
    ) -> tuple[_Projection, float, float, list[float]]:
        """(projection, thickness_px, step_px, top-face elevations, bottom-up).

        The scale is solved so the whole pile — footprint plus the current
        explode lift — fits the pane; the lift and the board thickness both
        ride on that scale, thickness clamped so a board still reads as a slab
        at any size.
        """
        area = QRectF(self.rect()).adjusted(
            MARGIN_PX, MARGIN_PX, -MARGIN_PX, -MARGIN_PX
        )
        w_mm = layout.sheet.width_mm
        h_mm = layout.sheet.height_mm
        short_mm = min(w_mm, h_mm)

        # In "c units": one unit of c is one millimetre's worth of pixels.
        units_w = w_mm + h_mm
        units_h = (w_mm + h_mm) / 2.0 + (count - 1) * self._explode * LIFT_FACTOR * short_mm
        avail_w = max(area.width() - LABEL_GUTTER_PX, 40.0)
        # Thickness does not scale below its clamp, so it is reserved at the
        # clamp's ceiling rather than folded into the solve.
        avail_h = max(area.height() - CAPTION_H_PX - count * THICKNESS_MAX_PX, 40.0)
        c = max(min(avail_w / units_w, avail_h / units_h), 0.01)

        thickness_px = min(max(BOARD_THICKNESS_MM * c, THICKNESS_MIN_PX), THICKNESS_MAX_PX)
        lift_px = LIFT_FACTOR * short_mm * c
        step_px = thickness_px + self._explode * lift_px
        elevations = [thickness_px + i * step_px for i in range(count)]
        top_px = elevations[-1] if elevations else 0.0

        draw_w = units_w * c
        total_h = top_px + (w_mm + h_mm) * c / 2.0
        x_left = area.left() + LABEL_GUTTER_PX + (avail_w - draw_w) / 2.0
        # Half-pixel anchors keep the 1px outlines on single pixel rows.
        x0 = math.floor(x_left + h_mm * c) + 0.5
        y0 = (
            math.floor(
                area.top() + top_px + (area.height() - CAPTION_H_PX - total_h) / 2.0
            )
            + 0.5
        )
        return _Projection(c=c, x0=x0, y0=y0), thickness_px, step_px, elevations

    # ----------------------------------------------------------------- paint

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        layout = self._layout
        if layout is None:
            self._paint_empty(painter)
            painter.end()
            return

        boards = self._boards(layout)
        proj, thickness_px, step_px, elevations = self._geometry(layout, len(boards))

        for i, board in enumerate(boards):
            self._paint_board(painter, proj, layout, board, elevations[i], thickness_px)
            if board.slots:
                self._paint_slots(painter, proj, layout, elevations[i])
            # Registration leaders rise from this board's corners to the next
            # board's top face; the next board is painted over them, so only
            # the open gap shows. Skipped when the pile is resting.
            if i + 1 < len(boards) and step_px - thickness_px > MIN_LEADER_GAP_PX:
                self._paint_leaders(
                    painter, proj, layout, elevations[i], elevations[i + 1]
                )

        self._paint_labels(painter, proj, layout, boards, elevations)
        self._paint_caption(painter, layout)
        if self._warning:
            self._paint_warning(painter)
        painter.end()

    def _paint_empty(self, painter: QPainter) -> None:
        painter.setFont(mono_font(8, caps=True))
        painter.setPen(PALETTE.ink_soft)
        painter.drawText(
            QRectF(self.rect()),
            int(Qt.AlignmentFlag.AlignCenter),
            "no layout\nthe current settings do not solve",
        )

    def _corners_mm(self, layout: Layout) -> tuple[tuple[float, float], ...]:
        """Sheet corners in draw order: top, right, bottom, left on screen."""
        w_mm = layout.sheet.width_mm
        h_mm = layout.sheet.height_mm
        return ((0.0, 0.0), (w_mm, 0.0), (w_mm, h_mm), (0.0, h_mm))

    def _paint_board(
        self,
        painter: QPainter,
        proj: _Projection,
        layout: Layout,
        board: _Board,
        elevation_px: float,
        thickness_px: float,
    ) -> None:
        """One slab: two visible side faces, then the top face with its holes.

        The 2:1 edges land on the grid by construction, so antialiasing stays
        off for everything here; turning it on would trade a clean staircase
        for a grey one.
        """
        corners = self._corners_mm(layout)
        at_top = [proj.point(x, y, elevation_px) for x, y in corners]
        at_base = [proj.point(x, y, elevation_px - thickness_px) for x, y in corners]
        _p_top, p_right, p_bottom, p_left = at_top
        _b_top, b_right, b_bottom, b_left = at_base

        ink = PALETTE.ink
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QPen(ink, 1.0))

        # Front face (the y = H edge), then the right face (x = W), darker:
        # one light source, from the upper left, fixed.
        painter.setBrush(_mix(board.face, ink, FRONT_SHADE))
        painter.drawPolygon(QPolygonF([p_left, p_bottom, b_bottom, b_left]))
        painter.setBrush(_mix(board.face, ink, RIGHT_SHADE))
        painter.drawPolygon(QPolygonF([p_bottom, p_right, b_right, b_bottom]))

        # Top face: one even-odd path, sheet minus every opening, so the layer
        # below shows through the holes exactly where the board is not.
        face = QPainterPath()
        face.setFillRule(Qt.FillRule.OddEvenFill)
        face.addPolygon(QPolygonF(at_top))
        face.closeSubpath()
        for opening in board.openings:
            hole = [
                proj.point(opening.x_mm, opening.y_mm, elevation_px),
                proj.point(opening.right_mm, opening.y_mm, elevation_px),
                proj.point(opening.right_mm, opening.bottom_mm, elevation_px),
                proj.point(opening.x_mm, opening.bottom_mm, elevation_px),
            ]
            face.addPolygon(QPolygonF(hole))
            face.closeSubpath()
        painter.setBrush(board.face)
        painter.drawPath(face)
        painter.restore()

    def _riso_thumbnail(self, image: QImage) -> QImage:
        """``image`` through :func:`riso_duotone`, cached until it goes stale.

        Stale two ways: the source image changed (a new ``cacheKey``), or the
        palette moved under the cache. The second is checked here rather than
        on any signal because no signal reaches this pane on a switch — the
        window just repaints — and three QColor reads per paint are cheaper
        than wiring one.
        """
        ramp = (PALETTE.paper.rgb(), PALETTE.ink_strong.rgb(), PALETTE.accent.rgb())
        if ramp != self._riso_ramp:
            self._riso_cache.clear()
            self._riso_ramp = ramp
        key = image.cacheKey()
        converted = self._riso_cache.get(key)
        if converted is None:
            converted = riso_duotone(image)
            self._riso_cache[key] = converted
        return converted

    def _paint_slots(
        self, painter: QPainter, proj: _Projection, layout: Layout, elevation_px: float
    ) -> None:
        """The images on the print's top face, through the sheared transform.

        Thumbnails — reprinted by :func:`riso_duotone` so they wear the skin —
        when the probe has produced them, else the exact palette-gradient
        placeholder the flat preview draws, built in the same millimetre rect
        so the wash runs the same way in both views.
        """
        painter.save()
        painter.setTransform(proj.shear(elevation_px), True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        for slot in layout.slots:
            r = QRectF(
                slot.rect.x_mm, slot.rect.y_mm, slot.rect.width_mm, slot.rect.height_mm
            )
            image = self._thumbnails.get(slot.index)
            if image is not None and not image.isNull():
                painter.drawImage(r, self._riso_thumbnail(image))
            else:
                painter.fillRect(r, _placeholder_gradient(r, slot.index))
        painter.restore()

        # Outlines after the restore: a 1px pen under the shear would render at
        # sheared width, and these edges are 2:1 by construction anyway.
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QPen(PALETTE.ink_soft, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for slot in layout.slots:
            painter.drawPolygon(
                QPolygonF(
                    [
                        proj.point(slot.rect.x_mm, slot.rect.y_mm, elevation_px),
                        proj.point(slot.rect.right_mm, slot.rect.y_mm, elevation_px),
                        proj.point(slot.rect.right_mm, slot.rect.bottom_mm, elevation_px),
                        proj.point(slot.rect.x_mm, slot.rect.bottom_mm, elevation_px),
                    ]
                )
            )
        painter.restore()

    def _paint_leaders(
        self,
        painter: QPainter,
        proj: _Projection,
        layout: Layout,
        lower_px: float,
        upper_px: float,
    ) -> None:
        """Dotted verticals joining adjacent boards' corners, so the exploded
        layers still read as one registered pile rather than four sheets."""
        for x_mm, y_mm in self._corners_mm(layout):
            draw_dotted_line(
                painter,
                proj.point(x_mm, y_mm, lower_px),
                proj.point(x_mm, y_mm, upper_px),
                colour=PALETTE.rule,
                dot=1.0,
                gap=3.0,
            )

    def _paint_labels(
        self,
        painter: QPainter,
        proj: _Projection,
        layout: Layout,
        boards: list[_Board],
        elevations: list[float],
    ) -> None:
        """BACKING / PRINT / MAT / TOP MAT at each board's left corner.

        On paper chips with a short dotted leader — the flat preview's callout
        language exactly. At low explode the anchors collapse onto one another,
        so the rows are spaced apart top-down and the leaders are allowed to
        run diagonally back to their corners.
        """
        h_mm = layout.sheet.height_mm
        anchors = [proj.point(0.0, h_mm, e) for e in elevations]

        font = mono_font(6.5, caps=True)
        metrics = QFontMetricsF(font)
        chip_right = proj.x0 - h_mm * proj.c - 10.0

        # Top board first: its anchor is the highest, and each lower label is
        # pushed down to keep a readable row.
        order = sorted(range(len(boards)), key=lambda i: anchors[i].y())
        rows_y: dict[int, float] = {}
        previous = -1e9
        for i in order:
            y = max(anchors[i].y(), previous + LABEL_ROW_PX)
            rows_y[i] = y
            previous = y

        for i, board in enumerate(boards):
            y = rows_y[i]
            width = metrics.horizontalAdvance(board.label.upper()) + 8.0
            chip = QRectF(chip_right - width, y - 7.0, width, 14.0)
            draw_dotted_line(
                painter,
                QPointF(chip.right() + 2.0, y),
                anchors[i] + QPointF(-2.0, 0.0),
                colour=PALETTE.rule,
                dot=1.0,
                gap=2.0,
            )
            painter.fillRect(chip, PALETTE.paper)
            draw_micro_label(
                painter,
                chip,
                board.label,
                colour=PALETTE.ink_strong,
                align=Qt.AlignmentFlag.AlignHCenter,
                size_pt=6.5,
            )

    def _paint_caption(self, painter: QPainter, layout: Layout) -> None:
        painter.setPen(PALETTE.ink_strong)
        painter.setFont(mono_font(7, caps=True))
        painter.drawText(
            QRectF(0.0, self.height() - CAPTION_H_PX - 2.0, self.width(), CAPTION_H_PX),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            f"{self._fmt(layout.sheet.width_mm)} × {self._fmt(layout.sheet.height_mm)} "
            f"{self._units}",
        )

    def _paint_warning(self, painter: QPainter) -> None:
        painter.setFont(mono_font(7, caps=True))
        painter.setPen(PALETTE.accent)
        painter.drawText(
            QRectF(self.rect()).adjusted(4, 4, -4, -4),
            int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight),
            self._warning or "",
        )


__all__ = ["StackPane", "DEFAULT_EXPLODE", "riso_duotone"]
