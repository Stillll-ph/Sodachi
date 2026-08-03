"""One working space, entered once and never guessed at.

Every image that reaches the compositor has been transformed into a single
working profile, and the sheet background — a hex value the user wrote, which
is sRGB by definition — has been transformed into that same space rather than
assumed to be 255 or 65535 in whatever space happened to be active. The rule
this module exists to enforce is that two images in different spaces are never
composited.

libvips takes a profile as a filename or as one of its built-in names, never as
a byte string, so profiles carried around as bytes are spilled to a cached temp
file on the way in.
"""

from __future__ import annotations

import atexit
import hashlib
import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sodachi.spec.model import Spec

try:
    import pyvips as vips
except (ImportError, OSError) as exc:  # pragma: no cover - depends on the machine
    raise ImportError(
        "Sodachi's raster path needs libvips, which could not be loaded. Install it with "
        "`pip install \"pyvips[binary]\"` for a bundled build, or install the system "
        "library first: `brew install vips` on macOS, `apt install libvips-dev` on Debian "
        "and Ubuntu."
    ) from exc


SRGB = "srgb"
"""libvips' built-in sRGB profile. Usable as an input or an output profile."""

Profile = bytes | str
"""ICC bytes, or :data:`SRGB` for the built-in."""

_ICC_FIELD = "icc-profile-data"

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

_profile_dir: Path | None = None
_profile_files: dict[str, str] = {}
_srgb_bytes: bytes | None = None
_band_counts: dict[str, int] = {}


class ColorError(RuntimeError):
    """Raised when a colour space cannot be established or entered."""


def _cache_dir() -> Path:
    global _profile_dir
    if _profile_dir is None:
        _profile_dir = Path(tempfile.mkdtemp(prefix="sodachi-icc-"))
        atexit.register(shutil.rmtree, _profile_dir, True)
    return _profile_dir


def profile_filename(profile: Profile) -> str:
    """A name libvips will accept for ``profile``.

    Keyed on the profile's digest so a batch of two hundred sheets spills the
    same profile once rather than two hundred times.
    """
    if isinstance(profile, str):
        return profile
    key = hashlib.sha256(profile).hexdigest()
    cached = _profile_files.get(key)
    if cached is None:
        path = _cache_dir() / f"{key}.icc"
        path.write_bytes(profile)
        cached = _profile_files[key] = str(path)
    return cached


def srgb_profile_bytes() -> bytes:
    """The built-in sRGB profile as bytes, so it can be embedded and not only used.

    libvips names that profile rather than exposing it. Transforming one pixel
    into it and reading back what libvips attached is the only way out.
    """
    global _srgb_bytes
    if _srgb_bytes is None:
        probe = vips.Image.black(1, 1).new_from_image([0, 0, 0]).copy(interpretation="srgb")
        _srgb_bytes = probe.icc_transform(SRGB, input_profile=SRGB).get(_ICC_FIELD)
    return _srgb_bytes


def embedded_profile(image: vips.Image) -> bytes | None:
    """The ICC profile carried by ``image``, or None if it is untagged."""
    if _ICC_FIELD not in image.get_fields():
        return None
    return image.get(_ICC_FIELD)


def probe_profile(path: str | Path) -> bytes | None:
    """The ICC profile embedded in a file, read without decoding its pixels."""
    try:
        image = vips.Image.new_from_file(str(path), access="sequential")
    except vips.Error as exc:
        raise ColorError(f"cannot open {path} to read its ICC profile: {exc}") from exc
    return embedded_profile(image)


def resolve_working_profile(
    paths: Sequence[str | Path], spec: "Spec"
) -> tuple[Profile, str]:
    """Settle the one space every input will be transformed into.

    Returns the profile and a description fit for ``RenderResult.working_profile``
    and for the window to show.
    """
    setting = spec.color.working_profile

    if setting == SRGB:
        return SRGB, "sRGB (libvips built-in)"

    if setting == "from_first":
        if not paths:
            raise ColorError(
                "color.working_profile is 'from_first' but no input images were given, "
                "so there is no first image to take a profile from"
            )
        first = Path(paths[0])
        data = probe_profile(first)
        if data is None:
            raise ColorError(
                f"color.working_profile is 'from_first' but {first.name} has no embedded "
                f"ICC profile; set color.working_profile to 'srgb' or to the path of an "
                f".icc file"
            )
        return data, f"{first.name} (embedded, {len(data)} bytes)"

    icc_path = Path(setting)
    try:
        data = icc_path.read_bytes()
    except OSError as exc:
        raise ColorError(
            f"color.working_profile is {setting!r}, which could not be read: {exc}"
        ) from exc
    return data, str(icc_path)


