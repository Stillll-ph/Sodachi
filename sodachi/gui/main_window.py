"""The window, laid out for full-screen work.

One panel, three regions. The preview owns the centre, because the sheet is the
thing being judged and every remaining pixel belongs to it. The left rail holds
the sheet's identity — which mode, which size, which way up, which unit — and
the file queue. The right rail holds the margins the user actually types, and
below them the one Export button, because entering numbers and taking the
output are the two acts of the whole program.

The two tabs are modes now. PHYSICAL is print work and PIXELS is screen work:
switching tab switches ``spec.target`` through the engine, which re-solves and
lets the spec's own rules coerce the bottom margin for a screen unless the
user has set it deliberately. The spec stays millimetre-native
underneath both — the tab changes what the controls mean and what the output is
for, never what the solver sees.

Every number is a typed field first. The short slider beside each one is for
coarse exploration; the field is the control. Render progress lives in the
status bar, where a message belongs, and nowhere else.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from PySide6.QtCore import QEvent, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFontMetricsF,
    QImage,
    QKeySequence,
    QPainter,
)
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sodachi import __version__
from sodachi.core.solver import ALIGN_MODES, SIZE_MATCH_MODES
from sodachi.core.units import inch_to_mm, mm_to_inch, mm_to_px, px_to_mm
# Safe at module scope: app.py reaches the other way only inside `main`.
from sodachi.gui.app import apply_palette_to
from sodachi.gui.axon import DEFAULT_EXPLODE, StackPane
from sodachi.gui.dialogs import (
    CheckDialog,
    CutOptionsDialog,
    FitReportDialog,
    PrintFromOpeningDialog,
    StandardSizeDialog,
)
from sodachi.gui.models import Job, SodachiEngine, vips_status
from sodachi.gui.preview import PreviewPane
from sodachi.gui.theme import (
    PALETTE,
    available_palettes,
    current_palette_name,
    draw_dotted_line,
    draw_micro_label,
    mono_font,
)
from sodachi.gui.widgets import (
    ColorSwatch,
    FieldSlider,
    Marquee,
    MenuButton,
    QueueView,
    Readout,
    SkinPanel,
    TabStrip,
    ToggleChip,
    WideButton,
)
from sodachi.spec.model import Spec

IMAGE_FILTER = "Images (*.tif *.tiff *.png *.jpg *.jpeg *.webp);;All files (*)"
SPEC_FILTER = "Sodachi spec (*.yaml *.yml);;All files (*)"

CUT_FILTERS = {
    "dxf": "DXF R12 (*.dxf)",
    "svg": "SVG (*.svg)",
    "csv": "CSV (*.csv)",
}
"""The save-dialog filter for each cutter format the options dialog offers."""

JOB_MARKERS = ("·", " ")
"""Alternating leaders that say where one sheet ends and the next begins."""

PHYSICAL_TAB = 0
PIXEL_TAB = 1
TAB_LABELS = ("PHYSICAL", "SCREEN")
TAB_TARGETS = ("print", "screen")
"""The tab is the mode: PHYSICAL edits a print spec, SCREEN a screen one."""

# name -> (dotted spec field, minimum, maximum, suffix, decimals), in millimetres
PHYSICAL_FIELDS = (
    ("TOP", "margins.top_mm", 0.0, 150.0, "mm", 1),
    ("SIDES", "margins.sides_mm", 0.0, 150.0, "mm", 1),
    ("BOTTOM", "margins.bottom_mm", 0.0, 150.0, "mm", 1),
    ("GUTTER", "layout.gutter_mm", 0.0, 60.0, "mm", 1),
    ("RATIO", "margins.optical_ratio", 1.0, 1.6, "", 2),
    # In the margins bank rather than on the left rail: DPI is a print number
    # consulted only when the sheet image is written, and it kept reading as
    # part of the sheet's identity up there, which it is not.
    ("DPI", "sheet.dpi", 72.0, 720.0, "", 0),
)

LENGTH_FIELDS = frozenset({"TOP", "SIDES", "BOTTOM", "GUTTER"})

RESOLVING_FIELDS = frozenset({"TOP", "SIDES", "BOTTOM"})
"""The margins the solve can honestly move past their typed value; only
these rows ever show the resolved arrow, so only they hold room for it."""

FIT_CAUSE = "image proportions"
"""The fit cause names its culprit outright: the frames' own shapes are
what left the box's spare room, not any setting."""

RESOLVED_CAUSE_RESERVE = FIT_CAUSE
"""The widest cause a resolved value can carry — the room is reserved up
front so a cause landing mid-drag never reflows the row."""

BOTTOM_MODES = ("fixed", "optical", "center")
"""The bottom margin's three ways of being stated. FIXED leads because the
neutral spec pins it, and the chip's off-default accent should mark a chosen
rule, not the opening state."""
"""Which of the above are lengths, and so are restated when the unit changes.
A ratio is the same number in any unit."""

INCH_DECIMALS = 2
"""A hundredth of an inch is 0.254mm, which is finer than the millimetre step."""

# name -> (dotted spec field, minimum, maximum), keyed by display unit. Stated
# per unit rather than converted, because `_display_range` rounds to a tenth of
# an inch and the overlap's whole working range sits below that.
MAT_FIELDS: dict[str, tuple[tuple[str, str, float, float], ...]] = {
    "mm": (
        ("OVERLAP", "mat.window_overlap_mm", 0.1, 25.0),
        ("REVEAL", "mat.reveal_mm", 0.0, 50.0),
        ("INNER", "mat.inner_reveal_mm", 0.1, 25.0),
    ),
    "in": (
        ("OVERLAP", "mat.window_overlap_mm", 0.01, 1.0),
        ("REVEAL", "mat.reveal_mm", 0.0, 2.0),
        ("INNER", "mat.inner_reveal_mm", 0.01, 1.0),
    ),
}

# name -> (minimum, maximum) in pixels. The range widens if a spec needs it to.
PIXEL_FIELDS = (
    # BORDER is the headline: type one count and all three margins take it,
    # which is the whole job for most screen borders. The per-side rows below
    # remain for the asymmetric cases.
    ("BORDER", 0.0, 1000.0),
    ("TOP", 0.0, 1000.0),
    ("SIDES", 0.0, 1000.0),
    ("BOTTOM", 0.0, 1000.0),
    ("WIDTH", 240.0, 8000.0),
    ("HEIGHT", 240.0, 8000.0),
)

LEFT_RAIL_W = 260
RIGHT_RAIL_W = 320
"""The right rail's floor; `_size_right_rail` widens it to whatever the
resolved font needs so every row carries a full-length rail."""

BATCH_DIR_SETTING = "export/batch_dir"
"""Where the last SCREEN batch landed. The destination is picked in the GUI
— the EXPORT TO row — and remembered, so the next roll goes to the same
place without asking again."""

PICKER_COLORS_SETTING = "picker/custom_colors"
"""The colour dialog's own custom-swatch row, kept across sessions. Qt holds
those slots per process; a board colour mixed today is one the user will want
at the next framing, so the slots are written back on every pick."""

# The beginner scheme's explanations, one line each. A chip's caption sits
# beside it and follows the value in force, so it always explains the choice
# actually selected rather than the control in the abstract.
TYPE_CAPTIONS = {
    "single": "each file, its own surface",
    "diptych": "pairs 1+2, 3+4 together",
    "triptych": "groups threes together",
    "grid": "the whole queue, one surface",
}
MATCH_CAPTIONS = {
    "area": "no frame outweighs another",
    "height": "shared height, widths vary",
    "width": "shared width, heights vary",
    "none": "native sizes, no matching",
}
# Short on purpose: the wide ALIGN (IMAGES) chip leaves these ~100px.
ALIGN_CAPTIONS = {
    "optical": "level by eye",
    "top": "tops level",
    "center": "midlines level",
    "bottom": "bottoms level",
}
MAT_CAPTIONS = {
    "on": "a windowed board on top",
    "off": "off — no board is cut",
}
DOUBLE_CAPTIONS = {
    "on": "two boards, a band between",
    "off": "off — one board only",
}
BOTTOM_FIELD_CAPTIONS = {
    "fixed": "the bottom margin, exactly as typed",
    "optical": "bottom margin derived: TOP × RATIO",
    "center": "bottom matches TOP, centring the image",
}
FIELD_CAPTIONS = {
    "TOP": "the empty margin above the image",
    "SIDES": "margin at each side; centring may widen it",
    "GUTTER": "the gap between frames sharing one",
    "RATIO": "weights the bottom while its mode is OPTICAL",
    "DPI": "turns inches into pixels at export",
    "OVERLAP": "how far the board grips over the print",
    "REVEAL": "print paper shown inside the window",
    "INNER": "width of the lower board's visible band",
    "BORDER": "sets all three margins at once",
    "WIDTH": "the finished image's width in pixels",
    "HEIGHT": "the finished image's height in pixels",
}
PIXEL_MARGIN_CAPTIONS = {
    "TOP": "the border above the image, in pixels",
    "SIDES": "the border at each side, in pixels",
    "BOTTOM": "the border below the image, in pixels",
}
EXPORT_CAPTION = "writes the mat guide, cutter file, or check table"

# When a control is currently doing nothing, its beginner caption says why
# instead of explaining a value that has no effect. One line, like the rest.
IDLE_CAPTIONS = {
    "MATCH": "idle — nothing to match",
    "GUTTER": "idle — one image has no gap to keep",
    "RATIO": "idle until BOTTOM mode is OPTICAL",
    "INNER": "idle until DOUBLE is on",
    "DOUBLE": "a click turns MAT on with it",
    "MAT_OFF_FIELDS": "idle while MAT is off",
    "DPI_PIXEL_SHEET": "fixed — a pixel canvas sets its own",
}

SCHEME_SETTING = "controls/scheme"
"""Whether the left rail is the standard control set or the beginner one.

Remembered so an app closed in beginner mode opens in it: the person who
wanted the captions yesterday is the person opening it today."""

CONTROL_SCHEMES = ("standard", "beginner")

PALETTE_SETTING = "appearance/palette"
"""Where the chosen skin is remembered.

A default-constructed QSettings reads the organisation and application names off
the QApplication, which is where `build_application` has already put them, so the
key lands in the same place whether the window was opened by `main` or by a test.
"""

PLAN_VIEW = 0
STACK_VIEW = 1
VIEW_LABELS = ("PLAN", "SANDWICH")

def _display_length(mm: float, units: str) -> float:
    return mm_to_inch(mm) if units == "in" else mm


def _length_mm(value: float, units: str) -> float:
    return inch_to_mm(value) if units == "in" else value


def _display_range(low_mm: float, high_mm: float, units: str) -> tuple[float, float]:
    """A millimetre range restated in ``units``, rounded inwards.

    Inwards because a field quantises to its own decimals: an end left at
    5.905in would present itself as 5.91in, which is a range the millimetre
    field behind it does not have.
    """
    if units != "in":
        return (low_mm, high_mm)
    return (
        math.ceil(mm_to_inch(low_mm) * 10.0) / 10.0,
        math.floor(mm_to_inch(high_mm) * 10.0) / 10.0,
    )


def _fit_range(field: FieldSlider, low: float, high: float, value: float) -> None:
    """Widen a field so a spec's value cannot be clamped out of sight.

    A pixel count has no natural ceiling — it is a millimetre size times a DPI
    the user also controls — so the nominal range is a comfortable one to drag
    in rather than a limit, and a sheet outside it moves the ceiling instead of
    being misreported.
    """
    if value > high:
        high = math.ceil(value / 1000.0) * 1000.0
    field.setRange(low, high)


