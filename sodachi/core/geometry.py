"""Rect, Size, Point — all millimetres, all immutable.

The ``_mm`` suffix on every field is deliberate noise. It is the thing that
makes a pixel value assigned into one of these read as obviously wrong.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Size:
    width_mm: float
    height_mm: float

    @property
    def area_mm2(self) -> float:
        return self.width_mm * self.height_mm

    @property
    def aspect(self) -> float:
        """Width over height, the convention used everywhere in the solver."""
        return self.width_mm / self.height_mm

    def scaled(self, factor: float) -> Size:
        return Size(self.width_mm * factor, self.height_mm * factor)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.width_mm:.1f}×{self.height_mm:.1f}mm"


@dataclass(frozen=True, slots=True)
class Point:
    x_mm: float
    y_mm: float

    def translated(self, dx_mm: float, dy_mm: float) -> Point:
        return Point(self.x_mm + dx_mm, self.y_mm + dy_mm)


@dataclass(frozen=True, slots=True)
class Rect:
    """An axis-aligned rectangle. Origin is top-left, y grows downwards.

    The mat-guide renderer flips to PDF's bottom-left origin at the boundary;
    that flip lives in ``render/matguide.py`` and nowhere else.
    """

    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float

    @classmethod
    def from_size(cls, size: Size, x_mm: float = 0.0, y_mm: float = 0.0) -> Rect:
        return cls(x_mm, y_mm, size.width_mm, size.height_mm)

    @property
    def right_mm(self) -> float:
        return self.x_mm + self.width_mm

    @property
    def bottom_mm(self) -> float:
        return self.y_mm + self.height_mm

    @property
    def size(self) -> Size:
        return Size(self.width_mm, self.height_mm)

    @property
    def center(self) -> Point:
        return Point(self.x_mm + self.width_mm / 2, self.y_mm + self.height_mm / 2)

    @property
    def area_mm2(self) -> float:
        return self.width_mm * self.height_mm

    @property
    def aspect(self) -> float:
        return self.width_mm / self.height_mm

    def translated(self, dx_mm: float, dy_mm: float) -> Rect:
        return Rect(self.x_mm + dx_mm, self.y_mm + dy_mm, self.width_mm, self.height_mm)

    def scaled(self, factor: float) -> Rect:
        """Scale position and size about the origin."""
        return Rect(
            self.x_mm * factor,
            self.y_mm * factor,
            self.width_mm * factor,
            self.height_mm * factor,
        )

    def inset(
        self,
        top_mm: float,
        right_mm: float | None = None,
        bottom_mm: float | None = None,
        left_mm: float | None = None,
    ) -> Rect:
        """Shrink inwards. One argument insets all four edges equally.

        Negative values grow the rect, which is how the mat guide turns a
        window opening back into a cut path.
        """
        if right_mm is None:
            right_mm = top_mm
        if bottom_mm is None:
            bottom_mm = top_mm
        if left_mm is None:
            left_mm = right_mm
        return Rect(
            self.x_mm + left_mm,
            self.y_mm + top_mm,
            self.width_mm - left_mm - right_mm,
            self.height_mm - top_mm - bottom_mm,
        )

    def union(self, other: Rect) -> Rect:
        x = min(self.x_mm, other.x_mm)
        y = min(self.y_mm, other.y_mm)
        return Rect(
            x,
            y,
            max(self.right_mm, other.right_mm) - x,
            max(self.bottom_mm, other.bottom_mm) - y,
        )

    def contains(self, other: Rect, tolerance_mm: float = 1e-6) -> bool:
        return (
            other.x_mm >= self.x_mm - tolerance_mm
            and other.y_mm >= self.y_mm - tolerance_mm
            and other.right_mm <= self.right_mm + tolerance_mm
            and other.bottom_mm <= self.bottom_mm + tolerance_mm
        )

    def __str__(self) -> str:  # pragma: no cover - display only
        return (
            f"({self.x_mm:.2f}, {self.y_mm:.2f}) {self.width_mm:.2f}×{self.height_mm:.2f}mm"
        )


def union_all(rects: list[Rect]) -> Rect:
    """Bounding box of a non-empty list of rects."""
    if not rects:
        raise ValueError("union_all needs at least one rect")
    out = rects[0]
    for r in rects[1:]:
        out = out.union(r)
    return out


__all__ = ["Size", "Point", "Rect", "union_all"]
