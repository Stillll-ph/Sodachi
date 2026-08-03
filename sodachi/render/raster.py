"""The pixel renderer: a solved Layout plus files, out the other side as a sheet.

This is the only module in the project allowed to think in pixels, and it does
the conversion in exactly one place per quantity. Its obligations, in the
product spec's priority order, are: never silently reduce bit depth, never
composite across colour spaces, put the background in the working space rather
than assuming its numbers, and resample with lanczos3 in gamma light unless the
spec asks otherwise.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sodachi.core.layout import Layout
from sodachi.core.units import MM_PER_INCH, mm_to_px, mm_to_px_floor
from sodachi.render.color import (
    SRGB,
    ColorError,
    Profile,
    attach_profile,
    embedded_profile,
    profile_filename,
    resolve_working_profile,
    srgb_hex_to_pixel,
    to_srgb8,
    to_working,
    vips,
    working_bands,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sodachi.spec.model import Spec


SCREEN_MAX_PX = 4096
"""Long-edge cap for ``target: screen``. Larger uploads are downscaled anyway."""

ASPECT_TOLERANCE = 0.005
"""Slot-versus-image aspect disagreement worth telling the user about."""

_WIDE_FORMATS = frozenset({"ushort", "short", "uint", "int", "float", "double"})
"""Band formats that carry more than eight bits."""

Progress = Callable[[str, float], None]


class RenderError(RuntimeError):
    """Raised when a sheet cannot be composed or written."""


@dataclass(frozen=True, slots=True)
class ImageInfo:
    path: Path
    width_px: int
    height_px: int
    aspect: float
    bands: int
    is_16bit: bool
    has_profile: bool
    interpretation: str


@dataclass(frozen=True, slots=True)
class RenderResult:
    path: Path
    size_px: tuple[int, int]
    bit_depth: int
    working_profile: str
    warnings: tuple[str, ...]


def probe(path: str | Path) -> ImageInfo:
    """Read a file's shape and colour without decoding it.

    The size reported is the size after ``autorot``, so the aspect the solver
    is handed is the aspect the renderer will actually produce. A portrait scan
    tagged Orientation 6 would otherwise be laid out sideways.
    """
    p = Path(path)
    try:
        image = vips.Image.new_from_file(str(p), access="sequential").autorot()
    except vips.Error as exc:
        raise RenderError(f"cannot read {p}: {exc}") from exc
    return ImageInfo(
        path=p,
        width_px=image.width,
        height_px=image.height,
        aspect=image.width / image.height,
        bands=image.bands,
        is_16bit=image.format in _WIDE_FORMATS,
        has_profile=embedded_profile(image) is not None,
        interpretation=image.interpretation,
    )


def render(
    layout: Layout,
    image_paths: Sequence[str | Path],
    spec: "Spec",
    out_path: str | Path,
    *,
    progress: Progress | None = None,
) -> RenderResult:
    """Compose one sheet and write it."""
    paths = [Path(p) for p in image_paths]
    out = Path(out_path)

    if len(paths) != len(layout.slots):
        raise RenderError(
            f"layout {layout.name!r} has {len(layout.slots)} slot(s) but "
            f"{len(paths)} image(s) were given"
        )

    _report(progress, "probing inputs", 0.02)
    infos = [probe(p) for p in paths]

    depth, warnings = _composite_depth(spec, infos)

    _report(progress, "resolving working profile", 0.10)
    profile, profile_desc = resolve_working_profile(paths, spec)
    bands = working_bands(profile)

    sheet_w_px, sheet_h_px = layout.sheet.px_size()
    dpi = layout.sheet.dpi

    _report(progress, "filling background", 0.16)
    bg_pixel = srgb_hex_to_pixel(
        layout.sheet.background_hex, profile, bands=bands, depth=depth
    )
    canvas = (
        vips.Image.black(sheet_w_px, sheet_h_px)
        .cast(_format_for(depth))
        .new_from_image(bg_pixel)
        .copy(interpretation=_interpretation(bands, depth))
    )

    slot_count = len(layout.slots)
    for i, slot in enumerate(layout.slots):
        _report(progress, f"placing {paths[i].name}", 0.20 + 0.60 * i / slot_count)
        x0_px, y0_px, w_px, h_px = _slot_px(slot.rect, dpi, sheet_w_px, sheet_h_px, i)

        deviation = abs(slot.rect.aspect / infos[i].aspect - 1.0)
        if deviation > ASPECT_TOLERANCE:
            warnings.append(
                f"{paths[i].name} is {infos[i].aspect:.4f}:1 but its slot is "
                f"{slot.rect.aspect:.4f}:1, a {deviation * 100:.2f}% disagreement; the "
                f"image is being stretched to fit"
            )

        placed = _resize_into_working(paths[i], w_px, h_px, profile, spec, depth)
        placed = _match_bands(placed, bands, bg_pixel, depth, paths[i])
        canvas = canvas.insert(placed, x0_px, y0_px)

    if layout.sheet.requested_px is not None:
        want_px = tuple(layout.sheet.requested_px)
        got_px = (canvas.width, canvas.height)
        if got_px != want_px:
            raise RenderError(
                f"sheet was requested as {want_px[0]}×{want_px[1]}px but composed to "
                f"{got_px[0]}×{got_px[1]}px; the millimetre round trip did not close"
            )

    final_depth = depth
    if spec.target == "screen":
        _report(progress, "flattening to sRGB", 0.84)
        canvas = to_srgb8(canvas)
        final_depth = 8
        long_px = max(canvas.width, canvas.height)
        if long_px > SCREEN_MAX_PX:
            canvas = canvas.resize(SCREEN_MAX_PX / long_px, kernel="lanczos3")
            warnings.append(
                f"sheet long edge was {long_px}px; downscaled to "
                f"{max(canvas.width, canvas.height)}px because screen targets are "
                f"capped at {SCREEN_MAX_PX}px"
            )
    else:
        out_profile = _resolve_output_profile(spec, profile)
        canvas = attach_profile(canvas, out_profile)

    # Resolution is measured off the finished sheet rather than taken from the
    # spec, because the screen cap above may have changed it.
    px_per_mm = canvas.width / layout.sheet.width_mm
    canvas = canvas.copy(xres=px_per_mm, yres=px_per_mm)

    _report(progress, f"writing {out.name}", 0.94)
    out.parent.mkdir(parents=True, exist_ok=True)
    _write(canvas, out, spec)
    _report(progress, "done", 1.0)

    return RenderResult(
        path=out,
        size_px=(canvas.width, canvas.height),
        bit_depth=final_depth,
        working_profile=profile_desc,
        warnings=tuple(warnings),
    )


def render_preview(
    layout: Layout,
    image_paths: Sequence[str | Path],
    spec: "Spec",
    *,
    max_px: int = 900,
) -> bytes:
    """A screen-sized PNG of the sheet, as bytes, writing nothing.

    The GUI calls this on a worker thread and hands the result to
    ``QImage.fromData``. It is sRGB 8-bit whatever the job's working space is,
    because that is what the screen is, and it renders at whatever DPI puts the
    long edge at ``max_px`` rather than at the spec's.
    """
    long_mm = max(layout.sheet.width_mm, layout.sheet.height_mm)
    dpi = max_px * MM_PER_INCH / long_mm
    sheet_w_px = max(1, mm_to_px(layout.sheet.width_mm, dpi))
    sheet_h_px = max(1, mm_to_px(layout.sheet.height_mm, dpi))

    text = layout.sheet.background_hex.strip()
    bg_pixel = [float(int(text[i : i + 2], 16)) for i in (1, 3, 5)]
    canvas = (
        vips.Image.black(sheet_w_px, sheet_h_px)
        .new_from_image(bg_pixel)
        .copy(interpretation="srgb")
    )

    paths = [Path(p) for p in image_paths]
    linear = spec.color.resize_in_linear_light
    for i, slot in enumerate(layout.slots):
        x0_px, y0_px, w_px, h_px = _slot_px(
            slot.rect, dpi, sheet_w_px, sheet_h_px, i
        )
        tile = None
        if i < len(paths):
            try:
                tile = vips.Image.thumbnail(
                    str(paths[i]), w_px, height=h_px, size="force", linear=linear
                )
                # The only ICC work worth doing here: a wide-gamut scan shown
                # untransformed reads as flat, and at this size it is free.
                if embedded_profile(tile) is not None:
                    tile = to_srgb8(tile)
            except vips.Error:
                tile = None
        if tile is None:
            tile = (
                vips.Image.black(w_px, h_px)
                .new_from_image([128.0, 128.0, 128.0])
                .copy(interpretation="srgb")
            )
        if tile.hasalpha():
            tile = tile.flatten(background=bg_pixel)
        canvas = canvas.insert(tile.colourspace("srgb"), x0_px, y0_px)

    return canvas.pngsave_buffer()


def _report(progress: Progress | None, message: str, value: float) -> None:
    if progress is not None:
        progress(message, value)


def _format_for(depth: int) -> str:
    return "uchar" if depth == 8 else "ushort"


def _interpretation(bands: int, depth: int) -> str:
    """What libvips should call a working-space image of this shape.

    Four bands means CMYK: an alpha channel never survives as far as the sheet,
    because a sheet is opaque and alpha is flattened against the background on
    the way in.
    """
    if bands == 1:
        return "b-w" if depth == 8 else "grey16"
    if bands == 4:
        return "cmyk"
    return "srgb" if depth == 8 else "rgb16"


def _composite_depth(spec: "Spec", infos: Sequence[ImageInfo]) -> tuple[int, list[str]]:
    """The depth the sheet is composed at, and what had to be said about it."""
    warnings: list[str] = []
    requested = spec.output.bit_depth
    if requested == "from_input":
        depth = 16 if any(i.is_16bit for i in infos) else 8
    else:
        depth = int(requested)

    if spec.output.format == "jpeg" and depth == 16:
        for info in infos:
            if info.is_16bit:
                warnings.append(
                    f"{info.path.name} is 16-bit but output.format is 'jpeg', which "
                    f"cannot hold more than 8 bits per channel; it will be written at "
                    f"8 bits"
                )
        depth = 8

    if spec.target == "screen" and depth == 16:
        for info in infos:
            if info.is_16bit:
                warnings.append(
                    f"{info.path.name} is 16-bit but target is 'screen'; the sheet will "
                    f"be flattened to 8-bit sRGB after compositing"
                )

    return depth, warnings


def _slot_px(
    rect, dpi: float, sheet_w_px: int, sheet_h_px: int, index: int
) -> tuple[int, int, int, int]:
    """A slot rect as whole pixels.

    Both edges are floored, not just the origin, so that a slot's width is the
    distance between two floored edges. Two slots either side of a gutter then
    cannot round towards each other and overlap by a pixel, and the gap between
    them stays exactly one gutter wide.
    """
    x0_px = mm_to_px_floor(rect.x_mm, dpi)
    y0_px = mm_to_px_floor(rect.y_mm, dpi)
    if x0_px >= sheet_w_px or y0_px >= sheet_h_px or x0_px < 0 or y0_px < 0:
        raise RenderError(
            f"slot {index + 1} starts at ({x0_px}, {y0_px})px, outside a sheet of "
            f"{sheet_w_px}×{sheet_h_px}px"
        )
    w_px = min(max(1, mm_to_px_floor(rect.right_mm, dpi) - x0_px), sheet_w_px - x0_px)
    h_px = min(max(1, mm_to_px_floor(rect.bottom_mm, dpi) - y0_px), sheet_h_px - y0_px)
    return x0_px, y0_px, w_px, h_px


def _resize_to(image: vips.Image, w_px: int, h_px: int) -> vips.Image:
    out = image.resize(
        w_px / image.width, vscale=h_px / image.height, kernel="lanczos3"
    )
    if (out.width, out.height) != (w_px, h_px):
        # vips_resize rounds its own way; a one-pixel drift here would put every
        # slot after this one in the wrong place.
        out = out.embed(
            0, 0, max(w_px, out.width), max(h_px, out.height), extend="copy"
        ).crop(0, 0, w_px, h_px)
    return out


def _resize_into_working(
    path: Path, w_px: int, h_px: int, profile: Profile, spec: "Spec", depth: int
) -> vips.Image:
    """Load, resample and colour-convert one image to fill one slot exactly.

    ``size="force"`` is what guarantees the composited sheet matches the solved
    layout: the distortion it can introduce is bounded by pixel rounding, well
    under 0.1%, and anything larger has already been reported as an aspect
    disagreement by the caller.
    """
    intent = spec.color.intent
    linear = spec.color.resize_in_linear_light

    if depth == 8:
        # thumbnail gives shrink-on-load and its own linear-light mode, but in
        # libvips 8.18 it always emits 8-bit, so the 16-bit path below cannot
        # use it without breaking the first rule in the spec.
        try:
            image = vips.Image.thumbnail(
                str(path), w_px, height=h_px, size="force", linear=linear
            )
        except vips.Error as exc:
            raise RenderError(f"cannot resample {path}: {exc}") from exc
        return to_working(image, profile, intent=intent, depth=8)

    try:
        image = vips.Image.new_from_file(str(path)).autorot()
    except vips.Error as exc:
        raise RenderError(f"cannot read {path}: {exc}") from exc

    if linear:
        # thumbnail's linear mode written out longhand: resample in the profile
        # connection space, which is linear, then leave it in the working space.
        try:
            light = image.icc_import(
                embedded=True, input_profile=SRGB, pcs="xyz", intent=intent
            )
            return _resize_to(light, w_px, h_px).icc_export(
                output_profile=profile_filename(profile),
                pcs="xyz",
                intent=intent,
                depth=16,
            )
        except vips.Error as exc:
            raise ColorError(
                f"could not resample {path.name} in linear light: {exc}"
            ) from exc

    return _resize_to(to_working(image, profile, intent=intent, depth=16), w_px, h_px)


def _match_bands(
    image: vips.Image, bands: int, bg_pixel: Sequence[float], depth: int, path: Path
) -> vips.Image:
    if image.bands == bands:
        return image
    if image.hasalpha() and image.bands - 1 == bands:
        # Dropping the band would composite the image against black. Resolving
        # it against the sheet colour is what the user can see and expects.
        return image.flatten(
            background=list(bg_pixel[:bands]),
            max_alpha=255.0 if depth == 8 else 65535.0,
        )
    raise RenderError(
        f"{path.name} has {image.bands} band(s) after the colour transform but the "
        f"working space has {bands}; they cannot be composited"
    )


def _resolve_output_profile(spec: "Spec", working: Profile) -> Profile:
    setting = spec.color.output_profile
    if setting == "same":
        return working
    if setting == SRGB:
        return SRGB
    try:
        return Path(setting).read_bytes()
    except OSError as exc:
        raise ColorError(
            f"color.output_profile is {setting!r}, which could not be read: {exc}"
        ) from exc


def _write(image: vips.Image, out: Path, spec: "Spec") -> None:
    try:
        if spec.output.format == "tiff":
            image.tiffsave(
                str(out), compression=spec.output.compression, predictor="horizontal"
            )
        elif spec.output.format == "png":
            image.pngsave(str(out))
        else:
            image.jpegsave(str(out), Q=spec.output.quality)
    except vips.Error as exc:
        raise RenderError(f"could not write {out}: {exc}") from exc


__all__ = [
    "SCREEN_MAX_PX",
    "ASPECT_TOLERANCE",
    "RenderError",
    "ImageInfo",
    "RenderResult",
    "probe",
    "render",
    "render_preview",
]
