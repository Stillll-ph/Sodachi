"""Deciding how much border an image needs to reach a chosen shape, and why.

This is the whole of the padding decision and none of the rendering: it reads
a :class:`~sodachi.presets.Preset` and an image's pixel dimensions and returns
a plan that can explain itself. Nothing here imports pyvips, so the window's fit
report costs a spec parse and no image decode.

The rule that shapes the module: never silently pad to a ratio the user did
not ask for. :meth:`FitPlan.report` is the deliverable, not a debug aid.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sodachi.core.geometry import Size
from sodachi.core.units import NOMINAL_SCREEN_DPI, px_to_mm, round_half_up
from sodachi.presets import Preset
from sodachi.spec.model import LayoutSpec, MarginsSpec, OutputSpec, SheetSpec, Spec

PadAxis = Literal["none", "horizontal", "vertical"]


@dataclass(frozen=True, slots=True)
class FitPlan:
    """One image's route from its own pixels to a sheet inside the target window."""

    preset_name: str
    image_px: tuple[int, int]
    scaled_image_px: tuple[int, int]
    downscaled: bool
    """True whenever ``scaled_image_px`` differs from ``image_px``.

    That includes the shrink the sheet clamp forces, not only the initial cap:
    the two fields sit side by side and a plan claiming it did not downscale
    while reporting fewer pixels than it was given would be reporting a lie.
    """
    input_aspect: float
    window: tuple[float, float]
    sheet_px: tuple[int, int]
    border_px: int
    pad_axis: PadAxis
    pad_per_side_px: int
    notes: tuple[str, ...] = field(default_factory=tuple)
    """Clamps and downscales applied after the padding decision."""

    @property
    def sheet_aspect(self) -> float:
        return self.sheet_px[0] / self.sheet_px[1]

    def sheet_mm(self) -> Size:
        """The sheet at the nominal screen DPI, which is what the solver wants."""
        return Size(
            px_to_mm(self.sheet_px[0], NOMINAL_SCREEN_DPI),
            px_to_mm(self.sheet_px[1], NOMINAL_SCREEN_DPI),
        )

    def margins_mm(self) -> tuple[float, float]:
        """``(top/bottom, sides)`` — the padding is part of the margin, not a separate band."""
        vertical_px = self.border_px + (
            self.pad_per_side_px if self.pad_axis == "vertical" else 0
        )
        horizontal_px = self.border_px + (
            self.pad_per_side_px if self.pad_axis == "horizontal" else 0
        )
        return (
            px_to_mm(vertical_px, NOMINAL_SCREEN_DPI),
            px_to_mm(horizontal_px, NOMINAL_SCREEN_DPI),
        )

    def report(self) -> str:
        low_aspect, high_aspect = self.window
        window_text = f"target window {high_aspect:.2f}:1–{low_aspect:.2f}:1"
        head = f"{_ratio_label(*self.image_px)} input, {window_text}"

        if self.pad_axis == "none":
            lines = [f"{head}, no padding needed"]
        else:
            edges = "top/bottom" if self.pad_axis == "vertical" else "left/right"
            pad_mm = px_to_mm(self.pad_per_side_px, NOMINAL_SCREEN_DPI)
            lines = [
                f"{head}, padding to {self.sheet_aspect:.2f}:1, "
                f"adding {pad_mm:.1f}mm ({self.pad_per_side_px}px) {edges}"
            ]

        border_mm = px_to_mm(self.border_px, NOMINAL_SCREEN_DPI)
        lines.append(
            f"border {border_mm:.1f}mm ({self.border_px}px) every side, "
            f"sheet {self.sheet_px[0]}×{self.sheet_px[1]}px "
            f"for preset {self.preset_name!r}"
        )
        lines.extend(self.notes)
        return "\n".join(lines)


