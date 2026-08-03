# Building the Windows executable

```
.venv\Scripts\python.exe packaging\build_exe.py
```

That is the whole thing. It cleans `dist/` and `build/`, runs PyInstaller over
[sodachi-gui.spec](sodachi-gui.spec), and then runs the result to prove it
works. It exits non-zero if any step fails, so it is safe to put in front of a
release.

The output is `dist\sodachi\`, a self-contained folder holding one program:

| File | Kind | Entry point |
| --- | --- | --- |
| `sodachi-gui.exe` | windowed, no console | `sodachi.gui.app:main` |

That is the only entry point `pyproject.toml` declares. Sodachi is a window and
nothing else — the build ships no console program, so there is nothing here to
run from a terminal and no arguments to pass. `_internal` beside the executable
holds Qt, libvips and reportlab. The folder is relocatable: copy it anywhere,
run `sodachi-gui.exe`, and nothing needs to be installed on the machine.

## Useful flags

`--no-clean` keeps the previous `build/` so PyInstaller can reuse its analysis
cache; a warm rebuild is a good deal faster than the cold one.

`--no-build` skips PyInstaller entirely and just verifies whatever is already
in `dist/`. Handy when you want to re-run the checks after poking at the
bundle by hand.

`--no-verify` builds and stops. Do not use it for anything you intend to ship.

## Why one folder and not one file

PyInstaller can produce a single `.exe`, and for this application it should
not. A one-file build is a self-extracting archive: every launch unpacks the
entire bundle — here 184 MB, including an 18 MB libvips and about 90 MB of Qt —
into a fresh temporary directory, runs from there, and deletes it on exit. Three
consequences, in order of how much they will annoy you:

- Startup goes from under a second to several seconds, every single time.
- Antivirus scanners treat "unpack a hundred megabytes of DLLs into `%TEMP%`
  and execute them" as exactly the behaviour they exist to stop. Cold-start
  scanning of the extracted tree is unpredictable and occasionally fatal.
- The extraction directory is not on the DLL search path in the way an install
  directory is. libvips in particular is loaded as a native dependency of
  `_libvips.pyd` rather than by name, and one-file builds are where that
  arrangement goes wrong.

One folder trades a tidy single icon for a build that starts instantly and
loads its native libraries the ordinary way. If a single artifact is wanted for
distribution, zip the folder or wrap it in an installer — do not ask
PyInstaller to do it at runtime.

## What the spec has to handle by hand

**libvips.** The `pyvips-binary` wheel is repaired by delvewheel, which puts
`_libvips.pyd` and one DLL directly into `site-packages`. The DLL's name carries
a content hash — currently `libvips-42-36f9e86a6e1ec21e441df17473803971.dll` —
and that hash changes with every wheel release. The spec therefore globs for
`libvips-*.dll` next to the extension module and fails the build if it finds
none. Writing the hash into the spec would keep building cleanly and start
shipping a broken application the day someone upgrades the wheel.

Both files land in `_internal\`, beside each other, because CPython loads
extension modules with `LOAD_WITH_ALTERED_SEARCH_PATH` and so finds a DLL
sitting next to the `.pyd`. delvewheel's `.load-order-pyvips_binary-*` manifest
is shipped too; it is unused in this layout, but it costs nothing and keeps the
bundle a faithful copy of the wheel.

**Package data.** `sodachi/presets/*.yaml` and `sodachi/gui/icon.ico` are read
back through `Path(__file__)` at runtime, so they have to keep their directory
structure inside `_internal`. The list of patterns is read out of
`[tool.setuptools.package-data]` in `pyproject.toml` rather than repeated here,
which means adding a preset needs no edit in this directory.

**`_cffi_backend`.** `_libvips.pyd` is a compiled cffi module and imports it
from C, where PyInstaller's module graph cannot see it. Left out, `pyvips`
catches the `ImportError`, silently drops to ABI mode, hunts for a system
libvips, and fails much later with a much worse message. It is listed as a
hidden import for that reason.

**Qt.** PyInstaller's PySide6 hooks collect the platform plugins correctly, but
the failure mode when they do not — "could not load the Qt platform plugin
windows" — is common enough that `build_exe.py` asserts `qwindows.dll` is in
the tree rather than trusting it.

UPX compression is off. It saves perhaps 30 MB and corrupts Qt plugin DLLs
often enough that the trade is not worth making.

## What the verification actually proves

`build_exe.py` does not stop at "PyInstaller exited 0". After the build it:

1. Checks the pieces that go missing quietly are present: a `libvips-*.dll`,
   `_libvips.pyd`, the Qt platform plugin, the presets, the icon, the reportlab
   fonts. It also fails if the bundle contains any executable other than
   `sodachi-gui.exe`, which is how a reintroduced console program would be
   caught.
2. Starts `sodachi-gui.exe` with `QT_QPA_PLATFORM=offscreen` and requires it to
   still be alive twelve seconds later. Everything that kills a frozen Qt
   application — missing platform plugin, missing module, unloadable DLL — kills
   it during startup.
3. Reads the live process's mapped DLLs and requires `libvips-*.dll`,
   `_libvips.pyd`, `Qt6Widgets.dll` and `qoffscreen.dll` to be there **and to
   have come from `_internal`**. A DLL of the right name loaded from elsewhere
   on the system is the failure worth catching, so the path is checked, not
   just the filename.

Step 3 is what replaced the old fixture probe. There used to be a console
program in the bundle, and the script fed it a spec and a 1237×811 TIFF and
required the dimensions back, then had it write a mat guide to prove reportlab.
With the command line gone there is nothing in the bundle to drive that way, so
the evidence moved from "the frozen program computed the right answer" to "the
frozen process mapped the right libraries". The window loads pyvips while it
builds its status bar, so libvips is genuinely exercised rather than merely
shipped.

What this does not cover: reportlab is checked for presence — its module and
its Type 1 fonts are in `_internal` — but nothing in the automated run makes it
write a PDF. Exercising the mat guide now needs a person to open the window and
press `MAT`. Worth doing once per release.

The by-hand version of step 3, if you want to see it yourself:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$p = Start-Process dist\sodachi\sodachi-gui.exe -PassThru
Start-Sleep -Seconds 10
(Get-Process -Id $p.Id).Modules | ForEach-Object { $_.FileName } |
    Select-String "libvips|qoffscreen|Qt6Widgets"
Stop-Process -Id $p.Id -Force
```

A correct build shows `_internal\_libvips.pyd`, `_internal\libvips-42-*.dll`,
`_internal\PySide6\plugins\platforms\qoffscreen.dll` and
`_internal\PySide6\Qt6Widgets.dll` — all of them from the bundle, none from
elsewhere on the system.

The negative control is worth knowing about: rename `_internal\libvips-42-*.dll`
and the window comes up saying it cannot find libvips, rather than falling
through to a system copy. If it still works with the DLL renamed, the bundle is
not the thing being tested.

## Size, and where it goes

The last measured build was 236 files and 184 MB, and it carried two
executables. Dropping the console one removes a bootloader and a second copy of
the PYZ, so the current build is smaller; the figure below has not been
re-measured since, and `build_exe.py` prints the real one at the end of every
run. The breakdown is still the right shape:

| | |
| --- | --- |
| PySide6 and Qt | 93 MB |
| libvips | 18 MB |
| `opengl32sw.dll` (Mesa software GL fallback) | 21 MB |
| the bootloader and its PYZ | about half of the 23 MB two of them cost |
| Pillow, pulled in by reportlab | 13 MB |
| CPython, pydantic-core, everything else | the remainder |

The spec already excludes the PySide6 modules nothing imports — WebEngine, Qml,
Quick, Multimedia, Charts, Designer — and tkinter. What is left is either used
or is a link-time dependency of something used. Pillow could go, saving 13 MB,
but reportlab reaches for it whenever a raster image ends up in a PDF, and the
saving is not worth finding out the hard way which code path does that.

## Notes

`dist/` and `build/` are generated. Neither is source; delete them freely.

The executables are unsigned. Windows SmartScreen will warn on first run of a
downloaded copy. Signing is a separate problem and needs a certificate.

The build must run in an environment that has `pyvips[binary]` installed,
because the spec locates libvips through the import system. A system libvips
on `PATH` is not enough and will produce a bundle with no libvips in it at all;
the spec raises rather than letting that happen.
