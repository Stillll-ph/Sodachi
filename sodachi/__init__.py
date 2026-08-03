"""Sodachi — layout for images headed for a wall or a screen.

Borders, diptychs and triptychs, and true-scale mat cutting guides, for any
artwork that ends up printed and matted or bordered and posted — film scans
were the first users, not the boundary.

All geometry is millimetre-native. Pixels exist only inside the raster
renderer; points exist only inside the mat-guide renderer. See ``core/units``
for the two conversion functions that are allowed to cross that line.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
