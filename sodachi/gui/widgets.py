"""The widget vocabulary: panels, tabs, buttons, readouts, sliders, the queue.

Every widget here paints itself with QPainter and the primitives in
``theme``. None of them is a styled stock control, because the skin's
proportions — a 7pt letter-spaced label, a 7px knob on a dotted rail — are not
reachable through QSS and would drift the moment a Qt style changed.

``FieldSlider`` is the part that has to be real rather than decorative: the
number field is the control — typing and committing re-solves the layout — and
the short slider beside it is for coarse exploration. It emits ``valueChanged``
only when the quantised value actually moves.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRectF, QRegularExpression, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QLineEdit,
    QMenu,
    QSizePolicy,
    QWidget,
)

from sodachi.gui.theme import (
    PALETTE,
    draw_bevel_rect,
    draw_dotted_line,
    draw_header,
    draw_micro_label,
    draw_panel,
    draw_readout,
    mono_font,
)


def _half(value: float) -> float:
    """Snap to a pixel centre so a 1px line covers exactly one pixel row."""
    return math.floor(value) + 0.5


class SkinPanel(QWidget):
    """A panel: chrome painted here, children laid out inside the dotted rule.

    Set a layout on it as normal; the content margins already account for the
    outline, the inset dotted rule and the optional header band.
    """

    PADDING = 10
    HEADER_HEIGHT = 18

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str | None = None,
        dotted_inset: float = 4.0,
        radius: float = 10.0,
        fill: QColor | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._dotted_inset = float(dotted_inset)
        self._radius = float(radius)
        self._fill = fill
        self._apply_margins()

    def _apply_margins(self) -> None:
        top = self.PADDING + (self.HEADER_HEIGHT if self._title else 0)
        self.setContentsMargins(self.PADDING, top, self.PADDING, self.PADDING)

    def title(self) -> str | None:
        return self._title

    def setTitle(self, title: str | None) -> None:
        self._title = title
        self._apply_margins()
        self.updateGeometry()
        self.update()

    def contentRect(self) -> QRectF:
        m = self.contentsMargins()
        return QRectF(self.rect()).adjusted(m.left(), m.top(), -m.right(), -m.bottom())

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        draw_panel(
            p,
            rect,
            dotted_inset=self._dotted_inset,
            radius=self._radius,
            fill=self._fill,
        )
        if self._title:
            band = QRectF(
                rect.left() + self.PADDING,
                rect.top() + self.PADDING - 2,
                rect.width() - 2 * self.PADDING,
                self.HEADER_HEIGHT,
            )
            draw_header(p, band, self._title)
        p.end()


class TabStrip(QWidget):
    """A row of tabs whose active member reads as the top of the panel below.

    Not a QTabBar: the active tab here is a paper-filled shape with no bottom
    edge, so the panel underneath continues straight out of it, and a QTabBar
    insists on drawing its own frame and its own platform-styled tab shape.

    The inactive tabs sit ``RECEDE`` pixels lower and keep the baseline running
    across them, which is what makes them read as behind rather than merely
    unselected.
    """

    currentChanged = Signal(int)

    HEIGHT = 24.0
    PAD_X = 12.0
    GAP = 3.0
    RADIUS = 4.0
    MIN_TAB_W = 44.0
    RECEDE = 3.0
    """How far below the active tab an inactive one sits."""

    def __init__(
        self,
        tabs: Sequence[str] = (),
        parent: QWidget | None = None,
        *,
        tail: float = 0.0,
    ) -> None:
        super().__init__(parent)
        self._tabs: list[str] = []
        self._current = -1
        self._hover = -1
        # Baseline run-out past the last tab. Zero for a strip that spans its
        # rail; a strip floating in open space asks for a deliberate length of
        # rule after it, so the line ends on purpose instead of at the widget.
        self._tail = float(tail)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        for label in tabs:
            self.addTab(label)

    def addTab(self, label: str) -> int:  # noqa: N802 - Qt naming
        self._tabs.append(str(label))
        index = len(self._tabs) - 1
        self.updateGeometry()
        # The first tab moves the index off -1, which is a real change and is
        # reported as one; a strip with tabs is never left with none current.
        if self._current < 0:
            self.setCurrentIndex(0)
        self.update()
        return index

    def count(self) -> int:
        return len(self._tabs)

    def currentIndex(self) -> int:  # noqa: N802 - Qt naming
        return self._current

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt naming
        index = -1 if not self._tabs else min(max(int(index), 0), len(self._tabs) - 1)
        if index == self._current:
            return
        self._current = index
        self.update()
        self.currentChanged.emit(index)

    def tabText(self, index: int) -> str:  # noqa: N802 - Qt naming
        if 0 <= index < len(self._tabs):
            return self._tabs[index]
        return ""

    def _label_font(self) -> QFont:
        return mono_font(7, bold=True, caps=True)

    def _tab_widths(self) -> list[float]:
        metrics = QFontMetricsF(self._label_font())
        return [
            max(self.MIN_TAB_W, metrics.horizontalAdvance(label) + 2 * self.PAD_X)
            for label in self._tabs
        ]

    def _tab_rects(self) -> list[QRectF]:
        """Full-height boxes, one per tab. The recede is applied at paint time."""
        top = 1.0
        bottom = max(float(self.height()) - 1.0, top + 4.0)
        rects: list[QRectF] = []
        x = 1.0
        for width in self._tab_widths():
            rects.append(QRectF(x, top, width, bottom - top))
            x += width + self.GAP
        return rects

    def _index_at(self, x: float) -> int:
        for i, box in enumerate(self._tab_rects()):
            if box.left() <= x <= box.right():
                return i
        return -1

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        widths = self._tab_widths()
        total = sum(widths) + self.GAP * max(len(widths) - 1, 0) + 2.0 + self._tail
        return QSize(int(max(total, self.MIN_TAB_W)), int(self.HEIGHT))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(int(min(self.sizeHint().width(), 120)), int(self.HEIGHT))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        index = self._index_at(event.position().x())
        if index >= 0:
            self.setCurrentIndex(index)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        index = self._index_at(event.position().x())
        if index != self._hover:
            self._hover = index
            self.update()
        event.accept()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        key = event.key()
        if key == Qt.Key.Key_Left:
            self.setCurrentIndex(self._current - 1)
        elif key == Qt.Key.Key_Right:
            self.setCurrentIndex(self._current + 1)
        elif key == Qt.Key.Key_Home:
            self.setCurrentIndex(0)
        elif key == Qt.Key.Key_End:
            self.setCurrentIndex(len(self._tabs) - 1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _paint_tab(
        self,
        p: QPainter,
        box: QRectF,
        base_y: float,
        *,
        label: str,
        active: bool,
        hover: bool,
    ) -> None:
        left = _half(box.left())
        right = _half(box.right())
        top = _half(box.top())
        # The active tab is carried one pixel past the baseline so its fill
        # covers the rule, which is what joins it to the panel below.
        bottom = base_y + (1.0 if active else 0.0)
        radius = min(self.RADIUS, (right - left) / 2, max(bottom - top, 1.0))

        path = QPainterPath()
        path.moveTo(left, bottom)
        path.lineTo(left, top + radius)
        path.quadTo(left, top, left + radius, top)
        path.lineTo(right - radius, top)
        path.quadTo(right, top, right, top + radius)
        path.lineTo(right, bottom)

        if active:
            face = PALETTE.paper
        elif hover:
            face = PALETTE.white
        else:
            face = PALETTE.surface

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillPath(path, face)
        p.setPen(QPen(PALETTE.rule, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        text_top = top + (3.0 if active else 1.0)
        if active:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(PALETTE.accent)
            p.drawRect(QRectF(left + radius, top + 1.0, right - left - 2 * radius, 2.0))
            ink = PALETTE.ink_strong
        elif hover:
            ink = PALETTE.ink
        else:
            ink = PALETTE.ink_soft

        draw_micro_label(
            p,
            QRectF(left + 5.0, text_top, right - left - 10.0, bottom - text_top - 1.0),
            label,
            colour=ink,
            align=Qt.AlignmentFlag.AlignHCenter,
            bold=active,
        )
        p.restore()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        rects = self._tab_rects()
        base_y = _half(rect.bottom() - 1.0)

        for i, box in enumerate(rects):
            if i == self._current:
                continue
            self._paint_tab(
                p,
                box.adjusted(0.0, self.RECEDE, 0.0, 0.0),
                base_y,
                active=False,
                hover=i == self._hover,
                label=self._tabs[i],
            )

        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setPen(QPen(PALETTE.rule, 1.0))
        p.drawLine(QPointF(rect.left(), base_y), QPointF(rect.right(), base_y))

        if 0 <= self._current < len(rects):
            self._paint_tab(
                p,
                rects[self._current],
                base_y,
                active=True,
                hover=False,
                label=self._tabs[self._current],
            )
            if self.hasFocus():
                box = rects[self._current]
                y = _half(base_y - 3.0)
                draw_dotted_line(
                    p,
                    QPointF(box.left() + 6.0, y),
                    QPointF(box.right() - 6.0, y),
                    colour=PALETTE.accent,
                )
        p.end()


class SmallButton(QAbstractButton):
    """A near-square transport button with a two-line micro-caps face.

    The label splits on an explicit newline, or as evenly as the words allow.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def _lines(self) -> list[str]:
        text = self.text()
        if "\n" in text:
            return text.split("\n", 1)
        words = text.split()
        if len(words) < 2:
            return [text]
        # Split at the seam that leaves the two lines closest in length.
        best = min(
            range(1, len(words)),
            key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))),
        )
        return [" ".join(words[:best]), " ".join(words[best:])]

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(mono_font(7, bold=True, caps=True))
        lines = self._lines()
        width = max((metrics.horizontalAdvance(line) for line in lines), default=0.0)
        return QSize(max(46, int(width) + 16), 34 if len(lines) > 1 else 26)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        pressed = self.isDown()
        if pressed:
            face, ink = PALETTE.rule, PALETTE.paper
        elif not self.isEnabled():
            face, ink = PALETTE.surface, PALETTE.rule
        elif self.underMouse():
            # Hover lifts towards white rather than tinting, so the one warm
            # colour in the palette stays reserved for the active state.
            face, ink = PALETTE.white, PALETTE.ink_strong
        else:
            face, ink = PALETTE.fill, PALETTE.ink

        draw_bevel_rect(p, rect, fill=face, border=PALETTE.rule, radius=2.0)
        if self.hasFocus():
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QPen(PALETTE.accent, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(rect).adjusted(2.5, 2.5, -2.5, -2.5))

        lines = self._lines()
        font = mono_font(7, bold=True, caps=True)
        p.setFont(font)
        p.setPen(ink)
        metrics = QFontMetricsF(font)
        line_h = metrics.height()
        total = line_h * len(lines)
        y = rect.center().y() - total / 2
        for line in lines:
            p.drawText(
                QRectF(rect.left() + 2, y, rect.width() - 4, line_h),
                int(Qt.AlignmentFlag.AlignCenter),
                metrics.elidedText(line, Qt.TextElideMode.ElideRight, rect.width() - 4),
            )
            y += line_h
        p.end()


