"""Mat window geometry: openings from a layout, and the print a window needs.

Two consumers cut the same holes -- the printed guide in ``render/matguide.py``
and the machine files in ``export/cutpath.py`` -- and a guide and a cut file
that disagree scrap a board. The opening computation therefore lives here,
once, importing nothing but core geometry, so the DXF writer never pulls in a
PDF library and the guide never re-derives a number the cutter did not see.

Two conventions meet in one field. With ``reveal_mm`` at zero the opening is
the image inset by ``overlap_mm``: the board grips the print's own edge. With
``reveal_mm`` above zero the opening is the image grown by ``reveal_mm``, so a
band of the print's paper shows inside the window, and ``overlap_mm`` becomes
the minimum grip the board must keep on that paper beyond the reveal.

A double mat adds a second board on top. The bottom board's openings follow
the rules above unchanged; the top board's openings are the bottom ones grown
by ``inner_reveal_mm`` on every side, so a band of the bottom board shows
inside the top window. The two boards relate by construction — the top
opening is derived from the bottom one, never computed independently — which
is what keeps the reveal band the same width on all four sides of every
window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sodachi.core.geometry import Rect, Size
from sodachi.core.units import mm_to_inch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sodachi.core.layout import Layout


class MatOpeningError(ValueError):
    """Raised when the requested openings could not be cut or could not hold."""


def _mirrored_rect(rect: Rect, sheet_width_mm: float) -> Rect:
    """Reflect a rect about the board's vertical centreline: x' = W - x."""
    return Rect(sheet_width_mm - rect.right_mm, rect.y_mm, rect.width_mm, rect.height_mm)


def openings_mm(
    layout: Layout,
    *,
    overlap_mm: float,
    reveal_mm: float = 0.0,
    mirrored: bool = False,
) -> list[Rect]:
    """Every slot rect turned into a window opening, in slot order.

    ``reveal_mm`` of zero reproduces the original convention exactly: each
    opening is the slot inset by ``overlap_mm``, so the board overlaps the
    print all round, and the only failure is an overlap wide enough to close
    a window. Above zero the opening is the slot grown by ``reveal_mm`` on
    every side, and two new failures appear: a margin too narrow to leave the
    board ``overlap_mm`` of grip beyond the reveal, and grown openings from
    adjacent slots left with less than an overlap's worth of board between
    them.
    """
    sheet_width_mm = layout.sheet.width_mm

    if reveal_mm > 0:
        _check_margins(layout, overlap_mm=overlap_mm, reveal_mm=reveal_mm)

    out: list[Rect] = []
    for slot in layout.slots:
        if reveal_mm > 0:
            opening_mm = slot.rect.inset(-reveal_mm)
        else:
            opening_mm = slot.rect.inset(overlap_mm)
            if opening_mm.width_mm <= 0 or opening_mm.height_mm <= 0:
                raise MatOpeningError(
                    f"mat.window_overlap_mm is {overlap_mm:g}mm, which closes window "
                    f"{slot.index + 1} entirely: the slot is {slot.rect.width_mm:.2f} × "
                    f"{slot.rect.height_mm:.2f} mm and the overlap takes {2 * overlap_mm:g}mm "
                    f"off each dimension"
                )
        out.append(_mirrored_rect(opening_mm, sheet_width_mm) if mirrored else opening_mm)

    if reveal_mm > 0:
        # Mirroring is a rigid reflection, so the gaps it is checked on here
        # are the gaps the front face has too.
        _check_collisions(out, layout, overlap_mm=overlap_mm, reveal_mm=reveal_mm)

    return out


def outer_openings_mm(
    layout: Layout,
    *,
    overlap_mm: float,
    reveal_mm: float = 0.0,
    inner_reveal_mm: float,
    mirrored: bool = False,
) -> list[Rect]:
    """The top board's openings of a double mat, in slot order.

    Each is the corresponding :func:`openings_mm` opening grown by
    ``inner_reveal_mm`` on every side, so the returned list lines up
    index-for-index with the bottom board's and the band between the two is
    ``inner_reveal_mm`` wide by construction. Growing pushes the top opening
    further into the margin than the reveal alone did, so the margin must
    cover the reveal, the inner reveal and the grip together; and grown
    openings from adjacent slots need the same overlap's worth of board
    between them that the bottom board's do.
    """
    if inner_reveal_mm <= 0:
        raise MatOpeningError(
            f"mat.inner_reveal_mm is {inner_reveal_mm:g}mm, but a double mat shows a "
            f"band of the bottom board inside the top opening, and a band must be "
            f"wider than zero"
        )

    # The bottom openings carry their own validation; a double mat only
    # tightens the margin and collision checks, run against the grown rects.
    inner = openings_mm(
        layout, overlap_mm=overlap_mm, reveal_mm=reveal_mm, mirrored=mirrored
    )

    _check_double_margins(
        layout, overlap_mm=overlap_mm, reveal_mm=reveal_mm, inner_reveal_mm=inner_reveal_mm
    )

    # Mirroring is a rigid reflection, so growing the mirrored openings is the
    # same rect as mirroring the grown ones; derive, never recompute.
    out = [opening_mm.inset(-inner_reveal_mm) for opening_mm in inner]

    _check_outer_collisions(
        out, layout, overlap_mm=overlap_mm, inner_reveal_mm=inner_reveal_mm
    )
    return out


def _check_margins(layout: Layout, *, overlap_mm: float, reveal_mm: float) -> None:
    """The board outside a revealed opening must still be wide enough to grip."""
    narrowest_mm = layout.margins.smallest_mm
    held_mm = narrowest_mm - reveal_mm
    if held_mm < overlap_mm:
        raise MatOpeningError(
            f"mat.reveal_mm is {reveal_mm:g}mm and mat.window_overlap_mm is "
            f"{overlap_mm:g}mm, but the narrowest resolved margin is "
            f"{narrowest_mm:g}mm; after the reveal that leaves {held_mm:g}mm of "
            f"board to hold the paper, and the grip needs {overlap_mm:g}mm, so "
            f"every margin must be at least {reveal_mm + overlap_mm:g}mm"
        )


def _check_collisions(
    openings: list[Rect], layout: Layout, *, overlap_mm: float, reveal_mm: float
) -> None:
    """Grown openings must leave an overlap's worth of board between them."""
    for i, a in enumerate(openings):
        for j in range(i + 1, len(openings)):
            b = openings[j]
            gap_x_mm = max(b.x_mm - a.right_mm, a.x_mm - b.right_mm)
            gap_y_mm = max(b.y_mm - a.bottom_mm, a.y_mm - b.bottom_mm)
            gap_mm = max(gap_x_mm, gap_y_mm)
            if gap_mm < overlap_mm:
                raise MatOpeningError(
                    f"mat.reveal_mm is {reveal_mm:g}mm, which grows windows {i + 1} "
                    f"and {j + 1} to within {gap_mm:.2f}mm of each other across the "
                    f"{layout.gutter_mm:g}mm gutter; the board between openings must "
                    f"be at least mat.window_overlap_mm ({overlap_mm:g}mm) wide, so "
                    f"widen layout.gutter_mm or shrink the reveal"
                )


