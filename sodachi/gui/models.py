"""The window's model layer: one spec, one queue, and no work on the UI thread.

Two rules shape this module. Probing and rendering both decode images, so both
run on the thread pool and report back by signal. And a spec edit made with a
slider has to be able to fail — a half-dragged margin is briefly an invalid
layout — so :meth:`SodachiEngine.set_spec_value` and :meth:`SodachiEngine.resolve`
report trouble through the ``problem`` signal instead of raising into the event
loop and killing the window.

The engine is also where the capabilities that once lived only on the
command line landed: cutter export, the fit report, the standard sizes and
the full check table. Each one is a method that either returns its result or
returns nothing and emits ``problem``; none of them raises, and the ones
that touch a file go through the thread pool like the renderers.

pyvips is imported lazily throughout, so the window still opens on a machine
with no libvips and says so rather than failing to start.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, Signal

from sodachi.core.layout import Layout
from sodachi.core.mat import MatOpeningError, PrintPlan, openings_mm
from sodachi.core.solver import LayoutError, solve
from sodachi.core.units import MM_PER_INCH, mm_to_inch
from sodachi.spec import SpecError, load_spec, load_spec_text, starter_spec_text
from sodachi.spec.model import Spec

if TYPE_CHECKING:  # pragma: no cover - typing only, and both are imported lazily
    from sodachi.fit import FitPlan
    from sodachi.presets import Preset

FABRICATED_ASPECTS = (3 / 2, 2 / 3, 1.0, 6 / 7, 5 / 4)
"""Stand-in aspects so the preview has geometry before any file is added."""

THUMBNAIL_PX = 1024
"""Large enough that a slot filling a maximised preview still shows the
image crisply. The whole point of the pixels workflow is the identical image
with a border; a 128px thumbnail stretched across 900px said otherwise."""

CUT_FORMATS = (".dxf", ".svg", ".csv")
"""Output extensions :meth:`SodachiEngine.cut_async` recognises."""

DISPLAY_UNITS = ("mm", "in")

DEFAULT_PRESET = "landscape"
"""The most permissive shipped window, so the default does least to an image."""

PIXEL_LAYOUTS_SETTING = "pixel/layouts"
"""Where saved screen-target layouts live: a JSON object of name -> spec dump.

