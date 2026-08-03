"""PyInstaller build description for the Windows one-folder distribution.

One executable comes out: ``sodachi-gui``, windowed, the only entry point
``pyproject.toml`` declares. Sodachi has no console program, so nothing in the
bundle is meant to be run from a terminal.

The awkward part is libvips. The ``pyvips-binary`` wheel is repaired by
delvewheel, which drops the extension module ``_libvips.pyd`` and a single
DLL whose filename carries a content hash straight into ``site-packages``. The
hash changes with every wheel, so the DLL is found by glob rather than named
here; hardcoding it would keep building cleanly and stop working the moment the
wheel is upgraded.

Run through :mod:`packaging.build_exe`, or directly::

    .venv\\Scripts\\pyinstaller.exe packaging\\sodachi-gui.spec
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

# SPECPATH is injected by PyInstaller and is the directory holding this file.
PROJECT_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821
ICON = PROJECT_ROOT / "sodachi" / "gui" / "icon.ico"

GUI_SCRIPT = PROJECT_ROOT / "sodachi" / "gui" / "__main__.py"

BUNDLE_NAME = "sodachi"
GUI_NAME = "sodachi-gui"


def _libvips_files() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (binaries, datas) for the delvewheel-repaired libvips.

    The extension module is located through the import system rather than by
    assuming a venv layout, and everything else is taken from the directory it
    sits in. Failing loudly here is the point: a build that quietly omits
    libvips produces an application that cannot open a single scan.
    """
    found = importlib.util.find_spec("_libvips")
    if found is None or not found.origin:
        raise SystemExit(
            "packaging: _libvips is not importable; install pyvips[binary] into "
            "the build environment before running PyInstaller"
        )

    pyd = Path(found.origin)
    home = pyd.parent

    dlls = sorted(home.glob("libvips-*.dll"))
    if not dlls:
        raise SystemExit(
            f"packaging: no libvips-*.dll beside {pyd}; the pyvips-binary wheel "
            "was expected to place the repaired DLL next to the extension module"
        )

    binaries = [(str(pyd), ".")] + [(str(dll), ".") for dll in dlls]
    # delvewheel's load-order manifest. Unused when the DLL sits beside the
    # .pyd, as it does here, but shipped so the bundle matches the wheel.
    datas = [(str(p), ".") for p in sorted(home.glob(".load-order-pyvips_binary-*"))]
    return binaries, datas


def _package_data() -> list[tuple[str, str]]:
    """Return sodachi's own data files, read from pyproject rather than guessed.

    The glob patterns live in ``[tool.setuptools.package-data]``; taking them
    from there means adding a preset or an asset needs no edit in this file.
    """
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"]["sodachi"]

    out: list[tuple[str, str]] = []
    for pattern in patterns:
        matches = sorted((PROJECT_ROOT / "sodachi").glob(pattern))
        if not matches:
            raise SystemExit(f"packaging: package-data pattern {pattern!r} matched nothing")
        for path in matches:
            # Keep the layout, because sodachi.presets and sodachi.gui.app both
            # locate their files relative to __file__.
            out.append((str(path), path.relative_to(PROJECT_ROOT).parent.as_posix()))
    return out


VIPS_BINARIES, VIPS_DATAS = _libvips_files()

DATAS = [*VIPS_DATAS, *_package_data(), *collect_data_files("reportlab")]

# _cffi_backend is imported by the compiled _libvips.pyd, which the module
# graph cannot see into; without it pyvips falls back to ABI mode and then
# fails to find a system libvips.
HIDDEN = ["_libvips", "_cffi_backend"]

# Nothing in the project imports these, and PySide6's addons wheel is large
# enough that letting them in by accident doubles the distribution.
EXCLUDES = [
    "tkinter",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
]

gui_a = Analysis(  # noqa: F821
    [str(GUI_SCRIPT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=VIPS_BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

gui_pyz = PYZ(gui_a.pure)  # noqa: F821

# UPX is off everywhere. It corrupts the Qt plugin DLLs often enough that the
# saved megabytes are not worth an application that fails to find a platform.
gui_exe = EXE(  # noqa: F821
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name=GUI_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)

coll = COLLECT(  # noqa: F821
    gui_exe,
    gui_a.binaries,
    gui_a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=BUNDLE_NAME,
)
