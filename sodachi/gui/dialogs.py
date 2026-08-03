"""The modal dialogs: check table, fit report, cutter options, standard sizes.

These are the windows the actions that once came in from the command line need,
and they are painted in the same skin as everything else — a stock Qt form dropped
into this window reads as a different application. Each one is built from the
vocabulary in :mod:`sodachi.gui.widgets` and the primitives in
:mod:`sodachi.gui.theme`, with two locally-defined pieces the panels did not
previously need: a label/value table and a block of wrapped prose.

Nothing here imports the engine or the window. A dialog takes plain data —
strings, pairs of strings — and returns a plain answer, so each one is
constructible and drivable in a test with no spec, no image and no solver behind
it. The caller does the solving, the formatting and the file path; these choose.
The one piece of arithmetic any dialog owns is the pure derivation in
:mod:`sodachi.core.mat`, which the print-from-opening dialog runs live on its
own fields — still no spec and no solver.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sodachi.core.geometry import Size
from sodachi.core.mat import MatOpeningError, PrintPlan, print_from_opening
from sodachi.core.units import inch_to_mm, mm_to_inch
from sodachi.gui.theme import (
    PALETTE,
    current_palette_name,
    draw_bevel_rect,
    draw_dotted_line,
    draw_micro_label,
    mono_font,
)
from sodachi.gui.widgets import (
    FieldSlider,
    SkinPanel,
    SmallButton,
    TabStrip,
    ToggleChip,
    WideButton,
)

UNITS = ("mm", "in")
"""The two ways the check table states a length."""

CUT_FORMATS = ("dxf", "svg", "csv")
"""The three cutter formats, in the order the tab strip offers them."""

CUT_FORMAT_NOTES = {
    "dxf": "DXF R12 polylines — the format cutting tables and plotters take. "
    "The default; use it unless your machine asks for something else.",
    "svg": "SVG at true millimetre scale. Cutter software reads it too, and "
    "it opens in any vector editor to check the paths before cutting.",
    "csv": "One row per vertex, in millimetres. For a machine with its own "
    "importer, or for checking the numbers by hand.",
}

MIRROR_NOTES = {
    False: "OFF is right for a machine: true geometry, and the cutter "
    "orients the board itself.",
    True: "ON reflects every path. Only for the rare cutter that asks for "
    "back-side coordinates — a mirrored path cuts a mirrored mat.",
}


def _half(value: float) -> float:
    """Snap to a pixel centre so a 1px line covers exactly one pixel row."""
    return math.floor(value) + 0.5


class _Prose(QWidget):
    """A block of wrapped monospace text, painted rather than labelled.

    A QLabel would do the wrapping, but it takes its ink from the widget
    palette and would keep the outgoing colour after a skin switch; reading
    `PALETTE` inside the paint event is what every other widget here does. The
    height is reported through ``heightForWidth`` so a column layout gives the
    block exactly the rows the wrap needs and no more.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        size_pt: float = 8.0,
        role: str = "ink",
    ) -> None:
        super().__init__(parent)
        self._text = str(text)
        self._size_pt = float(size_pt)
        self._role = role
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        text = str(text)
        if text == self._text:
            return
        self._text = text
        self.updateGeometry()
        self.update()

    def _flags(self) -> int:
        return int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(mono_font(self._size_pt))
        box = QRectF(0.0, 0.0, max(float(width) - 2.0, 20.0), 10_000.0)
        bounds = metrics.boundingRect(box, self._flags(), self._text)
        return int(math.ceil(bounds.height())) + 2

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(260, self.heightForWidth(260))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(120, self.heightForWidth(120))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setFont(mono_font(self._size_pt))
        p.setPen(getattr(PALETTE, self._role))
        p.drawText(QRectF(self.rect()).adjusted(1.0, 0.0, -1.0, 0.0), self._flags(), self._text)
        p.end()