class WideButton(QAbstractButton):
    """A single-line button with comfortable horizontal padding: "Remove File".

    The face treatment is SmallButton's — same fill, hover lift, press invert
    and disabled grey — but the label never wraps and the width comes from the
    text metrics, so it reads as one plain phrase in a rail.
    """

    PAD_X = 14.0
    HEIGHT = 26

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def _face_ink(self) -> tuple[QColor, QColor]:
        if self.isDown():
            return PALETTE.rule, PALETTE.paper
        if not self.isEnabled():
            return PALETTE.surface, PALETTE.rule
        if self.underMouse():
            # Hover lifts towards white rather than tinting; see SmallButton.
            return PALETTE.white, PALETTE.ink_strong
        return PALETTE.fill, PALETTE.ink

    def _marker_w(self) -> float:
        """Space reserved on the right; MenuButton claims some for its marker."""
        return 0.0

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(mono_font(7, bold=True, caps=True))
        width = metrics.horizontalAdvance(self.text()) + 2 * self.PAD_X + self._marker_w()
        return QSize(max(int(width), 60), self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        face, ink = self._face_ink()
        draw_bevel_rect(p, rect, fill=face, border=PALETTE.rule, radius=2.0)
        if self.hasFocus():
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QPen(PALETTE.accent, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(rect).adjusted(2.5, 2.5, -2.5, -2.5))

        font = mono_font(7, bold=True, caps=True)
        p.setFont(font)
        p.setPen(ink)
        metrics = QFontMetricsF(font)
        text_rect = rect.adjusted(4.0, 0.0, -4.0 - self._marker_w(), 0.0)
        p.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignCenter),
            metrics.elidedText(self.text(), Qt.TextElideMode.ElideRight, text_rect.width()),
        )
        p.end()


