"""Driver for the Windows build: run PyInstaller, then prove the result works.

An application that starts and then cannot open a TIFF is not a build, it is a
9-second demo, so this script does not stop at "PyInstaller exited 0". Sodachi
is a window and nothing else — there is no console program in the bundle to
feed a spec and a scan to — so the evidence comes from the running window
instead: the frozen executable is started offscreen, kept alive, and its
address space is read back. Requiring ``libvips-*.dll`` to be mapped into that
process proves the bundled copy loaded, which is the failure this script exists
to catch.

Usage::

    .venv\\Scripts\\python.exe packaging\\build_exe.py
    .venv\\Scripts\\python.exe packaging\\build_exe.py --no-clean --no-verify
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC = PROJECT_ROOT / "packaging" / "sodachi-gui.spec"
DIST = PROJECT_ROOT / "dist"
WORK = PROJECT_ROOT / "build"
BUNDLE = DIST / "sodachi"
GUI_EXE = BUNDLE / "sodachi-gui.exe"
INTERNAL = BUNDLE / "_internal"

# How long the window is given to get through import, QApplication and the
# first paint. Everything that kills a frozen Qt application does it inside
# this window, and libvips is loaded well before the end of it.
SETTLE_SECONDS = 12.0


class BuildError(RuntimeError):
    """A build or verification step failed. The message names the step."""


def _say(message: str) -> None:
    print(f"[build_exe] {message}", flush=True)


def clean() -> None:
    for path in (DIST, WORK):
        if path.exists():
            _say(f"removing {path}")
            shutil.rmtree(path)


def build() -> None:
    if not SPEC.is_file():
        raise BuildError(f"spec missing: {SPEC}")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        str(SPEC),
    ]
    _say(" ".join(cmd))
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise BuildError(f"PyInstaller exited {result.returncode}")


def check_layout() -> None:
    """Confirm the pieces that go missing silently are actually present."""
    if not GUI_EXE.is_file():
        raise BuildError(f"not built: {GUI_EXE}")

    strays = sorted(p.name for p in BUNDLE.glob("*.exe") if p != GUI_EXE)
    if strays:
        raise BuildError(
            f"the bundle ships more than the window: {', '.join(strays)}; "
            "sodachi-gui.exe is the only executable the spec should produce"
        )

    vips = sorted(INTERNAL.glob("libvips-*.dll"))
    if not vips:
        raise BuildError(f"no libvips-*.dll in {INTERNAL}")
    if not (INTERNAL / "_libvips.pyd").is_file():
        raise BuildError(f"no _libvips.pyd in {INTERNAL}")
    _say(f"libvips: {vips[0].name} ({vips[0].stat().st_size / 1e6:.1f} MB)")

    # Qt refuses to start without a platform plugin, and its absence shows up
    # only at runtime as "could not load the Qt platform plugin windows".
    platforms = list(INTERNAL.rglob("platforms/qwindows.dll"))
    if not platforms:
        raise BuildError(f"no platforms/qwindows.dll under {INTERNAL}")
    _say(f"qt platform plugin: {platforms[0].relative_to(BUNDLE)}")

    offscreen = list(INTERNAL.rglob("platforms/qoffscreen.dll"))
    _say(f"qt offscreen plugin: {'present' if offscreen else 'absent'}")

    presets = sorted((INTERNAL / "sodachi" / "presets").glob("*.yaml"))
    if not presets:
        raise BuildError(f"no presets under {INTERNAL / 'sodachi' / 'presets'}")
    _say(f"presets: {', '.join(p.stem for p in presets)}")

    if not (INTERNAL / "sodachi" / "gui" / "icon.ico").is_file():
        raise BuildError("sodachi/gui/icon.ico is not in the bundle")

    fonts = list((INTERNAL / "reportlab").rglob("*.pfb"))
    _say(f"reportlab fonts: {len(fonts)} type-1 files")


def _mapped_modules(pid: int) -> list[str]:
    """Every DLL the live process has mapped, by full path.

    Read through PowerShell rather than ctypes because it is the same query a
    person would run by hand against a suspect build, and the answer can be
    compared line for line with what they see.
    """
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Process -Id {pid}).Modules | ForEach-Object {{ $_.FileName }}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if proc.returncode != 0:
        raise BuildError(
            f"could not list the modules of pid {pid}: powershell exited "
            f"{proc.returncode}:\n{proc.stderr.strip()}"
        )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _require_from_bundle(modules: list[str], needle: str) -> str:
    """Return the one mapped module matching ``needle``, and prove it is ours.

    A DLL of the right name loaded from elsewhere on the system is the exact
    failure this check exists to find, so the path is required to sit under
    ``_internal`` and not merely to end in the right filename.
    """
    hits = [m for m in modules if needle in m.lower()]
    if not hits:
        raise BuildError(
            f"the running window has no {needle!r} mapped; the bundle either "
            "does not carry it or never got as far as loading it"
        )
    internal = str(INTERNAL).lower()
    inside = [m for m in hits if m.lower().startswith(internal)]
    if not inside:
        raise BuildError(
            f"{needle!r} was loaded from outside the bundle: {hits[0]}; the "
            "frozen application is using a system copy, so the bundle is not "
            "the thing being tested"
        )
    return inside[0]


def verify_gui() -> None:
    """Start the windowed executable offscreen and read its address space.

    Two things are proved at once. Everything that kills a frozen Qt
    application — a missing platform plugin, a missing module, a DLL that will
    not load — kills it during startup, so a process still alive after a few
    seconds has got through import, QApplication construction and the first
    paint. And the window asks :func:`sodachi.gui.models.vips_status` for the
    status bar while it builds, which imports pyvips, so by the time it is up
    the bundled libvips is either mapped into the process or the build is
    broken.
    """
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)

    _say("starting sodachi-gui.exe offscreen")
    proc = subprocess.Popen([str(GUI_EXE)], env=env, cwd=str(BUNDLE.parent))
    try:
        deadline = time.monotonic() + SETTLE_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise BuildError(f"sodachi-gui.exe exited early with code {proc.returncode}")
            time.sleep(0.25)
        _say(f"gui still running after {SETTLE_SECONDS:.0f}s, which is the pass condition")

        if sys.platform != "win32":
            _say("skipping the module check: it reads a Windows process")
            return

        modules = _mapped_modules(proc.pid)
        for needle in ("libvips-", "_libvips.pyd", "qt6widgets.dll"):
            found = _require_from_bundle(modules, needle)
            _say(f"mapped from the bundle: {Path(found).name}")
        # The offscreen plugin is what this run is using; on a real desktop it
        # would be qwindows.dll, which check_layout has already found on disk.
        _require_from_bundle(modules, "qoffscreen.dll")
        _say("mapped from the bundle: qoffscreen.dll")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


def report_size() -> None:
    total = sum(p.stat().st_size for p in BUNDLE.rglob("*") if p.is_file())
    count = sum(1 for p in BUNDLE.rglob("*") if p.is_file())
    _say(f"{BUNDLE}: {count} files, {total / 1e6:.1f} MB")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--clean",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Delete dist/ and build/ first.",
    )
    parser.add_argument(
        "--build",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run PyInstaller. --no-build verifies whatever is already in dist/.",
    )
    parser.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run the frozen executable and check it works.",
    )
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        _say(f"warning: this spec targets Windows and the platform is {sys.platform}")

    try:
        if args.clean and args.build:
            clean()
        if args.build:
            build()
        if args.verify:
            check_layout()
            verify_gui()
        report_size()
    except BuildError as exc:
        print(f"[build_exe] FAILED: {exc}", file=sys.stderr)
        return 1
    _say("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
