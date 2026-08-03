"""YAML in, validated :class:`Spec` out, with errors a person can act on.

Every failure mode a spec file has — missing, unparseable, not a mapping, a
field out of range, a profile path that points nowhere — comes back as one
:class:`SpecError` whose message is already written to be shown to a person.
The window puts it straight on the status bar; it never has to reformat
anything.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from sodachi.spec.model import ColorSpec, Spec


class SpecError(ValueError):
    """Raised for anything wrong with a spec file, already formatted for a human."""


def load_spec(path: str | Path) -> Spec:
    """Read and validate a spec file, resolving profile paths beside it."""
    spec_path = Path(path)
    try:
        text = spec_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SpecError(f"{spec_path}: no such spec file") from exc
    except OSError as exc:
        raise SpecError(f"{spec_path}: could not be read ({exc.strerror})") from exc

    spec = load_spec_text(text, base_dir=spec_path.parent, source=str(spec_path))
    spec._source_path = spec_path
    return spec


def load_spec_text(text: str, *, base_dir: Path | None = None, source: str = "<string>") -> Spec:
    """Validate spec YAML held in memory.

    ``base_dir`` is what relative ICC profile paths resolve against; without it
    they resolve against the working directory, which is what a spec piped in
    on stdin should do.
    """
    yaml = YAML(typ="rt")
    try:
        data = yaml.load(text)
    except YAMLError as exc:
        raise SpecError(f"{source}: not valid YAML{_mark_suffix(exc)}") from exc

    if data is None:
        raise SpecError(f"{source}: the spec file is empty")
    if not isinstance(data, dict):
        raise SpecError(
            f"{source}: a spec must be a mapping of sections, got {type(data).__name__}"
        )

    try:
        spec = Spec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(format_validation_error(exc, source)) from exc

    _resolve_profiles(spec, base_dir=base_dir, source=source)
    return spec


def format_validation_error(exc: ValidationError, source: str) -> str:
    """Render a pydantic error as one line per problem, deepest field first."""
    problems = exc.errors()
    header = f"{source}: {len(problems)} problem{'' if len(problems) == 1 else 's'}"
    lines = [header]
    for problem in problems:
        location = ".".join(str(part) for part in problem["loc"]) or "(spec)"
        message = _clean_message(problem["msg"])
        lines.append(f"  {location}: {message} (got {problem['input']!r})")
    return "\n".join(lines)


def starter_spec_text() -> str:
    """The commented YAML ``sodachi init`` emits.

    Kept as a literal rather than dumped from a model: the comments are the
    documentation, and a round-tripped dump would lose the ones that explain
    fields the user has not set.
    """
    return _STARTER_SPEC


def _resolve_profiles(spec: Spec, *, base_dir: Path | None, source: str) -> None:
    """Turn relative ICC paths into absolute ones, or say which file is missing."""
    root = Path(base_dir) if base_dir is not None else Path.cwd()
    for field_name in ("working_profile", "output_profile"):
        value = getattr(spec.color, field_name)
        if not ColorSpec.names_a_file(value):
            continue
        candidate = Path(value)
        resolved = candidate if candidate.is_absolute() else (root / candidate)
        if not resolved.is_file():
            raise SpecError(
                f"{source}: color.{field_name} points at {resolved}, which does not exist"
            )
        setattr(spec.color, field_name, str(resolved.resolve()))


def _mark_suffix(exc: YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return ""
    return f" (line {mark.line + 1}, column {mark.column + 1})"


def _clean_message(message: str) -> str:
    """Strip pydantic's wrapper and drop to lower case for the one-line form."""
    prefix = "Value error, "
    if message.startswith(prefix):
        message = message[len(prefix) :]
    return message[:1].lower() + message[1:] if message else message


_STARTER_SPEC = """\
# Sodachi spec — version 1
#
# All geometry is in millimetres. A sheet may instead be given in pixels, in
# inches, or by the name of a standard size, and a margin in inches; each is
# converted here, once, and the layout is millimetres from then on. Delete
# anything you do not need — every field except the sheet and the margins has
# a default.

version: 1

sheet:
  width_in: 20             # a 16x20 board, landscape
  height_in: 16
  # or: width_mm / height_mm, or width_px / height_px, or standard: 16x20
  dpi: 360
  background: "#FFFFFF"    # interpreted as sRGB, converted to working space

margins:
  top_in: 3                # or top_mm, and likewise sides_mm and bottom_mm
  sides_in: 3
  bottom_in: 4             # a number pins it; "optical" or "center" derive it
  optical_ratio: 1.15      # bottom = top * ratio

layout:
  type: single             # single | diptych | triptych | grid
  gutter_mm: 12            # between images, so only felt beyond single
  size_match: area         # area | height | width | none
  align: center            # top | center | bottom | optical — this is the default

color:
  working_profile: from_first    # from_first | srgb | path to .icc
  output_profile: same           # same | srgb | path to .icc
  resize_in_linear_light: false  # more correct, but will not match Lightroom

output:
  format: tiff             # tiff | png | jpeg
  bit_depth: from_input    # from_input | 8 | 16

target: print              # print | screen
display_units: in          # mm | in — what is shown, never what is stored

mat:
  enabled: true
  window_overlap_mm: 3     # how far board overlaps the print, per side
  reveal_mm: 0             # print paper shown inside the window, per side;
                           # 0 = board grips the image edge; above 0 the overlap
                           # becomes the grip on the paper beyond the reveal
  double: false            # a second board above the first
  inner_reveal_mm: 6       # bottom board shown inside the top opening, per
                           # side, when double; margins must then cover
                           # reveal + inner_reveal + overlap
  color: "#F6F1EA"         # board colour — the top board's when double
  inner_color: "#F6F1EA"   # bottom board's colour, the band seen when double
  board_thickness_mm: 1.4  # 4-ply is about 1.4, 8-ply about 2.8
  overcut_mm: auto         # auto = board_thickness_mm
  mirror: true             # mats are cut from the back
  calibration_bar: true
"""


__all__ = [
    "SpecError",
    "load_spec",
    "load_spec_text",
    "format_validation_error",
    "starter_spec_text",
]
