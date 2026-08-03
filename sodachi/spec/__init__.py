"""The spec file: pydantic models and the YAML loader.

This package must not import ``sodachi.core.solver``. The solver duck-types
:class:`Spec` precisely so that the dependency runs one way only — spec knows
about millimetres, the solver knows about specs, and neither needs the other's
imports at module scope.
"""

from sodachi.spec.load import (
    SpecError,
    format_validation_error,
    load_spec,
    load_spec_text,
    starter_spec_text,
)
from sodachi.spec.model import (
    BottomMargin,
    ColorSpec,
    LayoutSpec,
    MarginsSpec,
    MatSpec,
    OutputSpec,
    SheetSpec,
    Spec,
)

__all__ = [
    "BottomMargin",
    "SheetSpec",
    "MarginsSpec",
    "LayoutSpec",
    "ColorSpec",
    "OutputSpec",
    "MatSpec",
    "Spec",
    "SpecError",
    "load_spec",
    "load_spec_text",
    "format_validation_error",
    "starter_spec_text",
]