class FieldBank(QWidget):
    """A rail of :class:`FieldSlider` rows that reports edits by field name."""

    valueChanged = Signal(str, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fields: list[FieldSlider] = []
        self._captions: dict[str, CaptionNote] = {}
        self._captions_visible = False
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        # Wide enough that neighbouring rows never read as one control's two
        # halves; the rails carry ticks and bands now, and they need air.
        self._column.setSpacing(12)
        # The terminal stretch soaks up any height a container hands the bank
        # beyond its own — a QStackedWidget gives every page its tallest
        # sibling's height, and without this the surplus opens up as uneven
        # air between the rows.
        self._column.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def addField(  # noqa: N802 - Qt naming
        self,
        name: str,
        minimum: float,
        maximum: float,
        value: float,
        suffix: str = "mm",
        decimals: int = 1,
        *,
        slider: bool = True,
        companion: QWidget | None = None,
        companion_left: bool = False,
        caption: str | None = None,
        name_hidden: bool = False,
        also_fit: Sequence[str] = (),
        cause_reserve: str = "",
    ) -> FieldSlider:
        field = FieldSlider(
            name, minimum, maximum, value, suffix, decimals, self,
            slider=slider, name_hidden=name_hidden, also_fit=also_fit,
            cause_reserve=cause_reserve,
        )
        field.valueChanged.connect(lambda v, f=field: self.valueChanged.emit(f.name(), v))
        self._fields.append(field)
        before_stretch = self._column.count() - 1
        if companion is None:
            self._column.insertWidget(before_stretch, field)
        else:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            if companion_left:
                row.addWidget(companion, 0, Qt.AlignmentFlag.AlignVCenter)
                row.addWidget(field, 1)
            else:
                row.addWidget(field, 1)
                row.addWidget(companion, 0, Qt.AlignmentFlag.AlignVCenter)
            self._column.insertLayout(before_stretch, row)
        if caption is not None:
            # The beginner scheme's line under the row, indented to the value
            # column so it hangs from the number it explains. Built hidden;
            # the window shows the whole set at once when the scheme asks.
            note = CaptionNote(caption, indent=FieldSlider.NAME_W + 6.0)
            note.setVisible(self._captions_visible)
            self._captions[name] = note
            self._column.insertWidget(self._column.count() - 1, note)
        return field

    def field(self, name: str) -> FieldSlider | None:
        for field in self._fields:
            if field.name() == name:
                return field
        return None

    def fields(self) -> tuple[FieldSlider, ...]:
        return tuple(self._fields)

    def setCaptionsVisible(self, visible: bool) -> None:  # noqa: N802 - Qt naming
        self._captions_visible = bool(visible)
        for name, note in self._captions.items():
            field = self.field(name)
            shown = self._captions_visible and (field is None or field.isVisibleTo(self))
            note.setVisible(shown)

    def setCaptionText(self, name: str, text: str) -> None:  # noqa: N802 - Qt naming
        note = self._captions.get(name)
        if note is not None:
            note.setText(text)


class PaddingNote(QWidget):
    """The live one-line padding statement on the SCREEN rail.

    This replaces the old fit-report button: instead of asking, the decision is
    always on screen, refreshed from ``engine.fit_plan`` whenever the spec or
    the queue changes. The line is also the door: clicking it opens the full
    report, which is why it emits ``clicked``.
    """

    clicked = Signal()

    LINES = 3
    """Room for the decision to wrap in a narrow rail without eliding."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
        else:
            event.ignore()

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        text = str(text)
        if text == self._text:
            return
        self._text = text
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        from PySide6.QtGui import QFontMetricsF

        line_h = QFontMetricsF(mono_font(6.5, caps=True)).height()
        return QSize(200, int(line_h * self.LINES) + 6)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setFont(mono_font(6.5, caps=True))
        p.setPen(PALETTE.ink_soft)
        p.drawText(
            QRectF(self.rect()).adjusted(2, 2, -2, -2),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            | int(Qt.TextFlag.TextWordWrap),
            self._text,
        )
        p.end()


class PlaceholderDialog(QDialog):
    """State a frame that has no file yet: a width and a height.

    The numbers are whatever unit the user thinks in — 6 and 7 or 6000 and
    7000 — because only their ratio reaches the solver. This is the door to
    designing a mat board with nothing scanned: the phantom takes a slot,
    the geometry exports cut for it, and the sheet image politely waits.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Placeholder")
        column = QVBoxLayout(self)
        column.setSpacing(10)
        note = CaptionNote("a frame with no file — only the ratio matters")
        column.addWidget(note)
        self.fs_w = FieldSlider("WIDTH", 0.1, 10000.0, 3.0, "", 1, slider=False)
        self.fs_h = FieldSlider("HEIGHT", 0.1, 10000.0, 2.0, "", 1, slider=False)
        column.addWidget(self.fs_w)
        column.addWidget(self.fs_h)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_add = WideButton("Add")
        self.btn_cancel = WideButton("Cancel")
        self.btn_add.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_add, 1)
        row.addWidget(self.btn_cancel, 1)
        column.addLayout(row)
        self.setMinimumWidth(300)

    def frame(self) -> tuple[float, float]:
        return self.fs_w.value(), self.fs_h.value()


