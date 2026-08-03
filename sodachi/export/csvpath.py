"""Cut paths as CSV: one row per vertex, for machines with their own importer.

Some cutters and most shop-floor scripts would rather be handed numbers than a
drawing format. This is the plainest possible statement of the same geometry --
a header row, then role, layer, point index, x, y -- in layout coordinates:
millimetres, top-left origin, y increasing downwards. There is no flip here
because there is no target convention to flip into; whatever reads this file
decides that for itself.

The closing edge is implied, exactly as it is in ``CutPath``: a four-vertex
rectangle is four rows, and the last vertex joins back to point index 0.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING

from sodachi.export.cutpath import CutPath, cut_paths, format_mm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sodachi.core.layout import Layout
    from sodachi.spec.model import Spec

HEADER = ("role", "layer", "point_index", "x_mm", "y_mm")


def csv_rows(paths: tuple[CutPath, ...]) -> list[tuple[str, ...]]:
    """The header row followed by one row per vertex."""
    out: list[tuple[str, ...]] = [HEADER]
    for path in paths:
        for index, (x_mm, y_mm) in enumerate(path.points_mm):
            out.append((path.role, path.layer, str(index), format_mm(x_mm), format_mm(y_mm)))
    return out


def csv_document(paths: tuple[CutPath, ...]) -> str:
    """The whole CSV as text, so it can be asserted on without a temp file."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows(csv_rows(paths))
    return buffer.getvalue()


def write_csv(
    layout: Layout,
    spec: Spec,
    out_path: str | Path,
    *,
    mirror: bool = False,
) -> Path:
    """Write the cut paths as CSV. See ``cut_paths`` on ``mirror``."""
    paths = cut_paths(layout, spec, mirror=mirror)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the csv module's CRLF is the only line ending written.
    out_path.write_text(csv_document(paths), encoding="utf-8", newline="")
    return out_path


__all__ = ["HEADER", "csv_document", "csv_rows", "write_csv"]