def _check_double_margins(
    layout: Layout, *, overlap_mm: float, reveal_mm: float, inner_reveal_mm: float
) -> None:
    """The board outside a top-board opening must still be wide enough to grip."""
    narrowest_mm = layout.margins.smallest_mm
    held_mm = narrowest_mm - reveal_mm - inner_reveal_mm
    if held_mm < overlap_mm:
        raise MatOpeningError(
            f"mat.reveal_mm is {reveal_mm:g}mm, mat.inner_reveal_mm is "
            f"{inner_reveal_mm:g}mm and mat.window_overlap_mm is {overlap_mm:g}mm, "
            f"but the narrowest resolved margin is {narrowest_mm:g}mm; after the "
            f"reveal and the inner reveal that leaves {held_mm:g}mm of board to hold "
            f"the paper, and the grip needs {overlap_mm:g}mm, so every margin must "
            f"be at least {reveal_mm + inner_reveal_mm + overlap_mm:g}mm"
        )


def _check_outer_collisions(
    openings: list[Rect], layout: Layout, *, overlap_mm: float, inner_reveal_mm: float
) -> None:
    """Grown top-board openings must leave an overlap's worth of board between them."""
    for i, a in enumerate(openings):
        for j in range(i + 1, len(openings)):
            b = openings[j]
            gap_x_mm = max(b.x_mm - a.right_mm, a.x_mm - b.right_mm)
            gap_y_mm = max(b.y_mm - a.bottom_mm, a.y_mm - b.bottom_mm)
            gap_mm = max(gap_x_mm, gap_y_mm)
            if gap_mm < overlap_mm:
                raise MatOpeningError(
                    f"mat.inner_reveal_mm is {inner_reveal_mm:g}mm, which grows the top "
                    f"board's windows {i + 1} and {j + 1} to within {gap_mm:.2f}mm of "
                    f"each other across the {layout.gutter_mm:g}mm gutter; the board "
                    f"between openings must be at least mat.window_overlap_mm "
                    f"({overlap_mm:g}mm) wide, so widen layout.gutter_mm or shrink "
                    f"the inner reveal"
                )


