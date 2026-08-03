"""The skin: the palette in force, and the primitives every widget paints with.

Sodachi's window is hand-painted rather than stylesheeted. That is a deliberate
cost. A stylesheet cannot draw the dotted rule set a few pixels inside every
panel outline, and that rule is the whole look; putting the primitives here
means a change to the ink or to the dot rhythm lands everywhere at once.

The palette is switchable at runtime, which is why `PALETTE` is a proxy rather
than a `Palette`. About a hundred sites across `sodachi.gui` do
``from sodachi.gui.theme import PALETTE`` and then read ``PALETTE.paper`` inside
a paint event; rebinding the module global would leave every one of those
holding the palette that was in force at import. The proxy forwards each
attribute read to whichever `Palette` `set_palette` last installed, so the reads
stay as they were and a switch is visible on the next repaint.

Crispness rule for anything drawn here: rounded corners get antialiasing, 1px
borders and dotted rules get drawn on half-pixel centres with antialiasing off,
so they stay one pixel wide at 100% and do not smear at 125% or 150%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen

from sodachi.gui.palettes import (
    DEFAULT_PALETTE,
    ROLES,
    PaletteSpec,
    list_palettes,
    palette_named,
    parse_hex,
)

MONO_FAMILIES = ["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New"]


@dataclass(frozen=True, slots=True)
class Palette:
    """The ink set.

    Every name is a role rather than a colour, so the whole skin can be
    repainted by changing this block and nothing else. A palette supplies fewer
    values than there are roles, so some roles share one: ``ink`` and
    ``ink_strong`` are the same navy, separated by weight rather than by hue,
    and ``shadow`` is the fill. They stay distinct names so a later palette with
    more range can pull them apart without touching a widget.

    ``accent`` is the one entry that should stay rare on screen: it marks the
    active state, a setting moved off its default, and the optical alignment
    line, and it means nothing if it is everywhere.

    The defaults are Riddle, the shipped skin, so a `Palette` is constructible
    on its own. They are documentation rather than the source of truth: the
    running palette is solved from six hexes by :mod:`sodachi.gui.palettes`, and
    that is the copy a switch installs. ``paper`` and ``surface`` are therefore
    the *lifted* values -- criterion 6 tints Riddle's #F4E6E2 panel to 0.863
    luminance -- and not the raw hexes in `NAMED_PALETTES`.
    """

    paper: QColor = field(default_factory=lambda: QColor("#F7EDEA"))
    surface: QColor = field(default_factory=lambda: QColor("#F6F5EE"))
    fill: QColor = field(default_factory=lambda: QColor("#E3DBE7"))
    rule: QColor = field(default_factory=lambda: QColor("#A296AF"))
    # The same navy as `ink`, thinned. The palette's mid tone is right for a
    # rule and wrong for type: mauve on the panel is 2.4:1, and a 6pt letter-
    # spaced label at that contrast reads as a smudge. Thinning the ink instead
    # lands at about 3.6:1 over any of the three surfaces, which is where the
    # secondary labels were tuned, and keeps the hierarchy one ink deep.
    ink_soft: QColor = field(default_factory=lambda: QColor(43, 50, 82, 155))
    ink: QColor = field(default_factory=lambda: QColor("#2B3252"))
    ink_strong: QColor = field(default_factory=lambda: QColor("#2B3252"))
    white: QColor = field(default_factory=lambda: QColor("#FFFFFF"))
    shadow: QColor = field(default_factory=lambda: QColor("#E3DBE7"))
    accent: QColor = field(default_factory=lambda: QColor("#77835F"))


def _qcolor(value: str) -> QColor:
    """A spec hex to a QColor.

    Goes through `parse_hex` rather than handing the string to QColor, because a
    thinned role is written ``#RRGGBBAA`` in CSS order and Qt reads an eight-
    digit string as ``#AARRGGBB`` — the same characters, a different colour.
    """
    r, g, b, a = parse_hex(value)
    return QColor(r, g, b, a)


def palette_from_spec(spec: PaletteSpec) -> Palette:
    """The ten solved roles as QColor."""
    return Palette(**{role: _qcolor(spec.role(role)) for role in ROLES})


_solved: dict[str, Palette] = {}
_current_name: str = DEFAULT_PALETTE
_current: Palette = palette_from_spec(palette_named(DEFAULT_PALETTE))
_solved[_current_name] = _current


class _CurrentPalette:
    """Reads forwarded to the `Palette` in force at the moment of the read.

    Deliberately not a `Palette`, so there is no chance of a stale copy: it
    holds no colours of its own and cannot be one palette behind.
    """

    __slots__ = ()

    def __getattr__(self, role: str) -> QColor:
        try:
            colour = getattr(_current, role)
        except AttributeError:
            raise AttributeError(
                f"no such palette role: {role!r}; roles are {', '.join(ROLES)}"
            ) from None
        # A copy per read. A QColor handed out is trivially mutable, and a widget
        # that thins one for a highlight would otherwise thin it for every later
        # paint in every other widget.
        return QColor(colour)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"<PALETTE {_current_name!r}>"


PALETTE = _CurrentPalette()


def _invalidate() -> None:
    """Drop anything cached that carries a colour from the outgoing palette.

    Nothing does, today. `_font` is the module's only cache and it is not on
    this list on purpose: it caches type, which has no colour in it, and
    rebuilding every font on a switch would cost the first repaint for no
    visible difference. `_solved` is keyed by palette name, so it is a store
    rather than a cache and survives a switch intact. Anything added later that
    derives a colour from `_current` must be cleared here.
    """


def set_palette(name: str) -> None:
    """Install the named palette. Raises KeyError naming the palette if unknown.

    Widgets are not repainted from here: this module knows nothing about a
    QApplication. `sodachi.gui.app.apply_palette_to` does the repaint.
    """
    global _current, _current_name
    palette = _solved.get(name)
    if palette is None:
        palette = palette_from_spec(palette_named(name))  # KeyError names `name`
        _solved[name] = palette
    _current = palette
    _current_name = name
    _invalidate()


def current_palette_name() -> str:
    """The name of the palette in force."""
    return _current_name


def available_palettes() -> list[str]:
    """Every palette that can be passed to `set_palette`, the default first."""
    return list_palettes()


@lru_cache(maxsize=64)
def _font(size_pt: float, bold: bool, caps: bool) -> QFont:
    f = QFont()
    f.setFamilies(MONO_FAMILIES)
    f.setStyleHint(QFont.StyleHint.TypeWriter)
    f.setPointSizeF(size_pt)
    f.setBold(bold)
    if caps:
        f.setCapitalization(QFont.Capitalization.AllUppercase)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
    return f


def mono_font(size_pt: float = 9, *, bold: bool = False, caps: bool = False) -> QFont:
    """The one type family, at ``size_pt``.

    Returns a copy of a cached font, because a QFont handed to a painter is
    trivially mutable and a shared one would let a single widget's tweak leak
    into every other widget's labels.
    """
    return QFont(_font(float(size_pt), bool(bold), bool(caps)))


def draw_dotted_line(
    p: QPainter,
    a: QPointF,
    b: QPointF,
    *,
    colour: QColor | None = None,
    dot: float = 1.0,
    gap: float = 2.0,
) -> None:
    """A 1px dotted rule from ``a`` to ``b``.

    Axis-aligned runs are filled as rectangles rather than stroked, because a
    stroked dash pattern rounds its own way at fractional device ratios and the
    dots go grey. Diagonals fall back to a dashed pen; nothing in the skin uses
    one at a size where the difference shows.
    """
    colour = PALETTE.rule if colour is None else colour
    dx = b.x() - a.x()
    dy = b.y() - a.y()
    length = math.hypot(dx, dy)
    if length <= 0.0 or dot <= 0.0:
        return
    step = dot + max(gap, 0.0)

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    horizontal = abs(dy) < 1e-9
    vertical = abs(dx) < 1e-9
    if horizontal or vertical:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(colour)
        ux = dx / length
        uy = dy / length
        t = 0.0
        while t < length:
            seg = min(dot, length - t)
            x = a.x() + ux * t
            y = a.y() + uy * t
            if horizontal:
                p.drawRect(QRectF(x, y - 0.5, seg, 1.0))
            else:
                p.drawRect(QRectF(x - 0.5, y, 1.0, seg))
            t += step
    else:
        pen = QPen(colour, 1.0)
        pen.setDashPattern([dot, max(gap, 0.01)])
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        p.drawLine(a, b)
    p.restore()


def draw_bevel_rect(
    p: QPainter,
    rect: QRectF,
    *,
    fill: QColor,
    border: QColor,
    radius: float = 2.0,
) -> None:
    """A small filled rect with a 1px border and a white top-left highlight."""
    body = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    if body.width() <= 0 or body.height() <= 0:
        return

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(fill)
    p.setPen(QPen(border, 1.0))
    p.drawRoundedRect(body, radius, radius)

    inner = body.adjusted(1.0, 1.0, -1.0, -1.0)
    if inner.width() > 2 and inner.height() > 2:
        highlight = QColor(PALETTE.white)
        highlight.setAlpha(150)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setPen(QPen(highlight, 1.0))
        p.drawLine(
            QPointF(inner.left() + radius, inner.top()),
            QPointF(inner.right() - radius, inner.top()),
        )
        p.drawLine(
            QPointF(inner.left(), inner.top() + radius),
            QPointF(inner.left(), inner.bottom() - radius),
        )
    p.restore()


def draw_panel(
    p: QPainter,
    rect: QRectF,
    *,
    dotted_inset: float = 4.0,
    radius: float = 10.0,
    fill: QColor | None = None,
) -> None:
    """Panel chrome: shadow, rounded outline, and the inset dotted rule.

    The dotted rule is the signature of the skin. It is drawn as a rounded rect
    with a dash pattern rather than four `draw_dotted_line` calls so that it
    follows the corners instead of stopping short of them.
    """
    fill = PALETTE.paper if fill is None else fill
    body = QRectF(rect).adjusted(0.5, 0.5, -1.5, -1.5)
    if body.width() <= 0 or body.height() <= 0:
        return

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(PALETTE.shadow)
    p.drawRoundedRect(body.translated(1.0, 1.0), radius, radius)

    p.setBrush(fill)
    p.setPen(QPen(PALETTE.rule, 1.0))
    p.drawRoundedRect(body, radius, radius)

    inner = body.adjusted(dotted_inset, dotted_inset, -dotted_inset, -dotted_inset)
    if inner.width() > 2 and inner.height() > 2:
        pen = QPen(PALETTE.rule, 1.0)
        pen.setDashPattern([1.0, 2.0])
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        inner_radius = max(radius - dotted_inset, 1.0)
        p.drawRoundedRect(inner, inner_radius, inner_radius)
    p.restore()


def _with_vertical(align: Qt.Alignment) -> Qt.Alignment:
    """Vertically centre unless the caller asked for something else."""
    vertical = Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignVCenter
    if int(align) & int(vertical):
        return align
    return align | Qt.AlignmentFlag.AlignVCenter


def draw_readout(
    p: QPainter,
    rect: QRectF,
    text: str,
    *,
    align: Qt.Alignment = Qt.AlignmentFlag.AlignRight,
) -> None:
    """A recessed monospace readout: sunk fill, dark digits.

    The recess is one shadow-coloured line inside the top and left edge. Two
    lines and it reads as a 3D button from a different decade.
    """
    body = QRectF(rect)
    if body.width() <= 0 or body.height() <= 0:
        return

    p.save()
    draw_bevel_rect(p, body, fill=PALETTE.surface, border=PALETTE.rule, radius=2.0)

    inner = body.adjusted(1.5, 1.5, -1.5, -1.5)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    p.setPen(QPen(PALETTE.shadow, 1.0))
    p.drawLine(QPointF(inner.left(), inner.top()), QPointF(inner.right(), inner.top()))
    p.drawLine(QPointF(inner.left(), inner.top()), QPointF(inner.left(), inner.bottom()))

    font = mono_font(9)
    p.setFont(font)
    p.setPen(PALETTE.ink_strong)
    text_rect = body.adjusted(5.0, 1.0, -5.0, -1.0)
    metrics = QFontMetricsF(font)
    shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
    p.drawText(text_rect, int(_with_vertical(align)), shown)
    p.restore()


def draw_micro_label(
    p: QPainter,
    rect: QRectF,
    text: str,
    *,
    colour: QColor | None = None,
    align: Qt.Alignment = Qt.AlignmentFlag.AlignLeft,
    size_pt: float = 7.0,
    bold: bool = False,
) -> None:
    """Tiny letter-spaced uppercase. The skin's only label style."""
    colour = PALETTE.ink_soft if colour is None else colour
    font = mono_font(size_pt, bold=bold, caps=True)
    p.save()
    p.setFont(font)
    p.setPen(colour)
    metrics = QFontMetricsF(font)
    shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, QRectF(rect).width())
    p.drawText(QRectF(rect), int(_with_vertical(align)), shown)
    p.restore()