def plan_fit(
    image_px: tuple[int, int],
    preset: Preset,
    *,
    border_fraction: float | None = None,
) -> FitPlan:
    """Work out the sheet an image needs to reach the preset's target window."""
    width_px, height_px = image_px
    if width_px <= 0 or height_px <= 0:
        raise ValueError(f"image_px must be positive, got {width_px}×{height_px}")

    fraction = preset.border_fraction if border_fraction is None else border_fraction
    if fraction < 0:
        raise ValueError(f"border_fraction must not be negative, got {fraction}")

    notes: list[str] = []

    scale = min(1.0, preset.max_px / max(width_px, height_px))
    downscaled = scale < 1.0
    if downscaled:
        scaled_px = (
            max(1, round_half_up(width_px * scale)),
            max(1, round_half_up(height_px * scale)),
        )
        notes.append(
            f"image downscaled from {width_px}×{height_px}px to "
            f"{scaled_px[0]}×{scaled_px[1]}px to meet the {preset.max_px}px cap"
        )
    else:
        scaled_px = (width_px, height_px)

    border_px = round_half_up(fraction * min(scaled_px))
    pad_axis, pad_per_side_px = _pad_for_window(scaled_px, border_px, preset.window)
    sheet_px = _sheet_from_parts(scaled_px, border_px, pad_axis, pad_per_side_px)

    if sheet_px[0] < preset.min_width_px:
        grow = preset.min_width_px / sheet_px[0]
        before_px = sheet_px
        scaled_px = (math.ceil(scaled_px[0] * grow), math.ceil(scaled_px[1] * grow))
        border_px = math.ceil(border_px * grow)
        pad_axis, pad_per_side_px = _pad_for_window(scaled_px, border_px, preset.window)
        sheet_px = _sheet_from_parts(scaled_px, border_px, pad_axis, pad_per_side_px)
        notes.append(
            f"sheet widened from {before_px[0]}×{before_px[1]}px to "
            f"{sheet_px[0]}×{sheet_px[1]}px to reach the {preset.min_width_px}px minimum width"
        )

    # The padding has to be re-derived after any rescale, not rescaled with
    # everything else. Scaling the image, the border and the pad by the same
    # factor and flooring each one separately lets the reassembled sheet drift
    # off the ratio it was just padded to, which put a 4:1 panoramic back
    # outside the target window — silently, and after reporting that it had been
    # padded to land inside it. Shrinking strictly reduces, so this terminates.
    if max(sheet_px) > preset.max_px:
        before_px = sheet_px
        for _ in range(8):
            shrink = preset.max_px / max(sheet_px)
            scaled_px = (
                max(1, math.floor(scaled_px[0] * shrink)),
                max(1, math.floor(scaled_px[1] * shrink)),
            )
            border_px = math.floor(border_px * shrink)
            pad_axis, pad_per_side_px = _pad_for_window(scaled_px, border_px, preset.window)
            sheet_px = _sheet_from_parts(scaled_px, border_px, pad_axis, pad_per_side_px)
            if max(sheet_px) <= preset.max_px:
                break
        notes.append(
            f"sheet clamped from {before_px[0]}×{before_px[1]}px to "
            f"{sheet_px[0]}×{sheet_px[1]}px by the {preset.max_px}px cap"
        )

    downscaled = scaled_px != (width_px, height_px)

    return FitPlan(
        preset_name=preset.name,
        image_px=(width_px, height_px),
        scaled_image_px=scaled_px,
        downscaled=downscaled,
        input_aspect=width_px / height_px,
        window=preset.window,
        sheet_px=sheet_px,
        border_px=border_px,
        pad_axis=pad_axis,
        pad_per_side_px=pad_per_side_px,
        notes=tuple(notes),
    )


def spec_for_plan(
    plan: FitPlan,
    *,
    background: str = "#FFFFFF",
    format: str = "jpeg",
    quality: int = 95,
) -> Spec:
    """Turn a plan into the spec that renders it.

    ``margins.bottom_mm`` is deliberately left unset: under ``target: screen``
    the model coerces it to ``center`` and records a note, which is the whole
    point of that rule.
    """
    vertical_mm, horizontal_mm = plan.margins_mm()
    return Spec(
        sheet=SheetSpec(
            width_px=plan.sheet_px[0],
            height_px=plan.sheet_px[1],
            background=background,
        ),
        margins=MarginsSpec(top_mm=vertical_mm, sides_mm=horizontal_mm),
        layout=LayoutSpec(type="single"),
        output=OutputSpec(format=format, quality=quality),
        target="screen",
    )