@dataclass(frozen=True, slots=True)
class PrintPlan:
    """What to print so that a given mat opening works, all in millimetres.

    Produced by :func:`print_from_opening`; every field is a consequence of the
    opening and the two mat allowances, not an independent choice.
    """

    opening_mm: Size
    image_mm: Size
    reveal_mm: float
    overlap_mm: float
    min_border_mm: float
    min_paper_mm: Size
    inner_reveal_mm: float = 0.0
    """Above zero the plan is for a double mat: ``opening_mm`` is the top
    board's opening — the hole you see — and the bottom board's follows from
    it by :attr:`bottom_opening_mm`."""

    @property
    def double(self) -> bool:
        return self.inner_reveal_mm > 0

    @property
    def bottom_opening_mm(self) -> Size:
        """The bottom board's opening: the visible hole inset by the inner reveal.

        Equal to ``opening_mm`` for a single mat, which is what keeps every
        single-mat consumer of this class unchanged.
        """
        return Size(
            self.opening_mm.width_mm - 2 * self.inner_reveal_mm,
            self.opening_mm.height_mm - 2 * self.inner_reveal_mm,
        )

    def rows(self, units: str) -> list[tuple[str, str]]:
        """Label/value pairs for the derivation dialog.

        Each value says what the number is for, because the person reading it
        is about to order a print against these figures.
        """

        def fmt(size: Size) -> str:
            if units == "in":
                return (
                    f"{mm_to_inch(size.width_mm):.2f} × {mm_to_inch(size.height_mm):.2f} in"
                )
            return f"{size.width_mm:.2f} × {size.height_mm:.2f} mm"

        def fmt_len(value_mm: float) -> str:
            if units == "in":
                return f"{mm_to_inch(value_mm):.2f} in"
            return f"{value_mm:.2f} mm"

        if self.double:
            out: list[tuple[str, str]] = [
                (
                    "opening",
                    f"{fmt(self.opening_mm)} — the hole cut in the top board",
                ),
                (
                    "bottom opening",
                    f"{fmt(self.bottom_opening_mm)} — the hole cut in the bottom board",
                ),
                (
                    "inner reveal",
                    f"{fmt_len(self.inner_reveal_mm)} — bottom board shown inside the "
                    f"top opening, per side",
                ),
            ]
        else:
            out = [("opening", f"{fmt(self.opening_mm)} — the hole cut in the board")]

        out.extend(
            [
                ("image", f"{fmt(self.image_mm)} — the printed image itself"),
                (
                    "reveal",
                    f"{fmt_len(self.reveal_mm)} — paper shown inside the opening, per side",
                ),
            ]
        )
        out.extend([
            (
                "overlap",
                f"{fmt_len(self.overlap_mm)} — board grip on the paper beyond the "
                f"opening, per side",
            ),
            (
                "border",
                f"{fmt_len(self.min_border_mm)} — border printed around the image, "
                f"per side, at minimum",
            ),
            (
                "paper",
                f"{fmt(self.min_paper_mm)} — smallest sheet that satisfies both the "
                f"border and the grip",
            ),
        ])
        return out


