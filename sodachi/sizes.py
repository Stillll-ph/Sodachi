"""The standard frame and mat sizes a sheet may be named after.

A name is resolved to millimetres once, here, so that nothing downstream ever
learns that inches or paper series exist. The table is deliberately short: it
holds the sizes ready-made frames and pre-cut mat board are actually sold in,
not every size that has ever been given a name.

Lookup is forgiving about case and separators because these names are typed by
hand into a spec file, and ``16 x 20``, ``16X20`` and ``16x20`` are the same
request.
"""

from __future__ import annotations

from difflib import get_close_matches

from sodachi.core.geometry import Size
from sodachi.core.units import inch_to_mm

_INCH_SIZES: tuple[tuple[str, float, float], ...] = (
    ("4x6", 4.0, 6.0),
    ("5x7", 5.0, 7.0),
    ("8x10", 8.0, 10.0),
    ("9x12", 9.0, 12.0),
    ("11x14", 11.0, 14.0),
    ("12x16", 12.0, 16.0),
    ("16x20", 16.0, 20.0),
    ("18x24", 18.0, 24.0),
    ("20x24", 20.0, 24.0),
    ("24x36", 24.0, 36.0),
)
"""Name, width and height in inches. The first number is always the width."""

_ISO_SIZES: tuple[tuple[str, float, float], ...] = (
    ("A4", 210.0, 297.0),
    ("A3", 297.0, 420.0),
    ("A2", 420.0, 594.0),
)
"""The ISO A series is defined in millimetres, so it is not converted."""

STANDARD_SIZES: dict[str, Size] = {
    **{name: Size(inch_to_mm(width), inch_to_mm(height)) for name, width, height in _INCH_SIZES},
    **{name: Size(width, height) for name, width, height in _ISO_SIZES},
}
"""Every standard size, in millimetres, keyed by its canonical name."""

_SEPARATORS = str.maketrans({"×": "x", "*": "x", "-": "x", "_": "x"})
_DROPPED = " \t"


def _normalise(name: str) -> str:
    """The form two spellings of one size have in common.

    Every separator becomes the ``x``, so ``16 x 20``, ``16-20`` and ``16X20``
    are one key. Runs collapse because ``16 -x 20`` is still one separator.
    """
    folded = name.strip().lower().translate(_SEPARATORS)
    squeezed = "".join(character for character in folded if character not in _DROPPED)
    while "xx" in squeezed:
        squeezed = squeezed.replace("xx", "x")
    return squeezed


_BY_KEY: dict[str, str] = {_normalise(name): name for name in STANDARD_SIZES}


def resolve_standard(name: str) -> Size:
    """The millimetre size of a standard name, however it was spelled."""
    key = _normalise(name)
    canonical = _BY_KEY.get(key)
    if canonical is not None:
        return STANDARD_SIZES[canonical]

    near = [_BY_KEY[k] for k in get_close_matches(key, list(_BY_KEY), n=3, cutoff=0.5)]
    if near:
        raise ValueError(
            f"{name!r} is not a standard size; the closest are {', '.join(near)}"
        )
    raise ValueError(
        f"{name!r} is not a standard size; the sizes I know are "
        f"{', '.join(list_standards())}"
    )


def list_standards() -> list[str]:
    """Every canonical name, smallest inch size first and the A series last."""
    return list(STANDARD_SIZES)


__all__ = ["STANDARD_SIZES", "resolve_standard", "list_standards"]
