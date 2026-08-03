"""Cut-path export for computerised mat cutters.

Three writers over one geometry. ``cutpath`` derives the closed polygons from a
solved layout; ``dxf``, ``svgpath`` and ``csvpath`` state them in the formats a
cutter is likely to accept, all of them open and none of them needing a
dependency the project did not already have.

This package is deliberately free of reportlab, pyvips and PySide6: exporting
for a machine should not require a PDF engine, an image library or a toolkit.
"""

from __future__ import annotations

from sodachi.export.csvpath import write_csv
from sodachi.export.cutpath import CutPath, CutPathError, cut_paths
from sodachi.export.dxf import write_dxf
from sodachi.export.svgpath import write_svg

__all__ = [
    "CutPath",
    "CutPathError",
    "cut_paths",
    "write_dxf",
    "write_svg",
    "write_csv",
]