QSettings rather than a file beside the spec, because these are the user's own
recurring border recipes — '1080 story', 'print-shop proof' — not part of any
one job."""

PHANTOM_PATH = Path("<placeholder>")
"""The stand-in path a phantom item carries. Never opened."""

_EXPECTED_SLOTS = {"single": 1, "diptych": 2, "triptych": 3}


def vips_status() -> tuple[bool, str]:
    """Whether the raster path is usable, and the install hint if it is not."""
    try:
        import sodachi.render.raster  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the host
        return False, str(exc)
    return True, ""


@dataclass
class QueueItem:
    """One source file — or a phantom frame — and what is known of its shape.

    A phantom is a frame with no file behind it: a stated width and height,
    born probed, never decoded. It exists so a mat can be designed around an
    image that has not been shot or scanned yet, and it takes part in
    everything geometric — grouping, solving, the guide, the cutter — while
    the one export that needs actual pixels declines it by name.
    """

    path: Path
    width_px: int | None = None
    height_px: int | None = None
    aspect: float | None = None
    bit_depth: int | None = None
    has_profile: bool = False
    thumbnail_png: bytes | None = None
    error: str | None = None
    phantom: bool = False

    @property
    def name(self) -> str:
        if self.phantom:
            return f"placeholder {self.width_px}×{self.height_px}"
        return self.path.name

    @property
    def probed(self) -> bool:
        return self.aspect is not None and self.error is None

    def value_text(self) -> str:
        """The right-hand column of a queue row, where the duration used to be."""
        if self.error:
            return "ERROR"
        if not self.probed:
            return "…"
        if self.phantom:
            return f"{self.aspect:.3f}:1"
        depth = f"{self.bit_depth}b" if self.bit_depth else ""
        return f"{self.width_px}×{self.height_px} {depth}".strip()


@dataclass(frozen=True, slots=True)
class Job:
    """The images that make one output sheet."""

    index: int
    items: tuple[QueueItem, ...]

    @property
    def paths(self) -> list[Path]:
        return [i.path for i in self.items]

    @property
    def names(self) -> list[str]:
        return [i.name for i in self.items]

    @property
    def ready(self) -> bool:
        return bool(self.items) and all(i.probed for i in self.items)

    def aspects(self) -> list[float]:
        return [float(i.aspect) for i in self.items]

    def natural_widths(self) -> list[float]:
        return [float(i.width_px) for i in self.items]


class _TaskSignals(QObject):
    probed = Signal(object, object)
    progress = Signal(str, float)
    finished = Signal(object)
    failed = Signal(str)


class ProbeTask(QRunnable):
    """Read one file's shape and make a thumbnail, off the UI thread.

    Carries the QueueItem itself rather than its row: rows move — a removal
    or a reorder while a probe is in flight would land the result on
    whichever file now sits at the old number — and the item is the one
    identity that survives the queue being rearranged under it.
    """

    def __init__(self, item: QueueItem, thumb_px: int = THUMBNAIL_PX) -> None:
        super().__init__()
        self.signals = _TaskSignals()
        self._item = item
        self._path = item.path
        self._thumb_px = thumb_px

    def run(self) -> None:  # pragma: no cover - exercised through the engine
        try:
            import pyvips

            from sodachi.render.raster import probe

            info = probe(self._path)
            payload: dict[str, Any] = {
                "width_px": info.width_px,
                "height_px": info.height_px,
                "aspect": info.aspect,
                "bit_depth": 16 if info.is_16bit else 8,
                "has_profile": info.has_profile,
            }
            try:
                thumb = pyvips.Image.thumbnail(str(self._path), self._thumb_px)
                thumb = thumb.colourspace("srgb")
                if thumb.hasalpha():
                    thumb = thumb.flatten(background=[255.0, 255.0, 255.0])
                if thumb.format != "uchar":
                    thumb = thumb.cast("uchar")
                payload["thumbnail_png"] = thumb.pngsave_buffer()
            except Exception:
                # A thumbnail is decoration; losing it must not lose the probe.
                payload["thumbnail_png"] = None
            self.signals.probed.emit(self._item, payload)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            self.signals.probed.emit(self._item, {"error": str(exc)})


class RenderTask(QRunnable):
    """Compose and write one sheet, one mat guide, or one cut file, off the UI thread.

    The cutter writers do no decoding and finish in milliseconds, but they go
    through the pool anyway: the window's busy state, its progress bar and its
    error routing are all attached to this class, and a fourth path to the same
    three signals would be a second thing to keep correct.
    """

    def __init__(
        self,
        kind: str,
        layout: Layout,
        paths: Sequence[Path],
        spec: Spec,
        out_path: Path,
        *,
        sources: Sequence[str] | None = None,
        title: str | None = None,
        mirror: bool = False,
    ) -> None:
        super().__init__()
        self.signals = _TaskSignals()
        self._kind = kind
        self._layout = layout
        self._paths = list(paths)
        self._spec = spec
        self._out = Path(out_path)
        self._sources = list(sources) if sources else None
        self._title = title
        self._mirror = mirror

    def run(self) -> None:  # pragma: no cover - exercised through the engine
        try:
            if self._kind == "cut":
                from sodachi.export import write_csv, write_dxf, write_svg

                writers = {".dxf": write_dxf, ".svg": write_svg, ".csv": write_csv}
                writer = writers.get(self._out.suffix.lower())
                if writer is None:
                    raise ValueError(
                        f"{self._out} names no cutter format; "
                        f"expected one of {', '.join(CUT_FORMATS)}"
                    )
                self.signals.progress.emit("writing cut paths", 0.4)
                written = writer(self._layout, self._spec, self._out, mirror=self._mirror)
                self.signals.progress.emit("done", 1.0)
                self.signals.finished.emit(written)
            elif self._kind == "mat":
                from sodachi.render.matguide import render_mat_guide

                self.signals.progress.emit("drawing guide", 0.4)
                written = render_mat_guide(
                    self._layout,
                    self._spec,
                    self._out,
                    sources=self._sources,
                    title=self._title,
                )
                self.signals.progress.emit("done", 1.0)
                self.signals.finished.emit(written)
            else:
                from sodachi.render import raster

                result = raster.render(
                    self._layout,
                    self._paths,
                    self._spec,
                    self._out,
                    progress=self.signals.progress.emit,
                )
                self.signals.finished.emit(result)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            self.signals.failed.emit(str(exc))


class SodachiEngine(QObject):
    """The spec, the queue, and the solved layout the window paints."""

    specChanged = Signal()
    queueChanged = Signal()
    layoutChanged = Signal(object)
    problem = Signal(str)
    progress = Signal(str, float)
    finished = Signal(object)
    busyChanged = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._spec = load_spec_text(starter_spec_text())
        # The parked spec of whichever target is not live. Each tab keeps its
        # own whole spec, so print work and screen work never move each
        # other's numbers; the queue underneath is shared, because the files
        # are the job and the tabs are two things to make of them.
        self._parked: dict[str, Spec] = {}
        self._spec_path: Path | None = None
        self._queue: list[QueueItem] = []
        # Each target keeps its own file list, parked with its spec: what is
        # queued for printing and what is queued for bordering are different
        # jobs, not two views of one.
        self._parked_queues: dict[str, tuple[list[QueueItem], int]] = {}
        self._current_job = 0
        self._batch_tasks: list[RenderTask] = []
        self._layout: Layout | None = None
        self._padded_slots = 0
        self._placeholder_aspect: float | None = None
        self._busy = False
        self._live: set[QRunnable] = set()
        self._pool = QThreadPool.globalInstance()
        self.resolve()

    # ------------------------------------------------------------------ spec

    @property
    def spec(self) -> Spec:
        return self._spec

    @property
    def spec_path(self) -> Path | None:
        return self._spec_path

    @property
    def layout(self) -> Layout | None:
        return self._layout

    @property
    def busy(self) -> bool:
        return self._busy

    def set_target(
        self,
        target: str,
        *,
        changes: Mapping[str, Any] | None = None,
        clear: Sequence[str] = (),
    ) -> bool:
        """Switch between the print spec and the screen spec — queue and all.

        The first visit to a target derives its spec from the one in force,
        with whatever ``changes`` and ``clear`` the caller adds — that is where
        a screen spec loses its mat and gains its centred defaults. Every
        visit after that restores the parked spec exactly as it was left.
        The file list swaps with the spec: a print roll and a screen batch
        are different jobs, and the tabs stop sharing anything at all.
        """
        current = self._spec.target
        if target == current:
            return True
        parked = self._parked.pop(target, None)
        if parked is not None:
            self._parked[current] = self._spec
            self._swap_queue(current, target)
            self._spec = parked
            self.specChanged.emit()
            self.queueChanged.emit()
            self.resolve()
            return True
        before = self._spec
        self._swap_queue(current, target)
        merged = dict(changes or {})
        merged["target"] = target
        if not self.set_spec_values(merged, clear=tuple(clear)):
            self._swap_queue(target, current)
            self.queueChanged.emit()
            return False
        self._parked[current] = before
        self.queueChanged.emit()
        return True

    def _swap_queue(self, current: str, target: str) -> None:
        """Park this tab's file list and current sheet; take out the other's."""
        self._parked_queues[current] = (self._queue, self._current_job)
        self._queue, self._current_job = self._parked_queues.pop(target, ([], 0))

    def load_spec_file(self, path: str | Path) -> bool:
        """Open a design. Its frames become phantoms only if there is nothing
        real to apply it to.

        With images queued, the design applies to them — the placeholders
        were stand-ins for exactly these files, and queuing them alongside
        would double the slots. With no images, the placeholders are the only
        way to see the design as it was built, so they materialise.
        """
        try:
            loaded = load_spec(path)
        except SpecError as exc:
            self.problem.emit(str(exc))
            return False
        if loaded.target != self._spec.target:
            # The file moves us to its own tab, and the move must be the same
            # move a tab click makes: the tab in force parks its spec and its
            # queue, and the loaded target's queue comes out to receive the
            # design. Without this, a print spec opened from the screen tab
            # inherited the screen tab's files and orphaned the screen spec.
            self._parked[self._spec.target] = self._spec
            self._swap_queue(self._spec.target, loaded.target)
        self._spec = loaded
        # The file replaces this target's spec outright, parked copy included.
        self._parked.pop(self._spec.target, None)
        self._spec_path = Path(path)
        has_real = any(not item.phantom for item in self._queue)
        self._queue = [item for item in self._queue if not item.phantom]
        if not has_real:
            for frame in self._spec.placeholders:
                self._queue.append(
                    QueueItem(
                        path=PHANTOM_PATH,
                        width_px=int(round(frame.width)),
                        height_px=int(round(frame.height)),
                        aspect=frame.width / frame.height,
                        phantom=True,
                    )
                )
        elif self._spec.placeholders:
            self.problem.emit(
                "the spec's placeholder frames were not queued: the design "
                "applies to the images already loaded"
            )
        for note in self._spec.notes:
            self.problem.emit(note)
        self.specChanged.emit()
        self.queueChanged.emit()
        self.resolve()
        return True

    def save_spec_file(self, path: str | Path) -> bool:
        """Write the design, frames included.

        Every queued frame is recorded — a phantom as stated, a real file as
        its pixel size — so any spec reopened over an empty queue previews
        the geometry it was actually built around instead of stock stand-ins.
        The paths themselves are not recorded: they are the session's, and a
        design outlives its files.
        """
        from ruamel.yaml import YAML

        target = Path(path)
        data = self._spec.model_dump(mode="json", exclude_none=True)
        frames = [
            {"width": float(item.width_px), "height": float(item.height_px)}
            for item in self._queue
            if item.probed and item.width_px and item.height_px
        ]
        if frames:
            data["placeholders"] = frames
        else:
            data.pop("placeholders", None)
        try:
            with target.open("w", encoding="utf-8", newline="\n") as handle:
                yaml = YAML(typ="rt")
                yaml.default_flow_style = False
                yaml.dump(data, handle)
        except OSError as exc:
            self.problem.emit(f"cannot write {target}: {exc}")
            return False
        self._spec_path = target
        self.specChanged.emit()
        return True

    def set_spec_value(self, dotted_key: str, value: Any) -> bool:
        """Set one field by dotted path, re-validating the whole spec."""
        return self.set_spec_values({dotted_key: value})

    def set_spec_values(
        self,
        changes: Mapping[str, Any],
        *,
        clear: Sequence[str] = (),
    ) -> bool:
        """Set several fields by dotted path, re-validating the whole spec once.

        One validation for the group, because some edits are only legal
        together: a sheet given as a standard size has to lose that field in the
        same breath it gains a millimetre width, or the spec would briefly claim
        two sizes at once. ``clear`` removes a field entirely; a field that is
        already absent is not an error, since dropping the other ways of saying
        the same thing is the point.

        Returns False and emits ``problem`` rather than raising: this is called
        from a slider drag, and an intermediate value that breaks a cross-field
        rule is an ordinary event, not a crash.
        """
        data = self._spec.model_dump(exclude_none=True)
        blamed = next(iter(changes), "spec")

        for dotted_key in clear:
            parts = dotted_key.split(".")
            node: Any = data
            for part in parts[:-1]:
                node = node.get(part) if isinstance(node, dict) else None
            if isinstance(node, dict):
                node.pop(parts[-1], None)

        for dotted_key, value in changes.items():
            parts = dotted_key.split(".")
            node = data
            try:
                for part in parts[:-1]:
                    node = node[part]
                node[parts[-1]] = value
            except (KeyError, TypeError):
                self.problem.emit(f"{dotted_key} is not a field of the spec")
                return False

        try:
            candidate = Spec.model_validate(data)
        except Exception as exc:  # pydantic ValidationError, kept loose on purpose
            self.problem.emit(_first_problem(exc, blamed))
            return False

        self._spec = candidate
        self.specChanged.emit()
        self.resolve()
        return True

    # ----------------------------------------------------------------- queue

    @property
    def queue(self) -> tuple[QueueItem, ...]:
        return tuple(self._queue)

    def group_size(self) -> int:
        expected = _EXPECTED_SLOTS.get(self._spec.layout.type)
        if expected is not None:
            return expected
        return self._spec.layout.columns or max(1, len(self._queue))

    def jobs(self) -> list[Job]:
        """Sequential chunking, the one grouping: files share a sheet in the
        order they sit in the list, as many at a time as the layout takes.
        Rearranging the list is rearranging the pairing."""
        size = self.group_size()
        return [
            Job(index=n, items=tuple(self._queue[i : i + size]))
            for n, i in enumerate(range(0, len(self._queue), size))
        ]

    def current_job(self) -> Job | None:
        jobs = self.jobs()
        if not jobs:
            return None
        return jobs[min(self._current_job, len(jobs) - 1)]

    def set_current_row(self, row: int) -> None:
        job = self._job_for_row(row)
        if job is not None and job != self._current_job:
            self._current_job = job
            self.resolve()

    def _job_for_row(self, row: int) -> int | None:
        """Which sheet a queue row belongs to."""
        if row < 0 or row >= len(self._queue):
            return None
        size = self.group_size()
        if size <= 0:
            return None
        return row // size

    def add_paths(self, paths: Iterable[str | Path]) -> None:
        added = False
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                self.problem.emit(f"not a file: {path}")
                continue
            item = QueueItem(path=path)
            self._queue.append(item)
            task = ProbeTask(item)
            task.signals.probed.connect(self._on_probed)
            # The engine holds the only reference: QThreadPool owns the
            # QRunnable but not the QObject carrying its signals, and a
            # collected one drops the result on the floor.
            task.signals.probed.connect(lambda *_, t=task: self._live.discard(t))
            self._live.add(task)
            self._pool.start(task)
            added = True
        if added:
            self.queueChanged.emit()
            self.resolve()

    def remove(self, rows: Sequence[int]) -> None:
        for row in sorted({r for r in rows if 0 <= r < len(self._queue)}, reverse=True):
            del self._queue[row]
        self.queueChanged.emit()
        self.resolve()

    def add_placeholder(self, width: float, height: float) -> bool:
        """Append a phantom frame of the stated proportions.

        Width and height are whatever unit the user thinks in — 6 and 7, or
        6000 and 7000 — because only their ratio reaches the solver, plus the
        nominal width under MATCH none. Persisted at once: the phantom is a
        design decision, not a session artefact.
        """
        if self._spec.target == "screen":
            self.problem.emit(
                "a placeholder is print design; the SCREEN tab borders real files"
            )
            return False
        try:
            w = float(width)
            h = float(height)
        except (TypeError, ValueError):
            self.problem.emit("a placeholder needs numeric width and height")
            return False
        if w <= 0 or h <= 0:
            self.problem.emit("a placeholder needs a width and height above zero")
            return False
        item = QueueItem(
            path=PHANTOM_PATH,
            width_px=int(round(w)),
            height_px=int(round(h)),
            aspect=w / h,
            phantom=True,
        )
        self._queue.append(item)
        self.queueChanged.emit()
        self.resolve()
        return True

    def move(self, from_row: int, to_row: int) -> bool:
        """Put the file at ``from_row`` in front of whatever is at ``to_row``.

        Order is meaning under sequential grouping — it decides which files
        share a sheet — so rearranging re-solves like any other edit. Under a
        manifest the pairing is the CSV's and only the list's reading order
        changes, which is still the user's to arrange.
        """
        count = len(self._queue)
        if not (0 <= from_row < count):
            return False
        to_row = min(max(to_row, 0), count - 1)
        if from_row == to_row:
            return False
        item = self._queue.pop(from_row)
        self._queue.insert(to_row, item)
        self.queueChanged.emit()
        self.resolve()
        return True

    def clear(self) -> None:
        self._queue.clear()
        self._current_job = 0
        self.queueChanged.emit()
        self.resolve()

    def _on_probed(self, item: QueueItem, payload: dict[str, Any]) -> None:
        if item not in self._queue:
            return  # removed while its probe was still decoding
        if "error" in payload:
            item.error = payload["error"]
            self.problem.emit(f"{item.name}: {item.error}")
        else:
            for key, value in payload.items():
                setattr(item, key, value)
            item.error = None
        self.queueChanged.emit()
        self.resolve()

    # ----------------------------------------------------------------- solve

    def _fabricated_aspects(self, count: int) -> list[float]:
        """Stand-in aspects for a queue that is empty or still probing.

        A single slot follows the last applied print plan's image aspect when
        there is one, so the preview and the geometry exports show the frame
        the plan was derived for rather than a stock 3:2. More than one slot
        cycles the fabricated set as before: a plan describes one opening.
        """
        if count == 1 and self._placeholder_aspect is not None:
            return [self._placeholder_aspect]
        return [FABRICATED_ASPECTS[i % len(FABRICATED_ASPECTS)] for i in range(count)]

    def resolve(self) -> Layout | None:
        """Solve the current job, or fabricated aspects when it is not ready.

        A ready job that is short of its layout — a diptych holding one file
        after a removal — is padded with fabricated aspects rather than handed
        to the solver to refuse: the sheet stays solved, the empty slot shows
        a placeholder, and only the pixel export minds (it needs every slot to
        be a real file, and says so).
        """
        job = self.current_job()
        self._padded_slots = 0
        try:
            if job is not None and job.ready:
                aspects = job.aspects()
                names = list(job.names)
                widths: list[float] | None = job.natural_widths()
                if any(item.phantom for item in job.items):
                    # A phantom's "width" is the nominal the user typed — 6,
                    # for a 6x7 — not pixels, and weighing it against a real
                    # file's thousands collapses the placeholder to a sliver
                    # under MATCH none. No native size means no natural
                    # width: the sheet takes the same noted fallback a
                    # padded slot already gets.
                    widths = None
                expected = self.group_size()
                if len(aspects) < expected:
                    pad = self._fabricated_aspects(expected)[len(aspects):]
                    aspects = aspects + pad
                    names = names + [f"<placeholder {a:.3f}:1>" for a in pad]
                    # A placeholder has no native pixel width; size_match
                    # "none" falls back to aspect-proportional for the sheet.
                    widths = None
                    self._padded_slots = len(pad)
                layout = solve(
                    self._spec,
                    aspects,
                    names=names,
                    natural_widths=widths,
                    layout_name=self._spec_path.stem if self._spec_path else "layout",
                )
            else:
                aspects = self._fabricated_aspects(self.group_size())
                layout = solve(
                    self._spec,
                    aspects,
                    names=[f"<{a:.3f}:1>" for a in aspects],
                    layout_name=self._spec_path.stem if self._spec_path else "layout",
                )
        except LayoutError as exc:
            self._layout = None
            self.problem.emit(str(exc))
            self.layoutChanged.emit(None)
            return None

        self._layout = layout
        self.layoutChanged.emit(layout)
        return layout

    # ---------------------------------------------------------------- render

    def _start(self, task: RenderTask) -> None:
        task.signals.progress.connect(self.progress)
        task.signals.finished.connect(self._on_finished)
        task.signals.failed.connect(self._on_failed)
        task.signals.finished.connect(lambda *_, t=task: self._live.discard(t))
        task.signals.failed.connect(lambda *_, t=task: self._live.discard(t))
        self._live.add(task)
        self._set_busy(True)
        self._pool.start(task)

    def _set_busy(self, busy: bool) -> None:
        if busy != self._busy:
            self._busy = busy
            self.busyChanged.emit(busy)

    def _on_finished(self, result: Any) -> None:
        warnings = getattr(result, "warnings", ())
        for warning in warnings:
            self.problem.emit(warning)
        self.finished.emit(result)
        self._advance_batch()

    def _on_failed(self, message: str) -> None:
        self.problem.emit(message)
        self.finished.emit(None)
        self._advance_batch()

    def _advance_batch(self) -> None:
        """One write at a time: the next queued batch task, or quiet.

        Sequential rather than fanned out, because each render decodes full
        images and the pool is shared with the probes; a roll's worth at once
        would trade responsiveness for no finishing time worth having.
        """
        if self._batch_tasks:
            self._start(self._batch_tasks.pop(0))
        else:
            self._set_busy(False)

    def _phantom_slots(self, job: Job | None) -> int:
        """Placeholder slots on a sheet: padded gaps plus phantom frames."""
        phantoms = 0 if job is None else sum(1 for i in job.items if i.phantom)
        return self._padded_slots + phantoms

    def _ready_job(self) -> Job | None:
        job = self.current_job()
        if job is None or not job.ready:
            self.problem.emit("add some images first, and let them finish probing")
            return None
        empty = self._phantom_slots(job)
        if empty:
            self.problem.emit(
                f"this sheet still has {empty} placeholder "
                f"slot{'s' if empty > 1 else ''}; the geometry exports work "
                f"from it, but a sheet image needs real files"
            )
            return None
        if self._layout is None:
            self.problem.emit("the current settings do not solve to a layout")
            return None
        return job

    def can_export_image(self) -> bool:
        """Whether a sheet image can be exported; it needs real, probed pixels
        in every slot — a placeholder, padded or phantom, is geometry, not an
        image."""
        job = self.current_job()
        return (
            job is not None
            and job.ready
            and self._phantom_slots(job) == 0
            and self._layout is not None
        )

    def _geometry_job(self) -> tuple[Layout, list[Path], list[str]] | None:
        """The current job's layout, or one solved from the fabricated aspects.

        A mat guide and a cutter file need geometry, not pixels, so an empty
        or half-probed queue falls back to the same stand-in aspects
        :meth:`resolve` fabricates — named as placeholders, so the output says
        what it was cut from. Returns None and emits ``problem`` only when the
        spec itself will not solve.
        """
        job = self.current_job()
        if job is not None and job.ready:
            if self._layout is None:
                self.problem.emit("the current settings do not solve to a layout")
                return None
            # From the layout, not the job: a padded sheet has more slots than
            # files, and the guide's labels must line up with the windows.
            sources = [slot.source or "" for slot in self._layout.slots]
            return self._layout, job.paths, sources
        aspects = self._fabricated_aspects(self.group_size())
        names = [f"<placeholder {a:.3f}:1>" for a in aspects]
        try:
            layout = solve(
                self._spec,
                aspects,
                names=names,
                layout_name=self._spec_path.stem if self._spec_path else "layout",
            )
        except LayoutError as exc:
            self.problem.emit(str(exc))
            return None
        return layout, [], names

    def render_async(self, out_path: str | Path) -> bool:
        job = self._ready_job()
        if job is None:
            return False
        self._start(RenderTask("render", self._layout, job.paths, self._spec, Path(out_path)))
        return True

    def render_batch_async(self, out_dir: str | Path) -> bool:
        """Write every queued sheet's image into the folder the user picked.

        One output per sheet, named for its first file, collisions numbered.
        Sheets still probing or holding a phantom are skipped and counted
        aloud rather than blocking the rest: one stray placeholder should not
        hold the other thirty-seven hostage.
        """
        directory = Path(out_dir)
        if not directory.is_dir():
            self.problem.emit(f"not a folder: {directory}")
            return False
        jobs = self.jobs()
        if not jobs:
            self.problem.emit("add some images first")
            return False

        suffix = {"tiff": ".tif", "png": ".png", "jpeg": ".jpg"}[self._spec.output.format]
        tasks: list[RenderTask] = []
        skipped = 0
        used: set[Path] = set()
        for job in jobs:
            if not job.ready or any(item.phantom for item in job.items):
                skipped += 1
                continue
            try:
                layout = solve(
                    self._spec,
                    job.aspects(),
                    names=job.names,
                    natural_widths=job.natural_widths(),
                    layout_name=self._spec_path.stem if self._spec_path else "layout",
                )
            except LayoutError as exc:
                self.problem.emit(str(exc))
                skipped += 1
                continue
            stem = job.items[0].path.stem
            out = directory / f"{stem}{suffix}"
            counter = 2
            while out in used or out.exists():
                out = directory / f"{stem}-{counter}{suffix}"
                counter += 1
            used.add(out)
            tasks.append(RenderTask("render", layout, job.paths, self._spec, out))

        if skipped:
            self.problem.emit(
                f"{skipped} sheet{'s' if skipped > 1 else ''} skipped: still "
                f"probing, or holding a placeholder"
            )
        if not tasks:
            self.problem.emit("nothing ready to write")
            return False
        self._batch_tasks = tasks[1:]
        self._start(tasks[0])
        return True

    def mat_async(self, out_path: str | Path) -> bool:
        # Checked before the queue, because a spec with mats switched off is
        # the more specific complaint and has to be fixed either way.
        if not self._spec.mat.enabled:
            self.problem.emit("mat.enabled is false in this spec; turn it on first")
            return False
        made = self._geometry_job()
        if made is None:
            return False
        layout, paths, sources = made
        self._start(
            RenderTask(
                "mat",
                layout,
                paths,
                self._spec,
                Path(out_path),
                sources=sources,
                title=self._spec_path.stem if self._spec_path else "layout",
            )
        )
        return True

    def cut_async(self, out_path: str | Path, *, mirror: bool = False) -> bool:
        """Write the current job's board and windows for a computerised cutter.

        The format is the output extension: DXF R12, SVG at true millimetre
        scale, or CSV with one row per vertex.

        ``mirror`` defaults to False and deliberately ignores
        ``spec.mat.mirror``. That flag is about the printed guide: a mat cut by
        hand is cut from the back, so the drawing a person lays on the reverse
        of the board has to be reflected. A machine is given true geometry and
        works out its own orientation, and a mirrored path cuts a mirrored mat,
        which for any layout that is not symmetric is a wasted board. The
        parameter stays for the rare cutter that genuinely asks for back-side
        coordinates, and it has to be asked for by name.
        """
        out = Path(out_path)
        suffix = out.suffix.lower()
        if suffix not in CUT_FORMATS:
            named = f"the extension {suffix}" if suffix else "no extension at all"
            self.problem.emit(
                f"{out} has {named}; cutter geometry is written as "
                f"{', '.join(CUT_FORMATS)}"
            )
            return False
        # Checked before the queue, because a spec with mats switched off is
        # the more specific complaint and has to be fixed either way.
        if not self._spec.mat.enabled:
            self.problem.emit("mat.enabled is false in this spec; turn it on first")
            return False
        made = self._geometry_job()
        if made is None:
            return False
        layout, paths, _sources = made
        self._start(RenderTask("cut", layout, paths, self._spec, out, mirror=mirror))
        return True

    # ------------------------------------------------------------------- fit

    def fit_plan(self, preset_name: str | None = None) -> tuple[object, str] | None:
        """The padding decision for the current job, and the text explaining it.

        The report is the deliverable, not a debug aid: it says what shape the
        images are, what shape the preset wants, and how much border is being
        added to get from one to the other. Returns None and emits ``problem``
        when there is nothing to measure.
        """
        from sodachi.fit import plan_border
        from sodachi.presets import load_preset

        job = self.current_job()
        if job is None or not job.ready:
            self.problem.emit("add some images first, and let them finish probing")
            return None

        try:
            preset = load_preset(preset_name or DEFAULT_PRESET)
        except SpecError as exc:
            self.problem.emit(str(exc))
            return None

        sizes = [(int(item.width_px), int(item.height_px)) for item in job.items]
        try:
            plan, spec = plan_border(sizes, preset)
        except (ValueError, LayoutError) as exc:
            self.problem.emit(str(exc))
            return None
        return plan, _fit_report(plan, preset, spec, job.names)

    # ------------------------------------------------------------- layouts

    def _read_pixel_layouts(self) -> dict[str, Any]:
        stored = QSettings().value(PIXEL_LAYOUTS_SETTING)
        if not isinstance(stored, str) or not stored:
            return {}
        try:
            data = json.loads(stored)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_pixel_layouts(self, layouts: Mapping[str, Any]) -> None:
        QSettings().setValue(PIXEL_LAYOUTS_SETTING, json.dumps(dict(layouts)))

    def pixel_layouts(self) -> list[str]:
        """The saved screen layouts by name, in the order they read best."""
        return sorted(self._read_pixel_layouts())

    def save_pixel_layout(self, name: str) -> bool:
        """Keep the current screen spec under ``name``, replacing a namesake.

        Screen only: a saved layout is a border recipe for the pixels tab, and
        letting a print spec in would hand the tab a mat and a DPI it must
        then refuse.
        """
        title = str(name).strip()
        if not title:
            self.problem.emit("a saved layout needs a name")
            return False
        if self._spec.target != "screen":
            self.problem.emit("layouts are saved from the SCREEN tab; this is a print spec")
            return False
        layouts = self._read_pixel_layouts()
        layouts[title] = self._spec.model_dump(mode="json", exclude_none=True)
        self._write_pixel_layouts(layouts)
        return True

    def apply_pixel_layout(self, name: str) -> bool:
        """Put a saved screen layout in force as the current spec."""
        layouts = self._read_pixel_layouts()
        data = layouts.get(str(name).strip())
        if data is None:
            self.problem.emit(f"no saved layout named {name!r}")
            return False
        try:
            candidate = Spec.model_validate(data)
        except Exception as exc:  # pydantic ValidationError, kept loose on purpose
            self.problem.emit(_first_problem(exc, f"layout {name!r}"))
            return False
        if candidate.target != "screen":
            self.problem.emit(f"saved layout {name!r} is not a screen layout")
            return False
        if self._spec.target != "screen":
            self.problem.emit("switch to the SCREEN tab before applying a saved layout")
            return False
        self._spec = candidate
        self.specChanged.emit()
        self.resolve()
        return True

    def delete_pixel_layout(self, name: str) -> bool:
        layouts = self._read_pixel_layouts()
        if str(name).strip() not in layouts:
            self.problem.emit(f"no saved layout named {name!r}")
            return False
        del layouts[str(name).strip()]
        self._write_pixel_layouts(layouts)
        return True

    # --------------------------------------------------------------- sizes

    def standard_sizes(self) -> list[tuple[str, str]]:
        """Every standard sheet name, with its size in both units, for a picker."""
        from sodachi.sizes import STANDARD_SIZES

        return [
            (
                name,
                f"{_extent(size.width_mm, size.height_mm, 'mm')} · "
                f"{_extent(size.width_mm, size.height_mm, 'in')}",
            )
            for name, size in STANDARD_SIZES.items()
        ]

    def apply_standard_size(self, name: str) -> bool:
        """Set the sheet to a standard size by name.

        The other three ways of saying how big the sheet is go in the same
        edit, or the spec would briefly claim two sizes at once. A sheet that
        was given in pixels also loses its DPI, which it never chose: the model
        forces a pixel sheet to the nominal screen DPI, and carrying that
        number onto a sheet now measured in inches would quietly print a 16x20
        at screen resolution.
        """
        from sodachi.sizes import resolve_standard

        try:
            resolve_standard(name)
        except ValueError as exc:
            self.problem.emit(str(exc))
            return False

        clear = [
            "sheet.width_mm",
            "sheet.height_mm",
            "sheet.width_px",
            "sheet.height_px",
            "sheet.width_in",
            "sheet.height_in",
        ]
        if self._spec.sheet.given_in_px:
            clear.append("sheet.dpi")
        return self.set_spec_values({"sheet.standard": name}, clear=tuple(clear))

    def swap_orientation(self) -> bool:
        """Swap the sheet's width and height, in whichever form it was given.

        A standard-size sheet becomes its swapped explicit millimetre size —
        16x20 landscape is 508.0 × 406.4 mm and no longer "16x20" by name,
        which is correct because the name means portrait. Goes through
        :meth:`set_spec_values` so validation, re-solve and the usual signals
        all happen.
        """
        sheet = self._spec.sheet
        if sheet.standard is not None:
            size = sheet.size
            return self.set_spec_values(
                {"sheet.width_mm": size.height_mm, "sheet.height_mm": size.width_mm},
                clear=("sheet.standard",),
            )
        if sheet.given_in_px:
            return self.set_spec_values(
                {"sheet.width_px": sheet.height_px, "sheet.height_px": sheet.width_px}
            )
        if sheet.given_in_in:
            return self.set_spec_values(
                {"sheet.width_in": sheet.height_in, "sheet.height_in": sheet.width_in}
            )
        return self.set_spec_values(
            {"sheet.width_mm": sheet.height_mm, "sheet.height_mm": sheet.width_mm}
        )

    def orientation(self) -> str:
        """"portrait", "landscape" or "square", from the solved sheet's aspect.

        Falls back to the spec's own sheet size when nothing solves, so the
        toggle still reads correctly while the layout is broken mid-edit.
        """
        if self._layout is not None:
            width, height = self._layout.sheet.width_mm, self._layout.sheet.height_mm
        else:
            size = self._spec.sheet.size
            width, height = size.width_mm, size.height_mm
        if abs(width - height) < 1e-6:
            return "square"
        return "landscape" if width > height else "portrait"

    # ----------------------------------------------------------------- mat

    def mat_settings(self) -> tuple[bool, float, float]:
        """(enabled, overlap_mm, reveal_mm) — what the preview's mat band needs."""
        mat = self._spec.mat
        return bool(mat.enabled), float(mat.window_overlap_mm), float(mat.reveal_mm)

    def apply_print_plan(self, plan: PrintPlan) -> bool:
        """Rewrite the spec so it produces the print a plan derived.

        The sheet becomes the plan's minimum paper, stated in whichever unit
        the spec displays; the other ways of saying the sheet's size go in the
        same edit, exactly as :meth:`apply_standard_size` clears them. The
        margins become the explicit numbers ``(min_paper - image) / 2`` each
        way — explicit rather than 'optical', because the plan centred the
        image on the paper and a derived bottom margin would quietly move it.
        The mat takes the plan's reveal and overlap and switches on; a double
        plan switches the double mat on with its inner reveal, a single plan
        switches it off.

        The placeholder aspect follows the plan's image, so an empty queue
        previews the actual frame being ordered. Goes through
        :meth:`set_spec_values`, so a plan the spec rules reject (a screen
        target, say) emits ``problem`` and returns False with nothing changed.
        """
        paper = plan.min_paper_mm
        image = plan.image_mm
        side_mm = (paper.width_mm - image.width_mm) / 2.0
        vertical_mm = (paper.height_mm - image.height_mm) / 2.0

        changes: dict[str, Any] = {
            "margins.top_mm": vertical_mm,
            "margins.sides_mm": side_mm,
            "margins.bottom_mm": vertical_mm,
            "mat.enabled": True,
            "mat.window_overlap_mm": plan.overlap_mm,
            "mat.reveal_mm": plan.reveal_mm,
            "mat.double": plan.double,
        }
        if plan.double:
            # A single plan leaves inner_reveal_mm alone: the field is only
            # read when double, and zero would fail its own validation.
            changes["mat.inner_reveal_mm"] = plan.inner_reveal_mm
        clear = ["sheet.standard", "sheet.width_px", "sheet.height_px"]
        if self._spec.display_units == "in":
            changes["sheet.width_in"] = mm_to_inch(paper.width_mm)
            changes["sheet.height_in"] = mm_to_inch(paper.height_mm)
            clear += ["sheet.width_mm", "sheet.height_mm"]
        else:
            changes["sheet.width_mm"] = paper.width_mm
            changes["sheet.height_mm"] = paper.height_mm
            clear += ["sheet.width_in", "sheet.height_in"]
        if self._spec.sheet.given_in_px:
            # The forced screen DPI must not survive onto a physical sheet;
            # apply_standard_size drops it for the same reason.
            clear.append("sheet.dpi")

        previous_aspect = self._placeholder_aspect
        self._placeholder_aspect = image.aspect
        if not self.set_spec_values(changes, clear=tuple(clear)):
            self._placeholder_aspect = previous_aspect
            return False
        return True

    # --------------------------------------------------------------- check

    def surface_word(self) -> str:
        """What the layout's surface is called on the tab in force.

        One concept, two materials: paper under a print, canvas on a screen.
        The spec keeps calling the field ``sheet`` — renaming it would break
        every saved document — but no user reads the field name, and
        "sheet" was quietly doing double duty in the window.
        """
        return "canvas" if self._spec.target == "screen" else "paper"

    def check_rows(self, units: str | None = None) -> list[tuple[str, str]]:
        """The solved geometry as label/value pairs, in millimetres or inches.

        Defaults to the spec's ``display_units``. The layout is the one the
        engine already holds, which means the table reads the same fabricated
        aspect ratios the preview is drawing when the queue is not ready — said
        so in a note, because geometry solved from stand-in shapes is not
        something to cut board from.
        """
        chosen = self._spec.display_units if units is None else str(units).strip().lower()
        if chosen not in DISPLAY_UNITS:
            self.problem.emit(
                f"units must be one of {', '.join(DISPLAY_UNITS)}, got {units!r}"
            )
            return []

        layout = self._layout
        if layout is None:
            layout = self.resolve()  # emits its own problem if it fails
        if layout is None:
            return []

        rows = layout.check_rows() if chosen == "mm" else _inch_rows(layout)

        # The openings are what the board is actually cut with, from the same
        # derivation the guide and the cutter files use, so the table states
        # them whenever the spec asks for a mat. A combination the numbers
        # cannot hold mid-edit becomes its own row rather than an exception:
        # the table is a report, and the error's text is the report.
        if self._spec.mat.enabled:
            try:
                openings = openings_mm(
                    layout,
                    overlap_mm=self._spec.mat.window_overlap_mm,
                    reveal_mm=self._spec.mat.reveal_mm,
                )
            except MatOpeningError as exc:
                rows.append(("mat openings", str(exc)))
            else:
                rows.extend(
                    (
                        f"mat opening {index + 1}",
                        _extent(opening.width_mm, opening.height_mm, chosen),
                    )
                    for index, opening in enumerate(openings)
                )
                # What to order for the print mounted behind each window:
                # the opening plus the grip on every side. Stated for a
                # single window too — the paper rows above are the whole
                # sheet, which is the right size only when the print is
                # rendered on the full sheet rather than mounted behind
                # the board. Phrased as Requirements phrases its "paper,
                # at least": qualifier in the label, so the value column
                # stays a bare extent for the COPY-into-print-software path.
                grip_mm = 2.0 * self._spec.mat.window_overlap_mm
                rows.extend(
                    (
                        f"print for window {index + 1}, at least",
                        _extent(
                            opening.width_mm + grip_mm,
                            opening.height_mm + grip_mm,
                            chosen,
                        ),
                    )
                    for index, opening in enumerate(openings)
                )

        job = self.current_job()
        if job is None or not job.ready:
            rows.append(
                (
                    "note",
                    "the slots were solved from fabricated aspect ratios, because no "
                    f"probed image fills this {self.surface_word()} yet",
                )
            )
        rows.extend(("note", note) for note in layout.notes)
        # The surface is called by the tab's own word; the spec field stays
        # `sheet`, which no reader of this table ever sees.
        word = self.surface_word()
        return [
            (label.replace("sheet", word, 1) if label.startswith("sheet") else label, value)
            for label, value in rows
        ]


