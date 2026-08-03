"""The renderers: one solved Layout, two ways of putting it on something.

Importing this package imports pyvips, and fails with an install hint if
libvips is missing. The mat guide is fetched lazily instead, so a screen export
never pays for reportlab.
"""

from __future__ import annotations

from typing import Any

from sodachi.render.color import (
    SRGB,
    ColorError,
    attach_profile,
    embedded_profile,
    probe_profile,
    resolve_working_profile,
    srgb_hex_to_pixel,
    to_srgb8,
    to_working,
)
from sodachi.render.raster import (
    ImageInfo,
    RenderError,
    RenderResult,
    probe,
    render,
    render_preview,
)


def __getattr__(name: str) -> Any:
    if name == "render_mat_guide":
        from sodachi.render.matguide import render_mat_guide

        return render_mat_guide
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "SRGB",
    "ColorError",
    "attach_profile",
    "embedded_profile",
    "probe_profile",
    "resolve_working_profile",
    "srgb_hex_to_pixel",
    "to_srgb8",
    "to_working",
    "ImageInfo",
    "RenderError",
    "RenderResult",
    "probe",
    "render",
    "render_preview",
    "render_mat_guide",
]
