"""Target aspect windows for the border feature, kept in data files not in code.

A preset says what shape a bordered image should end up: the range of width to
height ratios that are acceptable, the pixel cap and floor for the finished
sheet, and how wide the border starts out. Nothing here is a rule imposed by
anything but the user — the shipped files are starting points, and
:func:`load_preset` takes a path to the user's own YAML just as readily as a
bundled name.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from sodachi.spec.load import SpecError


class Preset(BaseModel):
    """One target shape for a bordered image, with the limits that go with it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    aspect_min: float = Field(gt=0)
    aspect_max: float = Field(gt=0)
    max_px: int = Field(default=4096, gt=0)
    min_width_px: int = Field(default=600, gt=0)
    border_fraction: float = Field(default=0.05, ge=0, lt=0.5)
    description: str = ""

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Preset:
        if self.aspect_min > self.aspect_max:
            raise ValueError(
                f"aspect_min ({self.aspect_min:g}) is above aspect_max "
                f"({self.aspect_max:g}); the window is inverted"
            )
        return self

    @property
    def window(self) -> tuple[float, float]:
        return (self.aspect_min, self.aspect_max)


def preset_dir() -> Path:
    return Path(__file__).parent


def list_presets() -> list[str]:
    return sorted(p.stem for p in preset_dir().glob("*.yaml"))


def load_preset(name_or_path: str) -> Preset:
    """Load a bundled preset by name, or any preset file by path.

    A bare word is looked up among the shipped files; anything carrying a YAML
    suffix or a directory component is read as the user's own file, so a preset
    kept alongside a project needs no installation.
    """
    candidate = Path(name_or_path)
    if candidate.suffix in (".yaml", ".yml") or candidate.parent != Path("."):
        path = candidate
    else:
        path = preset_dir() / f"{name_or_path}.yaml"

    if not path.is_file():
        known = ", ".join(list_presets()) or "none"
        raise SpecError(f"{name_or_path}: no such preset ({path}); bundled presets are {known}")

    yaml = YAML(typ="rt")
    try:
        data = yaml.load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise SpecError(f"{path}: not valid YAML") from exc
    except OSError as exc:
        raise SpecError(f"{path}: could not be read ({exc.strerror})") from exc

    if not isinstance(data, dict):
        raise SpecError(f"{path}: a preset must be a mapping of fields")

    try:
        return Preset.model_validate(data)
    except ValueError as exc:
        raise SpecError(f"{path}: {exc}") from exc


__all__ = ["Preset", "load_preset", "list_presets", "preset_dir"]