def draw_header(p: QPainter, rect: QRectF, text: str) -> None:
    """The title alone, centred, with dotted rules running out to both edges.

    It used to carry a sparkle at each end; the dotted rules already frame the
    word, and the ornaments read as noise beside a version number.
    """
    body = QRectF(rect)
    if body.width() <= 0 or body.height() <= 0:
        return

    font = mono_font(8, bold=True, caps=True)
    metrics = QFontMetricsF(font)
    width = min(metrics.horizontalAdvance(text) + 8.0, body.width())
    centre_y = math.floor(body.center().y()) + 0.5

    p.save()
    p.setFont(font)
    p.setPen(PALETTE.ink_strong)
    text_rect = QRectF(body.center().x() - width / 2, body.top(), width, body.height())
    p.drawText(
        text_rect,
        int(Qt.AlignmentFlag.AlignCenter),
        metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width()),
    )

    rule_gap = 6.0
    left_end = text_rect.left() - rule_gap
    right_start = text_rect.right() + rule_gap
    if left_end - body.left() > 6:
        draw_dotted_line(p, QPointF(body.left(), centre_y), QPointF(left_end, centre_y))
    if body.right() - right_start > 6:
        draw_dotted_line(p, QPointF(right_start, centre_y), QPointF(body.right(), centre_y))
    p.restore()


__all__ = [
    "MONO_FAMILIES",
    "Palette",
    "PALETTE",
    "palette_from_spec",
    "set_palette",
    "current_palette_name",
    "available_palettes",
    "mono_font",
    "draw_panel",
    "draw_readout",
    "draw_dotted_line",
    "draw_micro_label",
    "draw_header",
    "draw_bevel_rect",
]

