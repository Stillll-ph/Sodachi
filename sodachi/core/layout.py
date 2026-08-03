"""The solved result: a Sheet, resolved Margins, and concrete Slot rects.

A ``Layout`` is the single object both renderers consume. The raster renderer
multiplies it by a DPI; the mat guide multiplies it by 72/25.4. Neither is
allowed to make a layout decision of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sodachi.core.geometry import Rect, Size, union_all
from sodachi.core.units import mm_to_inch, mm_to_px


@dataclass(frozen=True, slots=True)
class Sheet:
    size: Size
    dpi: float
    background_hex: str = "#FFFFFF"
    requested_px: tuple[int, int] | None = None
    """Set when the spec gave pixel dimensions.

    The raster renderer asserts it lands on exactly these numbers, which is how
    the millimetre round trip in §1's corollary stays honest.
    """

    @property
    def width_mm(self) -> float:
        return self.size.width_mm

    @property
    def height_mm(self) -> float:
        return self.size.height_mm

    def px_size(self) -> tuple[int, int]:
        if self.requested_px is not None:
            return self.requested_px
        return (
            mm_to_px(self.size.width_mm, self.dpi),
            mm_to_px(self.size.height_mm, self.dpi),
        )


@dataclass(frozen=True, slots=True)
class Margins:
    top_mm: float
    right_mm: float
    bottom_mm: float
    left_mm: float

    @classmethod
    def symmetric(cls, top_mm: float, sides_mm: float, bottom_mm: float) -> Margins:
        return cls(top_mm, sides_mm, bottom_mm, sides_mm)

    @property
    def smallest_mm(self) -> float:
        return min(self.top_mm, self.right_mm, self.bottom_mm, self.left_mm)


@dataclass(frozen=True, slots=True)
class Slot:
    """Where one image goes, in millimetres on the sheet."""

    index: int
    rect: Rect
    aspect: float
    source: str | None = None
    row: int = 0
    column: int = 0


@dataclass(frozen=True, slots=True)
class Layout:
    name: str
    sheet: Sheet
    margins: Margins
    gutter_mm: float
    slots: tuple[Slot, ...]
    scale: float
    """Relative solver units to millimetres. Diagnostic; renderers ignore it."""
    size_match: str = "area"
    align: str = "center"
    notes: tuple[str, ...] = field(default_factory=tuple)
    """Decisions the solver made that the user did not spell out."""

    @property
    def available(self) -> Rect:
        """The box left after margins, in sheet coordinates."""
        return Rect(
            self.margins.left_mm,
            self.margins.top_mm,
            self.sheet.width_mm - self.margins.left_mm - self.margins.right_mm,
            self.sheet.height_mm - self.margins.top_mm - self.margins.bottom_mm,
        )

    @property
    def content(self) -> Rect:
        """Bounding box of every slot."""
        return union_all([s.rect for s in self.slots])

    @property
    def row_count(self) -> int:
        return max((s.row for s in self.slots), default=0) + 1

    def rows(self) -> list[list[Slot]]:
        out: list[list[Slot]] = [[] for _ in range(self.row_count)]
        for slot in self.slots:
            out[slot.row].append(slot)
        return [sorted(r, key=lambda s: s.column) for r in out]

    def check_rows(self) -> list[tuple[str, str]]:
        """Label/value pairs for the window's check table.

        Every value is in millimetres unless it says otherwise. This table is
        the fastest debugging tool in the project and it costs nothing.
        """
        w_px, h_px = self.sheet.px_size()
        content = self.content
        rows: list[tuple[str, str]] = [
            ("layout", self.name),
            ("sheet", f"{self.sheet.width_mm:.2f} × {self.sheet.height_mm:.2f} mm"),
            (
                "sheet (in)",
                f"{mm_to_inch(self.sheet.width_mm):.2f} × "
                f"{mm_to_inch(self.sheet.height_mm):.2f} in",
            ),
            ("dpi", f"{self.sheet.dpi:g}"),
            ("sheet (px)", f"{w_px} × {h_px} px"),
            ("background", self.sheet.background_hex),
            ("margin top", f"{self.margins.top_mm:.2f} mm"),
            ("margin bottom", f"{self.margins.bottom_mm:.2f} mm"),
            ("margin left", f"{self.margins.left_mm:.2f} mm"),
            ("margin right", f"{self.margins.right_mm:.2f} mm"),
            (
                "optical weight",
                f"{self.margins.bottom_mm / self.margins.top_mm:.3f}× top"
                if self.margins.top_mm > 0
                else "n/a",
            ),
            ("gutter", f"{self.gutter_mm:.2f} mm"),
            ("size match", self.size_match),
            ("align", self.align),
            (
                "content",
                f"{content.width_mm:.2f} × {content.height_mm:.2f} mm "
                f"at ({content.x_mm:.2f}, {content.y_mm:.2f})",
            ),
        ]
        for slot in self.slots:
            label = f"slot {slot.index + 1}"
            if slot.source:
                label += f" · {slot.source}"
            rows.append(
                (
                    label,
                    f"{slot.rect.width_mm:.2f} × {slot.rect.height_mm:.2f} mm "
                    f"at ({slot.rect.x_mm:.2f}, {slot.rect.y_mm:.2f}) "
                    f"· a={slot.aspect:.4f} · area={slot.rect.area_mm2 / 100:.2f} cm²",
                )
            )
        return rows


__all__ = ["Sheet", "Margins", "Slot", "Layout"]
