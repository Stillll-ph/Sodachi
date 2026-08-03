"""The spec file as a validated object, with every cross-field rule enforced.

A spec that validates here is a spec the solver and both renderers can consume
without further checking. The rules that live here are the ones whose failure
would otherwise be discovered halfway through a render, or worse, on printed
board — a gutter wider than the margin, a 16-bit JPEG, a mat guide for a file
that will never be printed.

Coercions are never silent. Every one appends to :attr:`Spec.notes`, which the
window shows.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from sodachi.core.geometry import Size
from sodachi.core.layout import Sheet
from sodachi.core.units import NOMINAL_SCREEN_DPI, inch_to_mm, px_to_mm
from sodachi.sizes import resolve_standard

BottomMargin = float | Literal["optical", "center"]
"""A bottom margin is a number, or a rule for deriving one from the top."""

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

_PROFILE_KEYWORDS = frozenset({"from_first", "same", "srgb"})
"""Values of the two profile fields that name something other than a file."""

_SHEET_FORMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("millimetres", ("width_mm", "height_mm")),
    ("pixels", ("width_px", "height_px")),
    ("inches", ("width_in", "height_in")),
    ("a standard size", ("standard",)),
)
"""The four ways of saying how big the sheet is, and the fields each one uses."""

_MARGIN_PAIRS: tuple[tuple[str, str], ...] = (
    ("top_mm", "top_in"),
    ("sides_mm", "sides_in"),
    ("bottom_mm", "bottom_in"),
)


class SheetSpec(BaseModel):
    """The sheet, in millimetres, pixels, inches or a standard size — one of them.

    The alternative forms are input only. Each is converted in :attr:`size`,
    and the fields keep whatever the user wrote.
    """

    model_config = ConfigDict(extra="forbid")

    width_mm: float | None = Field(default=None, gt=0)
    height_mm: float | None = Field(default=None, gt=0)
    width_px: int | None = Field(default=None, gt=0)
    height_px: int | None = Field(default=None, gt=0)
    width_in: float | None = Field(default=None, gt=0)
    height_in: float | None = Field(default=None, gt=0)
    standard: str | None = None
    dpi: float = Field(default=360.0, gt=0)
    background: str = "#FFFFFF"

    @field_validator("background")
    @classmethod
    def _check_background(cls, value: str) -> str:
        if not _HEX_RE.match(value):
            raise ValueError(
                f"sheet.background must be a six-digit hex colour like '#FFFFFF', got {value!r}"
            )
        return value.upper()

    @field_validator("standard")
    @classmethod
    def _check_standard(cls, value: str | None) -> str | None:
        # Fail here rather than in `size`, where the caller has no field to blame.
        if value is not None:
            resolve_standard(value)
        return value

    @model_validator(mode="after")
    def _resolve_form(self) -> SheetSpec:
        given = [
            name
            for _label, fields in _SHEET_FORMS
            for name in fields
            if getattr(self, name) is not None
        ]
        seen = ", ".join(given) if given else "none of them"

        complete = [
            (label, fields)
            for label, fields in _SHEET_FORMS
            if all(getattr(self, name) is not None for name in fields)
        ]
        touched = [
            (label, fields)
            for label, fields in _SHEET_FORMS
            if any(getattr(self, name) is not None for name in fields)
        ]

        if len(touched) > 1:
            raise ValueError(
                "sheet must be given in millimetres, pixels, inches or a standard size, "
                f"not both; saw {seen}"
            )
        if touched and not complete:
            label, fields = touched[0]
            raise ValueError(
                f"a sheet in {label} needs both {' and '.join(fields)}; saw {seen}"
            )
        if not complete:
            raise ValueError(
                "sheet needs width_mm and height_mm, or width_px and height_px, or "
                f"width_in and height_in, or standard; saw {seen}"
            )

        if self.given_in_px:
            # The round trip has to land on the requested pixels exactly, so the
            # conversion DPI is not the user's to choose.
            if "dpi" in self.model_fields_set and self.dpi != NOMINAL_SCREEN_DPI:
                raise ValueError(
                    f"sheet.dpi is {self.dpi:g}, but a pixel-specified sheet is converted at "
                    f"{NOMINAL_SCREEN_DPI:g} dpi; remove sheet.dpi or set it to "
                    f"{NOMINAL_SCREEN_DPI:g}"
                )
            self.dpi = NOMINAL_SCREEN_DPI

        return self

    @property
    def size(self) -> Size:
        """The sheet in millimetres, whichever form it was given in.

        Every alternative form is converted here rather than written back into
        the ``_mm`` fields: pydantic re-runs this model's validator whenever the
        instance is handed to a parent model, and a sheet that had grown two
        forms would then look like a sheet that was given both.
        """
        if self.standard is not None:
            return resolve_standard(self.standard)
        if self.given_in_px:
            return Size(
                px_to_mm(self.width_px, NOMINAL_SCREEN_DPI),
                px_to_mm(self.height_px, NOMINAL_SCREEN_DPI),
            )
        if self.given_in_in:
            return Size(inch_to_mm(self.width_in), inch_to_mm(self.height_in))
        return Size(float(self.width_mm), float(self.height_mm))

    @property
    def given_in_px(self) -> bool:
        return self.width_px is not None and self.height_px is not None

    @property
    def given_in_in(self) -> bool:
        return self.width_in is not None and self.height_in is not None

    def to_sheet(self) -> Sheet:
        requested_px = (self.width_px, self.height_px) if self.given_in_px else None
        return Sheet(
            size=self.size,
            dpi=self.dpi,
            background_hex=self.background,
            requested_px=requested_px,
        )


class MarginsSpec(BaseModel):
    """The three outer margins, each in millimetres or in inches, never in both.

    Inches are converted at the input boundary rather than in a property, which
    is where the sheet does it. The two differ because the sheet is read through
    :attr:`SheetSpec.size` while a margin is read as a plain attribute — the
    solver takes ``margins.top_mm`` and :meth:`Spec._cross_field_rules` assigns
    to ``margins.bottom_mm`` — so millimetres have to be what the fields hold.
    Converting the incoming mapping keeps that true without the write-back the
    sheet avoids: the built model carries one form, so re-validating it cannot
    look like a spec that gave two.
    """

    model_config = ConfigDict(extra="forbid")

    top_mm: float = Field(ge=0)
    sides_mm: float = Field(ge=0)
    bottom_mm: BottomMargin = "optical"
    # Kept for display, excluded from every dump: the millimetres beside them
    # are the same measurement, and a dump that carried both would come back as
    # a spec that gave both. Their range is checked below, so that a negative
    # margin is reported against the field the user actually wrote.
    top_in: float | None = Field(default=None, exclude=True)
    sides_in: float | None = Field(default=None, exclude=True)
    bottom_in: BottomMargin | None = Field(default=None, exclude=True)
    optical_ratio: float = Field(default=1.15, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _convert_inches(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            # A built model handed to Spec; its millimetres are already resolved.
            return data

        converted = dict(data)
        for mm_name, in_name in _MARGIN_PAIRS:
            inches = converted.get(in_name)
            millimetres = converted.get(mm_name)
            if inches is None:
                if millimetres is None and mm_name != "bottom_mm":
                    raise ValueError(
                        f"margins needs {mm_name} or {in_name}, and has neither"
                    )
                continue
            if millimetres is not None:
                raise ValueError(
                    f"margins.{mm_name} and margins.{in_name} were both given; "
                    f"a margin takes one or the other"
                )
            if isinstance(inches, bool) or not isinstance(inches, (int, float)):
                # 'optical' and 'center' are rules, not lengths, so they pass
                # through untouched and the field's own union rejects the rest.
                converted[mm_name] = inches
                continue
            if inches < 0:
                raise ValueError(f"margins.{in_name} is {inches:g}, which is less than zero")
            converted[mm_name] = inch_to_mm(float(inches))
        return converted

    @property
    def numeric_bottom_mm(self) -> float | None:
        """The bottom margin if the user gave a number, else None.

        The rules below can only check a bottom margin they already know; the
        derived ones are checked again once the solver has resolved them.
        """
        return self.bottom_mm if isinstance(self.bottom_mm, (int, float)) else None


class LayoutSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["single", "diptych", "triptych", "grid"] = "single"
    gutter_mm: float = Field(default=12.0, ge=0)
    size_match: Literal["area", "height", "width", "none"] = "area"
    align: Literal["top", "center", "bottom", "optical"] = "center"
    columns: int | None = Field(default=None, ge=1)
    optical_align_fraction: float = Field(default=0.45, gt=0, lt=1)

    @model_validator(mode="after")
    def _columns_are_grid_only(self) -> LayoutSpec:
        if self.columns is not None and self.type != "grid":
            raise ValueError(
                f"layout.columns is only meaningful for layout.type 'grid', "
                f"but layout.type is {self.type!r}"
            )
        return self


class ColorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    working_profile: str = "from_first"
    output_profile: str = "same"
    resize_in_linear_light: bool = False
    intent: Literal["perceptual", "relative", "saturation", "absolute"] = "relative"

    @staticmethod
    def names_a_file(value: str) -> bool:
        """True when a profile field points at an ICC file rather than a keyword."""
        return value not in _PROFILE_KEYWORDS


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["tiff", "png", "jpeg"] = "tiff"
    bit_depth: Literal["from_input"] | Literal[8] | Literal[16] = "from_input"
    quality: int = Field(default=95, ge=1, le=100)
    compression: str = "lzw"


class PlaceholderFrame(BaseModel):
    """A frame with no file behind it, stated as a width and a height.

    Unitless on purpose: only the ratio reaches the solver, so 6x7 and
    6000x7000 are the same frame. Part of the spec because a placeholder is
    a design decision — the mat was cut around it — and the spec file is
    where a design is kept.
    """

    model_config = ConfigDict(extra="forbid")

    width: float = Field(gt=0)
    height: float = Field(gt=0)


class MatSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    window_overlap_mm: float = Field(default=3.0, gt=0)
    reveal_mm: float = Field(default=0.0, ge=0)
    """Print paper shown inside the window, per side. At zero the opening is
    the image inset by the overlap and the board grips the image edge itself;
    above zero the opening is the image grown by this much, and the overlap
    becomes the grip the board must keep on the paper beyond the reveal."""
    double: bool = False
    """A second board above the first. The bottom board's opening follows the
    overlap and reveal rules against the image; the top board's is the bottom
    one grown by ``inner_reveal_mm`` on every side, so a band of the bottom
    board shows inside the top window."""
    inner_reveal_mm: float = Field(default=6.0, gt=0)
    """Bottom board shown inside the top opening, per side. Only read when
    ``double`` is true, but always above zero: a band of width zero is not a
    double mat."""
    color: str = "#F6F1EA"
    """The board's colour — the top board's when double. A warm board white."""
    inner_color: str = "#F6F1EA"
    """The bottom board's colour, seen as the band inside the top opening.
    Only read when ``double`` is true."""
    board_thickness_mm: float = Field(default=1.4, gt=0)
    overcut_mm: float | Literal["auto"] = "auto"
    mirror: bool = True
    calibration_bar: bool = True

    @field_validator("color", "inner_color")
    @classmethod
    def _check_board_colour(cls, value: str, info: ValidationInfo) -> str:
        if not _HEX_RE.match(value):
            raise ValueError(
                f"mat.{info.field_name} must be a six-digit hex colour like "
                f"'#F6F1EA', got {value!r}"
            )
        return value.upper()

    @property
    def resolved_overcut_mm(self) -> float:
        """A 45-degree blade travels about the board thickness before it clears."""
        if self.overcut_mm == "auto":
            return self.board_thickness_mm
        return float(self.overcut_mm)


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    sheet: SheetSpec
    margins: MarginsSpec
    layout: LayoutSpec = LayoutSpec()
    color: ColorSpec = ColorSpec()
    output: OutputSpec = OutputSpec()
    target: Literal["print", "screen"] = "print"
    mat: MatSpec = MatSpec()
    display_units: Literal["mm", "in"] = "mm"
    """The unit the window shows. Presentation only; the solver never sees it,
    and every stored length stays a millimetre."""
    placeholders: list[PlaceholderFrame] = Field(default_factory=list)
    """The frames the design was built around — every save records the
    queue's frames here, real files as their pixel sizes and phantoms as
    stated. On open they materialise as phantoms only when no real images are
    queued; with images loaded the design applies to them instead, since
    these frames were stand-ins for exactly those files."""

    _notes: list[str] = PrivateAttr(default_factory=list)
    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def notes(self) -> tuple[str, ...]:
        """Every decision taken on the user's behalf, in the order taken."""
        return tuple(self._notes)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    def add_note(self, note: str) -> None:
        """Record a coercion once.

        The duplicate check earns its place because pydantic re-runs an after
        validator when a built model is handed to another model, and a note
        repeated twice reads like two separate decisions.
        """
        if note not in self._notes:
            self._notes.append(note)

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Spec:
        # Failing here beats writing an 8-bit file under a name the user
        # believes is 16-bit.
        if self.output.bit_depth == 16 and self.output.format == "jpeg":
            raise ValueError(
                "output.bit_depth is 16 but output.format is 'jpeg', which cannot hold "
                "more than 8 bits per channel; use tiff or png, or set bit_depth to 8"
            )

        if self.target == "screen" and self.mat.enabled:
            raise ValueError(
                "mat.enabled is true but target is 'screen'; a mat guide describes board "
                "cut for a physical print, so set target to 'print' or disable the mat"
            )

        # A reveal pushes each opening into the margin, and the board beyond
        # the opening still needs its full overlap of paper to grip. A double
        # mat pushes further still: the top opening is the bottom one grown by
        # the inner reveal. A derived bottom margin cannot be checked here; the
        # solver's resolved margins are checked again in core/mat.py.
        if self.mat.enabled and (self.mat.reveal_mm > 0 or self.mat.double):
            named_mm = [
                ("margins.top_mm", self.margins.top_mm),
                ("margins.sides_mm", self.margins.sides_mm),
            ]
            if self.margins.numeric_bottom_mm is not None:
                named_mm.append(("margins.bottom_mm", self.margins.numeric_bottom_mm))
            name, value_mm = min(named_mm, key=lambda pair: pair[1])
            if self.mat.double:
                needed_mm = (
                    self.mat.reveal_mm
                    + self.mat.inner_reveal_mm
                    + self.mat.window_overlap_mm
                )
                if value_mm < needed_mm:
                    raise ValueError(
                        f"mat.reveal_mm is {self.mat.reveal_mm:g}mm, "
                        f"mat.inner_reveal_mm is {self.mat.inner_reveal_mm:g}mm and "
                        f"mat.window_overlap_mm is {self.mat.window_overlap_mm:g}mm, "
                        f"but {name} is only {value_mm:g}mm; the reveal and the inner "
                        f"reveal leave "
                        f"{value_mm - self.mat.reveal_mm - self.mat.inner_reveal_mm:g}mm "
                        f"of board to hold the paper, and the grip needs "
                        f"{self.mat.window_overlap_mm:g}mm, so every margin must be at "
                        f"least {needed_mm:g}mm"
                    )
            else:
                needed_mm = self.mat.reveal_mm + self.mat.window_overlap_mm
                if value_mm < needed_mm:
                    raise ValueError(
                        f"mat.reveal_mm is {self.mat.reveal_mm:g}mm and mat.window_overlap_mm "
                        f"is {self.mat.window_overlap_mm:g}mm, but {name} is only "
                        f"{value_mm:g}mm; the reveal leaves "
                        f"{value_mm - self.mat.reveal_mm:g}mm of board to hold the paper, "
                        f"and the grip needs {self.mat.window_overlap_mm:g}mm, so every "
                        f"margin must be at least {needed_mm:g}mm"
                    )

        # The weighted bottom margin assumes a hung object at eye level. A
        # phone in a scrolling feed makes no such assumption.
        if self.target == "screen" and "bottom_mm" not in self.margins.model_fields_set:
            self.margins.bottom_mm = "center"
            self.add_note(
                "margins.bottom_mm defaulted to 'center' rather than 'optical' because "
                "target is 'screen'; set it explicitly to override"
            )

        if self.layout.type != "single":
            outer_mm = [self.margins.top_mm, self.margins.sides_mm]
            bottom_mm = self.margins.numeric_bottom_mm
            if bottom_mm is not None:
                outer_mm.append(bottom_mm)
            smallest_mm = min(outer_mm)
            # A gutter wider than the narrowest outer margin stops the pair
            # reading as one object. A gutter of zero cannot do that: it is the
            # edge-to-edge composition, which is a deliberate choice rather
            # than a mistake, so it is exempt even when the margins are zero too.
            if self.layout.gutter_mm > 0 and self.layout.gutter_mm >= smallest_mm:
                raise ValueError(
                    f"layout.gutter_mm is {self.layout.gutter_mm:g}mm but the smallest outer "
                    f"margin is {smallest_mm:g}mm; the gutter must be smaller or the images "
                    f"stop reading as one object"
                )

        sheet_width_mm = self.sheet.size.width_mm
        sheet_height_mm = self.sheet.size.height_mm
        if 2 * self.margins.sides_mm >= sheet_width_mm:
            raise ValueError(
                f"margins.sides_mm is {self.margins.sides_mm:g}mm, so the two side margins "
                f"take {2 * self.margins.sides_mm:g}mm of a sheet only "
                f"{sheet_width_mm:g}mm wide"
            )
        numeric_bottom_mm = self.margins.numeric_bottom_mm
        if numeric_bottom_mm is not None:
            used_mm = self.margins.top_mm + numeric_bottom_mm
            if used_mm >= sheet_height_mm:
                raise ValueError(
                    f"margins.top_mm plus margins.bottom_mm is {used_mm:g}mm on a sheet only "
                    f"{sheet_height_mm:g}mm tall"
                )

        # Not an error: the screen renderer flattens to 8-bit sRGB anyway, so
        # a TIFF here is merely a large file, not a wrong one.
        if self.target == "screen" and self.output.format == "tiff":
            self.add_note(
                "output.format is 'tiff' under target 'screen'; the screen renderer flattens "
                "to 8-bit sRGB regardless, so png or jpeg is usually what you want"
            )

        if self.sheet.given_in_px:
            self.add_note(
                f"sheet given in pixels ({self.sheet.width_px}×{self.sheet.height_px}px); "
                f"converted to millimetres at {NOMINAL_SCREEN_DPI:g} dpi and sheet.dpi forced "
                f"to {NOMINAL_SCREEN_DPI:g}"
            )

        return self


__all__ = [
    "BottomMargin",
    "SheetSpec",
    "MarginsSpec",
    "LayoutSpec",
    "ColorSpec",
    "OutputSpec",
    "MatSpec",
    "PlaceholderFrame",
    "Spec",
]