def _extent(width_mm: float, height_mm: float, units: str) -> str:
    if units == "in":
        return f"{mm_to_inch(width_mm):.3f} × {mm_to_inch(height_mm):.3f} in"
    return f"{width_mm:.2f} × {height_mm:.2f} mm"


def _inch_rows(layout: Layout) -> list[tuple[str, str]]:
    """:meth:`Layout.check_rows` restated in inches, label for label.

    The Layout stores millimetres and states millimetres, which is right for
    the object and wrong for someone whose sheet is 16x20. The labels match its
    table exactly, so switching units in the window moves the numbers and
    nothing else. The sheet is given twice, once in each unit: whichever the
    user chose, the other is the one a framer or a print lab will ask for.
    """
    width_px, height_px = layout.sheet.px_size()
    content = layout.content

    def length(value_mm: float) -> str:
        return f"{mm_to_inch(value_mm):.3f} in"

    def at(x_mm: float, y_mm: float) -> str:
        return f"({mm_to_inch(x_mm):.3f}, {mm_to_inch(y_mm):.3f})"

    rows: list[tuple[str, str]] = [
        ("layout", layout.name),
        ("sheet", _extent(layout.sheet.width_mm, layout.sheet.height_mm, "in")),
        ("sheet (mm)", _extent(layout.sheet.width_mm, layout.sheet.height_mm, "mm")),
        ("dpi", f"{layout.sheet.dpi:g}"),
        ("sheet (px)", f"{width_px} × {height_px} px"),
        ("background", layout.sheet.background_hex),
        ("margin top", length(layout.margins.top_mm)),
        ("margin bottom", length(layout.margins.bottom_mm)),
        ("margin left", length(layout.margins.left_mm)),
        ("margin right", length(layout.margins.right_mm)),
        (
            "optical weight",
            f"{layout.margins.bottom_mm / layout.margins.top_mm:.3f}× top"
            if layout.margins.top_mm > 0
            else "n/a",
        ),
        ("gutter", length(layout.gutter_mm)),
        ("size match", layout.size_match),
        ("align", layout.align),
        (
            "content",
            f"{_extent(content.width_mm, content.height_mm, 'in')} "
            f"at {at(content.x_mm, content.y_mm)}",
        ),
    ]
    for slot in layout.slots:
        label = f"slot {slot.index + 1}"
        if slot.source:
            label += f" · {slot.source}"
        rows.append(
            (
                label,
                f"{_extent(slot.rect.width_mm, slot.rect.height_mm, 'in')} "
                f"at {at(slot.rect.x_mm, slot.rect.y_mm)} "
                f"· a={slot.aspect:.4f} "
                f"· area={slot.rect.area_mm2 / (MM_PER_INCH * MM_PER_INCH):.2f} in²",
            )
        )
    return rows


