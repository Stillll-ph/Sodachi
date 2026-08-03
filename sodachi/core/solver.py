"""A spec plus a list of aspect ratios in, one solved ``Layout`` out.

The solver never sees an image, only floats. That is what keeps it pure
enough to test exhaustively, and what makes the raster renderer and the mat
guide two views of one piece of arithmetic rather than two implementations of
it.

``Spec`` is duck-typed on purpose. ``sodachi.spec.model`` imports
``sodachi.core``, so importing it back here would close the cycle; only the
type checker ever sees the name.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from sodachi.core.geometry import Rect
from sodachi.core.layout import Layout, Margins, Slot

if TYPE_CHECKING:
    from sodachi.spec.model import Spec


class LayoutError(ValueError):
    """Raised when a spec and a set of aspect ratios cannot make a sheet."""


SIZE_MATCH_MODES = ("area", "height", "width", "none")
ALIGN_MODES = ("top", "center", "bottom", "optical")

_EXPECTED_SLOTS = {"single": 1, "diptych": 2, "triptych": 3}
"""Layout types with a fixed image count. ``grid`` takes any n >= 1."""


def relative_widths(
    aspects: Sequence[float],
    size_match: str,
    natural_widths: Sequence[float] | None = None,
) -> tuple[float, ...]:
    """Widths of the unit-scale assembly, normalised so ``w_0 == 1``.

    Heights follow from ``h_i = w_i / a_i``, so these widths are the whole of
    the size-matching decision. ``area`` is the interesting one: it is the only
    form determined by aspect ratios alone, since a scale-factor form would
    depend on the images' native pixel sizes and two scans of the same frame at
    different resolutions would then lay out differently.
    """
    if not aspects:
        raise LayoutError("no aspect ratios given; the solver needs at least one")

    if size_match not in SIZE_MATCH_MODES:
        raise LayoutError(
            f"size_match is {size_match!r}, which is not one of "
            f"{', '.join(SIZE_MATCH_MODES)}"
        )

    for index, aspect in enumerate(aspects):
        if not math.isfinite(aspect):
            raise LayoutError(
                f"aspect ratio {index} is {aspect!r}; every aspect must be a finite number"
            )
        if aspect <= 0:
            raise LayoutError(
                f"aspect ratio {index} is {aspect:g}; width over height must be "
                f"greater than zero"
            )

    if natural_widths is not None:
        if len(natural_widths) != len(aspects):
            raise LayoutError(
                f"natural_widths has {len(natural_widths)} entries but there are "
                f"{len(aspects)} aspect ratios; they must match one to one"
            )
        for index, width in enumerate(natural_widths):
            if not math.isfinite(width) or width <= 0:
                raise LayoutError(
                    f"natural_widths[{index}] is {width!r}; every natural width must be "
                    f"a finite number greater than zero"
                )

    first_aspect = aspects[0]
    if size_match == "area":
        return tuple(math.sqrt(a / first_aspect) for a in aspects)
    if size_match == "height":
        return tuple(a / first_aspect for a in aspects)
    if size_match == "none" and natural_widths is not None:
        first_natural = natural_widths[0]
        return tuple(w / first_natural for w in natural_widths)
    # Both `width` and `none`-without-natural-widths; solve() notes the latter.
    return tuple(1.0 for _ in aspects)


def solve(
    spec: "Spec",
    aspects: Sequence[float],
    *,
    names: Sequence[str] | None = None,
    natural_widths: Sequence[float] | None = None,
    layout_name: str | None = None,
) -> Layout:
    """Place ``len(aspects)`` slots on the spec's sheet."""
    notes: list[str] = []
    aspects = tuple(float(a) for a in aspects)
    count = len(aspects)

    layout_spec = spec.layout
    if layout_spec.type not in _EXPECTED_SLOTS and layout_spec.type != "grid":
        raise LayoutError(
            f"layout.type is {layout_spec.type!r}, which is not one of "
            f"single, diptych, triptych, grid"
        )
    expected = _EXPECTED_SLOTS.get(layout_spec.type)
    if expected is not None and count != expected:
        raise LayoutError(
            f"layout.type is {layout_spec.type!r}, which takes exactly {expected} "
            f"image{'' if expected == 1 else 's'}, but {count} were given"
        )
    if names is not None and len(names) != count:
        raise LayoutError(
            f"names has {len(names)} entries but there are {count} aspect ratios"
        )

    align = layout_spec.align
    if align not in ALIGN_MODES:
        raise LayoutError(
            f"layout.align is {align!r}, which is not one of {', '.join(ALIGN_MODES)}"
        )

    rel_w = relative_widths(aspects, layout_spec.size_match, natural_widths)
    if layout_spec.size_match == "none" and natural_widths is None:
        notes.append(
            "layout.size_match is 'none' but no natural widths were available; "
            "fell back to equal widths"
        )
    rel_h = tuple(w / a for w, a in zip(rel_w, aspects))

    if layout_spec.type == "grid":
        columns = layout_spec.columns
        if columns is None:
            columns = math.ceil(math.sqrt(count))
            notes.append(
                f"layout.columns not set; used {columns} columns for {count} images"
            )
    else:
        columns = count

    rows_of_indexes = [
        list(range(start, min(start + columns, count)))
        for start in range(0, count, columns)
    ]
    row_count = len(rows_of_indexes)

    # Row assembly, in unit-scale units. `offsets` is each slot's distance from
    # the top of its own row.
    fraction = layout_spec.optical_align_fraction
    rel_row_h: list[float] = []
    offsets = [0.0] * count
    for indexes in rows_of_indexes:
        heights = [rel_h[i] for i in indexes]
        if align == "optical":
            # Line up the same fraction of every image rather than an edge or a
            # centre; for a portrait beside a landscape that is what the eye
            # reads as level.
            above = max(fraction * h for h in heights)
            below = max((1.0 - fraction) * h for h in heights)
            row_height = above + below
            for i in indexes:
                offsets[i] = above - fraction * rel_h[i]
        else:
            row_height = max(heights)
            for i in indexes:
                if align == "top":
                    offsets[i] = 0.0
                elif align == "center":
                    offsets[i] = (row_height - rel_h[i]) / 2.0
                else:
                    offsets[i] = row_height - rel_h[i]
        rel_row_h.append(row_height)

    sheet = spec.sheet.to_sheet()
    sides_mm = float(spec.margins.sides_mm)
    top_mm = float(spec.margins.top_mm)
    ratio_k = float(spec.margins.optical_ratio)
    bottom_rule = spec.margins.bottom_mm

    if isinstance(bottom_rule, str):
        if bottom_rule == "center":
            provisional_bottom_mm = top_mm
        elif bottom_rule == "optical":
            provisional_bottom_mm = top_mm * ratio_k
        else:
            raise LayoutError(
                f"margins.bottom_mm is {bottom_rule!r}, which is neither a number nor "
                f"one of 'optical', 'center'"
            )
    else:
        provisional_bottom_mm = float(bottom_rule)

    avail_w_mm = sheet.width_mm - 2 * sides_mm
    avail_h_mm = sheet.height_mm - top_mm - provisional_bottom_mm

    gutter_mm = float(layout_spec.gutter_mm)
    rel_row_w = [sum(rel_w[i] for i in indexes) for indexes in rows_of_indexes]
    rel_h_total = sum(rel_row_h)

    # The gutter is a distance between two prints on paper, not a feature of the
    # assembly. Subtracting it before dividing is what keeps it absolute: double
    # the sheet and the images grow, the gap between them does not.
    scale_w = min(
        (avail_w_mm - gutter_mm * (len(indexes) - 1)) / row_width
        for indexes, row_width in zip(rows_of_indexes, rel_row_w)
    )
    scale_h = (avail_h_mm - gutter_mm * (row_count - 1)) / rel_h_total
    scale = min(scale_w, scale_h)

    if scale <= 0:
        widest_row = max(len(indexes) for indexes in rows_of_indexes)
        raise LayoutError(
            f"the content does not fit after margins: the available box is "
            f"{avail_w_mm:.2f} × {avail_h_mm:.2f} mm and the gutters alone take "
            f"{gutter_mm * (widest_row - 1):.2f} mm across and "
            f"{gutter_mm * (row_count - 1):.2f} mm down"
        )

    content_w_mm = max(
        scale * row_width + gutter_mm * (len(indexes) - 1)
        for indexes, row_width in zip(rows_of_indexes, rel_row_w)
    )
    content_h_mm = scale * rel_h_total + gutter_mm * (row_count - 1)

    x0_mm = sides_mm + (avail_w_mm - content_w_mm) / 2.0

    # The product spec gives optical centring twice — "bottom = top * ratio" and
    # the M/(1+k) split. They agree exactly when the content fills the available
    # height, and the second generalises, so the first only sizes the available
    # box above and the second places the content here.
    slack_mm = sheet.height_mm - content_h_mm
    if bottom_rule == "optical":
        top_final_mm = slack_mm / (1.0 + ratio_k)
    elif bottom_rule == "center":
        top_final_mm = slack_mm / 2.0
    else:
        top_final_mm = top_mm + (avail_h_mm - content_h_mm) / 2.0
    bottom_final_mm = sheet.height_mm - top_final_mm - content_h_mm

    slots: list[Slot] = []
    y_mm = top_final_mm
    for row, indexes in enumerate(rows_of_indexes):
        row_w_mm = scale * rel_row_w[row] + gutter_mm * (len(indexes) - 1)
        x_mm = x0_mm + (content_w_mm - row_w_mm) / 2.0
        for column, i in enumerate(indexes):
            w_mm = rel_w[i] * scale
            h_mm = rel_h[i] * scale
            slots.append(
                Slot(
                    index=i,
                    rect=Rect(x_mm, y_mm + offsets[i] * scale, w_mm, h_mm),
                    aspect=aspects[i],
                    source=names[i] if names is not None else None,
                    row=row,
                    column=column,
                )
            )
            x_mm += w_mm + gutter_mm
        y_mm += scale * rel_row_h[row] + gutter_mm

    margins = Margins.symmetric(top_final_mm, sides_mm, bottom_final_mm)

    # The spec model checks this against the margins the user wrote; a derived
    # bottom margin is only known now, so it is checked again against the
    # resolved one.
    if count > 1 and gutter_mm > 0 and gutter_mm >= margins.smallest_mm:
        raise LayoutError(
            f"layout.gutter_mm is {gutter_mm:g}mm but the narrowest resolved outer "
            f"margin is {margins.smallest_mm:.2f}mm; a gutter wider than the margin "
            f"stops the images reading as one object"
        )

    return Layout(
        name=layout_name if layout_name is not None else layout_spec.type,
        sheet=sheet,
        margins=margins,
        gutter_mm=gutter_mm,
        slots=tuple(slots),
        scale=scale,
        size_match=layout_spec.size_match,
        align=align,
        notes=tuple(notes),
    )


__all__ = ["LayoutError", "SIZE_MATCH_MODES", "ALIGN_MODES", "relative_widths", "solve"]