def print_from_opening(
    opening: Size,
    *,
    reveal_mm: float = 0.0,
    overlap_mm: float = 3.0,
    min_border_mm: float = 10.0,
    inner_reveal_mm: float = 0.0,
) -> PrintPlan:
    """Work backwards from a desired opening to the print that fills it.

    With ``inner_reveal_mm`` above zero the plan is for a double mat, and
    ``opening`` is the top board's — the hole you see. The bottom board's
    opening is that inset by ``inner_reveal_mm`` on every side, and the image
    follows from the bottom opening and ``reveal_mm`` exactly as it does for a
    single mat, since the bottom board is the one facing the print.

    The paper is governed by two independent floors and must clear both: the
    user wants at least ``min_border_mm`` of printed border around the image
    on the sheet (paper >= image + 2 * min_border per dimension), and the
    board needs at least ``overlap_mm`` of paper beyond the opening to grip
    (paper >= bottom opening + 2 * overlap per dimension). The grip floor is
    measured from the bottom opening even when the mat is double: the band
    inside the top opening is bottom board, not paper, so the paper never has
    to reach past the bottom board's grip and a top board never enlarges the
    minimum sheet. ``min_paper_mm`` is the per-dimension maximum of the two
    floors; whichever is higher wins, and they cross where ``min_border_mm``
    equals ``reveal_mm + overlap_mm``.
    """
    if reveal_mm < 0:
        raise MatOpeningError(f"reveal_mm is {reveal_mm:g}, which is less than zero")
    if inner_reveal_mm < 0:
        raise MatOpeningError(
            f"inner_reveal_mm is {inner_reveal_mm:g}, which is less than zero"
        )

    bottom = Size(
        opening.width_mm - 2 * inner_reveal_mm, opening.height_mm - 2 * inner_reveal_mm
    )
    if bottom.width_mm <= 0 or bottom.height_mm <= 0:
        raise MatOpeningError(
            f"an inner reveal of {inner_reveal_mm:g}mm takes {2 * inner_reveal_mm:g}mm "
            f"off each dimension of a {opening.width_mm:.2f} × {opening.height_mm:.2f} "
            f"mm top opening, which leaves no bottom opening at all"
        )

    image = Size(bottom.width_mm - 2 * reveal_mm, bottom.height_mm - 2 * reveal_mm)
    if image.width_mm <= 0 or image.height_mm <= 0:
        raise MatOpeningError(
            f"a reveal of {reveal_mm:g}mm takes {2 * reveal_mm:g}mm off each dimension "
            f"of a {bottom.width_mm:.2f} × {bottom.height_mm:.2f} mm opening, which "
            f"leaves no image at all"
        )

    min_paper = Size(
        max(image.width_mm + 2 * min_border_mm, bottom.width_mm + 2 * overlap_mm),
        max(image.height_mm + 2 * min_border_mm, bottom.height_mm + 2 * overlap_mm),
    )
    return PrintPlan(
        opening_mm=opening,
        image_mm=image,
        reveal_mm=reveal_mm,
        overlap_mm=overlap_mm,
        min_border_mm=min_border_mm,
        min_paper_mm=min_paper,
        inner_reveal_mm=inner_reveal_mm,
    )


__all__ = [
    "MatOpeningError",
    "PrintPlan",
    "openings_mm",
    "outer_openings_mm",
    "print_from_opening",
]