def working_bands(profile: Profile) -> int:
    """How many bands the working space has: three for RGB, four for CMYK.

    The sheet background and every placed image must agree on this before
    ``insert`` will touch them.
    """
    key = profile if isinstance(profile, str) else hashlib.sha256(profile).hexdigest()
    cached = _band_counts.get(key)
    if cached is None:
        probe = vips.Image.black(1, 1).new_from_image([0, 0, 0]).copy(interpretation="srgb")
        cached = _band_counts[key] = to_working(probe, profile).bands
    return cached


def to_working(
    image: vips.Image,
    profile: Profile,
    *,
    intent: str = "relative",
    depth: int | None = None,
) -> vips.Image:
    """Transform ``image`` into the working space.

    ``depth`` is in bits and selects the output format: 16 gives ``ushort``,
    8 gives ``uchar``. An untagged input is read as sRGB, which is the only
    defensible guess; libvips falls back to ``input_profile`` only when the
    image carries nothing of its own.
    """
    kwargs: dict[str, object] = {
        "intent": intent,
        "embedded": True,
        "input_profile": SRGB,
    }
    if depth is not None:
        kwargs["depth"] = depth
    try:
        return image.icc_transform(profile_filename(profile), **kwargs)
    except vips.Error as exc:
        raise ColorError(f"could not transform into the working profile: {exc}") from exc


def attach_profile(image: vips.Image, profile: Profile | None) -> vips.Image:
    """Tag ``image`` with ``profile``, or strip its tag when ``profile`` is None.

    Stripping is a real choice rather than an oversight: an image tagged with a
    profile it is not actually in is worse than an untagged one.
    """
    out = image.copy()
    if profile is None:
        if _ICC_FIELD in out.get_fields():
            out.remove(_ICC_FIELD)
        return out
    data = srgb_profile_bytes() if profile == SRGB else profile
    out.set_type(vips.GValue.blob_type, _ICC_FIELD, data)
    return out


def srgb_hex_to_pixel(
    hex_str: str, profile: Profile, *, bands: int, depth: int
) -> list[float]:
    """A spec hex colour as a pixel in the working space.

    The value in the spec is sRGB, so white is the working space's white after
    a colorimetric transform, not a hardcoded 255 or 65535. Relative intent is
    forced here whatever the spec asked for: a flat fill has no gamut to
    compress, and perceptual intent would move the paper off white.
    """
    text = hex_str.strip()
    if not _HEX_RE.match(text):
        raise ColorError(
            f"background must be a six-digit hex colour like '#FFFFFF', got {hex_str!r}"
        )
    rgb = [float(int(text[i : i + 2], 16)) for i in (1, 3, 5)]

    probe = vips.Image.black(1, 1).new_from_image(rgb).copy(interpretation="srgb")
    try:
        out = probe.icc_transform(
            profile_filename(profile), input_profile=SRGB, intent="relative", depth=depth
        )
    except vips.Error as exc:
        raise ColorError(
            f"could not convert background {text} into the working profile: {exc}"
        ) from exc

    pixel = [float(v) for v in out(0, 0)]
    if len(pixel) >= bands:
        return pixel[:bands]
    # A band the working space does not have is alpha, and a sheet is opaque.
    opaque = 255.0 if depth == 8 else 65535.0
    return pixel + [opaque] * (bands - len(pixel))


def to_srgb8(image: vips.Image) -> vips.Image:
    """Flatten to 8-bit sRGB with the profile embedded.

    The exact inverse of the archival default, and the reason ``target: screen``
    is a target rather than a flag: a site that re-encodes what it is given
    rarely keeps an embedded profile, so a 16-bit AdobeRGB export arrives
    desaturated.
    """
    try:
        out = image.icc_transform(
            SRGB, embedded=True, input_profile=SRGB, intent="relative", depth=8
        )
    except vips.Error as exc:
        raise ColorError(f"could not convert to sRGB for screen output: {exc}") from exc
    return attach_profile(out, SRGB)


__all__ = [
    "SRGB",
    "Profile",
    "ColorError",
    "profile_filename",
    "srgb_profile_bytes",
    "embedded_profile",
    "probe_profile",
    "resolve_working_profile",
    "working_bands",
    "to_working",
    "attach_profile",
    "srgb_hex_to_pixel",
    "to_srgb8",
]
