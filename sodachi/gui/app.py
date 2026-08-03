"""Starting the window, and switching the palette under a running one.

The stylesheet here is deliberately thin. Everything that carries the skin is
painted by hand in :mod:`sodachi.gui.theme` and :mod:`sodachi.gui.widgets`; QSS
covers only the things a painter cannot reach — the window background, the menu
bar, the status bar and the scrollbars — so the native frame the user asked for
sits on top of a surface that matches the panels inside it.

Because the palette is switchable, the stylesheet is built on demand rather than
at import: an f-string evaluated at module scope would freeze whichever palette
was in force when the module first loaded, and no later switch could reach it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from sodachi.gui.theme import PALETTE, MONO_FAMILIES, set_palette

# Beside the module rather than beside the working directory, so the icon is
# found whichever directory the app was launched from.
ICON_PATH = Path(__file__).with_name("icon.ico")


def stylesheet() -> str:
    """The QSS for the chrome a painter cannot reach, in the palette in force."""
    return f"""
QWidget#sodachiRoot, QMainWindow {{
    background: {PALETTE.surface.name()};
}}
QMenuBar {{
    background: {PALETTE.surface.name()};
    color: {PALETTE.ink.name()};
    border-bottom: 1px solid {PALETTE.rule.name()};
}}
QMenuBar::item:selected {{
    background: {PALETTE.fill.name()};
    color: {PALETTE.ink_strong.name()};
}}
QMenu {{
    background: {PALETTE.paper.name()};
    color: {PALETTE.ink.name()};
    border: 1px solid {PALETTE.rule.name()};
}}
/* Explicit padding, because the platform style packs the shortcut column
   against the label under the app's wide mono face — "Mat guide…Ctrl+M". */
QMenu::item {{
    padding: 4px 28px 4px 12px;
}}
QMenu::item:selected {{
    background: {PALETTE.fill.name()};
    color: {PALETTE.ink_strong.name()};
}}
QMenu::separator {{
    height: 1px;
    background: {PALETTE.rule.name()};
    margin: 4px 10px;
}}
QStatusBar {{
    background: {PALETTE.surface.name()};
    color: {PALETTE.ink_strong.name()};
    border-top: 1px solid {PALETTE.rule.name()};
}}
/* The stock dialogs — colour picker, input, message boxes — are the one place
   Qt draws buttons of its own, and the platform style paints them white
   whatever the QPalette says. These rules put the skin's fill and ink on
   them; the hand-painted widgets never consult QSS, so nothing else moves. */
QDialog {{
    background: {PALETTE.paper.name()};
    color: {PALETTE.ink.name()};
}}
QDialog QLabel {{
    color: {PALETTE.ink.name()};
}}
QPushButton {{
    background: {PALETTE.fill.name()};
    color: {PALETTE.ink_strong.name()};
    border: 1px solid {PALETTE.rule.name()};
    border-radius: 2px;
    padding: 4px 14px;
}}
QPushButton:hover {{
    background: {PALETTE.white.name()};
}}
QPushButton:pressed {{
    background: {PALETTE.rule.name()};
    color: {PALETTE.paper.name()};
}}
QDialog QLineEdit, QDialog QSpinBox {{
    background: {PALETTE.surface.name()};
    color: {PALETTE.ink_strong.name()};
    border: 1px solid {PALETTE.rule.name()};
    selection-background-color: {PALETTE.fill.name()};
    selection-color: {PALETTE.ink_strong.name()};
}}
QToolTip {{
    background: {PALETTE.surface.name()};
    color: {PALETTE.ink_strong.name()};
    border: 1px solid {PALETTE.rule.name()};
}}
QScrollBar:vertical {{
    background: {PALETTE.surface.name()};
    width: 9px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {PALETTE.fill.name()};
    min-height: 20px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}
"""


def _apply_palette(app: QApplication) -> None:
    """Give Qt's own chrome the same ink as the painted panels."""
    palette = app.palette()
    # The backdrop is the cool neutral and the panels are the warm one, so the
    # panels read as sheets laid on a desk rather than as outlined regions of a
    # single flat surface.
    palette.setColor(QPalette.ColorRole.Window, PALETTE.surface)
    palette.setColor(QPalette.ColorRole.Base, PALETTE.paper)
    palette.setColor(QPalette.ColorRole.Text, PALETTE.ink)
    palette.setColor(QPalette.ColorRole.WindowText, PALETTE.ink)
    palette.setColor(QPalette.ColorRole.Button, PALETTE.fill)
    palette.setColor(QPalette.ColorRole.ButtonText, PALETTE.ink_strong)
    palette.setColor(QPalette.ColorRole.Highlight, PALETTE.fill)
    palette.setColor(QPalette.ColorRole.HighlightedText, PALETTE.ink_strong)
    app.setPalette(palette)


def _apply_icon(app: QApplication) -> None:
    """Set the window icon, if it is there.

    The .ico is preferred over the source SVG because it carries every size
    already rendered at the pitch the artwork was designed for; handing Qt an
    SVG would let it rescale to whatever the shell asks for, and the tile's
    stripes only survive at sizes where their pitch lands on whole pixels.

    An icon is decoration. Missing or unreadable, the window still opens.
    """
    if not ICON_PATH.is_file():
        print(f"sodachi: no window icon at {ICON_PATH}", file=sys.stderr)
        return
    icon = QIcon(str(ICON_PATH))
    if icon.isNull():
        print(f"sodachi: could not read window icon {ICON_PATH}", file=sys.stderr)
        return
    app.setWindowIcon(icon)


def build_application(argv: Sequence[str] | None = None) -> QApplication:
    """Return the running QApplication, creating and styling it if needed."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv if argv is not None else sys.argv))
    app.setApplicationName("Sodachi")
    app.setApplicationDisplayName("Sodachi")
    app.setOrganizationName("Sodachi")
    font = app.font()
    font.setFamilies(MONO_FAMILIES)
    app.setFont(font)
    _apply_palette(app)
    _apply_icon(app)
    app.setStyleSheet(stylesheet())
    return app


def apply_palette_to(app: QApplication, name: str) -> None:
    """Switch ``app`` to the named palette, visibly, without a restart.

    Three things have to move together. Qt's own chrome reads the QPalette, the
    menu and status bars read the stylesheet, and every panel in the window
    reads `PALETTE` inside its paint event — and that last group is repainted
    only when something asks it to. Setting a new stylesheet re-polishes the
    styled widgets but leaves the hand-painted ones showing the old skin until
    they happen to be invalidated, so the update is walked explicitly. Children
    are updated individually rather than relying on the parent's update region:
    an opaque child paints over its own rect and would otherwise keep the
    outgoing colours.
    """
    set_palette(name)  # KeyError here names the palette, before anything changes
    _apply_palette(app)
    app.setStyleSheet(stylesheet())
    for window in app.topLevelWidgets():
        window.update()
        for child in window.findChildren(QWidget):
            child.update()


def main(argv: Sequence[str] | None = None) -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = build_application(argv)

    from sodachi.gui.main_window import MainWindow

    window = MainWindow()
    # The layout is designed for full screen, so the window opens maximised.
    # The resize first is what un-maximising restores to; without it Qt falls
    # back to the size hint, which is far too small for three rails.
    window.resize(1280, 800)
    window.showMaximized()
    return app.exec()


__all__ = ["main", "build_application", "apply_palette_to", "stylesheet"]
