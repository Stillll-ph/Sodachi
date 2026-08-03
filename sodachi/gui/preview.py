"""The sheet, drawn to scale.

This is the panel that occupies the reference skin's illustration area, and it
is the reason the sliders are worth having: it repaints from the solved
``Layout`` alone, so dragging a margin is a pure Qt repaint with no image
decode behind it. Per-slot thumbnails are supplied separately and only when
the queue has finished probing.

Nothing here imports pyvips. Millimetres come in, device pixels go out, and the
one conversion is :meth:`PreviewPane._transform`.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from sodachi.core.layout import Layout
from sodachi.core.mat import MatOpeningError, openings_mm, outer_openings_mm
from sodachi.gui.theme import (
    PALETTE,
    draw_dotted_line,
    mono_font,
)

MARGIN_PX = 26.0
"""Room around the sheet for the dimension callouts."""


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """``a`` blended ``t`` of the way to ``b``, in sRGB."""
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def _placeholder_gradient(rect: QRectF, index: int) -> QLinearGradient:
    """The empty-slot wash: the palette's fill, run toward its rule.

    Built from PALETTE at paint time, so a palette switch re-themes every
    placeholder on the next repaint with no wiring. Fill and rule are the
    pairing because both are solved roles with guarantees: fill is the
    palette's chromatic surface and rule lands at 1.6-3.5:1 against the
    panel at either polarity, so the wash carries the palette's own hue and
    its deep end stays visible over the sheet on all fifteen palettes,
    light or dark.

    The axis and the depth of the run both step with the slot index, so a
    diptych of placeholders reads as two frames rather than one swatch
    stamped twice.
    """
    fill = PALETTE.fill
    rule = PALETTE.rule
    corners = (
        (rect.topLeft(), rect.bottomRight()),
        (rect.topRight(), rect.bottomLeft()),
        (rect.bottomLeft(), rect.topRight()),
        (rect.bottomRight(), rect.topLeft()),
    )
    start, end = corners[index % 4]
    gradient = QLinearGradient(start, end)
    # The light end is pushed a step off the fill so it cannot vanish into a
    # near-white sheet; the deep end stops short of the raw rule so the slot
    # stays material rather than becoming chrome.
    gradient.setColorAt(0.0, _mix(fill, rule, 0.12))
    gradient.setColorAt(1.0, _mix(fill, rule, 0.55 + 0.15 * (index % 3)))
    return gradient


@dataclass(frozen=True, slots=True)
class _Transform:
    """Millimetres to widget pixels. Uniform, so the sheet keeps its shape."""

    scale: float
    x0: float
    y0: float

    def point(self, x_mm: float, y_mm: float) -> QPointF:
        return QPointF(self.x0 + x_mm * self.scale, self.y0 + y_mm * self.scale)

    def rect(self, x_mm: float, y_mm: float, w_mm: float, h_mm: float) -> QRectF:
        return QRectF(
            self.x0 + x_mm * self.scale,
            self.y0 + y_mm * self.scale,
            w_mm * self.scale,
            h_mm * self.scale,
        )


class PreviewPane(QWidget):
    """The solved sheet with its margins dimensioned."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout: Layout | None = None
        self._warning: str | None = None
        self._thumbnails: dict[int, QImage] = {}
        self._units = "mm"
        self._mat_enabled = False
        self._mat_overlap_mm = 3.0
        self._mat_reveal_mm = 0.0
        self._mat_color = "#F6F1EA"
        self._mat_double = False
        self._mat_inner_reveal_mm = 6.0
        self._mat_inner_color = "#F6F1EA"
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(QSize(200, 220))

    # ------------------------------------------------------------------- api

    def units(self) -> str:
        return self._units

    def set_units(self, units: str) -> None:
        """The unit the callouts speak: "mm", "in" or "px".

        The pane draws from a millimetre Layout either way; this only changes
        the labels, so the preview stops contradicting a rail that says 1.57 IN
        while the sheet under it says 40.0. Pixels come from the layout's own
        DPI, the same conversion the raster renderer makes.
        """
        if units not in ("mm", "in", "px"):
            raise ValueError(f"units is {units!r}; the preview speaks mm, in or px")
        if units != self._units:
            self._units = units
            self.update()

    def set_mat(
        self,
        enabled: bool,
        overlap_mm: float,
        reveal_mm: float,
        *,
        color: str = "#F6F1EA",
        double: bool = False,
        inner_reveal_mm: float = 6.0,
        inner_color: str = "#F6F1EA",
    ) -> None:
        """Show the mat board over the sheet, cut with the current openings.

        The pane draws the same holes the guide and the cut file get, via
        :func:`sodachi.core.mat.openings_mm`, so what the preview shows lapping
        the image (or revealing its paper border) is what the board will do.

        The keyword fields carry the spec's board colours and its double mat,
        keyword-only with the spec's own defaults so the original three-argument
        call still means what it always did: one warm board-white board.
        ``color`` is the board you look at first — the only board when single,
        the TOP board when double — and ``inner_color`` is a double mat's
        bottom board, the band revealed inside the top opening; the same
        reading the stack pane states in ``_board_faces``.
        """
        state = (
            bool(enabled),
            float(overlap_mm),
            float(reveal_mm),
            str(color),
            bool(double),
            float(inner_reveal_mm),
            str(inner_color),
        )
        if state != (
            self._mat_enabled,
            self._mat_overlap_mm,
            self._mat_reveal_mm,
            self._mat_color,
            self._mat_double,
            self._mat_inner_reveal_mm,
            self._mat_inner_color,
        ):
            (
                self._mat_enabled,
                self._mat_overlap_mm,
                self._mat_reveal_mm,
                self._mat_color,
                self._mat_double,
                self._mat_inner_reveal_mm,
                self._mat_inner_color,
            ) = state
            self.update()

    def _fmt(self, mm: float) -> str:
        if self._units == "in":
            return f"{mm / 25.4:.2f}"
        if self._units == "px":
            dpi = self._layout.sheet.dpi if self._layout else 300.0
            return f"{round(mm * dpi / 25.4)}"
        return f"{mm:.1f}"

    def layoutResult(self) -> Layout | None:  # noqa: N802 - Qt naming
        return self._layout

    def set_layout_result(self, layout: Layout | None, warning: str | None = None) -> None:
        self._layout = layout
        self._warning = warning
        self.update()

    def set_thumbnails(self, images: dict[int, QImage]) -> None:
        self._thumbnails = dict(images)
        self.update()

    # ---------------------------------------------------------------- paint

    def _transform(self, layout: Layout) -> _Transform:
        area = QRectF(self.rect()).adjusted(MARGIN_PX, MARGIN_PX, -MARGIN_PX, -MARGIN_PX)
        sheet_w_mm = layout.sheet.width_mm
        sheet_h_mm = layout.sheet.height_mm
        scale = min(area.width() / sheet_w_mm, area.height() / sheet_h_mm)
        scale = max(scale, 0.01)
        w = sheet_w_mm * scale
        h = sheet_h_mm * scale
        return _Transform(
            scale=scale,
            x0=area.x() + (area.width() - w) / 2,
            y0=area.y() + (area.height() - h) / 2,
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        layout = self._layout
        if layout is None:
            self._paint_empty(painter)
            painter.end()
            return

        t = self._transform(layout)
        sheet = t.rect(0, 0, layout.sheet.width_mm, layout.sheet.height_mm)

        painter.fillRect(sheet.translated(2, 2), PALETTE.shadow)
        painter.fillRect(sheet, QColor(layout.sheet.background_hex))
        painter.setPen(QPen(PALETTE.rule, 1.0))
        painter.drawRect(sheet)

        self._paint_available(painter, layout, t)
        for slot in layout.slots:
            self._paint_slot(painter, slot, t)
        if self._mat_enabled:
            self._paint_mat(painter, layout, t, sheet)
        if not self._mat_enabled:
            self._mat_drawn = False
        # After the mat, deliberately: the size chip sits at the slot's own
        # corner, which at reveal zero is exactly the strip the board covers,
        # and a dimension the wash half-hides is a dimension not stated.
        for slot in layout.slots:
            self._paint_slot_label(painter, slot, t)
        self._paint_dimensions(painter, layout, t, sheet)
        if layout.align == "optical":
            self._paint_optical_line(painter, layout, t, sheet)
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

    def _paint_available(self, painter: QPainter, layout: Layout, t: _Transform) -> None:
        """The box left after margins, as a dotted rule.

        Worth drawing because it is the thing the margin sliders move, and
        under optical centring it is not where a reader expects it to be.
        """
        box = layout.available
        r = t.rect(box.x_mm, box.y_mm, box.width_mm, box.height_mm)
        for a, b in (
            (r.topLeft(), r.topRight()),
            (r.bottomLeft(), r.bottomRight()),
            (r.topLeft(), r.bottomLeft()),
            (r.topRight(), r.bottomRight()),
        ):
            draw_dotted_line(painter, a, b, colour=PALETTE.fill, dot=1.0, gap=3.0)

    def _paint_slot(self, painter: QPainter, slot, t: _Transform) -> None:
        r = t.rect(slot.rect.x_mm, slot.rect.y_mm, slot.rect.width_mm, slot.rect.height_mm)
        image = self._thumbnails.get(slot.index)
        if image is not None and not image.isNull():
            painter.drawImage(r, image)
        else:
            painter.fillRect(r, _placeholder_gradient(r, slot.index))

        painter.setPen(QPen(PALETTE.ink_soft, 1.0))
        painter.drawRect(r)

    def _paint_slot_label(self, painter: QPainter, slot, t: _Transform) -> None:
        """The slot's stated size, painted over everything the board covers."""
        r = t.rect(slot.rect.x_mm, slot.rect.y_mm, slot.rect.width_mm, slot.rect.height_mm)
        if r.width() > 46 and r.height() > 18:
            # On a chip, not straight onto the slot. What is behind this label
            # is the user's photograph, whose brightness has nothing to do with
            # the palette: bare ink is unreadable over a light image under a dark
            # palette, and over a dark image under a light one. The margin
            # callouts already back themselves the same way.
            painter.setFont(mono_font(7))
            metrics = QFontMetricsF(painter.font())
            text = f"{self._fmt(slot.rect.width_mm)}×{self._fmt(slot.rect.height_mm)}"
            chip = QRectF(
                r.left() + 2,
                r.top() + 2,
                min(metrics.horizontalAdvance(text) + 6, r.width() - 4),
                14,
            )
            painter.fillRect(chip, PALETTE.paper)
            painter.setPen(PALETTE.ink_strong)
            painter.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), text)

    def _paint_mat(
        self, painter: QPainter, layout: Layout, t: _Transform, sheet: QRectF
    ) -> None:
        """The board over the sheet, with the window openings cut out.

        One even-odd path — the sheet with every opening subtracted — rather
        than four rectangles per opening, so adjacent openings cannot leave
        seams or double-washed slivers. The wash wears the spec's ``mat.color``
        pulled a short step toward ``PALETTE.surface`` — enough palette that
        the overlay still sits in the skin at either polarity, not enough to
        hide the chosen board — at partial alpha, low enough that the strip of
        image under the lap stays faintly visible: seeing what the board
        covers is the point of drawing it.

        A double mat adds the bottom board's band: inside each top opening,
        the ring between the top and the bottom opening, in ``mat.inner_color``
        at a firmer alpha than the wash. Firmer on purpose — the band is
        genuinely visible board, not board over sheet, so there is nothing
        under it the alpha needs to keep alive. The holes both come from the
        module the cutter reads, :mod:`sodachi.core.mat`, the top ones derived
        from the bottom by construction, so the band the preview shows is the
        band the two cut files produce.

        Each cut edge then gets a crisp rule-coloured outline. At reveal > 0
        the bottom opening's outline stands off the image edge, and the
        unwashed band between the two is the print's own paper showing inside
        the window.
        """
        self._mat_drawn = False
        try:
            openings = openings_mm(
                layout,
                overlap_mm=self._mat_overlap_mm,
                reveal_mm=self._mat_reveal_mm,
            )
            outer = None
            if self._mat_double:
                outer = outer_openings_mm(
                    layout,
                    overlap_mm=self._mat_overlap_mm,
                    reveal_mm=self._mat_reveal_mm,
                    inner_reveal_mm=self._mat_inner_reveal_mm,
                )
        except MatOpeningError:
            # An overlap that closes a window or a reveal the margins cannot
            # hold is reported by the spec pipeline; the preview must not
            # raise mid-paint, so it simply leaves the mat off until the
            # numbers work again.
            return
        self._mat_drawn = True

        opening_rects = [
            t.rect(o.x_mm, o.y_mm, o.width_mm, o.height_mm) for o in openings
        ]
        outer_rects = (
            None
            if outer is None
            else [t.rect(o.x_mm, o.y_mm, o.width_mm, o.height_mm) for o in outer]
        )

        # The wash is the board you look at first — the only board when
        # single, the TOP board when double — so its holes are the top ones.
        board = QPainterPath()
        board.setFillRule(Qt.FillRule.OddEvenFill)
        board.addRect(sheet)
        for r in outer_rects if outer_rects is not None else opening_rects:
            board.addRect(r)

        wash = _mix(QColor(self._mat_color), PALETTE.surface, 0.2)
        wash.setAlpha(175)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(wash)
        painter.drawPath(board)

        if outer_rects is not None:
            band = QColor(self._mat_inner_color)
            band.setAlpha(205)
            painter.setBrush(band)
            for outer_r, inner_r in zip(outer_rects, opening_rects):
                ring = QPainterPath()
                ring.setFillRule(Qt.FillRule.OddEvenFill)
                ring.addRect(outer_r)
                ring.addRect(inner_r)
                painter.drawPath(ring)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(PALETTE.rule, 1.0))
        for r in opening_rects:
            painter.drawRect(r)
        if outer_rects is not None:
            for r in outer_rects:
                painter.drawRect(r)
        painter.restore()

    def _paint_dimensions(
        self, painter: QPainter, layout: Layout, t: _Transform, sheet: QRectF
    ) -> None:
        painter.setFont(mono_font(7, caps=True))
        metrics = QFontMetricsF(painter.font())
        painter.setPen(PALETTE.ink_soft)

        content = layout.content
        content_rect = t.rect(
            content.x_mm, content.y_mm, content.width_mm, content.height_mm
        )

        pairs = (
            (
                QPointF(content_rect.center().x(), sheet.top()),
                QPointF(content_rect.center().x(), content_rect.top()),
                self._fmt(layout.margins.top_mm),
            ),
            (
                QPointF(content_rect.center().x(), content_rect.bottom()),
                QPointF(content_rect.center().x(), sheet.bottom()),
                self._fmt(layout.margins.bottom_mm),
            ),
            (
                QPointF(sheet.left(), content_rect.center().y()),
                QPointF(content_rect.left(), content_rect.center().y()),
                self._fmt(layout.margins.left_mm),
            ),
        )
        for a, b, text in pairs:
            draw_dotted_line(painter, a, b, colour=PALETTE.ink_soft, dot=1.0, gap=2.0)
            mid = QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
            width = metrics.horizontalAdvance(text) + 6
            box = QRectF(mid.x() - width / 2, mid.y() - 7, width, 14)

            # A narrow margin gives the callout less room than its own text, and
            # the overflow lands on the image rather than on the paper. Push it
            # clear of the sheet instead, where it is still obviously attached
            # to the line it labels.
            horizontal = abs(a.y() - b.y()) < 1e-6
            if horizontal and width > abs(b.x() - a.x()):
                box.moveRight(min(a.x(), b.x()) - 2)
                if box.left() < 0:
                    box.moveLeft(max(a.x(), b.x()) + 2)
            elif not horizontal and box.height() > abs(b.y() - a.y()):
                box.moveBottom(min(a.y(), b.y()) - 2)

            painter.fillRect(box, PALETTE.paper)
            painter.setPen(PALETTE.ink)
            painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)
            painter.setPen(PALETTE.ink_soft)

        self._paint_minor_dimensions(painter, layout, t, metrics)

        painter.setPen(PALETTE.ink_strong)
        painter.setFont(mono_font(7, caps=True))
        painter.drawText(
            QRectF(sheet.left(), sheet.bottom() + 3, sheet.width(), 16),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            f"{self._fmt(layout.sheet.width_mm)} × {self._fmt(layout.sheet.height_mm)} "
            f"{self._units}",
        )

    def _paint_minor_dimensions(
        self, painter: QPainter, layout: Layout, t: _Transform, metrics: QFontMetricsF
    ) -> None:
        """The small distances that are decisions too.

        The gutter between two frames and the reveal band around one are both
        numbers the user set, and both were invisible except as consequences.
        Each gets the margin callouts' own treatment: a dotted lead, a paper
        chip, the value in the pane's unit.
        """

        def chip(mid_x: float, mid_y: float, text: str) -> None:
            width = metrics.horizontalAdvance(text) + 6
            box = QRectF(mid_x - width / 2, mid_y - 7, width, 14)
            painter.fillRect(box, PALETTE.paper)
            painter.setPen(PALETTE.ink)
            painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)
            painter.setPen(PALETTE.ink_soft)

        painter.setPen(PALETTE.ink_soft)

        rows = layout.rows()
        first_row = rows[0] if rows else []
        if len(first_row) >= 2:
            left, right = first_row[0].rect, first_row[1].rect
            gap_mm = right.x_mm - left.right_mm
            if gap_mm > 0:
                y_mm = max(left.y_mm, right.y_mm) + min(left.height_mm, right.height_mm) / 2
                a = t.point(left.right_mm, y_mm)
                b = t.point(right.x_mm, y_mm)
                draw_dotted_line(painter, a, b, colour=PALETTE.ink_soft, dot=1.0, gap=2.0)
                chip((a.x() + b.x()) / 2, a.y() - 10, self._fmt(gap_mm))

        # Gated on the overlay having actually drawn: an impossible reveal
        # leaves no board on screen, so it must leave no callout either.
        if getattr(self, "_mat_drawn", False) and self._mat_reveal_mm > 0 and layout.slots:
            # The visible border: image edge to opening edge, on the first slot.
            slot = layout.slots[0].rect
            y_mm = slot.y_mm + slot.height_mm / 2
            a = t.point(slot.right_mm, y_mm)
            b = t.point(slot.right_mm + self._mat_reveal_mm, y_mm)
            draw_dotted_line(painter, a, b, colour=PALETTE.accent, dot=1.0, gap=2.0)
            chip(b.x() + 16, a.y(), self._fmt(self._mat_reveal_mm))

    def _paint_optical_line(
        self, painter: QPainter, layout: Layout, t: _Transform, sheet: QRectF
    ) -> None:
        """Mark the line the slots were aligned on, at 0.45 of each height.

        Under optical alignment the slots do not share an edge or a centre, and
        without this the layout looks like a mistake rather than a decision.
        """
        if not layout.slots:
            return
        first = layout.slots[0]
        y_mm = first.rect.y_mm + first.rect.height_mm * 0.45
        y = t.point(0, y_mm).y()
        pen = QPen(PALETTE.accent, 1.0, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(sheet.left(), y), QPointF(sheet.right(), y))

        # Outside the sheet, not on it: the line crosses the slots by
        # definition, and a label sitting on a thumbnail reads as an artefact
        # of the image rather than a note about the layout.
        #
        # To the right, because the left margin's callout is pushed out on that
        # side whenever the margin is narrower than its own number, and it sits
        # at the content's vertical centre — which is within a few pixels of
        # this line. The two collided until this moved.
        painter.setFont(mono_font(6, caps=True))
        metrics = QFontMetricsF(painter.font())
        text = "optical"
        width = metrics.horizontalAdvance(text) + 4
        box = QRectF(sheet.right() + 2, y - 7, width, 14)
        if box.right() > self.width():
            box.moveRight(sheet.left() - 2)
        painter.fillRect(box, PALETTE.paper)
        painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)

    def _paint_warning(self, painter: QPainter) -> None:
        painter.setFont(mono_font(7, caps=True))
        painter.setPen(PALETTE.accent)
        painter.drawText(
            QRectF(self.rect()).adjusted(4, 4, -4, -4),
            int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight),
            self._warning or "",
        )


__all__ = ["PreviewPane"]