def plan_border(
    image_sizes_px: Sequence[tuple[int, int]],
    preset: Preset,
    *,
    background: str = "#FFFFFF",
    format: str = "jpeg",
    quality: int = 95,
) -> tuple[FitPlan, Spec]:
    """Put a border around one image or several, and give the spec that renders it.

    A single image is the plain :func:`plan_fit` case. Several take two passes:
    the first solves the assembly on a large square sheet to learn the composed
    aspect, the second treats that composition as if it were one image and fits
    it. The sheet lands inside the target window by construction, because the
    sheet is the thing being chosen; the second pass rescales the content a
    little, which moves the margins rather than cropping anything.

    Several images become one file rather than several: the arrangement the
    user composed is only preserved if it leaves here as a single image, since
    nothing downstream knows the images were meant to be seen together.
    """
    from sodachi.core.solver import solve

    sizes = [(int(w), int(h)) for w, h in image_sizes_px]
    if not sizes:
        raise ValueError("image_sizes_px must hold at least one image size, got none")

    if len(sizes) == 1:
        plan = plan_fit(sizes[0], preset)
        return plan, spec_for_plan(
            plan, background=background, format=format, quality=quality
        )

    count = len(sizes)
    layout_type = {2: "diptych", 3: "triptych"}.get(count, "grid")
    columns = count if layout_type == "grid" else None
    aspects = [w / h for w, h in sizes]

    probe_spec = Spec(
        sheet=SheetSpec(width_px=preset.max_px, height_px=preset.max_px),
        margins=MarginsSpec(top_mm=0.0, sides_mm=0.0, bottom_mm="center"),
        layout=LayoutSpec(type=layout_type, columns=columns, gutter_mm=0.0),
        target="screen",
    )
    content = solve(probe_spec, aspects).content

    long_px = preset.max_px
    if content.width_mm >= content.height_mm:
        content_px = (long_px, max(1, round_half_up(long_px * content.height_mm / content.width_mm)))
    else:
        content_px = (max(1, round_half_up(long_px * content.width_mm / content.height_mm)), long_px)

    plan = plan_fit(content_px, preset)
    vertical_mm, horizontal_mm = plan.margins_mm()
    spec = Spec(
        sheet=SheetSpec(
            width_px=plan.sheet_px[0], height_px=plan.sheet_px[1], background=background
        ),
        margins=MarginsSpec(top_mm=vertical_mm, sides_mm=horizontal_mm),
        layout=LayoutSpec(
            type=layout_type,
            columns=columns,
            # Half the narrowest margin, capped. It has to stay strictly under
            # that margin or the pair stops reading as one object and the
            # solver refuses the layout outright.
            gutter_mm=min(12.0, min(vertical_mm, horizontal_mm) * 0.5),
        ),
        output=OutputSpec(format=format, quality=quality),
        target="screen",
    )
    return plan, spec


def _pad_for_window(
    image_px: tuple[int, int], border_px: int, window: tuple[float, float]
) -> tuple[PadAxis, int]:
    """The padding that brings a bordered image inside the target window.

    Rounds up, so the sheet always reaches the target ratio rather than
    stopping a pixel short of it and staying croppable.
    """
    low_aspect, high_aspect = window
    provisional_px = (image_px[0] + 2 * border_px, image_px[1] + 2 * border_px)
    aspect = provisional_px[0] / provisional_px[1]

    if aspect > high_aspect:
        # Too wide for the window, so grow the short axis: taller, not narrower.
        target_height_px = provisional_px[0] / high_aspect
        return "vertical", math.ceil((target_height_px - provisional_px[1]) / 2)
    if aspect < low_aspect:
        target_width_px = provisional_px[1] * low_aspect
        return "horizontal", math.ceil((target_width_px - provisional_px[0]) / 2)
    return "none", 0


def _sheet_from_parts(
    image_px: tuple[int, int], border_px: int, pad_axis: PadAxis, pad_per_side_px: int
) -> tuple[int, int]:
    horizontal_pad_px = pad_per_side_px if pad_axis == "horizontal" else 0
    vertical_pad_px = pad_per_side_px if pad_axis == "vertical" else 0
    return (
        image_px[0] + 2 * border_px + 2 * horizontal_pad_px,
        image_px[1] + 2 * border_px + 2 * vertical_pad_px,
    )


def _ratio_label(width_px: int, height_px: int) -> str:
    """``3:2`` where the ratio is simple, a decimal where it is not."""
    divisor = math.gcd(width_px, height_px)
    reduced = (width_px // divisor, height_px // divisor)
    if max(reduced) <= 20:
        return f"{reduced[0]}:{reduced[1]}"
    return f"{width_px / height_px:.2f}:1"


__all__ = ["PadAxis", "FitPlan", "plan_fit", "plan_border", "spec_for_plan"]