class _Rule(QWidget):
    """One dotted rule across the width, for separating a dialog's sections."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(7)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        y = _half(rect.center().y())
        draw_dotted_line(p, QPointF(rect.left(), y), QPointF(rect.right(), y))
        p.end()


class _RowTable(QWidget):
    """Label/value rows on a recessed field, with a dotted leader between them.

    Not a QTableView: the content is a flat list of pairs that never sorts,
    never edits and never needs a delegate, and a painted widget keeps the row
    rhythm on the same grid as the queue.

    A row whose value is empty is a heading — micro-caps with a rule running out
    to the right edge, and not selectable. That is the only grouping a check
    table wants, and it keeps the caller's data a plain sequence of pairs rather
    than a tree.
    """

    selectionChanged = Signal(int)
    activated = Signal(int)

    ROW_H = 16.0
    PAD = 5.0
    FONT_PT = 8.0

    def __init__(
        self,
        rows: Sequence[tuple[str, str]] = (),
        parent: QWidget | None = None,
        *,
        selectable: bool = False,
    ) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, str]] = []
        self._selectable = bool(selectable)
        self._current = -1
        self._scroll_px = 0.0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.set_rows(rows)

    # ------------------------------------------------------------------ data

    def rows(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._rows)

    def set_rows(self, rows: Sequence[tuple[str, str]]) -> None:
        cleaned: list[tuple[str, str]] = []
        for row in rows:
            if isinstance(row, str):
                cleaned.append((row, ""))
                continue
            parts = list(row) + ["", ""]
            cleaned.append((str(parts[0]), str(parts[1])))
        self._rows = cleaned
        if self._current >= len(self._rows) or not self._is_selectable(self._current):
            self._current = -1
        self._clamp_scroll()
        self.update()

    def _is_selectable(self, index: int) -> bool:
        if not self._selectable or not (0 <= index < len(self._rows)):
            return False
        return bool(self._rows[index][1])

    def current_index(self) -> int:
        return self._current

    def set_current_index(self, index: int) -> None:
        if index != -1 and not self._is_selectable(index):
            return
        if index == self._current:
            return
        self._current = index
        self._scroll_into_view(index)
        self.update()
        self.selectionChanged.emit(index)

    def selected_row(self) -> tuple[str, str] | None:
        if 0 <= self._current < len(self._rows):
            return self._rows[self._current]
        return None

    def select_first(self) -> None:
        """Put the selection on the first row that can carry it, if any."""
        for i in range(len(self._rows)):
            if self._is_selectable(i):
                self.set_current_index(i)
                return

    def plain_text(self) -> str:
        """The table as text, for the clipboard. Values in one aligned column."""
        width = max((len(label) for label, value in self._rows if value), default=0)
        lines: list[str] = []
        for label, value in self._rows:
            if value:
                lines.append(f"{label.ljust(width)}  {value}")
            else:
                if lines:
                    lines.append("")
                lines.append(label)
        return "\n".join(lines)

    # --------------------------------------------------------------- scroll

    def _content_h(self) -> float:
        return len(self._rows) * self.ROW_H

    def _viewport(self) -> QRectF:
        return QRectF(self.rect()).adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)

    def _clamp_scroll(self) -> None:
        overflow = max(0.0, self._content_h() - self._viewport().height())
        self._scroll_px = min(max(self._scroll_px, 0.0), overflow)

    def _scroll_into_view(self, index: int) -> None:
        if index < 0:
            return
        view_h = self._viewport().height()
        top = index * self.ROW_H
        if top < self._scroll_px:
            self._scroll_px = top
        elif top + self.ROW_H > self._scroll_px + view_h:
            self._scroll_px = top + self.ROW_H - view_h
        self._clamp_scroll()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._clamp_scroll()
        super().resizeEvent(event)

    # ----------------------------------------------------------------- input

    def _index_at(self, y: float) -> int:
        view = self._viewport()
        offset = y - view.top() + self._scroll_px
        index = int(math.floor(offset / self.ROW_H))
        return index if 0 <= index < len(self._rows) else -1

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        index = self._index_at(event.position().y())
        if self._is_selectable(index):
            self.set_current_index(index)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        index = self._index_at(event.position().y())
        if self._is_selectable(index):
            self.set_current_index(index)
            self.activated.emit(index)
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._scroll_px -= event.angleDelta().y() / 120.0 * self.ROW_H * 2
        self._clamp_scroll()
        self.update()
        event.accept()

    def _step_selection(self, delta: int) -> None:
        index = self._current
        for _ in range(len(self._rows)):
            index += delta
            if not (0 <= index < len(self._rows)):
                return
            if self._is_selectable(index):
                self.set_current_index(index)
                return

    def _scroll_by(self, rows: float) -> None:
        self._scroll_px += rows * self.ROW_H
        self._clamp_scroll()
        self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        key = event.key()
        if not self._selectable:
            # No selection to move, so the keys scroll instead of doing nothing.
            if key == Qt.Key.Key_Down:
                self._scroll_by(1)
            elif key == Qt.Key.Key_Up:
                self._scroll_by(-1)
            elif key == Qt.Key.Key_PageDown:
                self._scroll_by(self._viewport().height() / self.ROW_H)
            elif key == Qt.Key.Key_PageUp:
                self._scroll_by(-self._viewport().height() / self.ROW_H)
            elif key == Qt.Key.Key_Home:
                self._scroll_px = 0.0
                self.update()
            elif key == Qt.Key.Key_End:
                self._scroll_px = self._content_h()
                self._clamp_scroll()
                self.update()
            else:
                super().keyPressEvent(event)
                return
            event.accept()
            return

        if key == Qt.Key.Key_Down:
            if self._current < 0:
                self.select_first()
            else:
                self._step_selection(1)
        elif key == Qt.Key.Key_Up:
            self._step_selection(-1)
        elif key == Qt.Key.Key_Home:
            self.select_first()
        elif key == Qt.Key.Key_End:
            self._current = -1
            self._step_selection(len(self._rows))
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._current >= 0:
                self.activated.emit(self._current)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # ----------------------------------------------------------------- paint

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(mono_font(self.FONT_PT))
        widest = 0.0
        for label, value in self._rows:
            widest = max(widest, metrics.horizontalAdvance(f"{label}    {value}"))
        rows = min(max(len(self._rows), 4), 16)
        return QSize(
            int(max(240.0, widest + 4 * self.PAD)),
            int(rows * self.ROW_H + 2 * self.PAD),
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(180, int(self.ROW_H * 3 + 2 * self.PAD))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        draw_bevel_rect(p, rect, fill=PALETTE.paper, border=PALETTE.rule, radius=3.0)

        view = self._viewport()
        font = mono_font(self.FONT_PT)
        metrics = QFontMetricsF(font)
        value_w = 0.0
        for _label, value in self._rows:
            value_w = max(value_w, metrics.horizontalAdvance(value))
        value_w = min(value_w + 6.0, view.width() * 0.55)

        p.save()
        p.setClipRect(view)
        first = max(0, int(math.floor(self._scroll_px / self.ROW_H)))
        last = min(len(self._rows), first + int(view.height() / self.ROW_H) + 2)
        for i in range(first, last):
            label, value = self._rows[i]
            y = view.top() + i * self.ROW_H - self._scroll_px
            row = QRectF(view.left(), y, view.width(), self.ROW_H)
            if not value:
                self._paint_heading(p, row, label)
                continue
            self._paint_row(p, row, label, value, font, metrics, value_w, i == self._current)
        p.restore()

        overflow = self._content_h() - view.height()
        if overflow > 0:
            track_x = _half(rect.right() - 4)
            draw_dotted_line(
                p,
                QPointF(track_x, view.top()),
                QPointF(track_x, view.bottom()),
                colour=PALETTE.rule,
            )
            visible = view.height() / self._content_h()
            thumb_h = max(view.height() * visible, 10.0)
            thumb_y = view.top() + (view.height() - thumb_h) * (self._scroll_px / overflow)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(PALETTE.rule)
            p.drawRect(QRectF(track_x - 1.5, thumb_y, 3.0, thumb_h))
        p.end()

    def _paint_heading(self, p: QPainter, row: QRectF, label: str) -> None:
        font = mono_font(7.0, bold=True, caps=True)
        width = QFontMetricsF(font).horizontalAdvance(label)
        draw_micro_label(
            p,
            QRectF(row.left() + 4.0, row.top(), row.width() - 8.0, row.height()),
            label,
            colour=PALETTE.ink_strong,
            bold=True,
        )
        rule_left = row.left() + 4.0 + width + 6.0
        if row.right() - 4.0 - rule_left > 8.0:
            y = _half(row.center().y() + 1.0)
            draw_dotted_line(p, QPointF(rule_left, y), QPointF(row.right() - 4.0, y))

    def _paint_row(
        self,
        p: QPainter,
        row: QRectF,
        label: str,
        value: str,
        font,
        metrics: QFontMetricsF,
        value_w: float,
        selected: bool,
    ) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(PALETTE.fill)
            p.drawRect(row)
            p.setBrush(PALETTE.accent)
            p.drawRect(QRectF(row.left(), row.top(), 2.0, row.height()))

        label_w = row.width() - value_w - 20.0
        label_rect = QRectF(row.left() + 8.0, row.top(), max(label_w, 20.0), row.height())
        shown = metrics.elidedText(label, Qt.TextElideMode.ElideRight, label_rect.width())
        p.setFont(font)
        p.setPen(PALETTE.ink_strong if selected else PALETTE.ink)
        p.drawText(
            label_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            shown,
        )

        value_rect = QRectF(row.right() - value_w - 6.0, row.top(), value_w, row.height())
        p.setPen(PALETTE.ink_strong)
        p.drawText(
            value_rect,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(value, Qt.TextElideMode.ElideRight, value_rect.width()),
        )

        # The leader is what ties a label to a value three inches away. It is
        # drawn only when there is room for more than a couple of dots.
        gap_left = label_rect.left() + metrics.horizontalAdvance(shown) + 6.0
        gap_right = value_rect.left() - 6.0
        if gap_right - gap_left > 10.0 and not selected:
            y = _half(row.center().y() + 1.0)
            draw_dotted_line(
                p,
                QPointF(gap_left, y),
                QPointF(gap_right, y),
                colour=PALETTE.rule,
                dot=1.0,
                gap=3.0,
            )


class _SkinText(QPlainTextEdit):
    """Read-only monospace prose that can be selected and copied.

    The one stock control in this module, because selectable text is the point
    and a painted widget would have to reimplement selection, caret and copy to
    get there. Qt's own frame is dropped for a 1px rule, so the report reads as
    a field on the panel rather than as loose text, and the QSS is rebuilt when
    the palette changes — checked on the repaint a palette switch already
    triggers, since nothing else tells a widget the ink has moved.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setFont(mono_font(9))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._styled_for = ""
        self._restyle()

    def _restyle(self) -> None:
        name = current_palette_name()
        if name == self._styled_for:
            return
        self._styled_for = name
        self.setStyleSheet(
            "QPlainTextEdit {"
            f" background: {PALETTE.paper.name()};"
            f" color: {PALETTE.ink_strong.name()};"
            f" selection-background-color: {PALETTE.fill.name()};"
            f" selection-color: {PALETTE.ink_strong.name()};"
            f" border: 1px solid {PALETTE.rule.name()};"
            " padding: 6px; }"
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._restyle()
        super().paintEvent(event)


class _SkinDialog(QDialog):
    """The shared shell: a surface backdrop, one titled panel, a button row.

    Subclasses fill ``self.body`` and append buttons with `add_button`. The
    backdrop is painted rather than left to the widget palette so a dialog looks
    right in a test process that never installed the application stylesheet.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "",
        window_title: str = "",
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle(window_title or title.title() or "Sodachi")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        self._panel = SkinPanel(title=title or None)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(6)
        column.addLayout(self.body, 1)

        self._buttons = QHBoxLayout()
        self._buttons.setContentsMargins(0, 0, 0, 0)
        self._buttons.setSpacing(6)
        self._buttons.addStretch(1)
        column.addLayout(self._buttons)

        self._panel.setLayout(column)
        outer.addWidget(self._panel, 1)

    def add_button(self, text: str) -> SmallButton:
        """A button on the trailing edge of the row, in the order added."""
        button = SmallButton(text, self._panel)
        self._buttons.addWidget(button)
        return button

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.fillRect(self.rect(), PALETTE.surface)
        p.end()


class CheckDialog(_SkinDialog):
    """The solved geometry as a two-column table, in millimetres or inches.

    This replaces a one-line summary in the status bar with the thing that was
    actually useful. It does no arithmetic: `unitsChanged` reports that the
    switch moved and the caller refills the rows with `set_rows`, because only
    the caller has the layout the numbers come from.

    A row with an empty value is a heading, which is how a caller groups sheet,
    windows and margins in one flat sequence.
    """

    unitsChanged = Signal(str)

    def __init__(
        self,
        rows: Sequence[tuple[str, str]],
        *,
        units: str = "mm",
        title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        if units not in UNITS:
            raise ValueError(f"units is {units!r}; the check table states {' or '.join(UNITS)}")
        super().__init__(parent, title=title or "CHECK", window_title="Sodachi — Check")

        head = QHBoxLayout()
        head.setSpacing(6)
        self._caption = _Prose(
            "The solved geometry. Copy it out if it is going into a note or a "
            "cutting list.",
            size_pt=7.5,
            role="ink_soft",
        )
        head.addWidget(self._caption, 1)
        self._units = ToggleChip("UNITS", UNITS, units)
        self._units.valueChanged.connect(self.unitsChanged.emit)
        head.addWidget(self._units, 0, Qt.AlignmentFlag.AlignTop)
        self.body.addLayout(head)

        self._table = _RowTable(rows)
        self.body.addWidget(self._table, 1)

        self.btn_copy = self.add_button("COPY")
        self.btn_copy.clicked.connect(self._copy)
        self.btn_close = self.add_button("CLOSE")
        self.btn_close.clicked.connect(self.accept)

        self.resize(QSize(460, 480))
        self.setMinimumSize(QSize(340, 260))

    def rows(self) -> tuple[tuple[str, str], ...]:
        return self._table.rows()

    def set_rows(self, rows: Sequence[tuple[str, str]]) -> None:
        """Refill the table, which is what a caller does after `unitsChanged`."""
        self._table.set_rows(rows)

    def units(self) -> str:
        return self._units.value()

    def set_units(self, units: str) -> None:
        if units not in UNITS:
            raise ValueError(f"units is {units!r}; the check table states {' or '.join(UNITS)}")
        self._units.setValue(units)

    def plain_text(self) -> str:
        """The table as text, exactly what `COPY` puts on the clipboard."""
        return self._table.plain_text()

    def _copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:  # pragma: no cover - no clipboard on some platforms
            return
        clipboard.setText(self.plain_text())


class FitReportDialog(_SkinDialog):
    """The padding decision as prose: what border was chosen, and why.

    Monospace and selectable, because the reasoning is written in aligned lines
    and because a reader who wants one number out of it should be able to take
    it without retyping.
    """

    def __init__(self, report: str, *, parent: QWidget | None = None) -> None:
        super().__init__(parent, title="FIT", window_title="Sodachi — Fit report")

        self._text = _SkinText(str(report))
        self.body.addWidget(self._text, 1)

        self.btn_close = self.add_button("CLOSE")
        self.btn_close.clicked.connect(self.accept)

        self.resize(QSize(520, 420))
        self.setMinimumSize(QSize(320, 220))

    def report(self) -> str:
        return self._text.toPlainText()

    def set_report(self, report: str) -> None:
        self._text.setPlainText(str(report))


class CutOptionsDialog(_SkinDialog):
    """Format and mirroring for a cutter export. The caller asks for the path.

    Mirroring is off and says so on its face. The distinction is the one thing
    about this export that is easy to get wrong in a way that only shows up
    after board has been cut: a machine wants true geometry, and the mirrored
    form is the printed guide for cutting by hand from the back.
    """

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent, title="CUTTER FILE", window_title="Sodachi — Cutter file")

        self.body.setSpacing(10)
        self.body.addWidget(
            _Prose(
                "True-size cut paths your machine runs as-is: the window "
                "openings first, then the board outline. Labels and the "
                "calibration bar belong to the printed guide, not in a "
                "toolpath.",
                size_pt=7.5,
                role="ink_soft",
            )
        )

        self.tabs = TabStrip([f.upper() for f in CUT_FORMATS])
        strip = QHBoxLayout()
        strip.setSpacing(0)
        strip.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignLeft)
        strip.addStretch(1)
        self.body.addLayout(strip)

        self._format_note = _Prose(CUT_FORMAT_NOTES[CUT_FORMATS[0]])
        self.body.addWidget(self._format_note)
        self.tabs.currentChanged.connect(self._format_changed)

        self.body.addWidget(_Rule())

        mirror_row = QHBoxLayout()
        mirror_row.setSpacing(8)
        self.mirror_chip = ToggleChip("MIRROR", ("OFF", "ON"), "OFF")
        self.mirror_chip.valueChanged.connect(self._mirror_changed)
        mirror_row.addWidget(self.mirror_chip, 0, Qt.AlignmentFlag.AlignTop)
        self._mirror_note = _Prose(MIRROR_NOTES[False])
        mirror_row.addWidget(self._mirror_note, 1)
        self.body.addLayout(mirror_row)
        self.body.addStretch(1)

        self.btn_write = self.add_button("WRITE")
        self.btn_write.clicked.connect(self.accept)
        self.btn_cancel = self.add_button("CANCEL")
        self.btn_cancel.clicked.connect(self.reject)

        self.resize(QSize(430, 380))
        self.setMinimumSize(QSize(360, 320))

    def selected_format(self) -> str:
        """``"dxf"``, ``"svg"`` or ``"csv"`` — the extension without the dot."""
        index = self.tabs.currentIndex()
        return CUT_FORMATS[index] if 0 <= index < len(CUT_FORMATS) else CUT_FORMATS[0]

    def set_selected_format(self, name: str) -> None:
        name = str(name).lower().lstrip(".")
        if name not in CUT_FORMATS:
            raise ValueError(
                f"format is {name!r}; cutter geometry is written as {', '.join(CUT_FORMATS)}"
            )
        self.tabs.setCurrentIndex(CUT_FORMATS.index(name))

    def mirror(self) -> bool:
        return self.mirror_chip.value() == "ON"

    def set_mirror(self, on: bool) -> None:
        self.mirror_chip.setValue("ON" if on else "OFF")

    def _format_changed(self, index: int) -> None:
        self._format_note.set_text(CUT_FORMAT_NOTES[self.selected_format()])

    def _mirror_changed(self, value: str) -> None:
        self._mirror_note.set_text(MIRROR_NOTES[self.mirror()])


SAVED_SIZES_KEY = "sizes/custom"
"""Where the user's own sheet sizes live in QSettings: a JSON list of
``{"width_mm": float, "height_mm": float}``. Millimetres always, whatever
units the dialog was opened in — the store sits on the spec's side of the
unit boundary, so a size saved in an inch session reopens exact in a
millimetre one."""

SAVED_SIZE_TOL_MM = 0.05
"""Closer than this on both sides is the same size, not a new one. Half the
millimetre fields' step and finer than a hundredth of an inch, so no two
sizes a user can actually type collapse into each other."""


def _load_saved_sizes() -> list[tuple[float, float]]:
    """The saved sizes as (width_mm, height_mm), oldest first.

    Anything unreadable — a hand-edited value, a truncated write — costs the
    entry, not the dialog: a settings store is outside this process's control
    and is not worth a crash on open.
    """
    raw = QSettings().value(SAVED_SIZES_KEY)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    out: list[tuple[float, float]] = []
    for entry in entries:
        try:
            w_mm = float(entry["width_mm"])
            h_mm = float(entry["height_mm"])
        except (KeyError, TypeError, ValueError):
            continue
        if w_mm > 0 and h_mm > 0:
            out.append((w_mm, h_mm))
    return out


def _store_saved_sizes(sizes: Sequence[tuple[float, float]]) -> None:
    QSettings().setValue(
        SAVED_SIZES_KEY,
        json.dumps([{"width_mm": w_mm, "height_mm": h_mm} for w_mm, h_mm in sizes]),
    )


def _size_label(w_mm: float, h_mm: float, units: str) -> str:
    """A saved size's face: its dimensions in ``units``, one decimal.

    The dimensions are the name — a saved size never asked for a christening —
    and one decimal is enough to tell any two sizes the tolerance keeps apart.
    The store stays millimetres, so the same entry answers to "12.0 × 9.0 in"
    today and "304.8 × 228.6 mm" after a unit switch.
    """
    if units == "in":
        return f"{mm_to_inch(w_mm):.1f} × {mm_to_inch(h_mm):.1f} in"
    return f"{w_mm:.1f} × {h_mm:.1f} mm"


class StandardSizeDialog(_SkinDialog):
    """The standard sheet sizes, with what each one is in millimetres.

    Takes the pairs already formatted, so the dialog neither knows nor converts:
    `sodachi.sizes` is the authority on which sizes exist and what they measure.

    Below the named sizes sits the user's own saved list, kept in QSettings
    under `SAVED_SIZES_KEY`. A saved size is a pair of millimetre numbers, not
    a name in `sodachi.sizes`, so choosing one answers through `custom()` —
    the numbers path the caller already applies — and never through
    `selected()`, which is reserved for names the spec can record.
    """

    def __init__(
        self,
        sizes: Sequence[tuple[str, str]],
        *,
        units: str = "in",
        parent: QWidget | None = None,
    ) -> None:
        self._units = units
        super().__init__(parent, title="SIZE", window_title="Sodachi — Paper size")

        self.body.addWidget(
            _Prose(
                "Pick a named size, or type one. The spec keeps millimetres "
                "either way; the name is only how it is written down.",
                size_pt=7.5,
                role="ink_soft",
            )
        )

        self._standard = list(sizes)
        self._saved: list[tuple[float, float]] = _load_saved_sizes()
        self._table = _RowTable((), selectable=True)
        self._refresh_rows()
        self._table.select_first()
        self._table.activated.connect(lambda _index: self.accept())
        self._table.selectionChanged.connect(self._selection_moved)
        self.body.addWidget(self._table, 1)

        self.body.addWidget(_Rule())
        # Custom entry: a size the table does not name. Committing either
        # field claims the choice from the list; picking from the list clears
        # the claim. 0 means "not given".
        decimals = _INCH_DECIMALS if units == "in" else 1
        top = (1.0, 60.0, 0.0) if units == "in" else (25.0, 1500.0, 0.0)
        self.fs_custom_w = FieldSlider("WIDTH", 0.0, top[1], 0.0, units, decimals, slider=False)
        self.fs_custom_h = FieldSlider("HEIGHT", 0.0, top[1], 0.0, units, decimals, slider=False)
        self._custom_claimed = False
        for field in (self.fs_custom_w, self.fs_custom_h):
            field.valueChanged.connect(self._claim_custom)
            self.body.addWidget(field)
        self._table.activated.connect(lambda _i: self._release_custom())

        saved_rail = QHBoxLayout()
        saved_rail.setSpacing(6)
        self.btn_save_size = WideButton("Save size", self._panel)
        self.btn_save_size.clicked.connect(self._save_size)
        saved_rail.addWidget(self.btn_save_size)
        self.btn_remove_size = WideButton("Remove size", self._panel)
        self.btn_remove_size.setEnabled(False)
        self.btn_remove_size.clicked.connect(self._remove_size)
        saved_rail.addWidget(self.btn_remove_size)
        saved_rail.addStretch(1)
        self.body.addLayout(saved_rail)

        self.btn_choose = self.add_button("CHOOSE")
        self.btn_choose.clicked.connect(self.accept)
        self.btn_cancel = self.add_button("CANCEL")
        self.btn_cancel.clicked.connect(self.reject)

        # A row taller than it was: the save/remove rail sits under the fields.
        self.resize(QSize(400, 540))
        self.setMinimumSize(QSize(320, 340))

    def _claim_custom(self, _value: float) -> None:
        self._custom_claimed = True

    def _release_custom(self) -> None:
        self._custom_claimed = False

    # ------------------------------------------------------------ saved sizes

    def _saved_rows(self) -> list[tuple[str, str]]:
        """The saved section: a heading row, then one row per saved size.

        The label is the size in this dialog's units — the name it was saved
        under — and the value restates it in the other unit, the same rhythm
        as the standard rows above. An empty list contributes nothing, so the
        heading never sits over a blank."""
        if not self._saved:
            return []
        other = "mm" if self._units == "in" else "in"
        rows: list[tuple[str, str]] = [("SAVED", "")]
        for w_mm, h_mm in self._saved:
            rows.append(
                (_size_label(w_mm, h_mm, self._units), _size_label(w_mm, h_mm, other))
            )
        return rows

    def _refresh_rows(self) -> None:
        self._table.set_rows(self._standard + self._saved_rows())

    def _saved_at(self, index: int) -> tuple[float, float] | None:
        """The saved size a table row stands for, in millimetres.

        None for every standard row and for the SAVED heading, which is how
        the rest of the dialog tells the two populations apart."""
        offset = index - len(self._standard) - 1  # -1 for the heading row
        if 0 <= offset < len(self._saved):
            return self._saved[offset]
        return None

    def _selection_moved(self, index: int) -> None:
        # Removal is only meaningful on the user's own entries; a standard
        # size is the package's to keep.
        self.btn_remove_size.setEnabled(self._saved_at(index) is not None)

    def _save_size(self) -> None:
        """Keep the typed size, named by its own dimensions.

        An incomplete pair is not a size, and a size already saved — within
        `SAVED_SIZE_TOL_MM` on both sides — is already on the list; both
        decline without ceremony, the same way CHOOSE treats an empty claim.
        """
        w, h = self.fs_custom_w.value(), self.fs_custom_h.value()
        if w <= 0 or h <= 0:
            return
        w_mm = inch_to_mm(w) if self._units == "in" else w
        h_mm = inch_to_mm(h) if self._units == "in" else h
        for saved_w_mm, saved_h_mm in self._saved:
            if (
                abs(saved_w_mm - w_mm) <= SAVED_SIZE_TOL_MM
                and abs(saved_h_mm - h_mm) <= SAVED_SIZE_TOL_MM
            ):
                return
        self._saved.append((w_mm, h_mm))
        _store_saved_sizes(self._saved)
        self._refresh_rows()

    def _remove_size(self) -> None:
        saved = self._saved_at(self._table.current_index())
        if saved is None:
            return
        self._saved.remove(saved)
        _store_saved_sizes(self._saved)
        # The row under the selection is gone. Letting the highlight land on
        # whichever row slides up would quietly change what CHOOSE means, so
        # the selection is cleared instead.
        self._table.set_current_index(-1)
        self._refresh_rows()

    # ---------------------------------------------------------------- answers

    def custom(self) -> tuple[float, float] | None:
        """The chosen size in this dialog's units, or None if a name rules.

        The typed fields claim first, exactly as they always have; both
        dimensions have to be given, because half a sheet is not a size and
        treating one filled field as a choice would apply a 0-length side.
        Failing a typed claim, a selected saved size answers here — it has no
        name the spec could record, so the numbers path is its only way out.
        """
        if self._custom_claimed:
            w, h = self.fs_custom_w.value(), self.fs_custom_h.value()
            if w > 0 and h > 0:
                return (w, h)
        saved = self._saved_at(self._table.current_index())
        if saved is None:
            return None
        w_mm, h_mm = saved
        if self._units == "in":
            return (mm_to_inch(w_mm), mm_to_inch(h_mm))
        return (w_mm, h_mm)

    def selected(self) -> str | None:
        """The name of the chosen standard size, or None if numbers rule.

        A selected saved size makes `custom()` answer, so this reports None
        for it by construction — its label is a measurement, not a name the
        caller could look up.
        """
        if self.custom() is not None:
            return None
        row = self._table.selected_row()
        return row[0] if row is not None else None

    def set_selected(self, name: str) -> None:
        """Move the selection to ``name``. Raises KeyError naming it if absent.

        Only the standard rows answer to a name; a saved row whose label
        happens to read the same is not the size the spec recorded.
        """
        for i, (label, value) in enumerate(self._table.rows()):
            if label == name and value and self._saved_at(i) is None:
                self._table.set_current_index(i)
                return
        raise KeyError(f"no such standard size in this dialog: {name!r}")


# name -> (minimum, maximum, default), keyed by unit. The millimetre defaults
# are the spec's own — 3mm overlap, 10mm border — and the inch column restates
# them at the field's two decimals, so 3mm presents as 0.12in and goes back
# into the plan as 3.048mm: the field is the boundary, and what it shows is
# what it means.
_PLAN_FIELDS: dict[str, tuple[tuple[str, float, float, float], ...]] = {
    "mm": (
        ("IMAGE W", 25.0, 1500.0, 254.0),
        ("IMAGE H", 25.0, 1500.0, 203.2),
        ("REVEAL", 0.0, 50.0, 0.0),
        ("OVERLAP", 0.1, 25.0, 3.0),
        ("BORDER", 0.0, 100.0, 10.0),
        ("FRAME W", 0.0, 1500.0, 0.0),
        ("FRAME H", 0.0, 1500.0, 0.0),
    ),
    "in": (
        ("IMAGE W", 1.0, 60.0, 10.0),
        ("IMAGE H", 1.0, 60.0, 8.0),
        ("REVEAL", 0.0, 2.0, 0.0),
        ("OVERLAP", 0.01, 1.0, round(mm_to_inch(3.0), 2)),
        ("BORDER", 0.0, 4.0, round(mm_to_inch(10.0), 2)),
        ("FRAME W", 0.0, 60.0, 0.0),
        ("FRAME H", 0.0, 60.0, 0.0),
    ),
}

_INCH_DECIMALS = 2
"""A hundredth of an inch is 0.254mm, finer than the millimetre fields' step."""


class RequirementsDialog(_SkinDialog):
    """What the print you want will require.

    Image-first, because that is the decision people arrive holding: the frame
    you mean to show, at the size you mean to show it. The opening, the paper
    minimum and — when a frame size is given — the margins all follow, live on
    every field commit, via :func:`sodachi.core.mat.print_from_opening` run on
    the opening the image and reveal imply. An impossible combination puts the
    error's own text in the results area instead of numbers, because mid-edit
    impossibility is an ordinary state here, not a crash.

    APPLY emits ``apply_requested`` with the current :class:`PrintPlan` and
    leaves the dialog open, so the caller can push the plan into the spec and
    the user can watch the preview take it. All fields are in the constructor's
    units; conversion to millimetres happens at this boundary and nowhere else.
    """

    apply_requested = Signal(object)

    def __init__(self, *, units: str = "in", parent: QWidget | None = None) -> None:
        if units not in UNITS:
            raise ValueError(f"units is {units!r}; the fields are in {' or '.join(UNITS)}")
        super().__init__(
            parent, title="REQUIREMENTS", window_title="Sodachi — Requirements"
        )
        self._units = units
        self._plan: PrintPlan | None = None

        self.body.addWidget(
            _Prose(
                "Start from the image you want on the wall. The opening, the "
                "paper and — given a frame — the margins follow from it.",
                size_pt=7.5,
                role="ink_soft",
            )
        )

        decimals = _INCH_DECIMALS if units == "in" else 1
        fields: list[FieldSlider] = []
        for name, low, high, default in _PLAN_FIELDS[units]:
            field = FieldSlider(name, low, high, default, units, decimals)
            field.valueChanged.connect(self._recompute)
            self.body.addWidget(field)
            fields.append(field)
        (
            self.fs_image_w,
            self.fs_image_h,
            self.fs_reveal,
            self.fs_overlap,
            self.fs_border,
            self.fs_frame_w,
            self.fs_frame_h,
        ) = fields
        # Aliases from when the dialog led with the hole; callers may hold them.
        self.fs_opening_w = self.fs_image_w
        self.fs_opening_h = self.fs_image_h

        self.body.addWidget(_Rule())

        self._results = _SkinText()
        # The report's floor is measured, not guessed, because the resolved
        # font is not this module's to choose: a host without the mono
        # families falls back to something wider, and a fixed pixel width
        # would wrap there exactly the rows it was sized to protect.
        #
        # Height: the tallest report is nine lines — three headline sizes,
        # three allowance rows, the three-line frame block — and the floor
        # reserves ten. Width: the widest row is the border explanation
        # against the frame block's sixteen-character label column, restated
        # here as a template because the report is composed live and has no
        # widest row until it is too late to size for it. Both floors carry
        # the document margin and _SkinText's 6px padding and 1px rule.
        metrics = QFontMetricsF(mono_font(9.0))
        widest_row = (
            "margins, centred  "
            "50.00 mm — border printed around the image, per side, at minimum"
        )
        chrome = 2.0 * self._results.document().documentMargin() + 2 * (6 + 1)
        self._results.setMinimumHeight(
            int(math.ceil(10 * metrics.lineSpacing() + chrome))
        )
        self._results.setMinimumWidth(
            int(math.ceil(metrics.horizontalAdvance(widest_row) + chrome + 12))
        )
        self.body.addWidget(self._results, 1)

        self.btn_apply = self.add_button("APPLY")
        self.btn_apply.clicked.connect(self._apply)
        self.btn_close = self.add_button("CLOSE")
        self.btn_close.clicked.connect(self.accept)

        # The explicit sizes are floors for a roomy open, not the guarantee;
        # the guarantee is the results area's own minimums, which the layout
        # carries into `minimumSizeHint` and which `expandedTo` honours when
        # the resolved font outgrows these numbers.
        self.setMinimumSize(QSize(680, 520))
        self.resize(QSize(760, 640).expandedTo(self.minimumSizeHint()))
        self._recompute()

    def units(self) -> str:
        return self._units

    def plan(self) -> PrintPlan | None:
        """The current plan, or None while the fields describe an impossibility."""
        return self._plan

    def results_text(self) -> str:
        """What the results area shows: the derivation, or the error's text."""
        return self._results.toPlainText()

    def _to_mm(self, value: float) -> float:
        return inch_to_mm(value) if self._units == "in" else value

    def _fmt(self, w_mm: float, h_mm: float) -> str:
        if self._units == "in":
            return f"{w_mm / 25.4:.2f} x {h_mm / 25.4:.2f} in"
        return f"{w_mm:.1f} x {h_mm:.1f} mm"

    def _recompute(self) -> None:
        reveal_mm = self._to_mm(self.fs_reveal.value())
        image = Size(
            self._to_mm(self.fs_image_w.value()), self._to_mm(self.fs_image_h.value())
        )
        # The image is what was asked for; the opening it implies is the image
        # grown by the reveal, which is the hole print_from_opening starts at.
        opening = Size(image.width_mm + 2 * reveal_mm, image.height_mm + 2 * reveal_mm)
        try:
            plan = print_from_opening(
                opening,
                reveal_mm=reveal_mm,
                overlap_mm=self._to_mm(self.fs_overlap.value()),
                min_border_mm=self._to_mm(self.fs_border.value()),
            )
        except MatOpeningError as exc:
            self._plan = None
            self._results.setPlainText(str(exc))
            return
        self._plan = plan
        # The print leads: image, then the paper to order, then the cutting.
        rows: list[tuple[str, str]] = [
            ("print image", self._fmt(plan.image_mm.width_mm, plan.image_mm.height_mm)),
            (
                "paper, at least",
                self._fmt(plan.min_paper_mm.width_mm, plan.min_paper_mm.height_mm),
            ),
            ("opening to cut", self._fmt(plan.opening_mm.width_mm, plan.opening_mm.height_mm)),
        ]
        # The plan restates the three headline sizes; keep only its
        # explanatory rows so nothing appears twice.
        headline = {"opening", "image", "paper"}
        rows.extend(r for r in plan.rows(self._units) if r[0] not in headline)

        frame_w_mm = self._to_mm(self.fs_frame_w.value())
        frame_h_mm = self._to_mm(self.fs_frame_h.value())
        if frame_w_mm > 0 and frame_h_mm > 0:
            rows.append(("", ""))
            rows.append(("in the frame", self._fmt(frame_w_mm, frame_h_mm)))
            too_small = (
                frame_w_mm < plan.min_paper_mm.width_mm
                or frame_h_mm < plan.min_paper_mm.height_mm
            )
            if too_small:
                rows.append(("", "the frame is smaller than the paper this print needs"))
            else:
                side_mm = (frame_w_mm - image.width_mm) / 2
                vert_mm = (frame_h_mm - image.height_mm) / 2
                rows.append(
                    (
                        "margins, centred",
                        f"{self._fmt(side_mm, vert_mm)} (sides, top+bottom)",
                    )
                )
        width = max(len(label) for label, _value in rows)
        self._results.setPlainText(
            "\n".join(f"{label.ljust(width)}  {value}".rstrip() for label, value in rows)
        )

    def _apply(self) -> None:
        # An impossible combination has no plan to apply; the results area is
        # already saying why, so the button quietly declines.
        if self._plan is not None:
            self.apply_requested.emit(self._plan)


PrintFromOpeningDialog = RequirementsDialog
"""The dialog's name while it led with the opening; kept for callers."""


__all__ = [
    "UNITS",
    "CUT_FORMATS",
    "SAVED_SIZES_KEY",
    "CheckDialog",
    "FitReportDialog",
    "CutOptionsDialog",
    "RequirementsDialog",
    "PrintFromOpeningDialog",
    "StandardSizeDialog",
]