class SectionLabel(QWidget):
    """A micro-caps section name with a dotted rule running to the right.

    The right rail stacks a dozen rows; without a stated seam the margins run
    into the mat numbers and the eye has to sort them by vocabulary. The seam
    costs sixteen pixels and says it outright.
    """

    HEIGHT = 16

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        if text != self._text:
            self._text = text
            self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(120, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(60, self.HEIGHT)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        from PySide6.QtCore import QPointF

        p = QPainter(self)
        rect = QRectF(self.rect())
        text_w = QFontMetricsF(mono_font(7, bold=True, caps=True)).horizontalAdvance(
            self._text
        )
        draw_micro_label(
            p,
            QRectF(rect.left(), rect.top(), text_w + 8.0, rect.height()),
            self._text,
            colour=PALETTE.ink_soft,
            align=Qt.AlignmentFlag.AlignLeft,
            bold=True,
        )
        y = math.floor(rect.center().y()) + 0.5
        start = rect.left() + text_w + 10.0
        if rect.right() - start > 8:
            draw_dotted_line(
                p, QPointF(start, y), QPointF(rect.right(), y), colour=PALETTE.rule
            )
        p.end()


class CaptionNote(PaddingNote):
    """A one-line soft caption beside a beginner-mode control: what it does,
    said plainly, because the beginner scheme exists for the person who does
    not yet know what "grouping" means. One line on purpose — a caption that
    wraps starts reading as a paragraph, and the texts are written to fit.

    ``indent`` shifts the line to the right, which is how a caption under a
    FieldSlider starts at the value column instead of under the row's name:
    attached to the number it explains, not opening a new row of its own.
    """

    LINES = 1

    def __init__(
        self, text: str = "", parent: QWidget | None = None, *, indent: float = 0.0
    ) -> None:
        super().__init__(text, parent)
        self._indent = float(indent)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setFont(mono_font(6.5, caps=True))
        p.setPen(PALETTE.ink_soft)
        p.drawText(
            QRectF(self.rect()).adjusted(2 + self._indent, 2, -2, -2),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            | int(Qt.TextFlag.TextWordWrap),
            self._text,
        )
        p.end()


class ArtBox(QWidget):
    """The album-art square: the first source in the current job."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self.setFixedSize(QSize(54, 54))

    def setImage(self, image: QImage | None) -> None:  # noqa: N802 - Qt naming
        self._image = image
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.fillRect(r, PALETTE.surface)
        if self._image is not None and not self._image.isNull():
            scaled = self._image.scaled(
                r.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = r.x() + (r.width() - scaled.width()) // 2
            y = r.y() + (r.height() - scaled.height()) // 2
            p.drawImage(x, y, scaled)
        else:
            p.setFont(mono_font(7, caps=True))
            p.setPen(PALETTE.ink_soft)
            p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), "no\nfile")
        p.setPen(PALETTE.rule)
        p.drawRect(r)
        p.end()


class MainWindow(QMainWindow):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sodachi")
        # Full screen is the design target; this is merely where usable stops.
        self.setMinimumSize(QSize(880, 620))

        # Before any widget exists: every one of them reads the palette while it
        # paints, so restoring afterwards would show the default skin first.
        self._restore_palette()

        self.engine = SodachiEngine(self)
        self._thumbnails: dict[int, QImage] = {}
        self._unit_mode = self.engine.spec.display_units
        self._vips_ok, vips_message = vips_status()
        self._scheme = "standard"
        # The stock warm-white pair the spec ships with; boards showing it (or
        # the pair a previous palette derived) follow the palette, boards the
        # user has recoloured stay theirs.
        self._auto_mat_pair = ("#F6F1EA", "#F6F1EA")
        self._follow_palette_mat_colors()

        central = QWidget(self)
        central.setObjectName("sodachiRoot")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.addWidget(self._build_panel())
        self.setCentralWidget(central)

        self._build_menus()
        # Messages moved up under the version header; an empty grey band at
        # the window's foot would only repeat what the notice line already says.
        self.statusBar().hide()
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(lambda: self.notice.setText(""))

        self._connect()
        self._sync_from_spec()
        self._restore_scheme()
        self._restore_picker_colors()
        self.engine.resolve()

        if not self._vips_ok:
            self._say(f"libvips unavailable: {vips_message}")
        self._sync_export_menu()

    # ----------------------------------------------------------------- build

    def _build_panel(self) -> SkinPanel:
        panel = SkinPanel(title=f"SODACHI {__version__}")
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        # The message line sits directly under the version band, where the eye
        # already is, instead of in a status bar at the far foot of a
        # full-screen window. One line; a long complaint scrolls.
        self.notice = Marquee("")
        column.addWidget(self.notice)
        column.addLayout(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_left_rail())
        body.addWidget(self._build_view_area(), 1)
        body.addWidget(self._build_right_rail())
        column.addLayout(body, 1)

        panel.setLayout(column)
        return panel

    def _build_view_area(self) -> QWidget:
        """The centre: PLAN or STACK of the same solved layout.

        The two panes are readings of one Layout, so they share the centre
        through a QStackedWidget and are both fed from the same engine
        signals. The EXPLODE slider is stack equipment and shows only with
        the stack; the chosen view is remembered beside the palette.
        """
        area = QWidget()
        column = QVBoxLayout(area)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        self.view_bar = QWidget()
        row = QHBoxLayout(self.view_bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        # The tail carries the baseline a deliberate distance past SANDWICH,
        # continuing the line under PHYSICAL/PIXELS without running the whole
        # centre — a rule that ends on purpose instead of at the rail.
        self.view_tabs = TabStrip(VIEW_LABELS, tail=140.0)
        row.addWidget(self.view_tabs)
        row.addStretch(1)
        column.addWidget(self.view_bar)

        self.preview = PreviewPane()
        self.stack_pane = StackPane()
        # Sandwich equipment lives on the sandwich: the slider sits in the
        # pane's bottom-right corner and appears and vanishes with it.
        self.fs_explode = FieldSlider(
            "EXPLODE", 0.0, 1.0, DEFAULT_EXPLODE, "", 2, self.stack_pane
        )
        # Wide enough that its rail reaches the same SLIDER_W as every other
        # row; a lone short slider would read as a different kind of control.
        self.fs_explode.setFixedWidth(260)
        self.stack_pane.installEventFilter(self)
        self._view_stack = QStackedWidget()
        self._view_stack.addWidget(self.preview)
        self._view_stack.addWidget(self.stack_pane)
        column.addWidget(self._view_stack, 1)
        return area

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if watched is self.stack_pane and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
        ):
            self._place_explode()
        return super().eventFilter(watched, event)

    def _place_explode(self) -> None:
        pane = self.stack_pane.rect()
        self.fs_explode.setGeometry(
            pane.right() - 272,
            pane.bottom() - 30,
            260,
            FieldSlider.HEIGHT,
        )

    def _build_header(self) -> QHBoxLayout:
        """The slim strip above the rails: art, title, and the readouts."""
        head = QHBoxLayout()
        head.setSpacing(8)
        self.art = ArtBox()
        head.addWidget(self.art)

        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.title = Marquee("no spec loaded")
        titles.addWidget(self.title)
        readouts = QHBoxLayout()
        readouts.setSpacing(4)
        self.ro_sheet = Readout("406×508mm", min_chars=12)
        self.ro_other = Readout("0×0", min_chars=12)
        self.ro_dpi = Readout("360", min_chars=7)
        for readout in (self.ro_sheet, self.ro_other, self.ro_dpi):
            readouts.addWidget(readout)
        readouts.addStretch(1)
        titles.addLayout(readouts)
        head.addLayout(titles, 1)
        return head

    def _build_left_rail(self) -> QWidget:
        """Mode, sheet identity, layout chips, and the file queue."""
        rail = QWidget()
        rail.setFixedWidth(LEFT_RAIL_W)
        column = QVBoxLayout(rail)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self.tabs = TabStrip(TAB_LABELS)
        column.addWidget(self.tabs)

        self.btn_size = WideButton("Size…")
        column.addWidget(self.btn_size)

        chips = QHBoxLayout()
        chips.setSpacing(4)
        # Landscape leads because the neutral sheet is landscape, and the
        # chip's accent marks off-default: portrait is the departure now.
        self.tg_orient = ToggleChip("ORIENT", ("landscape", "portrait"))
        self.tg_units = ToggleChip("UNITS", ("in", "mm"))
        # Equal stretch on every chip row: the chips share the rail evenly and
        # their edges line up with the rows above and below.
        chips.addWidget(self.tg_orient, 1)
        chips.addWidget(self.tg_units, 1)
        column.addLayout(chips)

        self.layout_chip_row = QWidget()
        self._layout_chips = QHBoxLayout(self.layout_chip_row)
        self._layout_chips.setContentsMargins(0, 0, 0, 0)
        self._layout_chips.setSpacing(4)
        self.tg_type = ToggleChip("TYPE", ("single", "diptych", "triptych", "grid"))
        self.tg_match = ToggleChip("MATCH", SIZE_MATCH_MODES)
        for chip in (self.tg_type, self.tg_match):
            self._layout_chips.addWidget(chip, 1)
        column.addWidget(self.layout_chip_row)

        # Alignment rides a full-width row of its own: the label names what
        # it acts on, and those words cost more width than a three-chip
        # share of the rail affords.
        self.align_chip_row = QWidget()
        self._align_chips = QHBoxLayout(self.align_chip_row)
        self._align_chips.setContentsMargins(0, 0, 0, 0)
        self._align_chips.setSpacing(4)
        self.tg_align = ToggleChip("ALIGN (IMAGES)", ALIGN_MODES)
        self._align_chips.addWidget(self.tg_align, 1)
        column.addWidget(self.align_chip_row)

        # The mat switches share a row of their own: whether there is a board,
        # and whether there are two, are one decision made in one place. The
        # row is a widget so PIXELS can take the whole thing away at once.
        self.mat_chip_row = QWidget()
        self._mat_chips = QHBoxLayout(self.mat_chip_row)
        self._mat_chips.setContentsMargins(0, 0, 0, 0)
        self._mat_chips.setSpacing(4)
        self.tg_mat = ToggleChip("MAT", ("on", "off"))
        self.tg_mat.valueChanged.connect(self._on_mat_chip)
        self.tg_double = ToggleChip("DOUBLE", ("off", "on"))
        self.tg_double.valueChanged.connect(self._on_double_chip)
        self._mat_chips.addWidget(self.tg_mat, 1)
        self._mat_chips.addWidget(self.tg_double, 1)
        column.addWidget(self.mat_chip_row)

        # The beginner arrangement: the same chips, one per line, each with
        # its explanation at its side, so there is never a question of which
        # line belongs to which control. The chips themselves migrate between
        # the compact rows and this grid when the scheme flips.
        self.cap_type = CaptionNote("")
        self.cap_match = CaptionNote("")
        self.cap_align = CaptionNote("")
        self.cap_mat = CaptionNote("")
        self.cap_double = CaptionNote("")
        self.chip_grid = QWidget()
        grid = QVBoxLayout(self.chip_grid)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        self._chip_pairs: list[tuple[ToggleChip, QHBoxLayout, QWidget]] = []
        for chip, note in (
            (self.tg_type, self.cap_type),
            (self.tg_match, self.cap_match),
            (self.tg_align, self.cap_align),
            (self.tg_mat, self.cap_mat),
            (self.tg_double, self.cap_double),
        ):
            # The align chip keeps the width its full label needs; every
            # other chip yields that room to the caption beside it.
            chip.setMinimumWidth(
                chip.sizeHint().width() if chip is self.tg_align else 80
            )
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            # The chip is inserted at index 0 when the beginner scheme takes
            # over; until then the row holds only its caption, centred on the
            # chip it explains.
            row.addWidget(note, 1, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(row_widget)
            self._chip_pairs.append((chip, row, row_widget))
        self.row_mat_beginner = self._chip_pairs[3][2]
        self.row_double_beginner = self._chip_pairs[4][2]
        self.chip_grid.setVisible(False)
        column.addWidget(self.chip_grid)

        self.queue = QueueView()
        column.addWidget(self.queue, 1)
        self.ro_count = Readout("no files", min_chars=22)
        column.addWidget(self.ro_count)

        self.standard_buttons = QWidget()
        pairs = QVBoxLayout(self.standard_buttons)
        pairs.setContentsMargins(0, 0, 0, 0)
        pairs.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_add = WideButton("Add files")
        self.btn_rem = WideButton("Remove file")
        row.addWidget(self.btn_add, 1)
        row.addWidget(self.btn_rem, 1)
        pairs.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_clr = WideButton("Clear all")
        # A frame with no file: the queue can hold the design before the
        # images exist, because a mat board is geometry, not pixels.
        self.btn_phantom = WideButton("Add placeholder…")
        self.btn_phantom.clicked.connect(self._add_placeholder)
        row.addWidget(self.btn_clr, 1)
        row.addWidget(self.btn_phantom, 1)
        pairs.addLayout(row)
        column.addWidget(self.standard_buttons)

        # The beginner scheme's stand-in for the paired buttons: the same acts,
        # one per line, each with a caption saying what it means. The queue
        # collapses with it — the count line stays, the rows go — because the
        # captions need the room and a beginner has a handful of files, not a
        # roll.
        self.beginner_box = self._build_beginner_box()
        self.beginner_box.setVisible(False)
        column.addWidget(self.beginner_box)
        return rail

    def _build_beginner_box(self) -> QWidget:
        box = QWidget()
        rows = QVBoxLayout(box)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(6)

        self.beginner_buttons: list[WideButton] = []
        entries: list[tuple[WideButton, str]] = []
        made = WideButton("Add files")
        made.clicked.connect(self._add_files)
        entries.append((made, "bring images in"))
        made = WideButton("Placeholder…")
        made.clicked.connect(self._add_placeholder)
        entries.append((made, "a frame with no file yet"))
        self.btn_phantom_beginner = made
        made = WideButton("Remove file")
        made.clicked.connect(self._remove_selected)
        entries.append((made, "drop the selected image"))
        made = WideButton("Clear all")
        made.clicked.connect(self.engine.clear)
        entries.append((made, "empty the whole queue"))
        made = WideButton("Size…")
        made.clicked.connect(self._choose_size)
        entries.append((made, "pick the paper size"))

        for button, caption in entries:
            row = QHBoxLayout()
            row.setSpacing(6)
            button.setMinimumWidth(104)
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
            note = CaptionNote(caption)
            row.addWidget(note, 1, Qt.AlignmentFlag.AlignVCenter)
            rows.addLayout(row)
            self.beginner_buttons.append(button)
            if button is getattr(self, "btn_phantom_beginner", None):
                self.cap_phantom_beginner = note
        # Compact at the top; the queue's freed height stays empty air below
        # rather than being dealt out between the rows.
        rows.addStretch(1)
        return box

    # ---------------------------------------------------------------- scheme

    def _restore_scheme(self) -> None:
        stored = QSettings().value(SCHEME_SETTING)
        scheme = stored if stored in CONTROL_SCHEMES else "standard"
        if scheme != "standard":
            self.act_beginner.setChecked(True)  # triggers _on_scheme_toggled
        else:
            self._apply_scheme("standard")

    def _on_scheme_toggled(self, checked: bool) -> None:
        scheme = "beginner" if checked else "standard"
        QSettings().setValue(SCHEME_SETTING, scheme)
        self._apply_scheme(scheme)

    def _apply_scheme(self, scheme: str) -> None:
        beginner = scheme == "beginner"
        self._scheme = scheme
        # The file list stays in beginner, cut down rather than gone: a
        # handful of rows is enough to see what is queued, and the captions
        # need the height it gives back.
        self.queue.setMaximumHeight(110 if beginner else 16777215)
        self.standard_buttons.setVisible(not beginner)
        self.beginner_box.setVisible(beginner)
        self._arrange_chips(beginner)
        self.layout_chip_row.setVisible(not beginner)
        self.align_chip_row.setVisible(not beginner)
        self.chip_grid.setVisible(beginner)
        self._phys_bank.setCaptionsVisible(beginner)
        self._pixel_bank.setCaptionsVisible(beginner)
        for note in self._mat_captions:
            note.setVisible(beginner)
        self.cap_export.setVisible(beginner)
        # Owns the per-tab caption visibility too, so a scheme flip on the
        # PIXELS tab does not resurrect the print captions.
        self._sync_pixel_visibility(self.tabs.currentIndex() == PIXEL_TAB)
        self._refresh_captions()

    def _arrange_chips(self, beginner: bool) -> None:
        """Move the five chips between their two homes.

        The same widgets serve both schemes — a duplicate set would need its
        values chased — so the chips migrate: compact paired rows under
        standard, one per line beside its caption under beginner.
        """
        if beginner:
            for chip, row, _row_widget in self._chip_pairs:
                row.insertWidget(0, chip, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            for index, (chip, _row, _row_widget) in enumerate(self._chip_pairs[:2]):
                self._layout_chips.insertWidget(index, chip, 1)
            self._align_chips.insertWidget(0, self.tg_align, 1)
            for index, (chip, _row, _row_widget) in enumerate(self._chip_pairs[3:]):
                self._mat_chips.insertWidget(index, chip, 1)

    def _refresh_captions(self) -> None:
        """Keep every caption honest: the value's meaning while the control
        is doing something, the reason while it is not.

        Runs after `_sync_inert`, so a control's idle state is already
        decided; the caption is the beginner-legible restatement of the same
        fact the soft ink is painting.
        """
        if self._scheme != "beginner":
            return
        spec = self.engine.spec
        single = spec.layout.type == "single"
        self.cap_type.setText(TYPE_CAPTIONS.get(spec.layout.type, ""))
        self.cap_match.setText(
            IDLE_CAPTIONS["MATCH"]
            if single
            else MATCH_CAPTIONS.get(spec.layout.size_match, "")
        )
        self.cap_align.setText(ALIGN_CAPTIONS.get(spec.layout.align, ""))
        self.cap_mat.setText(MAT_CAPTIONS["on" if spec.mat.enabled else "off"])
        self.cap_double.setText(
            IDLE_CAPTIONS["DOUBLE"]
            if not spec.mat.enabled
            else DOUBLE_CAPTIONS["on" if spec.mat.double else "off"]
        )

        rule = spec.margins.bottom_mm
        mode = rule if isinstance(rule, str) else "fixed"
        self._phys_bank.setCaptionText("BOTTOM", BOTTOM_FIELD_CAPTIONS[mode])
        self._phys_bank.setCaptionText(
            "GUTTER",
            IDLE_CAPTIONS["GUTTER"] if single else FIELD_CAPTIONS["GUTTER"],
        )
        self._phys_bank.setCaptionText(
            "RATIO",
            FIELD_CAPTIONS["RATIO"] if mode == "optical" else IDLE_CAPTIONS["RATIO"],
        )
        self._phys_bank.setCaptionText(
            "DPI",
            IDLE_CAPTIONS["DPI_PIXEL_SHEET"]
            if spec.sheet.given_in_px
            else FIELD_CAPTIONS["DPI"],
        )

        for name, note in self._mat_caption_by_name.items():
            if not spec.mat.enabled:
                note.setText(IDLE_CAPTIONS["MAT_OFF_FIELDS"])
            elif name == "INNER" and not spec.mat.double:
                note.setText(IDLE_CAPTIONS["INNER"])
            else:
                note.setText(FIELD_CAPTIONS.get(name, ""))

    def _build_right_rail(self) -> QWidget:
        """The typed margins for the active mode, and the export below them."""
        rail = QWidget()
        self._right_rail = rail
        rail.setFixedWidth(RIGHT_RAIL_W)
        column = QVBoxLayout(rail)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        self._phys_bank = self._make_physical_bank(self._unit_mode)
        self._pixel_bank = FieldBank()
        for name, low, high in PIXEL_FIELDS:
            self._pixel_bank.addField(
                name, low, high, low, "px", 0,
                caption=PIXEL_MARGIN_CAPTIONS.get(name, FIELD_CAPTIONS.get(name)),
            )

        pixel_page = QWidget()
        pixel_column = QVBoxLayout(pixel_page)
        pixel_column.setContentsMargins(0, 0, 0, 0)
        pixel_column.setSpacing(8)
        pixel_column.addWidget(self._pixel_bank)
        self.padding_note = PaddingNote("padding: add images to measure the fit")
        pixel_column.addWidget(self.padding_note)
        # A border recipe worth typing twice is worth keeping: the menu holds
        # the saved screen layouts, and rebuilds itself each time it opens so
        # it never shows a layout deleted a moment ago.
        self.btn_layouts = MenuButton("Layouts")
        self.layouts_menu = QMenu()
        self.layouts_menu.aboutToShow.connect(self._fill_layouts_menu)
        self.btn_layouts.setMenu(self.layouts_menu)
        pixel_column.addWidget(self.btn_layouts)
        pixel_column.addStretch(1)

        # The seams: each block of rows opens with its name, so the margins,
        # the board and the output read as three matters rather than one run.
        self.lbl_fields = SectionLabel("MARGINS")
        column.addWidget(self.lbl_fields)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._phys_bank)
        self._stack.addWidget(pixel_page)
        self._stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        column.addWidget(self._stack)

        # Held so a unit change can swap the group in place; see
        # `_rebuild_mat_group`.
        self._right_column = column
        self.lbl_mat = SectionLabel("MAT WINDOW")
        column.addWidget(self.lbl_mat)
        self._mat_group = self._build_mat_group(self._unit_mode)
        column.addWidget(self._mat_group)

        self.lbl_output = SectionLabel("OUTPUT")
        column.addWidget(self.lbl_output)
        self.btn_export = MenuButton("Export")
        column.addWidget(self.btn_export)
        # On SCREEN the button is the act itself: press, get the bordered
        # image — no menu, because the tab has exactly one output. The BATCH
        # chip beside it widens the act to every queued file.
        self.screen_export_row = QWidget()
        screen_row = QHBoxLayout(self.screen_export_row)
        screen_row.setContentsMargins(0, 0, 0, 0)
        screen_row.setSpacing(6)
        self.btn_export_screen = WideButton("Export")
        self.btn_export_screen.clicked.connect(self._export_screen)
        self.tg_batch = ToggleChip("BATCH", ("off", "on"), horizontal=True)
        self.tg_batch.valueChanged.connect(lambda _v: self._sync_batch_dest())
        screen_row.addWidget(self.btn_export_screen, 1)
        screen_row.addWidget(self.tg_batch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.screen_export_row.setVisible(False)
        column.addWidget(self.screen_export_row)
        # A batch needs a destination stated up front, so it lives in the GUI
        # rather than ambushing the press with a dialog; a single export asks
        # in the ordinary save dialog, exactly as PHYSICAL does.
        self.btn_export_to = WideButton("Export to…")
        self.btn_export_to.clicked.connect(self._pick_batch_dir)
        self.btn_export_to.setVisible(False)
        column.addWidget(self.btn_export_to)
        self.cap_export = CaptionNote(EXPORT_CAPTION)
        self.cap_export.setVisible(False)
        column.addWidget(self.cap_export)
        column.addStretch(1)
        self._size_right_rail()
        return rail

    def _size_right_rail(self) -> None:
        """Width the rail so every row fits a full SLIDER_W of rail.

        Measured from the rows actually built, because the resolved font
        decides every advance: a hard-coded width that fits Consolas clips
        under a fallback family, and equal rails are the point. RIGHT_RAIL_W
        is the floor, not the truth.
        """
        needed = float(RIGHT_RAIL_W)
        rows = [(field, None) for field in self._phys_bank.fields()]
        rows += [(field, None) for field in self._pixel_bank.fields()]
        rows += [(field, None) for field, _dotted in self._mat_fields]
        for field, _ in rows:
            width = field._fixed_w() + FieldSlider.GAP + FieldSlider.SLIDER_W
            if field.name() == "BOTTOM" and field is self._phys_bank.field("BOTTOM"):
                width += self.tg_bottom_mode.sizeHint().width() + 6.0
            needed = max(needed, width + 2.0)
        self._right_rail.setFixedWidth(int(needed))

    def _build_mat_group(self, units: str) -> QWidget:
        """The mat controls between the margins and Export, print work only.

        Reads top-down as the board does: the window's grip and reveal, the
        board's colour, the INNER band, and — only once DOUBLE is on, over on
        the left rail beside MAT — the second board's own colour. That colour
        row is the whole of what a second mat adds beyond its band.
        """
        group = QWidget()
        column = QVBoxLayout(group)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)

        decimals = INCH_DECIMALS if units == "in" else 1
        self._mat_fields: list[tuple[FieldSlider, str]] = []
        self._mat_captions: list[CaptionNote] = []
        self._mat_caption_by_name: dict[str, CaptionNote] = {}
        # Sized for the other unit's extremes as well — see the physical
        # bank — so the whole rail keeps one width across a unit flip.
        other = "mm" if units == "in" else "in"
        other_dec = INCH_DECIMALS if other == "in" else 1
        for (name, dotted, low, high), (_o, _od, olo, ohi) in zip(
            MAT_FIELDS[units], MAT_FIELDS[other]
        ):
            field = FieldSlider(
                name, low, high, low, units, decimals,
                also_fit=(f"{olo:.{other_dec}f}", f"{ohi:.{other_dec}f}"),
            )
            field.valueChanged.connect(lambda v, d=dotted: self._on_mat_field(d, v))
            self._mat_fields.append((field, dotted))
        self.fs_mat_overlap = self._mat_fields[0][0]
        self.fs_mat_reveal = self._mat_fields[1][0]
        self.fs_mat_inner = self._mat_fields[2][0]

        def note_for(name: str) -> CaptionNote:
            note = CaptionNote(
                FIELD_CAPTIONS.get(name, ""), indent=FieldSlider.NAME_W + 6.0
            )
            note.setVisible(self._scheme == "beginner")
            self._mat_captions.append(note)
            self._mat_caption_by_name[name] = note
            return note

        column.addWidget(self.fs_mat_overlap)
        column.addWidget(note_for("OVERLAP"))
        column.addWidget(self.fs_mat_reveal)
        column.addWidget(note_for("REVEAL"))
        self.sw_mat_color = ColorSwatch("COLOR", self.engine.spec.mat.color)
        self.sw_mat_color.clicked.connect(lambda: self._pick_mat_color("mat.color"))
        column.addWidget(self.sw_mat_color)
        column.addWidget(self.fs_mat_inner)
        column.addWidget(note_for("INNER"))

        self.double_group = QWidget()
        double_column = QVBoxLayout(self.double_group)
        double_column.setContentsMargins(0, 0, 0, 0)
        double_column.setSpacing(8)
        self.sw_inner_color = ColorSwatch("INNER", self.engine.spec.mat.inner_color)
        self.sw_inner_color.clicked.connect(lambda: self._pick_mat_color("mat.inner_color"))
        double_column.addWidget(self.sw_inner_color)
        column.addWidget(self.double_group)
        return group

    def _fill_layouts_menu(self) -> None:
        """Rebuild the pixel-layouts menu from what is saved right now."""
        self.layouts_menu.clear()
        save = self.layouts_menu.addAction("Save current…")
        save.triggered.connect(self._save_pixel_layout)
        names = self.engine.pixel_layouts()
        if names:
            self.layouts_menu.addSeparator()
            for name in names:
                action = self.layouts_menu.addAction(name)
                action.triggered.connect(
                    lambda _checked=False, n=name: self._apply_pixel_layout(n)
                )
            remove = self.layouts_menu.addMenu("Delete")
            for name in names:
                action = remove.addAction(name)
                action.triggered.connect(
                    lambda _checked=False, n=name: self.engine.delete_pixel_layout(n)
                )

    def _save_pixel_layout(self) -> None:
        name, accepted = QInputDialog.getText(
            self, "Save layout", "Name for this pixel layout:"
        )
        if accepted and name.strip():
            if self.engine.save_pixel_layout(name.strip()):
                self._say(f"layout saved: {name.strip()}")

    def _apply_pixel_layout(self, name: str) -> None:
        if self.engine.apply_pixel_layout(name):
            self._say(f"layout: {name}")

    def _pick_mat_color(self, dotted: str) -> None:
        """One picker for both boards; the swatch clicked names the field.

        The Qt-drawn dialog rather than the native one, so the app stylesheet
        reaches its buttons; the platform picker draws its own chrome and sat
        white in the middle of the skin.
        """
        swatch = self.sw_mat_color if dotted == "mat.color" else self.sw_inner_color
        self._touch_notice(swatch)
        chosen = QColorDialog.getColor(
            swatch.color(),
            self,
            "Board colour",
            QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        # Whatever the outcome, keep the picker's custom row: a colour mixed
        # and parked there is kept even when this particular pick was
        # cancelled.
        self._save_picker_colors()
        if chosen.isValid():
            self._set(dotted, chosen.name().upper())

    def _restore_picker_colors(self) -> None:
        stored = QSettings().value(PICKER_COLORS_SETTING)
        if not isinstance(stored, str) or not stored:
            return
        try:
            hexes = json.loads(stored)
        except ValueError:
            return
        if not isinstance(hexes, list):
            return
        for i, value in enumerate(hexes[: QColorDialog.customCount()]):
            colour = QColor(str(value))
            if colour.isValid():
                QColorDialog.setCustomColor(i, colour)

    def _save_picker_colors(self) -> None:
        hexes = [
            QColorDialog.customColor(i).name().upper()
            for i in range(QColorDialog.customCount())
        ]
        QSettings().setValue(PICKER_COLORS_SETTING, json.dumps(hexes))

    def _rebuild_mat_group(self) -> None:
        """Swap in a mat group measured in the spec's display unit.

        Same reasoning as `_rebuild_physical_bank`: a FieldSlider's suffix and
        step cannot be restated after construction.
        """
        old = self._mat_group
        index = self._right_column.indexOf(old)
        self._mat_group = self._build_mat_group(self._unit_mode)
        self._right_column.insertWidget(index, self._mat_group)
        self._right_column.removeWidget(old)
        old.deleteLater()
        self._apply_scheme(self._scheme)
        self._size_right_rail()

    def _make_physical_bank(self, units: str) -> FieldBank:
        bank = FieldBank()
        for name, _field, low_mm, high_mm, suffix, decimals in PHYSICAL_FIELDS:
            also_fit: tuple[str, ...] = ()
            if name in LENGTH_FIELDS:
                # The recess is sized for the other unit's extremes too, so
                # the row the flip rebuilds is the width of the row it
                # replaces and the window holds still.
                other = "mm" if units == "in" else "in"
                other_dec = INCH_DECIMALS if other == "in" else decimals
                olo, ohi = _display_range(low_mm, high_mm, other)
                also_fit = (f"{olo:.{other_dec}f}", f"{ohi:.{other_dec}f}")
                low, high = _display_range(low_mm, high_mm, units)
                suffix = units
                decimals = INCH_DECIMALS if units == "in" else decimals
            else:
                low, high = low_mm, high_mm
            if name == "BOTTOM":
                # The bottom margin is the one with a mode: FIXED takes the
                # typed number, OPTICAL and CENTER derive it and the field
                # turns into a report. The chip leads the row and carries the
                # row's name itself — "BOTTOM FIXED 4.00" left to right — so
                # the field hides its own label instead of echoing it, and
                # the freed width is what lets this row's rail match its
                # neighbours'.
                self.tg_bottom_mode = ToggleChip("BOTTOM", BOTTOM_MODES, horizontal=True)
                self.tg_bottom_mode.valueChanged.connect(self._on_bottom_mode)
                bank.addField(
                    name, low, high, low, suffix, decimals,
                    companion=self.tg_bottom_mode,
                    companion_left=True,
                    caption=BOTTOM_FIELD_CAPTIONS["fixed"],
                    name_hidden=True,
                    also_fit=also_fit,
                    cause_reserve=RESOLVED_CAUSE_RESERVE,
                )
            elif name == "DPI":
                # Typed only: real DPI values are a handful of printer-native
                # stops, and sweeping 72-720 continuously is motion nobody
                # needs. Shown only once a file exists, because until then
                # nothing the program can produce consults it.
                self.fs_dpi = bank.addField(
                    name, low, high, 360.0, suffix, decimals,
                    slider=False, caption=FIELD_CAPTIONS.get(name),
                )
            else:
                bank.addField(
                    name, low, high, low, suffix, decimals,
                    caption=FIELD_CAPTIONS.get(name),
                    also_fit=also_fit,
                    cause_reserve=(
                        RESOLVED_CAUSE_RESERVE if name in RESOLVING_FIELDS else ""
                    ),
                )
        return bank

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.act_open_spec = self._act(
            file_menu, "&Open spec…", QKeySequence.StandardKey.Open, self._load_spec
        )
        self.act_save_spec = self._act(
            file_menu, "&Save spec as…", QKeySequence.StandardKey.SaveAs, self._save_spec
        )
        # No "Add images" here: the rail's button is the one door for files,
        # and a menu twin was a second thing to keep in step for nothing.
        file_menu.addSeparator()
        self._act(file_menu, "&Quit", QKeySequence.StandardKey.Quit, self.close)

        # One vocabulary for the one act: these say what they produce. The
        # composed-sheet render is SCREEN's act alone — people who print
        # their own artwork already have a printing workflow, and Sodachi
        # hands that workflow numbers (the check table), not pixels. The
        # action survives for SCREEN's Ctrl+R only.
        self.act_export_image = QAction("&Bordered image…", self)
        self.act_export_image.setShortcut("Ctrl+R")
        self.act_export_image.setStatusTip(
            "The bordered image at its exact pixel size."
        )
        self.act_export_image.triggered.connect(self._export_image)
        self.addAction(self.act_export_image)

        self.act_mat = QAction("&Mat guide…", self)
        self.act_mat.setShortcut("Ctrl+M")
        self.act_mat.setStatusTip(
            "A printed PDF guide, mirrored for cutting the mat by hand from the back."
        )
        self.act_mat.triggered.connect(self._mat)

        self.act_cut = QAction("&Cutter file…", self)
        self.act_cut.setShortcut("Ctrl+E")
        self.act_cut.setStatusTip(
            "True-size cut paths for a computerised mat cutter: DXF, SVG or CSV."
        )
        self.act_cut.triggered.connect(self._cut)

        self.act_check = QAction("Chec&k table…", self)
        self.act_check.setShortcut("Ctrl+K")
        self.act_check.setStatusTip("The solved geometry as a table, in mm or in.")
        self.act_check.triggered.connect(self._show_check)

        # No Export menu in the menu bar, and no Requirements here either:
        # the popup is the tab's outputs, and Requirements is planning, with
        # its own button below Export. The actions are also added to the
        # window itself, because a QAction living only in a popup menu would
        # lose its keyboard shortcut.
        button_menu = QMenu()
        button_menu.setToolTipsVisible(True)
        for action in (
            self.act_mat,
            self.act_cut,
            self.act_check,
        ):
            action.setToolTip(action.statusTip())
            button_menu.addAction(action)
            self.addAction(action)
        self.btn_export.setMenu(button_menu)

        # The fit report's face is the live padding line on the SCREEN rail —
        # clicking it opens the full report — so the action holds only the
        # shortcut and sits in no menu.
        self.act_fit = QAction("&Fit / padding report…", self)
        self.act_fit.setShortcut("Ctrl+F")
        self.act_fit.triggered.connect(self._show_fit)
        self.addAction(self.act_fit)

        # Options rather than View: palettes and the beginner scheme are how
        # the window looks, but the export customisation beside them is not,
        # and one honest name covers both.
        options_menu = self.menuBar().addMenu("&Options")
        self.act_beginner = QAction("&Beginner controls", self)
        self.act_beginner.setCheckable(True)
        self.act_beginner.setStatusTip(
            "Swap the file list and paired buttons for one captioned button "
            "per act. Remembered: close in it, open in it."
        )
        self.act_beginner.toggled.connect(self._on_scheme_toggled)
        options_menu.addAction(self.act_beginner)
        self.palette_menu = options_menu.addMenu("&Palette")
        self._build_palette_menu(self.palette_menu)
        view_menu = options_menu
        # PySide gives Python ownership of a menu reached through
        # `QAction.menu()`, so a caller that looks a menu up and then lets the
        # action fall out of scope takes the menu down with it — which is what
        # walking the menu bar to find the palette entry looks like. Holding the
        # two actions that open these menus keeps the branch alive to be read.
        self._menu_refs = (view_menu.menuAction(), self.palette_menu.menuAction())

        # Requirements is configuration, not output: applying rewrites the
        # paper, margins and mat to fit a plan, so it stands in the bar under
        # its own name — one click, no submenu — rather than riding the
        # export rail it used to sit beneath.
        self.act_requirements = QAction("Configure mat from requirement", self)
        self.act_requirements.triggered.connect(self._print_from_opening)
        self.menuBar().addAction(self.act_requirements)

        help_menu = self.menuBar().addMenu("&Help")
        self._act(help_menu, "&About Sodachi", None, self._about)

    def _build_palette_menu(self, menu: QMenu) -> None:
        """One checkable entry per registered palette, the one in force checked."""
        self.palette_group = QActionGroup(self)
        self.palette_group.setExclusive(True)
        self.palette_actions: dict[str, QAction] = {}

        in_force = current_palette_name()
        for name in available_palettes():
            action = QAction(name, self)
            action.setCheckable(True)
            action.setChecked(name == in_force)
            # The default argument binds the name at each iteration; a closure
            # over the loop variable would give every entry the last palette.
            action.triggered.connect(lambda _checked=False, n=name: self._choose_palette(n))
            self.palette_group.addAction(action)
            menu.addAction(action)
            self.palette_actions[name] = action

    def _restore_palette(self) -> None:
        """Put the remembered palette in force, if it is still a palette."""
        stored = QSettings().value(PALETTE_SETTING)
        if not isinstance(stored, str) or not stored:
            return
        if stored not in available_palettes():
            # Renamed or dropped since it was stored. The default is already in
            # force, so name what was lost and open the window anyway.
            print(
                f"sodachi: no palette named {stored!r}; using {current_palette_name()}",
                file=sys.stderr,
            )
            return
        apply_palette_to(QApplication.instance(), stored)

    def _choose_palette(self, name: str) -> None:
        apply_palette_to(QApplication.instance(), name)
        QSettings().setValue(PALETTE_SETTING, name)
        # A menu click has already checked the action; a programmatic call has
        # not, and the menu is the record of what is in force.
        action = self.palette_actions.get(name)
        if action is not None:
            action.setChecked(True)
        self._follow_palette_mat_colors()
        self._say(f"palette: {name}")

    def _follow_palette_mat_colors(self) -> None:
        """Boards the user has not recoloured take the palette's own pair.

        The top board wears the palette's paper and the lower board its
        accent, so the sandwich and the plan open already dressed for the
        skin. The follow stops the moment either colour is the user's: a
        chosen board is never repainted under them.
        """
        derived = (PALETTE.paper.name().upper(), PALETTE.accent.name().upper())
        mat = self.engine.spec.mat
        current = (mat.color, mat.inner_color)
        if current == derived:
            self._auto_mat_pair = derived
            return
        if current in (("#F6F1EA", "#F6F1EA"), self._auto_mat_pair):
            if self.engine.set_spec_values(
                {"mat.color": derived[0], "mat.inner_color": derived[1]}
            ):
                self._auto_mat_pair = derived

    def _act(self, menu, text: str, shortcut, slot) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _connect(self) -> None:
        self.tabs.currentChanged.connect(self._on_tab)
        self.view_tabs.currentChanged.connect(self._on_view_tab)
        self.fs_explode.valueChanged.connect(self.stack_pane.set_explode)
        self._phys_bank.valueChanged.connect(self._on_physical_field)
        self._pixel_bank.valueChanged.connect(self._on_pixel_field)
        self.tg_type.valueChanged.connect(lambda v: self._set("layout.type", v))
        self.tg_match.valueChanged.connect(lambda v: self._set("layout.size_match", v))
        self.tg_align.valueChanged.connect(self._on_align_chip)
        self.tg_orient.valueChanged.connect(self._on_orientation)
        self.tg_units.valueChanged.connect(lambda v: self._set("display_units", v))

        self.btn_size.clicked.connect(self._choose_size)
        self.btn_add.clicked.connect(self._add_files)
        self.btn_rem.clicked.connect(self._remove_selected)
        self.btn_clr.clicked.connect(self.engine.clear)
        self.padding_note.clicked.connect(self._show_fit)

        self.queue.selectionChanged.connect(self.engine.set_current_row)
        self.queue.rowMoved.connect(self.engine.move)
        self.engine.layoutChanged.connect(self._on_layout)
        self.engine.queueChanged.connect(self._on_queue)
        self.engine.specChanged.connect(self._sync_from_spec)
        self.engine.problem.connect(self._say)
        self.engine.progress.connect(self._on_progress)
        self.engine.finished.connect(self._on_finished)
        self.engine.busyChanged.connect(self._on_busy)

    @property
    def fields(self) -> FieldBank:
        """The margin bank the active tab points at."""
        return (
            self._phys_bank
            if self._stack.currentIndex() == PHYSICAL_TAB
            else self._pixel_bank
        )

    def _rebuild_physical_bank(self) -> None:
        """Swap in a bank measured in the spec's display unit.

        A field carries its unit in its range, its suffix and the size of its
        step, and the last two cannot be restated after construction, so a spec
        that arrives in inches gets a new bank rather than an edited one.
        """
        old = self._phys_bank
        self._phys_bank = self._make_physical_bank(self._unit_mode)
        self._phys_bank.valueChanged.connect(self._on_physical_field)
        self._stack.insertWidget(PHYSICAL_TAB, self._phys_bank)
        self._stack.removeWidget(old)
        old.deleteLater()
        # A fresh bank starts with its captions hidden; the scheme decides.
        self._apply_scheme(self._scheme)
        self._size_right_rail()

    # ------------------------------------------------------------------ mode

    def _on_tab(self, index: int) -> None:
        """Switch mode: each tab is its own whole spec, and the engine swaps.

        The engine parks the leaving spec and restores the arriving one, so
        nothing set on PHYSICAL ever moves a number on PIXELS or back. Only a
        target's first visit derives a spec from the other side, and that is
        where a screen spec loses the mat (board is print equipment, and the
        model refuses it under a screen target) and has align and the bottom
        margin cleared — align falls to its centred default, and the spec's
        own screen rule centres the bottom.
        """
        if index == PIXEL_TAB:
            # The sandwich is a print object; a screen sheet has no boards.
            self.view_tabs.setCurrentIndex(PLAN_VIEW)
        self.view_bar.setVisible(index == PHYSICAL_TAB)

        target = TAB_TARGETS[index]
        if self.engine.spec.target == target:
            self._sync_from_spec()
            return

        changes: dict[str, Any] = {}
        clear = ["layout.align", "margins.bottom_mm"]
        if target == "screen":
            changes["mat.enabled"] = False
        if not self.engine.set_target(target, changes=changes, clear=clear):
            self._sync_from_spec()  # puts the tab back on the target in force

    # ------------------------------------------------------------------ view

    def _on_view_tab(self, index: int) -> None:
        """Show the flat plan or the exploded sandwich.

        Deliberately not remembered: the app opens to one neutral screen, and
        the sandwich is a thing you go and look at rather than a place you live.
        """
        self._view_stack.setCurrentIndex(index)

    def _feed_stack_mat(self) -> None:
        """Restate the mat for the stack, double-mat fields included.

        The flat pane's three-value feed stays as it was — its wash never
        shows a second board — but the stack draws real boards, so it takes
        the spec's double, inner reveal and the two board colours as well.
        """
        enabled, overlap_mm, reveal_mm = self.engine.mat_settings()
        mat = self.engine.spec.mat
        self.stack_pane.set_mat(
            enabled,
            overlap_mm,
            reveal_mm,
            double=mat.double,
            inner_reveal_mm=mat.inner_reveal_mm,
            color=mat.color,
            inner_color=mat.inner_color,
        )

    # ------------------------------------------------------------------ sync

    def _sync_from_spec(self) -> None:
        """Push spec values into the controls without echoing back.

        Blocking signals matters here: a spec load would otherwise walk every
        field, and each one would re-validate and re-solve on the way past.
        """
        spec = self.engine.spec
        if spec.display_units != self._unit_mode:
            self._unit_mode = spec.display_units
            self._rebuild_physical_bank()
            self._rebuild_mat_group()

        tab = PIXEL_TAB if spec.target == "screen" else PHYSICAL_TAB
        blocked = self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(tab)
        self.tabs.blockSignals(blocked)
        self._stack.setCurrentIndex(tab)
        self._sync_stack_height()
        # A mat is print equipment; the group leaves with the PHYSICAL rail
        # rather than sitting disabled under pixel fields it has nothing to say
        # about. Its seam goes with it, and the fields' seam renames to what
        # the rows actually are on this tab.
        self._mat_group.setVisible(tab == PHYSICAL_TAB)
        self.lbl_mat.setVisible(tab == PHYSICAL_TAB)
        self.lbl_fields.setText("MARGINS" if tab == PHYSICAL_TAB else "BORDERS")
        # The preview's callouts speak the same language as the rail beside
        # them: pixels on the screen tab, the display unit on the print tab.
        # Both views, so switching PLAN/STACK never changes the unit spoken.
        units = "px" if tab == PIXEL_TAB else spec.display_units
        self.preview.set_units(units)
        self.stack_pane.set_units(units)

        self._sync_physical_bank(spec)
        self._sync_pixel_bank(spec)
        self._sync_mat_group(spec)

        for chip, value in (
            (self.tg_type, spec.layout.type),
            (self.tg_match, spec.layout.size_match),
            (self.tg_align, spec.layout.align),
            (self.tg_units, spec.display_units),
        ):
            blocked = chip.blockSignals(True)
            chip.setValue(value)
            chip.blockSignals(blocked)
        self._sync_orientation()

        # A TYPE change regroups the queue, and the sheet on screen must
        # follow the row the user selected rather than snapping to whatever
        # lands first — the bug this fixes showed a leading placeholder's
        # aspect the moment types were cycled over real images.
        selected = self.queue.currentIndex()
        if selected >= 0:
            self.engine.set_current_row(selected)

        self._sync_inert(spec)
        self._sync_allowed(spec)
        self._refresh_count_line()
        self._sync_readouts(spec)
        self._sync_pixel_visibility(tab == PIXEL_TAB)
        self._sync_export_menu()
        self._refresh_padding_note()
        self._refresh_captions()
        name = self.engine.spec_path.name if self.engine.spec_path else "unsaved spec"
        queued = ", ".join(i.name for i in self.engine.queue[:4]) or "no files"
        self.title.setText(f"{name} · {spec.layout.type} · {queued}")

    def _sync_stack_height(self) -> None:
        """The stack is as tall as its visible page, not its tallest one.

        A QStackedWidget's size hint is the maximum over every page, which
        opened a dead band between the margin rows and the mat group whenever
        the other tab's page was the taller. Hidden pages get an Ignored
        policy so only the page on screen states a height.
        """
        current = self._stack.currentIndex()
        for i in range(self._stack.count()):
            page = self._stack.widget(i)
            if i == current:
                page.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
                )
            else:
                page.setSizePolicy(
                    QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
                )
        self._stack.updateGeometry()

    def _sync_pixel_visibility(self, on_pixels: bool) -> None:
        """Take print-only equipment off screen while PIXELS is the mode.

        Hidden rather than disabled, because a greyed control still asks to be
        understood, and the pixel workflow is meant to be the simple one: an
        image, a border, a size. UNITS goes too — every pixel field speaks px
        whichever display unit the print spec prefers.
        """
        beginner = self._scheme == "beginner"
        self.tg_units.setVisible(not on_pixels)
        self.btn_size.setVisible(not on_pixels)
        # A placeholder is print design — a frame to cut board around. The
        # screen tab borders files that exist, so the act leaves with the mat.
        self.btn_phantom.setVisible(not on_pixels)
        self.btn_phantom_beginner.setVisible(beginner and not on_pixels)
        self.cap_phantom_beginner.setVisible(beginner and not on_pixels)
        self.mat_chip_row.setVisible(not beginner and not on_pixels)
        self.row_mat_beginner.setVisible(beginner and not on_pixels)
        self.row_double_beginner.setVisible(beginner and not on_pixels)
        self.act_requirements.setVisible(not on_pixels)
        # SCREEN's export is the one-press act with its BATCH chip; the menu
        # button with the print vocabulary belongs to PHYSICAL.
        self.btn_export.setVisible(not on_pixels)
        self.screen_export_row.setVisible(on_pixels)
        self._sync_batch_dest()
        self.cap_export.setText(
            "writes the bordered image — BATCH: the whole queue, to EXPORT TO"
            if on_pixels
            else EXPORT_CAPTION
        )
        # Each tab offers only its own exports by construction now: the
        # button menu with the print vocabulary leaves with PHYSICAL's
        # button, and the fit report rides the SCREEN rail's padding line.
        self.act_fit.setEnabled(on_pixels)

    def _sync_inert(self, spec: Spec) -> None:
        """Say which controls are currently doing nothing, and why.

        Inert is painted soft but stays editable; touching one shows its
        reason in the status bar. The reasons are the layout's own logic:
        MATCH and GUTTER compare and separate multiple images, RATIO shapes a
        bottom margin only the OPTICAL rule is deriving.
        """
        single = spec.layout.type == "single"
        multi_reason = (
            "this compares images against each other; a single frame has nothing to compare"
            if single
            else None
        )
        self.tg_match.set_inert(multi_reason)
        gutter_reason = (
            "the gutter separates images; a single frame has no gap to keep"
            if single
            else None
        )
        for bank in (self._phys_bank, self._pixel_bank):
            gutter = bank.field("GUTTER")
            if gutter is not None:
                gutter.set_inert(gutter_reason)

        bottom_rule = spec.margins.bottom_mm
        ratio = self._phys_bank.field("RATIO")
        if ratio is not None:
            ratio.set_inert(
                None
                if bottom_rule == "optical"
                else "RATIO weights the bottom only while BOTTOM mode is OPTICAL"
            )

    def _sync_allowed(self, spec: Spec) -> None:
        """Restate every rail's legal stretch from the other fields' values.

        This is the live face of the cross-field rules, all of them:

        - the sheet: two sides across its width, top plus bottom down its
          height — with a derived bottom folded in, so under OPTICAL the top's
          ceiling is ``sheet_h / (1 + ratio)`` because raising TOP raises the
          bottom with it, and RATIO gets the mirror-image bound;
        - the margin budget: every margin holds ``reveal + overlap`` and the
          inner band when doubled, which is one pool three mat fields and
          three margins all draw from;
        - the window itself: at reveal zero the overlap closes in from the
          slot's edges, so it can never reach half the smallest slot;
        - adjacency, the three-way coupling with the gutter: grown openings
          from neighbouring slots must keep an overlap's worth of board
          between them, which puts a floor under GUTTER and a matching
          ceiling on REVEAL, OVERLAP and INNER whenever more than one frame
          shares the sheet.
        """
        size = spec.sheet.size
        sheet_w, sheet_h = size.width_mm, size.height_mm
        top = spec.margins.top_mm
        sides = spec.margins.sides_mm
        bottom = self._bottom_mm(spec)
        rule = spec.margins.bottom_mm
        mode = rule if isinstance(rule, str) else "fixed"
        mat = spec.mat
        gutter_mm = spec.layout.gutter_mm
        inner_part = mat.inner_reveal_mm if mat.double else 0.0
        # The margin floor exists only where the opening grows outward into
        # the margin — a reveal, or the double mat's band. At reveal zero the
        # overlap grips by cutting inward from the slot, the margins owe the
        # board nothing, and zero is honestly legal: the model accepts it, so
        # the rail must not claim otherwise.
        floor = (
            mat.reveal_mm + mat.window_overlap_mm + inner_part
            if mat.enabled and (mat.reveal_mm > 0 or mat.double)
            else 0.0
        )
        layout = self.engine.layout
        multi = layout is not None and len(layout.slots) > 1
        margin_lo = floor
        if multi and gutter_mm > 0:
            margin_lo = max(margin_lo, gutter_mm)

        def show(name: str, lo_mm: float, hi_mm: float) -> None:
            field = self._phys_bank.field(name)
            if field is not None:
                field.set_allowed(
                    _display_length(max(lo_mm, 0.0), self._unit_mode),
                    _display_length(max(hi_mm, 0.0), self._unit_mode),
                )

        # A derived bottom rises with the top, so the ceiling accounts for
        # both ends of the sheet moving at once.
        if mode == "optical":
            top_hi = sheet_h / (1.0 + spec.margins.optical_ratio)
        elif mode == "center":
            top_hi = sheet_h / 2.0
        else:
            top_hi = sheet_h - bottom
        show("TOP", margin_lo, top_hi)
        show("SIDES", margin_lo, sheet_w / 2.0)
        show("BOTTOM", margin_lo, sheet_h - top)

        ratio = self._phys_bank.field("RATIO")
        if ratio is not None:
            if mode == "optical" and top > 0:
                ratio.set_allowed(1.0, sheet_h / top - 1.0)
            else:
                ratio.set_allowed(None, None)

        smallest = min(top, sides, bottom)
        # The expansion of each board's openings past the slot edge; negative
        # at reveal zero, where the bottom board closes in by the overlap.
        grow_bottom = mat.reveal_mm if mat.reveal_mm > 0 else -mat.window_overlap_mm
        grow_top = grow_bottom + inner_part

        gutter = self._phys_bank.field("GUTTER")
        if gutter is not None:
            if multi:
                gutter_lo = 0.0
                if mat.enabled:
                    if mat.reveal_mm > 0:
                        gutter_lo = 2.0 * grow_bottom + mat.window_overlap_mm
                    if mat.double:
                        gutter_lo = max(
                            gutter_lo, 2.0 * grow_top + mat.window_overlap_mm
                        )
                gutter.set_allowed(
                    _display_length(max(gutter_lo, 0.0), self._unit_mode),
                    _display_length(smallest, self._unit_mode),
                )
            else:
                gutter.set_allowed(None, None)

        min_slot_mm = (
            min(min(s.rect.width_mm, s.rect.height_mm) for s in layout.slots)
            if layout is not None and layout.slots
            else None
        )
        for field, dotted in self._mat_fields:
            if not mat.enabled:
                field.set_allowed(None, None)
                continue
            if dotted == "mat.window_overlap_mm":
                hi_mm = smallest - mat.reveal_mm - inner_part
                if mat.reveal_mm <= 0 and min_slot_mm is not None:
                    # Closing in from both sides: half the smallest slot
                    # dimension and the window is shut.
                    hi_mm = min(hi_mm, min_slot_mm / 2.0)
                if multi and mat.reveal_mm > 0:
                    hi_mm = min(hi_mm, gutter_mm - 2.0 * (mat.reveal_mm + inner_part))
            elif dotted == "mat.reveal_mm":
                hi_mm = smallest - mat.window_overlap_mm - inner_part
                if multi:
                    hi_mm = min(
                        hi_mm,
                        (gutter_mm - mat.window_overlap_mm) / 2.0 - inner_part,
                    )
            else:
                hi_mm = smallest - mat.window_overlap_mm - mat.reveal_mm
                if multi:
                    hi_mm = min(
                        hi_mm,
                        (gutter_mm - mat.window_overlap_mm) / 2.0 - grow_bottom,
                    )
            field.set_allowed(
                field.minimum(), _display_length(max(hi_mm, 0.0), self._unit_mode)
            )

        dpi = spec.sheet.dpi
        width_px, height_px = spec.sheet.to_sheet().px_size()
        top_px = mm_to_px(top, dpi)
        sides_px = mm_to_px(sides, dpi)
        bottom_px = mm_to_px(bottom, dpi)
        pixel_allowed = {
            "BORDER": (0.0, float(self._max_border_px())),
            "TOP": (0.0, float(max(0, height_px - bottom_px - 1))),
            "SIDES": (0.0, float(max(0, (width_px - 1) // 2))),
            "BOTTOM": (0.0, float(max(0, height_px - top_px - 1))),
            "WIDTH": (float(2 * sides_px + 1), None),
            "HEIGHT": (float(top_px + bottom_px + 1), None),
        }
        for name, (lo, hi) in pixel_allowed.items():
            field = self._pixel_bank.field(name)
            if field is not None:
                field.set_allowed(lo, hi)

    def _touch_notice(self, widget) -> None:
        reason = widget.inert_reason()
        if reason:
            self._say(reason)

    def _sync_orientation(self) -> None:
        """Point the chip at the sheet actually solved; a square sheet is left
        showing whichever way it was last, because the chip has no third face."""
        orientation = self.engine.orientation()
        if orientation in self.tg_orient.values():
            blocked = self.tg_orient.blockSignals(True)
            self.tg_orient.setValue(orientation)
            self.tg_orient.blockSignals(blocked)

    def _sync_physical_bank(self, spec: Spec) -> None:
        bottom_rule = spec.margins.bottom_mm
        mode = bottom_rule if isinstance(bottom_rule, str) else "fixed"
        values = {
            "TOP": _display_length(spec.margins.top_mm, self._unit_mode),
            "SIDES": _display_length(spec.margins.sides_mm, self._unit_mode),
            "BOTTOM": _display_length(self._bottom_mm(spec), self._unit_mode),
            "GUTTER": _display_length(spec.layout.gutter_mm, self._unit_mode),
            "RATIO": spec.margins.optical_ratio,
        }
        for name, value in values.items():
            field = self._phys_bank.field(name)
            if field is None:
                continue
            blocked = field.blockSignals(True)
            field.setValue(value)
            field.blockSignals(blocked)

        bottom = self._phys_bank.field("BOTTOM")
        if bottom is not None:
            # Under a derived rule the row reports; under FIXED it takes input.
            bottom.setReadOnly(mode != "fixed")
        blocked = self.tg_bottom_mode.blockSignals(True)
        self.tg_bottom_mode.setValue(mode)
        self.tg_bottom_mode.blockSignals(blocked)

        blocked = self.fs_dpi.blockSignals(True)
        self.fs_dpi.setValue(spec.sheet.dpi)
        self.fs_dpi.blockSignals(blocked)
        # A pixel-specified sheet is converted at a fixed DPI, so the field
        # would only ever be able to produce an error. Hidden with no files,
        # because until pixels can be produced nothing consults it.
        self.fs_dpi.setEnabled(not spec.sheet.given_in_px)
        self.fs_dpi.setVisible(bool(self.engine.queue))
        # The DPI caption follows the row it explains.
        self._phys_bank.setCaptionsVisible(self._scheme == "beginner")

    def _sync_mat_group(self, spec: Spec) -> None:
        enabled = spec.mat.enabled
        blocked = self.tg_mat.blockSignals(True)
        self.tg_mat.setValue("on" if enabled else "off")
        self.tg_mat.blockSignals(blocked)

        blocked = self.tg_double.blockSignals(True)
        self.tg_double.setValue("on" if spec.mat.double else "off")
        self.tg_double.blockSignals(blocked)
        # Inert rather than disabled while MAT is off: clicking DOUBLE on is
        # read as wanting both boards, and _on_double_chip switches MAT on
        # with it rather than refusing the obvious intent.
        self.tg_double.set_inert(
            None if enabled else "a second board needs MAT on; DOUBLE brings it with it"
        )

        values_mm = {
            "mat.window_overlap_mm": spec.mat.window_overlap_mm,
            "mat.reveal_mm": spec.mat.reveal_mm,
            "mat.inner_reveal_mm": spec.mat.inner_reveal_mm,
        }
        for field, dotted in self._mat_fields:
            blocked = field.blockSignals(True)
            field.setValue(_display_length(values_mm[dotted], self._unit_mode))
            field.blockSignals(blocked)
            # The numbers only mean anything while there is a board to cut.
            field.setEnabled(enabled)
        self.fs_mat_inner.set_inert(
            None
            if spec.mat.double
            else "INNER is the second board's reveal; it does nothing until DOUBLE is on"
        )
        self.sw_mat_color.setColor(spec.mat.color)
        self.sw_mat_color.setEnabled(enabled)
        self.sw_mat_color.set_inert(
            None if enabled else "the board's colour matters once MAT is on"
        )
        self.sw_inner_color.setColor(spec.mat.inner_color)
        # The second board's own row exists only while there is a second board.
        self.double_group.setVisible(spec.mat.double)

    def _bottom_mm(self, spec: Spec) -> float:
        """The bottom margin as a number, however the spec states it."""
        bottom = spec.margins.numeric_bottom_mm
        if bottom is not None:
            return bottom
        if self.engine.layout is not None:
            return self.engine.layout.margins.bottom_mm
        if spec.margins.bottom_mm == "optical":
            return spec.margins.top_mm * spec.margins.optical_ratio
        return spec.margins.top_mm

    def _sync_pixel_bank(self, spec: Spec) -> None:
        dpi = spec.sheet.dpi
        width_px, height_px = spec.sheet.to_sheet().px_size()
        values = {
            # BORDER reports the top count; when the three differ, the rows
            # beneath it carry the truth per side.
            "BORDER": float(mm_to_px(spec.margins.top_mm, dpi)),
            "TOP": float(mm_to_px(spec.margins.top_mm, dpi)),
            "SIDES": float(mm_to_px(spec.margins.sides_mm, dpi)),
            "BOTTOM": float(mm_to_px(self._bottom_mm(spec), dpi)),
            "WIDTH": float(width_px),
            "HEIGHT": float(height_px),
        }
        for name, low, high in PIXEL_FIELDS:
            field = self._pixel_bank.field(name)
            if field is None:
                continue
            blocked = field.blockSignals(True)
            _fit_range(field, low, high, values[name])
            field.setValue(values[name])
            field.blockSignals(blocked)

    def _sync_readouts(self, spec: Spec) -> None:
        """The tab decides which unit gets the large readout, not which facts.

        Both sizes are always on screen; the tab only says which one is the one
        being worked in.
        """
        size = spec.sheet.size
        if self._unit_mode == "in":
            physical = f"{mm_to_inch(size.width_mm):.2f}×{mm_to_inch(size.height_mm):.2f}in"
        else:
            physical = f"{size.width_mm:.0f}×{size.height_mm:.0f}mm"
        width_px, height_px = spec.sheet.to_sheet().px_size()
        pixels = f"{width_px}×{height_px}px"

        on_pixels = self.tabs.currentIndex() == PIXEL_TAB
        if on_pixels:
            self.ro_sheet.setText(pixels)
        else:
            self.ro_sheet.setText(physical)
        # Pixels are screen facts and DPI is a print fact, so each shows only
        # on its own tab — and DPI only once a file exists, because until then
        # nothing the program can produce consults it. The second readout is
        # the *other* statement of the same sheet, not the same one twice.
        self.ro_other.setText(physical)
        self.ro_other.setVisible(on_pixels)
        self.ro_dpi.setText(f"{spec.sheet.dpi:g}dpi")
        self.ro_dpi.setVisible(not on_pixels and bool(self.engine.queue))

    def _sync_export_menu(self) -> None:
        """Enabled state and, where disabled, the reason as the explanation.

        The sheet image genuinely needs pixels; a mat guide and a cutter file
        need only geometry, so they stay live over the placeholder layout and
        are refused only while a write is running or the spec has no mat.
        """
        busy = self.engine.busy
        image_tip = "The bordered image at its exact pixel size."
        on_screen = self.engine.spec.target == "screen"
        if not on_screen:
            # PHYSICAL hands a printing workflow numbers, not pixels: the
            # check table is the placement spec, and the render is SCREEN's.
            self.act_export_image.setEnabled(False)
        elif not self._vips_ok:
            self.act_export_image.setEnabled(False)
            image_tip += " Disabled: libvips is unavailable, so nothing can be composed."
        elif not self.engine.can_export_image():
            self.act_export_image.setEnabled(False)
            image_tip += (
                " Disabled: needs loaded, probed images; the other exports "
                "work from placeholder geometry."
            )
        else:
            self.act_export_image.setEnabled(not busy)
        self.act_export_image.setStatusTip(image_tip)
        self.act_export_image.setToolTip(image_tip)

        mat_on = self.engine.spec.mat.enabled
        mat_tip = "A printed PDF guide, mirrored for cutting the mat by hand from the back."
        cut_tip = "True-size cut paths for a computerised mat cutter: DXF, SVG or CSV."
        for action in (self.act_mat, self.act_cut):
            action.setEnabled(mat_on and not busy)
        if not mat_on:
            reason = " Disabled: MAT is off in this spec."
            mat_tip += reason
            cut_tip += reason
        self.act_mat.setStatusTip(mat_tip)
        self.act_cut.setStatusTip(cut_tip)
        self.act_mat.setToolTip(mat_tip)
        self.act_cut.setToolTip(cut_tip)

    def _refresh_padding_note(self) -> None:
        """Keep the PIXELS rail's one-line padding statement current.

        Only computed while the pixel rail is showing: `fit_plan` reads a
        preset off disk, and a hidden line is refreshed the moment the tab
        brings it back.
        """
        if self._stack.currentIndex() != PIXEL_TAB:
            return
        job = self.engine.current_job()
        if job is None or not job.ready:
            self.padding_note.setText("padding: add images to measure the fit")
            return
        result = self.engine.fit_plan()
        if result is None:
            self.padding_note.setText("padding: nothing to measure")
            return
        plan, _report = result
        # The first line of the plan's own report is the decision itself.
        self.padding_note.setText(plan.report().splitlines()[0])

    def _on_layout(self, layout) -> None:
        self.preview.set_layout_result(layout)
        self.stack_pane.set_layout_result(layout)
        # The solved margins, restated beside what was asked for: a side
        # margin is a minimum, so the rail says 3.00 and, when the sheet says
        # 3.25, the row shows the arrow the moment it happens — and names
        # what moved it. SIDES only ever widens by centring the content;
        # TOP and BOTTOM absorb the height the frames' shapes leave spare
        # ("fit"), except that a derived bottom's weighting hands TOP its
        # share by RATIO or by an even split.
        rule = self.engine.spec.margins.bottom_mm
        top_cause = (
            {"optical": "ratio", "center": "centred"}.get(rule, FIT_CAUSE)
            if isinstance(rule, str)
            else FIT_CAUSE
        )
        resolved = {
            "TOP": (None if layout is None else layout.margins.top_mm, top_cause),
            "SIDES": (None if layout is None else layout.margins.left_mm, "centred"),
            "BOTTOM": (None if layout is None else layout.margins.bottom_mm, FIT_CAUSE),
        }
        for name, (value_mm, cause) in resolved.items():
            field = self._phys_bank.field(name)
            if field is not None and not field.isReadOnly():
                field.set_resolved(
                    None if value_mm is None
                    else _display_length(value_mm, self._unit_mode),
                    cause,
                )
        # Every spec edit ends here via resolve, so a live overlap or reveal
        # drag repaints the board overlay in the same breath as the layout —
        # boards in their chosen colours on the plan as well as the sandwich.
        mat = self.engine.spec.mat
        self.preview.set_mat(
            *self.engine.mat_settings(),
            color=mat.color,
            double=mat.double,
            inner_reveal_mm=mat.inner_reveal_mm,
            inner_color=mat.inner_color,
        )
        self._feed_stack_mat()
        # The solve is what resolves a derived bottom margin and the sheet's
        # orientation, so both are restated once it lands — and the allowed
        # bands and the per-file dpi readings with them.
        self._sync_orientation()
        self._sync_allowed(self.engine.spec)
        self._refresh_queue_rows()
        self._sync_pixel_bank(self.engine.spec)
        self._sync_export_menu()
        self._refresh_padding_note()

    def _effective_dpi_by_item(self) -> dict[int, float]:
        """Each previewed file's real sampling density, keyed by identity.

        The sheet has one DPI; the images do not. A 1500px frame in a 10in
        slot prints at 150, whatever the export writes at, and that is a fact
        about the file worth reading off its row. Only the current job's items
        are stated — other sheets' slots are not solved — and only under a
        print target, where density means anything.
        """
        out: dict[int, float] = {}
        if self.engine.spec.target != "print":
            return out
        job = self.engine.current_job()
        layout = self.engine.layout
        if job is None or layout is None:
            return out
        for index, item in enumerate(job.items):
            if item.phantom or not item.probed or index >= len(layout.slots):
                continue
            width_mm = layout.slots[index].rect.width_mm
            if width_mm > 0 and item.width_px:
                out[id(item)] = item.width_px / (width_mm / 25.4)
        return out

    def _job_index_by_item(self) -> dict[int, int]:
        """Which sheet each queued item is on, keyed by identity.

        By identity rather than by path because a queue may hold the same file
        twice, and by asking `jobs` rather than dividing the row number because
        a manifest groups whatever it likes in whatever order.
        """
        by_item: dict[int, int] = {}
        for job in self.engine.jobs():
            for item in job.items:
                by_item[id(item)] = job.index
        return by_item

    def _refresh_queue_rows(self) -> None:
        """Restate the list's rows; cheap enough to follow every re-solve,
        because the effective dpi in the value column moves with the slots."""
        by_item = self._job_index_by_item()
        effective_dpi = self._effective_dpi_by_item()
        seen_names: dict[str, int] = {}
        rows = []
        for item in self.engine.queue:
            index = by_item.get(id(item), 0)
            marker = JOB_MARKERS[index % 2]
            # Two files may honestly share a name — different folders, same
            # roll numbering — and the list must still tell them apart.
            count = seen_names.get(item.name, 0) + 1
            seen_names[item.name] = count
            shown = item.name if count == 1 else f"{item.name} ({count})"
            value = item.value_text()
            dpi = effective_dpi.get(id(item))
            if dpi is not None:
                value = f"{value} · {dpi:.0f}dpi"
            rows.append((f"{marker} {shown}", value, bool(item.error)))
        self.queue.setRows(rows)

    def _on_queue(self) -> None:
        items = self.engine.queue
        self._refresh_queue_rows()
        # Something is always selected once anything is queued, and the
        # default is the first real image, not a phantom that happens to sit
        # at the head: the sheet on screen should be the user's picture
        # unless they chose otherwise. A phantom-only queue selects the
        # phantom, and that selection yields the moment a real file arrives —
        # a selection on an actual image is never stolen.
        if items:
            current = self.queue.currentIndex()
            first_real = next(
                (i for i, item in enumerate(items) if not item.phantom), None
            )
            if current < 0:
                self.queue.setCurrentIndex(first_real if first_real is not None else 0)
            elif (
                first_real is not None
                and 0 <= current < len(items)
                and items[current].phantom
            ):
                self.queue.setCurrentIndex(first_real)

        self._thumbnails = {}
        job = self.engine.current_job()
        if job is not None:
            for index, item in enumerate(job.items):
                if item.thumbnail_png:
                    image = QImage()
                    if image.loadFromData(item.thumbnail_png, "PNG"):
                        self._thumbnails[index] = image
        self.preview.set_thumbnails(self._thumbnails)
        self.stack_pane.set_thumbnails(self._thumbnails)
        self.art.setImage(self._thumbnails.get(0))

        self._refresh_count_line()
        self._sync_from_spec()

    def _refresh_count_line(self) -> None:
        """A sheet is one output: one solved composition, however many frames
        share it. Under single that is one per file, so restating the same
        number twice would be noise; the count only splits when a layout
        actually groups — which is why this follows spec edits, not just the
        queue."""
        items = self.engine.queue
        sheets = len(self.engine.jobs())
        if not items:
            self.ro_count.setText("no files")
        elif sheets == len(items):
            self.ro_count.setText(f"{len(items)} file{'s' if len(items) > 1 else ''}")
        else:
            self.ro_count.setText(
                f"{len(items)} files / {sheets} sheet{'s' if sheets > 1 else ''}"
            )

    def _on_progress(self, message: str, value: float) -> None:
        # The notice line is where a running job reports; there is no bar to
        # fill, so the percentage is said in words.
        self._say(f"{message} — {round(value * 100)}%")

    def _on_finished(self, result) -> None:
        if result is None:
            self._say("export failed")
            return
        path = getattr(result, "path", result)
        self._say(f"wrote {path}")

    def _on_busy(self, busy: bool) -> None:
        self._sync_export_menu()

    def _say(self, message: str) -> None:
        self.notice.setText(message)
        self._notice_timer.start(12000)

    # --------------------------------------------------------------- actions

    def _set(self, dotted: str, value: Any) -> None:
        if not self.engine.set_spec_value(dotted, value):
            self._sync_from_spec()

    def _set_many(self, changes: Mapping[str, Any], clear: Sequence[str] = ()) -> None:
        if not self.engine.set_spec_values(changes, clear=clear):
            self._sync_from_spec()

    def _on_bottom_mode(self, mode: str) -> None:
        self._touch_notice(self.tg_bottom_mode)
        if mode == "fixed":
            # Pin whatever the rule was producing, so the switch is seamless
            # and the number is immediately there to be edited.
            self._set("margins.bottom_mm", self._bottom_mm(self.engine.spec))
        else:
            self._set("margins.bottom_mm", mode)

    def _on_align_chip(self, value: str) -> None:
        self._set("layout.align", value)

    def _on_orientation(self, face: str) -> None:
        if face != self.engine.orientation():
            if not self.engine.swap_orientation():
                self._sync_from_spec()

    def _on_mat_chip(self, face: str) -> None:
        if not self.engine.set_spec_value("mat.enabled", face == "on"):
            self._sync_from_spec()

    def _on_double_chip(self, face: str) -> None:
        """DOUBLE next to MAT, and the two agree: a second board implies a
        first, so DOUBLE=on over a switched-off mat turns both on in one
        edit rather than lecturing about the order of clicks."""
        on = face == "on"
        if on and not self.engine.spec.mat.enabled:
            if self.engine.set_spec_values({"mat.enabled": True, "mat.double": True}):
                self._say("double mat: MAT switched on with it")
            else:
                self._sync_from_spec()
            return
        self._set("mat.double", on)

    def _on_mat_field(self, dotted: str, value: float) -> None:
        field = next((f for f, d in self._mat_fields if d == dotted), None)
        if field is not None:
            self._touch_notice(field)
        if not self.engine.set_spec_value(dotted, _length_mm(value, self._unit_mode)):
            if field is not None:
                field.flash_invalid()
            self._sync_from_spec()

    def _on_physical_field(self, name: str, value: float) -> None:
        field = self._phys_bank.field(name)
        if field is not None:
            self._touch_notice(field)
        for field_name, dotted, *_rest in PHYSICAL_FIELDS:
            if field_name != name:
                continue
            if name in LENGTH_FIELDS:
                value = _length_mm(value, self._unit_mode)
            if not self.engine.set_spec_value(dotted, value):
                # The refusal is worth seeing where it happened, not only on
                # the notice line: the value snaps back and pulses accent.
                if field is not None:
                    field.flash_invalid()
                self._sync_from_spec()
            return

    def _on_pixel_field(self, name: str, value: float) -> None:
        px = int(round(value))
        dpi = self.engine.spec.sheet.dpi
        if name == "BORDER":
            # The headline case: one count, all three margins. Clamped first,
            # so a border bigger than the sheet becomes the biggest border the
            # sheet can hold instead of a refusal.
            px = min(px, self._max_border_px())
            mm = px_to_mm(px, dpi)
            self._set_many(
                {
                    "margins.top_mm": mm,
                    "margins.sides_mm": mm,
                    "margins.bottom_mm": mm,
                }
            )
        elif name == "TOP":
            self._set_pixel_margin("margins.top_mm", px)
        elif name == "SIDES":
            self._set_pixel_margin("margins.sides_mm", px)
        elif name == "BOTTOM":
            self._set_pixel_margin("margins.bottom_mm", px)
        elif name == "WIDTH":
            self._set_sheet_axis_px("width", px)
        elif name == "HEIGHT":
            self._set_sheet_axis_px("height", px)

    def _max_border_px(self) -> int:
        """The largest uniform border the sheet can hold, one pixel spare."""
        width_px, height_px = self.engine.spec.sheet.to_sheet().px_size()
        return max(0, (min(width_px, height_px) - 1) // 2)

    def _set_pixel_margin(self, dotted: str, px: int) -> None:
        """A per-side count, clamped into the sheet rather than refused.

        The pixel workflow is meant to be the simple one, so a margin the
        sheet cannot hold becomes the largest one it can — said on the notice
        line — instead of snapping back and asking the user to do arithmetic.
        """
        spec = self.engine.spec
        dpi = spec.sheet.dpi
        width_px, height_px = spec.sheet.to_sheet().px_size()
        if dotted == "margins.sides_mm":
            ceiling = max(0, (width_px - 1) // 2)
        else:
            other_mm = (
                self._bottom_mm(spec)
                if dotted == "margins.top_mm"
                else spec.margins.top_mm
            )
            ceiling = max(0, height_px - int(mm_to_px(other_mm, dpi)) - 1)
        clamped = min(px, ceiling)
        if not self.engine.set_spec_value(dotted, px_to_mm(clamped, dpi)):
            self._sync_from_spec()
            return
        if clamped != px:
            self._say(f"{px}px does not fit this canvas; clamped to {clamped}px")

    def _set_sheet_axis_px(self, axis: str, px: int) -> None:
        """Resize one side of the sheet, given a number of pixels.

        A sheet already given in pixels keeps that form, so the renderer goes on
        landing on the requested count exactly. Any other form becomes
        millimetres at the sheet's own DPI — the size has changed, so a standard
        name or a round inch measurement is no longer true of it and has to go
        in the same edit, or the spec would briefly claim two sizes at once.
        """
        spec = self.engine.spec
        if spec.sheet.given_in_px:
            self._set(f"sheet.{axis}_px", px)
            return
        size = spec.sheet.size
        millimetres = {"width": size.width_mm, "height": size.height_mm}
        millimetres[axis] = px_to_mm(px, spec.sheet.dpi)
        self._set_many(
            {
                "sheet.width_mm": millimetres["width"],
                "sheet.height_mm": millimetres["height"],
            },
            clear=("sheet.standard", "sheet.width_in", "sheet.height_in"),
        )

    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add images", "", IMAGE_FILTER)
        if paths:
            self.engine.add_paths(paths)

    def _add_placeholder(self) -> None:
        dialog = PlaceholderDialog(parent=self)
        answered = dialog.exec()
        width, height = dialog.frame()
        dialog.deleteLater()
        if answered == QDialog.DialogCode.Accepted:
            if self.engine.add_placeholder(width, height):
                self._say(f"placeholder queued: {width:g}:{height:g}")

    def _remove_selected(self) -> None:
        row = self.queue.currentIndex()
        if row >= 0:
            self.engine.remove([row])

    def _first_row_of(self, job: Job) -> int | None:
        """The queue row a sheet starts on. None if none of it is still queued."""
        wanted = {id(item) for item in job.items}
        for row, item in enumerate(self.engine.queue):
            if id(item) in wanted:
                return row
        return None

    # ------------------------------------------------------------------ spec

    def _load_spec(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open spec", "", SPEC_FILTER)
        if path and self.engine.load_spec_file(path):
            self._say(f"loaded {Path(path).name}")

    def _save_spec(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save spec", "layout.yaml", SPEC_FILTER)
        if path and self.engine.save_spec_file(path):
            self._say(f"saved {Path(path).name}")

    # ---------------------------------------------------------------- export

    def _export_image(self) -> None:
        suffix = {"tiff": ".tif", "png": ".png", "jpeg": ".jpg"}[self.engine.spec.output.format]
        path, _ = QFileDialog.getSaveFileName(self, "Export image", f"sheet{suffix}")
        if path:
            self.engine.render_async(path)

    def _batch_dir(self) -> str | None:
        stored = QSettings().value(BATCH_DIR_SETTING)
        if isinstance(stored, str) and stored and Path(stored).is_dir():
            return stored
        return None

    def _sync_batch_dest(self) -> None:
        """The EXPORT TO row exists only while BATCH does, and its face says
        where the batch will land."""
        on_screen = self.tabs.currentIndex() == PIXEL_TAB
        batch = self.tg_batch.value() == "on"
        self.btn_export_to.setVisible(on_screen and batch)
        chosen = self._batch_dir()
        self.btn_export_to.setText(
            f"Export to: {Path(chosen).name or chosen}" if chosen else "Export to…"
        )

    def _pick_batch_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Export to", self._batch_dir() or ""
        )
        if directory:
            QSettings().setValue(BATCH_DIR_SETTING, directory)
            self._sync_batch_dest()

    def _export_screen(self) -> None:
        """SCREEN's one button: the bordered image, to a place the user picks.

        A single export asks in the ordinary save dialog, exactly as PHYSICAL
        does. A batch writes into the EXPORT TO folder — asked for once,
        shown on the rail, remembered.
        """
        if self.tg_batch.value() != "on":
            self._export_image()
            return
        directory = self._batch_dir()
        if directory is None:
            self._pick_batch_dir()
            directory = self._batch_dir()
        if directory is not None:
            self.engine.render_batch_async(directory)

    def _mat(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Mat guide", "mat-guide.pdf", "PDF (*.pdf)")
        if path:
            self.engine.mat_async(path)

    def _cut(self) -> None:
        """Format and mirroring first, then a path, then the write off-thread.

        The options come before the path because the format decides the
        extension, and a save dialog that has already been told which one it is
        can default to it instead of asking the user to type it.
        """
        options = CutOptionsDialog(parent=self)
        answered = options.exec()
        fmt = options.selected_format()
        mirror = options.mirror()
        # Parented to the window, so it outlives this frame unless it is told
        # not to. Every dialog opened here ends the same way.
        options.deleteLater()
        if answered != QDialog.DialogCode.Accepted:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Cutter file", f"cut.{fmt}", CUT_FILTERS[fmt]
        )
        if not path:
            return
        # A dialog that did not append the filter's extension would otherwise
        # hand the engine a path it has to refuse.
        if not Path(path).suffix:
            path = f"{path}.{fmt}"
        self.engine.cut_async(path, mirror=mirror)

    def _print_from_opening(self) -> None:
        """Derive a print from an owned mat's opening, and watch it land live.

        APPLY leaves the dialog open on purpose: the plan goes into the spec,
        the preview takes it behind the dialog, and the user can keep adjusting
        the opening until the sheet reads right. The first APPLY of a session
        warns — it rewrites the sheet, margins and mat, and a design built on
        the rails should not be overwritten by mistake — but only the first:
        a warning on every press would kill the adjust-and-look loop the
        dialog exists for.
        """
        dialog = PrintFromOpeningDialog(units=self.engine.spec.display_units, parent=self)
        confirmed = {"done": False}

        def apply_with_warning(plan) -> None:
            if not confirmed["done"]:
                answer = QMessageBox.question(
                    self,
                    "Apply requirements",
                    "Applying rewrites the paper, the margins and the mat to "
                    "fit this plan — the mat design currently on the rails is "
                    "replaced.\n\nApply it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                confirmed["done"] = True
            self._apply_print_plan(plan)

        dialog.apply_requested.connect(apply_with_warning)
        dialog.exec()
        dialog.deleteLater()

    def _apply_print_plan(self, plan) -> None:
        if not self.engine.apply_print_plan(plan):
            return  # apply_print_plan has already said why
        size = self.engine.spec.sheet.size
        if self._unit_mode == "in":
            stated = f"{mm_to_inch(size.width_mm):.2f}×{mm_to_inch(size.height_mm):.2f}in"
        else:
            stated = f"{size.width_mm:.1f}×{size.height_mm:.1f}mm"
        self._say(f"plan applied: paper {stated}, mat on")

    def _show_check(self) -> None:
        """The solved geometry in full, in whichever unit the spec asks for.

        The dialog switches units but does no arithmetic, so the refill comes
        back here: only the engine holds the layout the numbers are read off.
        """
        units = self.engine.spec.display_units
        rows = self.engine.check_rows(units)
        if not rows:
            return  # check_rows has already said why
        dialog = CheckDialog(rows, units=units, parent=self)
        dialog.unitsChanged.connect(lambda chosen: self._refill_check(dialog, chosen))
        dialog.exec()
        dialog.deleteLater()

    def _refill_check(self, dialog: CheckDialog, units: str) -> None:
        rows = self.engine.check_rows(units)
        if rows:
            dialog.set_rows(rows)

    def _show_fit(self) -> None:
        result = self.engine.fit_plan()
        if result is None:
            return  # fit_plan has already said why
        _plan, report = result
        dialog = FitReportDialog(report, parent=self)
        dialog.exec()
        dialog.deleteLater()

    def _choose_size(self) -> None:
        """Name the sheet from `sodachi.sizes`, and let the layout re-solve."""
        sizes = self.engine.standard_sizes()
        if not sizes:  # pragma: no cover - the table ships with the package
            self._say("no standard sizes are registered")
            return
        dialog = StandardSizeDialog(sizes, units=self._unit_mode, parent=self)
        current = self.engine.spec.sheet.standard
        if current:
            try:
                dialog.set_selected(current)
            except KeyError:
                pass  # A size the spec names that this build no longer lists.
        answered = dialog.exec()
        name = dialog.selected()
        dialog.deleteLater()
        if answered != QDialog.DialogCode.Accepted:
            return
        custom = dialog.custom()
        if custom is not None:
            # A typed size beats the list: the fields are the claim.
            width, height = custom
            suffix = "in" if self._unit_mode == "in" else "mm"
            self._set_many(
                {
                    f"sheet.width_{suffix}": width,
                    f"sheet.height_{suffix}": height,
                },
                clear=(
                    "sheet.standard",
                    "sheet.width_mm" if suffix == "in" else "sheet.width_in",
                    "sheet.height_mm" if suffix == "in" else "sheet.height_in",
                    "sheet.width_px",
                    "sheet.height_px",
                ),
            )
            self._say(f"{self.engine.surface_word()}: {width:g} × {height:g} {suffix}")
            return
        if name and self.engine.apply_standard_size(name):
            self._say(f"{self.engine.surface_word()}: {name}")

    def _about(self) -> None:
        self._say(
            f"Sodachi {__version__} — millimetre-native layout for images. "
            "Mats are cut from the back; print guides at 100%."
        )


__all__ = ["MainWindow", "ArtBox", "CaptionNote", "FieldBank", "PaddingNote"]