class MenuButton(WideButton):
    """A WideButton with a down-pointing marker that opens an owned QMenu.

    The menu pops directly under the button's left edge, so the choices read
    as an extension of the face rather than a floating list.
    """

    MARKER_W = 12.0

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._menu: QMenu | None = None
        self.clicked.connect(self._show_menu)

    def menu(self) -> QMenu | None:
        return self._menu

    def setMenu(self, menu: QMenu | None) -> None:  # noqa: N802 - Qt naming
        self._menu = menu
        if menu is not None:
            # Own the menu, but keep the popup flag: a plain setParent would
            # strip it and the menu would embed as a child widget.
            menu.setParent(self, Qt.WindowType.Popup)

    def _marker_w(self) -> float:
        return self.MARKER_W

    def _show_menu(self) -> None:
        if self._menu is not None:
            self._menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        _, ink = self._face_ink()
        rect = QRectF(self.rect())
        cx = rect.right() - self.PAD_X + 1.0
        cy = rect.center().y() - 0.5
        marker = QPainterPath()
        marker.moveTo(cx - 3.5, cy - 2.0)
        marker.lineTo(cx + 3.5, cy - 2.0)
        marker.lineTo(cx, cy + 2.5)
        marker.closeSubpath()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawPath(marker)
        p.end()


class ToggleChip(QAbstractButton):
    """A small square that cycles through a fixed list of values on click.

    The value is drawn in ``accent`` once it differs from the first entry, so a
    glance at the strip says which settings have been moved off their default.

    ``horizontal`` lays the label to the left of the value on one 20px line,
    which is the form that sits inside a FieldSlider row without towering
    over it.
    """

    valueChanged = Signal(str)

    def __init__(
        self,
        label: str = "",
        values: Sequence[str] = (),
        value: str | None = None,
        parent: QWidget | None = None,
        *,
        horizontal: bool = False,
    ) -> None:
        super().__init__(parent)
        self._label = label
        self._values = list(values)
        self._index = 0
        self._horizontal = bool(horizontal)
        if value is not None and value in self._values:
            self._index = self._values.index(value)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Preferred sideways, so a row of chips given equal stretch shares the
        # rail evenly instead of each claiming its own text's width.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._inert_reason: str | None = None
        self.clicked.connect(self._advance)

    def set_inert(self, reason: str | None) -> None:
        """Paint the chip soft while its value has no effect. Still clickable:
        inert is a statement about the current layout, not a lock."""
        if reason != self._inert_reason:
            self._inert_reason = reason
            self.update()

    def inert_reason(self) -> str | None:
        return self._inert_reason

    def label(self) -> str:
        return self._label

    def values(self) -> tuple[str, ...]:
        return tuple(self._values)

    def value(self) -> str:
        return self._values[self._index] if self._values else ""

    def setValue(self, value: str) -> None:  # noqa: N802 - Qt naming
        if value not in self._values or value == self.value():
            return
        self._index = self._values.index(value)
        self.update()
        self.valueChanged.emit(self.value())

    def setValues(self, values: Sequence[str], value: str | None = None) -> None:  # noqa: N802
        self._values = list(values)
        self._index = self._values.index(value) if value in self._values else 0
        self.updateGeometry()
        self.update()

    def _advance(self) -> None:
        if len(self._values) < 2:
            return
        self._index = (self._index + 1) % len(self._values)
        self.update()
        self.valueChanged.emit(self.value())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(mono_font(8, bold=True, caps=True))
        widest = max(
            (metrics.horizontalAdvance(v) for v in self._values),
            default=metrics.horizontalAdvance(self._label),
        )
        label_w = QFontMetricsF(mono_font(7, caps=True)).horizontalAdvance(self._label)
        if self._horizontal:
            return QSize(int(label_w + widest) + 22, 20)
        return QSize(max(52, int(max(widest, label_w)) + 14), 34)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def _value_ink(self) -> QColor:
        default = self._values[0] if self._values else ""
        if self.isDown():
            return PALETTE.paper
        if self._inert_reason:
            # Inert outranks off-default: an accent on a setting that is not
            # doing anything would be the accent telling a lie.
            return PALETTE.ink_soft
        if self.value() != default:
            return PALETTE.accent
        return PALETTE.ink_strong

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        face = PALETTE.rule if self.isDown() else PALETTE.surface
        draw_bevel_rect(p, rect, fill=face, border=PALETTE.rule, radius=2.0)

        if self.hasFocus():
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QPen(PALETTE.accent, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(rect).adjusted(2.5, 2.5, -2.5, -2.5))

        label_ink = PALETTE.paper if self.isDown() else PALETTE.ink_soft
        if self._horizontal:
            inner = rect.adjusted(6, 1, -6, -1)
            label_w = (
                QFontMetricsF(mono_font(7, caps=True)).horizontalAdvance(self._label) + 4.0
            )
            draw_micro_label(
                p,
                QRectF(inner.left(), inner.top(), label_w, inner.height()),
                self._label,
                colour=label_ink,
                align=Qt.AlignmentFlag.AlignLeft,
            )
            draw_micro_label(
                p,
                QRectF(
                    inner.left() + label_w,
                    inner.top(),
                    inner.width() - label_w,
                    inner.height(),
                ),
                self.value(),
                colour=self._value_ink(),
                align=Qt.AlignmentFlag.AlignRight,
                size_pt=8.0,
                bold=True,
            )
            p.end()
            return

        inner = rect.adjusted(4, 3, -4, -3)
        label_h = inner.height() * 0.42
        draw_micro_label(
            p,
            QRectF(inner.left(), inner.top(), inner.width(), label_h),
            self._label,
            colour=label_ink,
            align=Qt.AlignmentFlag.AlignHCenter,
        )
        draw_micro_label(
            p,
            QRectF(inner.left(), inner.top() + label_h, inner.width(), inner.height() - label_h),
            self.value(),
            colour=self._value_ink(),
            align=Qt.AlignmentFlag.AlignHCenter,
            size_pt=8.0,
            bold=True,
        )
        p.end()