def _fit_report(plan: FitPlan, preset: Preset, spec: Spec, names: Sequence[str]) -> str:
    """The plan's own report, then the numbers behind it and anything coerced."""
    measured = f"{plan.image_px[0]}×{plan.image_px[1]}px"
    if len(names) > 1:
        # plan_border fits the assembly, so this is the composed block rather
        # than any one image, and reading it as an image's size would confuse.
        measured += " (the composition, not one image)"

    lines = [plan.report(), ""]
    rows: list[tuple[str, str]] = [
        ("preset", preset.name),
        ("input", " + ".join(names) if names else "—"),
        ("measured", measured),
        ("target window", f"{preset.window[1]:.2f}:1 to {preset.window[0]:.2f}:1 (w/h)"),
        ("sheet", f"{plan.sheet_px[0]}×{plan.sheet_px[1]}px"),
        ("sheet aspect", f"{plan.sheet_aspect:.4f}:1"),
        ("pad axis", plan.pad_axis),
    ]
    if preset.description:
        # Last, because it is the only multi-line value and the numbers above
        # it read as a column until something wraps.
        rows.append(("description", " ".join(preset.description.split())))
    width = max(len(label) for label, _value in rows)
    lines.extend(f"{label.ljust(width)}  {value}" for label, value in rows)
    lines.extend(f"note: {note}" for note in spec.notes)
    return "\n".join(lines)


def _first_problem(exc: Exception, dotted_key: str) -> str:
    """One line out of a pydantic ValidationError, naming the field."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            location = ".".join(str(p) for p in first.get("loc", ())) or dotted_key
            return f"{location}: {first.get('msg', 'invalid value')}"
        except Exception:  # pragma: no cover - defensive
            pass
    return f"{dotted_key}: {exc}"


__all__ = [
    "CUT_FORMATS",
    "DEFAULT_PRESET",
    "DISPLAY_UNITS",
    "FABRICATED_ASPECTS",
    "PHANTOM_PATH",
    "PIXEL_LAYOUTS_SETTING",
    "Job",
    "ProbeTask",
    "QueueItem",
    "RenderTask",
    "SodachiEngine",
    "vips_status",
]