class ColorSwatch(QAbstractButton):
    """A labelled colour well: tiny caps name, then the colour itself.

    The well shows the colour and the hex beside it; clicking is a request to
    choose a new one, which the owner answers with a picker — the swatch holds
    no dialog of its own, because what "choosing a colour" means belongs to
    the window.
    """

    HEIGHT = 20
    NAME_W = 54.0
    """Matches FieldSlider's name column, so a swatch sits in a rail of rows
    without breaking the aligned left edge."""

    def __init__(self, name: str, color: str = "#FFFFFF", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._color = QColor(color)
        self._inert_reason: str | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def name(self) -> str:
        return self._name

    def color(self) -> str:
        return self._color.name().upper()

    def setColor(self, color: str) -> None:  # noqa: N802 - Qt naming
        made = QColor(color)
        if made.isValid() and made != self._color:
            self._color = made
            self.update()

    def set_inert(self, reason: str | None) -> None:
        if reason != self._inert_reason:
            self._inert_reason = reason
            self.update()

    def inert_reason(self) -> str | None:
        return self._inert_reason

    def _name_w(self) -> float:
        measured = QFontMetricsF(mono_font(7, caps=True)).horizontalAdvance(self._name) + 6.0
        return max(self.NAME_W, measured)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        hex_w = QFontMetricsF(mono_font(9)).horizontalAdvance("#DDDDDD")
        return QSize(int(self._name_w() + 6.0 + 34.0 + 6.0 + hex_w), self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        soft = self._inert_reason is not None
        draw_micro_label(
            p,
            QRectF(rect.left(), rect.top(), self._name_w(), rect.height()),
            self._name,
            colour=PALETTE.ink_soft if soft else PALETTE.ink,
            align=Qt.AlignmentFlag.AlignLeft,
        )
        well = QRectF(rect.left() + self._name_w() + 6.0, rect.top() + 2.0, 34.0, rect.height() - 4.0)
        shown = QColor(self._color)
        if soft:
            shown.setAlpha(110)
        draw_bevel_rect(p, well, fill=shown, border=PALETTE.rule, radius=2.0)
        if self.hasFocus():
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QPen(PALETTE.accent, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(well.adjusted(1.5, 1.5, -1.5, -1.5))
        p.setFont(mono_font(9))
        p.setPen(PALETTE.ink_soft if soft else PALETTE.ink)
        p.drawText(
            QRectF(well.right() + 6.0, rect.top(), rect.right() - well.right() - 6.0, rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.color(),
        )
        p.end()


class Readout(QWidget):
    """A monospace figure on a dotted baseline. Display only.

    Deliberately not the recessed box it used to be: the recess is the skin's
    signal for "type here", and a readout that wore it sat indistinguishable
    from the fields beside it that genuinely take input. A report rests on a
    dotted rule instead, the same treatment a derived FieldSlider row gets.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        align: Qt.Alignment = Qt.AlignmentFlag.AlignRight,
        min_chars: int = 6,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._align = align
        self._min_chars = int(min_chars)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        text = str(text)
        if text == self._text:
            return
        self._text = text
        self.update()

    def setAlign(self, align: Qt.Alignment) -> None:  # noqa: N802 - Qt naming
        self._align = align
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(mono_font(9))
        chars = max(self._min_chars, len(self._text))
        return QSize(int(metrics.horizontalAdvance("0") * chars) + 14, 20)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(mono_font(9))
        return QSize(int(metrics.horizontalAdvance("0") * 3) + 14, 20)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        font = mono_font(9)
        p.setFont(font)
        p.setPen(PALETTE.ink_strong)
        text_rect = rect.adjusted(5.0, 0.0, -5.0, -3.0)
        metrics = QFontMetricsF(font)
        shown = metrics.elidedText(self._text, Qt.TextElideMode.ElideRight, text_rect.width())
        p.drawText(
            text_rect,
            int(self._align | Qt.AlignmentFlag.AlignVCenter),
            shown,
        )
        baseline = _half(rect.bottom() - 2.0)
        draw_dotted_line(
            p,
            QPointF(rect.left() + 2.0, baseline),
            QPointF(rect.right() - 2.0, baseline),
            colour=PALETTE.rule,
        )
        p.end()


class FieldSlider(QWidget):
    """A labelled numeric row: tiny caps name, a typed field, a slider.

    The field is the primary control: typing a number and committing it (Enter
    or focus-out) emits ``valueChanged``. The slider beside it is for coarse
    exploration and writes the field live as it drags. Values clamp to range
    and quantise to ``decimals``; the field takes only digits and one decimal
    point, so a non-numeric entry cannot be typed in the first place.

    The recess is exactly as wide as the range's own widest number, so the
    whole value is always inside it and the slider takes every remaining
    pixel. The geometry is fixed by the range alone — the resolved arrow and
    the suffix live in a reserved gap — which is what keeps the QLineEdit and
    the painted frame from ever drifting apart mid-row.

    The QLineEdit inside is frameless and transparent — the recessed frame is
    painted here with the Readout treatment, because Qt's stock field chrome
    would sit foreign on the skin.
    """

    valueChanged = Signal(float)

    HEIGHT = 20
    SLIDER_W = 110.0
    """The rail's one length. Fixed rather than elastic, and right-anchored,
    so every slider in a bank is the same size in the same place — a rail
    of rows whose sliders all differed by their labels' widths read as
    different controls when they are the same control five times."""
    MIN_SLIDER_W = 56.0
    """The floor when a row genuinely has less room than SLIDER_W."""
    GAP = 6.0
    NAME_W = 54.0
    """Floor for the name column, so a rail of these reads as one aligned
    stack rather than each row placing its field where its own label ends."""

    def __init__(
        self,
        name: str,
        minimum: float,
        maximum: float,
        value: float,
        suffix: str = "mm",
        decimals: int = 1,
        parent: QWidget | None = None,
        *,
        slider: bool = True,
        name_hidden: bool = False,
        also_fit: Sequence[str] = (),
    ) -> None:
        super().__init__(parent)
        self._name = name
        # Number texts the recess must fit besides its own range's — the same
        # field's extremes in the other display unit. Sized for both up front
        # so a unit flip rebuilds the row at the width it already had, and
        # nothing in the window moves.
        self._also_fit = tuple(str(text) for text in also_fit)
        # Hidden, not absent: a companion widget may already say the name —
        # the BOTTOM mode chip carries "BOTTOM" itself — and painting it twice
        # would spend the row's width on an echo. Lookup by name still works.
        self._name_hidden = bool(name_hidden)
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._suffix = suffix
        self._decimals = int(decimals)
        self._value = self._quantise(value)
        self._dragging = False
        self._has_slider = bool(slider)
        self._resolved: float | None = None
        self._allowed: tuple[float, float] | None = None
        self._inert_reason: str | None = None
        self._read_only = False
        self._flash = False

        self._edit = QLineEdit(self)
        self._edit.setFrame(False)
        self._edit.setFont(mono_font(9))
        self._edit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._edit.setText(self._format(self._value))
        self._edit.editingFinished.connect(self._commit)
        self._sync_validator()
        self._sync_edit_palette()
        self.setFocusProxy(self._edit)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def name(self) -> str:
        return self._name

    def suffix(self) -> str:
        return self._suffix

    def minimum(self) -> float:
        return self._minimum

    def maximum(self) -> float:
        return self._maximum

    def value(self) -> float:
        return self._value

    def valueText(self) -> str:  # noqa: N802 - Qt naming
        return f"{self._value:.{self._decimals}f}{self._suffix}"

    def lineEdit(self) -> QLineEdit:  # noqa: N802 - Qt naming
        """The field itself, exposed for focus chains and tests."""
        return self._edit

    def set_resolved(self, value: float | None) -> None:
        """The value the solver actually used, when it differs from the field.

        A side margin is a minimum, so 3.00 in the field can honestly become
        3.25 on the sheet. Painting the resolved number beside the suffix says
        so at the moment it happens instead of leaving the preview to disagree
        quietly with the rail.
        """
        rounded = None if value is None else round(float(value), self._decimals)
        if rounded == self._value:
            rounded = None
        if rounded != self._resolved:
            self._resolved = rounded
            self.update()

    def set_allowed(self, low: float | None, high: float | None) -> None:
        """The stretch of the range the other settings currently leave legal.

        The rail's dots run strong inside it and faint outside, with a tick at
        each boundary, so the interplay between fields is visible where the
        hand is: shrink the sheet and the margin rails visibly close down in
        the same repaint. Purely informative — dragging past the tick still
        asks, and the refusal path still answers — because the boundary moves
        with every other field and a hard stop would fight the cursor.

        ``None`` for both ends clears the band and the rail paints as before.
        """
        if low is None and high is None:
            band = None
        else:
            lo = self._minimum if low is None else max(float(low), self._minimum)
            hi = self._maximum if high is None else min(float(high), self._maximum)
            band = (lo, max(hi, lo))
        if band != self._allowed:
            self._allowed = band
            self.update()

    def allowed(self) -> tuple[float, float] | None:
        return self._allowed

    def set_inert(self, reason: str | None) -> None:
        """Mark the row as currently doing nothing, without disabling it.

        Inert is not disabled: the value is real and editing it is allowed, it
        just has no effect until some other setting changes — GUTTER under a
        single frame, RATIO under a fixed bottom. The row paints soft, and the
        window shows ``reason`` when the user touches it anyway.
        """
        if reason != self._inert_reason:
            self._inert_reason = reason
            self.update()

    def inert_reason(self) -> str | None:
        return self._inert_reason

    def setReadOnly(self, read_only: bool) -> None:  # noqa: N802 - Qt naming
        """A display row: the number is derived, not for typing.

        Painted without the recessed frame so a glance separates fields that
        take input from ones that merely report — the recess is the skin's
        signal for "type here".
        """
        self._read_only = bool(read_only)
        self._edit.setReadOnly(self._read_only)
        self.update()

    def isReadOnly(self) -> bool:  # noqa: N802 - Qt naming
        return self._read_only

    def flash_invalid(self) -> None:
        """One accent pulse on the value: the edit was refused or clamped."""
        self._flash = True
        self.update()
        QTimer.singleShot(900, self._end_flash)

    def _end_flash(self) -> None:
        self._flash = False
        self.update()

    def _format(self, value: float) -> str:
        return f"{value:.{self._decimals}f}"

    def _quantise(self, value: float) -> float:
        value = min(max(float(value), self._minimum), self._maximum)
        return round(value, self._decimals)

    def _step(self) -> float:
        return 10.0 ** (-self._decimals)

    def setRange(self, minimum: float, maximum: float) -> None:  # noqa: N802 - Qt naming
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self.setValue(self._value)
        # The recess is sized by the range's widest number, so a new range is
        # the one thing that can honestly move the field under the edit.
        self._sync_validator()
        self._place_edit()
        self.updateGeometry()
        self.update()

    def _sync_validator(self) -> None:
        """Digits and one point; a minus only where the range can hold one.

        The suffix cannot be typed — it is painted beside the recess — and
        letters cannot be typed at all, so `_commit` only ever sees a number
        or an empty string.
        """
        sign = "-?" if self._minimum < 0 else ""
        pattern = QRegularExpression(rf"^{sign}\d*\.?\d*$")
        self._edit.setValidator(QRegularExpressionValidator(pattern, self._edit))

    def setValue(self, value: float) -> None:  # noqa: N802 - Qt naming
        value = self._quantise(value)
        if abs(value - self._value) < self._step() / 2:
            return
        self._value = value
        self._edit.setText(self._format(value))
        self.update()
        self.valueChanged.emit(self._value)

    def _commit(self) -> None:
        text = self._edit.text().strip()
        # "12mm" is an honest attempt at "12"; the suffix is painted anyway.
        if self._suffix and text.lower().endswith(self._suffix.lower()):
            text = text[: -len(self._suffix)].strip()
        try:
            value = float(text)
        except ValueError:
            self._edit.setText(self._format(self._value))
            return
        self.setValue(value)
        # Canonical form even when the value did not move ("07", out of range).
        self._edit.setText(self._format(self._value))

    def _sync_edit_palette(self) -> None:
        """Keep the field's ink on the running palette; frame and fill are ours.

        The ink also carries the row's state: accent while a refused edit is
        flashing, soft while the row is inert or merely reporting a derived
        number, strong otherwise.
        """
        if self._flash:
            ink = PALETTE.accent
        elif self._inert_reason or self._read_only:
            ink = PALETTE.ink_soft
        else:
            ink = PALETTE.ink_strong
        pal = self._edit.palette()
        if pal.color(QPalette.ColorRole.Text) != ink or pal.color(
            QPalette.ColorRole.Base
        ).alpha() != 0:
            pal.setColor(QPalette.ColorRole.Base, QColor(0, 0, 0, 0))
            pal.setColor(QPalette.ColorRole.Text, ink)
            self._edit.setPalette(pal)

    def _name_w(self) -> float:
        if self._name_hidden:
            return 0.0
        measured = QFontMetricsF(mono_font(7, caps=True)).horizontalAdvance(self._name) + 6.0
        return max(self.NAME_W, measured)

    def _suffix_w(self) -> float:
        if not self._suffix:
            return 0.0
        return QFontMetricsF(mono_font(7, caps=True)).horizontalAdvance(self._suffix) + 4.0

    def _resolved_text(self) -> str:
        if self._resolved is None:
            return ""
        return f"→{self._resolved:.{self._decimals}f}"

    def _resolved_w(self) -> float:
        text = self._resolved_text()
        if not text:
            return 0.0
        return QFontMetricsF(mono_font(7, caps=True)).horizontalAdvance(text) + 4.0

    def _field_w(self) -> float:
        """The recess hugs the widest number the range can produce — in any
        unit the field is built for, so the width survives a unit flip."""
        metrics = QFontMetricsF(mono_font(9))
        texts = [self._format(self._maximum), self._format(self._minimum), *self._also_fit]
        return max(metrics.horizontalAdvance(text) for text in texts) + 14.0

    def _field_rect(self) -> QRectF:
        rect = QRectF(self.rect())
        left = rect.left() + self._name_w() + self.GAP
        return QRectF(left, rect.top(), self._field_w(), rect.height())

    def _reserved_resolved_w(self) -> float:
        """Space held for the resolved arrow whether or not it is showing.

        Reserved permanently because the arrow lands mid-drag: a rail that
        shortened at that moment would move under the cursor that is driving
        the very solve that produced the arrow.
        """
        metrics = QFontMetricsF(mono_font(7, caps=True))
        texts = [self._format(self._maximum), *self._also_fit]
        return max(metrics.horizontalAdvance(f"→{text}") for text in texts) + 4.0

    def _slider_rect(self) -> QRectF:
        """SLIDER_W of rail, anchored to the right edge; slack becomes the
        gap before it rather than extra rail."""
        rect = QRectF(self.rect())
        field = self._field_rect()
        earliest = (
            field.right() + 2.0 + self._suffix_w() + self._reserved_resolved_w() + self.GAP
        )
        left = max(rect.right() - self.SLIDER_W, earliest)
        left = min(left, rect.right() - self.MIN_SLIDER_W)
        return QRectF(left, rect.top(), max(rect.right() - left, 0.0), rect.height())

    def _rail(self) -> QRectF:
        # Inset so the knob stays inside the slider box at both extremes.
        return self._slider_rect().adjusted(4.0, 0.0, -4.0, 0.0)

    def _value_at(self, x: float) -> float:
        rail = self._rail()
        fraction = (x - rail.left()) / max(rail.width(), 1.0)
        fraction = min(max(fraction, 0.0), 1.0)
        return self._minimum + fraction * (self._maximum - self._minimum)

    def _knob_x(self) -> float:
        rail = self._rail()
        span = self._maximum - self._minimum
        fraction = 0.5 if span <= 0 else (self._value - self._minimum) / span
        return rail.left() + fraction * rail.width()

    def _fixed_w(self) -> float:
        """Everything left of the rail: name, recess, suffix, resolved gap."""
        return (
            self._name_w()
            + self.GAP
            + self._field_w()
            + 2.0
            + self._suffix_w()
            + self._reserved_resolved_w()
        )

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        slider = self.GAP + self.SLIDER_W if self._has_slider else 0.0
        return QSize(int(self._fixed_w() + slider), self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        slider = self.GAP + self.MIN_SLIDER_W if self._has_slider else 0.0
        return QSize(int(self._fixed_w() + slider), self.HEIGHT)

    def _place_edit(self) -> None:
        field = self._field_rect()
        self._edit.setGeometry(
            int(field.left() + 4),
            int(field.top() + 2),
            int(field.width() - 8),
            int(field.height() - 4),
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._place_edit()
        super().resizeEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        if self._has_slider and not self._read_only and self._slider_rect().contains(
            event.position()
        ):
            self._dragging = True
            self.setValue(self._value_at(event.position().x()))
        else:
            # A click on the name lands in the field: the field is the control.
            self._edit.setFocus(Qt.FocusReason.MouseFocusReason)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self._dragging:
            event.ignore()
            return
        self.setValue(self._value_at(event.position().x()))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._dragging = False
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._read_only:
            event.ignore()
            return
        notches = event.angleDelta().y() / 120.0
        if notches:
            self.setValue(self._value + notches * self._step() * 10.0)
            event.accept()
        else:
            event.ignore()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._sync_edit_palette()
        p = QPainter(self)
        rect = QRectF(self.rect())

        if not self._name_hidden:
            name_ink = (
                PALETTE.ink_soft if (self._inert_reason or self._read_only) else PALETTE.ink
            )
            draw_micro_label(
                p,
                QRectF(rect.left(), rect.top(), self._name_w(), rect.height()),
                self._name,
                colour=name_ink,
                align=Qt.AlignmentFlag.AlignLeft,
            )

        field = self._field_rect()
        if self._read_only:
            # No recess: the recess is the skin's signal for "type here", and
            # this row only reports. A single dotted baseline keeps its place
            # in the stack.
            baseline = _half(field.bottom() - 3.0)
            draw_dotted_line(
                p,
                QPointF(field.left() + 4.0, baseline),
                QPointF(field.right() - 4.0, baseline),
                colour=PALETTE.rule,
            )
        else:
            # An empty readout is exactly the recessed frame; the QLineEdit
            # child paints its text over it afterwards.
            draw_readout(p, field, "")

        x = field.right() + 2.0
        if self._suffix:
            draw_micro_label(
                p,
                QRectF(x, rect.top(), self._suffix_w(), rect.height()),
                self._suffix,
                colour=PALETTE.ink_soft,
                align=Qt.AlignmentFlag.AlignLeft,
            )
            x += self._suffix_w()
        if self._resolved is not None:
            draw_micro_label(
                p,
                QRectF(x, rect.top(), self._resolved_w(), rect.height()),
                self._resolved_text(),
                colour=PALETTE.accent,
                align=Qt.AlignmentFlag.AlignLeft,
            )

        if self._has_slider:
            rail = self._rail()
            y = _half(rail.center().y())
            if self._allowed is None:
                draw_dotted_line(
                    p,
                    QPointF(rail.left(), y),
                    QPointF(rail.right(), y),
                    colour=PALETTE.ink_soft,
                )
            else:
                # Faint end to end, strong across the legal stretch, a tick at
                # each boundary: the rail itself says where the other fields
                # currently let this one go.
                draw_dotted_line(
                    p,
                    QPointF(rail.left(), y),
                    QPointF(rail.right(), y),
                    colour=PALETTE.rule,
                )
                span = self._maximum - self._minimum
                lo, hi = self._allowed
                if span > 0:
                    x_lo = rail.left() + (lo - self._minimum) / span * rail.width()
                    x_hi = rail.left() + (hi - self._minimum) / span * rail.width()
                    draw_dotted_line(
                        p,
                        QPointF(x_lo, y),
                        QPointF(x_hi, y),
                        colour=PALETTE.ink_soft,
                    )
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(PALETTE.ink_soft)
                    for x in (x_lo, x_hi):
                        p.drawRect(QRectF(math.floor(x), y - 3.0, 1.0, 6.0))
            if not self._read_only:
                knob = QRectF(self._knob_x() - 3.0, rail.center().y() - 5.0, 6.0, 10.0)
                fill = PALETTE.surface if self._inert_reason else PALETTE.fill
                draw_bevel_rect(p, knob, fill=fill, border=PALETTE.rule, radius=1.0)
        p.end()


class QueueView(QWidget):
    """Numbered rows with a right-aligned value column, and one selected row.

    Deliberately not a QAbstractItemView: the rows are a flat list of
    label/value pairs that never needs sorting, editing or a delegate, and a
    painted widget keeps the row rhythm exactly on the skin's grid.

    The selected row is the only row that carries a fill. Rows are the tightest
    the type allows and the size hints claim little height, because every pixel
    the queue takes is one the sheet preview does not get.
    """

    selectionChanged = Signal(int)
    rowMoved = Signal(int, int)
    """A row dragged to a new place: (from_row, to_row). The owner reorders
    its model; the view only reports the gesture."""

    ROW_H = 14.0
    PAD = 4.0
    FONT_PT = 7.5
    """Row type. Small enough that ROW_H holds a line with the leading intact."""

    DRAG_THRESHOLD_PX = 5.0
    """Vertical travel before a press becomes a drag rather than a click."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, str, bool]] = []
        self._current = -1
        self._scroll_px = 0.0
        self._press_y: float | None = None
        self._drag_from = -1
        self._drop_at = -1
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    @staticmethod
    def _normalise(row: object) -> tuple[str, str, bool]:
        """Accept a bare string, a (label, value) pair, or (label, value, bad)."""
        if isinstance(row, str):
            return (row, "", False)
        if isinstance(row, (tuple, list)):
            parts = list(row) + ["", "", False]
            return (str(parts[0]), str(parts[1]), bool(parts[2]))
        return (str(row), "", False)

    def setRows(self, rows: Sequence[object]) -> None:  # noqa: N802 - Qt naming
        self._rows = [self._normalise(r) for r in rows]
        if self._current >= len(self._rows):
            self._current = len(self._rows) - 1
            self.selectionChanged.emit(self._current)
        self._clamp_scroll()
        self.update()

    def rows(self) -> tuple[tuple[str, str, bool], ...]:
        return tuple(self._rows)

    def rowCount(self) -> int:  # noqa: N802 - Qt naming
        return len(self._rows)

    def currentIndex(self) -> int:  # noqa: N802 - Qt naming
        return self._current

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt naming
        index = -1 if not self._rows else min(max(index, -1), len(self._rows) - 1)
        if index == self._current:
            return
        self._current = index
        self._scroll_into_view(index)
        self.update()
        self.selectionChanged.emit(index)

    def clear(self) -> None:
        self.setRows([])

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

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(280, int(self.ROW_H * 5 + 2 * self.PAD))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(140, int(self.ROW_H * 3 + 2 * self.PAD))

    def _row_at(self, y_widget: float) -> int:
        view = self._viewport()
        y = y_widget - view.top() + self._scroll_px
        return int(math.floor(y / self.ROW_H))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        index = self._row_at(event.position().y())
        if 0 <= index < len(self._rows):
            self.setCurrentIndex(index)
            self._press_y = event.position().y()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._press_y is None:
            event.ignore()
            return
        if self._drag_from < 0:
            if abs(event.position().y() - self._press_y) < self.DRAG_THRESHOLD_PX:
                event.accept()
                return
            self._drag_from = self._current
        # The drop index is where the row would land, so dragging below the
        # last row parks at the end rather than vanishing off the list.
        target = self._row_at(event.position().y())
        self._drop_at = min(max(target, 0), len(self._rows) - 1)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._drag_from >= 0 and self._drop_at >= 0 and self._drop_at != self._drag_from:
            self.rowMoved.emit(self._drag_from, self._drop_at)
        self._press_y = None
        self._drag_from = -1
        self._drop_at = -1
        self.update()
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._scroll_px -= event.angleDelta().y() / 120.0 * self.ROW_H * 2
        self._clamp_scroll()
        self.update()
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        key = event.key()
        if key == Qt.Key.Key_Down:
            self.setCurrentIndex(self._current + 1)
        elif key == Qt.Key.Key_Up:
            self.setCurrentIndex(max(self._current - 1, 0))
        elif key == Qt.Key.Key_Home:
            self.setCurrentIndex(0)
        elif key == Qt.Key.Key_End:
            self.setCurrentIndex(len(self._rows) - 1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect())
        draw_bevel_rect(p, rect, fill=PALETTE.paper, border=PALETTE.rule, radius=3.0)

        view = self._viewport()
        p.save()
        p.setClipRect(view)
        font = mono_font(self.FONT_PT)
        metrics = QFontMetricsF(font)
        value_w = 0.0
        for _, value, _bad in self._rows:
            value_w = max(value_w, metrics.horizontalAdvance(value))
        value_w = min(value_w + 6, view.width() * 0.45)

        first = max(0, int(math.floor(self._scroll_px / self.ROW_H)))
        last = min(len(self._rows), first + int(view.height() / self.ROW_H) + 2)
        for i in range(first, last):
            label, value, bad = self._rows[i]
            y = view.top() + i * self.ROW_H - self._scroll_px
            row = QRectF(view.left(), y, view.width(), self.ROW_H)
            selected = i == self._current

            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            if selected:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(PALETTE.fill)
                p.drawRect(row)
                p.setBrush(PALETTE.accent)
                p.drawRect(QRectF(row.left(), row.top(), 2.0, row.height()))

            number = f"{i + 1}."
            number_w = metrics.horizontalAdvance("00.") + 3
            p.setFont(font)
            p.setPen(PALETTE.ink_soft)
            p.drawText(
                QRectF(row.left() + 4, row.top(), number_w, row.height()),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                number,
            )

            label_rect = QRectF(
                row.left() + number_w + 6,
                row.top(),
                row.width() - number_w - value_w - 12,
                row.height(),
            )
            if bad:
                ink = PALETTE.accent
            elif selected:
                ink = PALETTE.ink_strong
            else:
                ink = PALETTE.ink
            p.setPen(ink)
            p.drawText(
                label_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                metrics.elidedText(label, Qt.TextElideMode.ElideMiddle, max(label_rect.width(), 1)),
            )

            if value:
                p.setPen(PALETTE.ink_strong if selected else PALETTE.ink_soft)
                p.drawText(
                    QRectF(row.right() - value_w - 4, row.top(), value_w, row.height()),
                    int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                    value,
                )

        if self._drag_from >= 0 and self._drop_at >= 0:
            # The insertion line: where the dragged row will land on release.
            y = view.top() + self._drop_at * self.ROW_H - self._scroll_px
            if self._drop_at > self._drag_from:
                y += self.ROW_H
            y = _half(min(max(y, view.top()), view.bottom()))
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QPen(PALETTE.accent, 1.0))
            p.drawLine(QPointF(view.left(), y), QPointF(view.right(), y))
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


class Marquee(QWidget):
    """A one-line title that scrolls only when it does not fit.

    The timer is stopped whenever the text fits or the widget is hidden; a
    permanently running 30Hz repaint behind a hidden panel is the kind of thing
    that turns up later as unexplained battery drain.
    """

    INTERVAL_MS = 40
    GAP_PX = 40.0

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._offset_px = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._advance)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        text = str(text)
        if text == self._text:
            return
        self._text = text
        self._offset_px = 0.0
        self._sync_timer()
        self.update()

    def _text_width(self) -> float:
        return QFontMetricsF(mono_font(9)).horizontalAdvance(self._text)

    def _overflows(self) -> bool:
        return self._text_width() > self.width() - 8

    def _sync_timer(self) -> None:
        if self.isVisible() and self._overflows():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._offset_px = 0.0

    def _advance(self) -> None:
        self._offset_px += 1.0
        if self._offset_px > self._text_width() + self.GAP_PX:
            self._offset_px = 0.0
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._sync_timer()
        super().resizeEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(200, int(QFontMetricsF(mono_font(9)).height()) + 4)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(60, int(QFontMetricsF(mono_font(9)).height()) + 4)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        rect = QRectF(self.rect()).adjusted(4, 0, -4, 0)
        p.setClipRect(rect)
        p.setFont(mono_font(9))
        p.setPen(PALETTE.ink_strong)
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if self._overflows():
            width = self._text_width()
            x = rect.left() - self._offset_px
            p.drawText(QRectF(x, rect.top(), width, rect.height()), flags, self._text)
            p.drawText(
                QRectF(x + width + self.GAP_PX, rect.top(), width, rect.height()),
                flags,
                self._text,
            )
        else:
            p.drawText(rect, flags, self._text)
        p.end()


__all__ = [
    "SkinPanel",
    "TabStrip",
    "SmallButton",
    "WideButton",
    "MenuButton",
    "ToggleChip",
    "ColorSwatch",
    "Readout",
    "FieldSlider",
    "QueueView",
    "Marquee",
]
