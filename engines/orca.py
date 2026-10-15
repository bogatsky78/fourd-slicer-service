"""Upstream OrcaSlicer engine.

Chosen over the Snapmaker Orca fork because the fork's CLI segfaults on every
3MF project — see the note in ../Dockerfile for the backtrace. Upstream ships
the Snapmaker U1 profiles too, so nothing is lost.
"""
from __future__ import annotations

import base64
import dataclasses
import gzip
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
import zipfile

import layout
import paint
import supports

from .base import (
    Assembly,
    EngineUnavailable,
    FilamentUsage,
    ModelInfo,
    ModelObject,
    PlateLayout,
    PlateUsage,
    SliceFailed,
    SliceRequest,
    SliceResult,
    SlicerEngine,
)

SLICE_INFO = "Metadata/slice_info.config"
MODEL_SETTINGS = "Metadata/model_settings.config"
MODEL_FILE = "3D/3dmodel.model"
PROJECT_SETTINGS = "Metadata/project_settings.config"
# Written by the binary into the output directory on the way out, success or
# failure, and the only place the reason is spelled in words.
RESULT_JSON = "result.json"

# `--info` prints one `key = value` per line under a `[filename]` header.
INFO_LINE = re.compile(r"^\s*([a-z_]+)\s*=\s*(.+?)\s*$")

# G-code the binary leaves in the output directory next to the exported 3MF.
GCODE_MOVE = re.compile(r"^G[01] ")
GCODE_XY = re.compile(r"\b([XY])(-?\d+\.?\d*)")
FILAMENT_CHANGES = re.compile(r"^;\s*total filament change\s*=\s*(\d+)", re.M)
PRIME_TOWER_FEATURE = ";TYPE:Prime tower"

# Orca refuses a print whose G-code leaves the bed with this code, which it
# reports as exit status 154 (256 - 102).
GCODE_OUTSIDE_BED = -102

# And it refuses the plate before slicing it — as exit status 206 — when nothing
# on the plate is *fully* inside the bed. The two are one problem seen from two
# distances: this one is the objects as the file lays them out, `-102` is the
# toolpaths that came out of them, which is why a prime tower we placed
# ourselves can only ever show up as the latter. Its message says "empty or has
# no object fully inside", because the binary cannot tell those apart at that
# point — the file can, and `_plate_has_objects()` asks it.
PLATE_NOT_INSIDE = -50

# And — as exit status 204 — when some object is only partly on the bed: the
# file's own layout puts a part over the edge. The same family as the two above,
# found before slicing; see _plate_onto_bed().
OBJECTS_PARTLY_INSIDE = -52

# Orca lays the plates of a file out in a grid on one canvas, a fifth of a bed
# apart; a plate's bed in the file's coordinates follows from its index.
PLATE_GAP = 0.2

# And it refuses a sliced plate — exit status 155 — when two toolpaths cross:
# its own check after slicing found the G-code of one thing inside another. The
# file's objects do not collide with each other in a file anyone has printed
# from, so in practice this is the prime tower, which the engine places where
# the file says even on a bed the file was not laid out for — see
# `_tower_away()`. Reported in words only in the engine's own log, never on
# either stream, which is why every run is now asked to write one.
GCODE_CONFLICT = -101

# The engine's own log, asked for on every run with `--debug 4 --logfile`. Four
# things live only there and nowhere on stdout or stderr: which two things a
# `-101` found crossing, whether the plate was moved to fit a smaller bed than
# the file's, where the engine thinks the prime tower is, and which object a
# `-100` found floating. The last one is a status message the engine logs only
# from level 4 up, which is why the level is 4 and not 3: a few thousand lines,
# about 350 KB on a full plate, deleted with the rest of the working directory.
ENGINE_LOG = "engine.log"
ENGINE_LOG_LEVEL = "4"
CONFLICT_LINE = re.compile(r"gcode path conflicts found between (.+?)\s*$", re.M)
SHRUNK_LINE = re.compile(r"is larger than new printable size")
NEW_CENTER_LINE = re.compile(r"new_center: \{\s*(-?[\d.]+),\s*(-?[\d.]+)\s*\}")
TOWER_ESTIMATE_LINE = re.compile(
    r"wipe bbox: min \{\s*(-?[\d.]+),\s*(-?[\d.]+),[^}]*\}\s*-\s*max \{\s*(-?[\d.]+),\s*(-?[\d.]+),"
)
TOWER_ORIGIN_LINE = re.compile(r"wipe_x\s+(-?[\d.]+),\s*wipe_y\s+(-?[\d.]+)")
# The engine names the tower this way in its conflict line.
TOWER_PARTY = "WipeTower"

# And it refuses a plate — exit status 156, `-100`, "Failed slicing the model" —
# when an object has islands that start in mid-air and the file has supports
# off for it. The name of the object is in the engine's log only.
SLICE_FAILED = -100
FLOATING_LINE = re.compile(r"It seems object (.+?) has floating regions")
# Written whenever the plate's supports are trees; see the last remedy in
# _remedy() for why it matters.
TREE_SUPPORT_LINE = re.compile(r"Tree support overhang detection done")


@dataclasses.dataclass
class _Remedy:
    """One thing to change about a refused plate, and the sentence for it."""

    kind: str
    argv: list[str]
    note: str
    # A rewritten copy of the file to slice from now on, for the one remedy
    # that cannot be said on the command line (_plate_onto_bed).
    source: str | None = None
    # True when the finding is about the file rather than about one plate, so
    # the rest of the plates can start with it instead of each paying for the
    # same refused run.
    sticky: bool = False


class OrcaSlicerEngine(SlicerEngine):
    code = "orca"
    label = "OrcaSlicer"

    def __init__(
        self,
        binary: str = "/opt/engines/orca/bin/orca-slicer",
        env_wrapper: str = "/opt/engines/orca/libexec/orca-slicer-env",
        profiles_dir: str = "/opt/engines/orca/resources/profiles",
        timeout: int = 1800,
    ):
        self.binary = binary
        self.env_wrapper = env_wrapper
        self.profiles_dir = profiles_dir
        self.timeout = timeout
        self._version: str | None = None
        # The journal of the slice running on this thread; see slice().
        self._local = threading.local()

    # -- discovery ---------------------------------------------------------

    def is_available(self) -> bool:
        return os.path.exists(self.binary)

    def version(self) -> str:
        if self._version is not None:
            return self._version

        proc = self._run(["--help"], expect_success=False)
        # The banner reads "OrcaSlicer-2.4.2:" and may land on either stream,
        # interleaved with boost log lines.
        match = re.search(r"OrcaSlicer-([\d.]+)", f"{proc.stdout}\n{proc.stderr}")
        # The image pins the version it fetched, so that is a reliable fallback.
        self._version = match.group(1) if match else os.environ.get(
            "ORCA_VERSION", "unknown"
        )
        return self._version

    def profiles(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {"machine": [], "process": [], "filament": []}
        if not os.path.isdir(self.profiles_dir):
            return found
        for vendor in sorted(os.listdir(self.profiles_dir)):
            vendor_dir = os.path.join(self.profiles_dir, vendor)
            if not os.path.isdir(vendor_dir):
                continue
            for kind in found:
                kind_dir = os.path.join(vendor_dir, kind)
                if not os.path.isdir(kind_dir):
                    continue
                for name in sorted(os.listdir(kind_dir)):
                    if name.endswith(".json"):
                        found[kind].append(f"{vendor}/{kind}/{name[:-5]}")
        return found

    # -- slicing -----------------------------------------------------------

    def preprocess(
        self,
        input_path: str,
        workdir: str,
        project_overrides: dict | None = None,
        object_extruders: dict | None = None,
        object_settings: dict | None = None,
        heads: int | None = None,
        mesh_paint: dict | None = None,
    ) -> str:
        """Bambu project files need repairing before Orca will load them. Files
        that are already clean pass through untouched."""
        from repair3mf import repair

        if not input_path.lower().endswith(".3mf"):
            return input_path
        with zipfile.ZipFile(input_path) as zf:
            if PROJECT_SETTINGS not in zf.namelist():
                return input_path

        repaired = os.path.join(workdir, "repaired.3mf")
        repair(
            input_path, repaired, project_overrides, object_extruders, object_settings, heads, mesh_paint
        )
        return repaired

    def slice(self, request: SliceRequest, workdir: str) -> SliceResult:
        """Slice the file, and say how: a journal of the steps and every engine
        run's full log, on the answer and on the refusal alike.

        **Why it is carried out rather than kept.** Until 1.13 a refused plate
        left a sentence and nothing else: the work directory with the engine's
        log is deleted at the end of every request, and finding out why the
        capybara's plate 5 failed took slicing it again by hand in the
        container — where it turned out to be this service's own doing (paint,
        see paint.py). The caller stores what it is given, so the
        service itself keeps nothing and its RAM disk stays empty.

        `journal` is a list of short entries — `{plate, pass, step, text, ...}`,
        steps `prepare`, `split`, `run`, `remedy` — in the order they happened.
        `engine_logs` holds one entry per run of the binary: `name`, `plate`,
        `pass`, `run`, and `gzip_base64` of the command line, both streams and
        the engine's own log file.
        """
        self._local.journal = []
        self._local.logs = []
        self._local.pass_no = None
        try:
            result = self._slice_file(request, workdir)
        except SliceFailed as exc:
            exc.journal, exc.engine_logs = self._local.journal, self._local.logs
            raise
        finally:
            journal, logs = self._local.journal, self._local.logs
            self._local.journal = self._local.logs = None
        result.journal, result.engine_logs = journal, logs
        return result

    def _note(self, plate: int | None, step: str, text: str, **extra) -> None:
        journal = getattr(self._local, "journal", None)
        if journal is None:
            return
        journal.append({
            "plate": plate,
            "pass": getattr(self._local, "pass_no", None),
            "step": step,
            "text": text,
            **extra,
        })

    def _keep_log(self, plate: int, argv: list[str], proc, workdir: str) -> str | None:
        logs = getattr(self._local, "logs", None)
        if logs is None:
            return None
        pass_no = getattr(self._local, "pass_no", None)
        run = 1 + sum(1 for e in logs if e["plate"] == plate and e["pass"] == pass_no)
        name = f"plate-{plate}" + (f"-pass-{pass_no}" if pass_no else "") + f"-run-{run}.log"
        text = (
            f"argv: {' '.join(argv)}\nexit: {proc.returncode}\n\n"
            f"== stdout ==\n{proc.stdout or ''}\n== stderr ==\n{proc.stderr or ''}\n"
            f"== engine log ==\n{self._engine_log(workdir)}"
        )
        logs.append({
            "name": name,
            "plate": plate,
            "pass": pass_no,
            "run": run,
            "gzip_base64": base64.b64encode(gzip.compress(text.encode("utf-8"), 6)).decode("ascii"),
        })
        return name

    def _slice_file(self, request: SliceRequest, workdir: str) -> SliceResult:
        """Slice the file, one plate per run of the binary.

        **Why not hand the binary the whole file and let it walk the plates.**
        That is what `--slice 0` does, and on an assembly of six to eleven plates
        it dies: the run holds every plate it has finished in memory and ends in
        `std::bad_alloc`, with tree-support errors on the way down. The same file
        slices plate by plate without complaint. Measured on the two-plate
        `TheCowLevel.3mf`, which survives both routes, the answers agree to
        0.03 g in 276 g and 55 s in 12.7 h — inside the run-to-run noise this
        engine already has.

        What the split buys beyond survival is the breakdown: which colours sit
        on which plate. A caller pricing an assembly needs that to know whether
        the job wants a four-head machine or four passes of a one-head one, and
        summing the plates away in here would be throwing it out at the only
        point where it is known.

        A single-plate file still goes through as `--slice 0`, exactly as before,
        so nothing that already had a price moves because of this.
        """
        if not self.is_available():
            raise EngineUnavailable(f"{self.code}: binary missing at {self.binary}")

        heads = self._head_count(request.machine_profile)
        rewrites, palette, painted = self._extruder_remap(request.input_path, heads)
        facts = self._palette_facts(request.input_path) if palette else {}
        source = self.preprocess(
            request.input_path,
            workdir,
            self._machine_overrides(request.machine_profile),
            rewrites,
            # `--brim-type=no_brim` is a default the file's objects can
            # override for themselves, and some do — see _run_slice.
            {} if request.brim else {"brim_type": "no_brim"},
            # Only a profile says how many heads there are; without one the
            # file's own assignment stands — see point 4 in repair3mf.
            heads if request.machine_profile else None,
            painted,
        )
        for n, mapping in sorted(palette.items()):
            self._note(n, "prepare", "filaments renumbered onto the heads: " + ", ".join(
                f"{old} → {new}" for new, old in sorted(mapping.items())))
        if painted:
            self._note(None, "prepare", f"paint changed in {sum(len(v) for v in painted.values())} "
                       f"mesh(es): renumbered with the base, or dropped from parts painted all "
                       f"over in one colour (those take that colour)")

        declared = self._plate_count(source)
        if request.plates:
            # Only the plates a person chose, in the file's order. A number the
            # file does not have is refused rather than skipped: the price of
            # "plates 1 and 14" of a twelve-plate file is not the price of plate 1.
            wanted = sorted(set(request.plates))
            if wanted[0] < 1 or wanted[-1] > declared:
                raise SliceFailed(
                    f"{self.code}: plates {wanted} asked of a file with {declared}",
                )
        else:
            wanted = [request.plate] if request.plate else self._plates_to_slice(declared)

        sliced = []
        learned: list[str] = []
        for n in wanted:
            try:
                groups = self._split_plan(source, n)
            except SliceFailed as exc:
                self._note(n, "split", str(exc), reason=exc.reason)
                raise
            if groups:
                self._note(n, "split", f"printed in {len(groups)} passes, one per layer height: "
                           + " | ".join(", ".join(g) for g in groups) + " (object ids)")
                usage, log = self._slice_in_passes(
                    request, source, workdir, n, groups, palette.get(n, {}), facts, learned
                )
            else:
                usage, log = self._slice_plate(
                    request,
                    source,
                    os.path.join(workdir, f"plate-{n}"),
                    n,
                    palette.get(n, {}),
                    facts,
                    learned,
                )
            sliced.append((usage, log))

        result = self._merge([p for p, _ in sliced], plate_count=max(declared, len(sliced)))
        result.plates_sliced = wanted if request.plates else []
        # The log of the last plate to run, which is where a warning about the
        # whole job lands; the per-plate detail is in `plates`.
        result.raw = sliced[-1][1] if sliced else None
        # Measured off the same repaired copy the slice just consumed, so the
        # dimensions describe exactly the geometry the weights came from.
        result.model = self._inspect_file(
            source, request.scale, models=True, only=set(wanted) if request.plates else None
        )
        # A plate's kind as the slice found it, which beats the file's slot
        # assignments: the hydra's file puts two colours on a plate the engine
        # loads one filament for, and a prime tower is built for what is loaded.
        sliced_kind = {p.index: ("ams" if p.colours > 1 else "simple") for p in result.plates}
        for plate in result.model.plates:
            if plate.index in sliced_kind:
                plate.kind = sliced_kind[plate.index]

        return result

    def _plates_to_slice(self, declared: int) -> list[int]:
        """Plate numbers for a `plate=0` (whole file) request.

        One plate keeps asking for plate 0 rather than plate 1: the two are the
        same slice, and leaving the single-plate path byte-identical to what it
        was before means no catalogue price can move as a side effect of this.
        """
        return list(range(1, declared + 1)) if declared > 1 else [0]

    def _slice_plate(
        self,
        request: SliceRequest,
        source: str,
        workdir: str,
        plate: int,
        palette: dict[int, int] | None = None,
        facts: dict[int, dict] | None = None,
        learned: list[str] | None = None,
    ) -> tuple[PlateUsage, str]:
        os.makedirs(workdir, exist_ok=True)
        # Whatever an earlier plate of this file had to be told, this one starts
        # with: the turtle needs its object labels off on all nine, and paying
        # for nine refused runs to rediscover that is most of a slice each time.
        extra = list(learned or [])
        proc, output_path = self._run_slice(request, source, workdir, plate, extra=extra)

        # A refusal is not always about the model. A prime tower placed for the
        # author's bed hangs off ours; a file's object labels can be more than
        # the binary can follow. Both are worth one more run with that one thing
        # changed, and both are reported rather than done quietly. And a refusal
        # is not always about anything: past the named remedies the plate is
        # simply run again, because some of them come and go — see _remedy().
        adjustments: list[str] = []
        tried: list[str] = []
        while not output_path and len(tried) < len(self.REMEDIES) + self.RETRIES:
            remedy = self._remedy(request, source, workdir, plate, proc, tried, extra)
            if not remedy:
                break
            extra = self._with_flags(extra, remedy.argv)
            if remedy.source:
                source = remedy.source
            tried.append(remedy.kind)
            self._note(plate, "remedy", remedy.kind + (f": {remedy.note}" if remedy.note else ""),
                       flags=list(remedy.argv))
            # A plain retry says nothing on its own; what is worth reporting is
            # how many of them it took, and that is only known once one worked.
            if remedy.note:
                adjustments.append(remedy.note)
            if remedy.sticky and learned is not None:
                learned += remedy.argv
            proc, output_path = self._run_slice(
                request, source, workdir, plate, extra=extra
            )

        retries = tried.count("retry")
        # Only when the run that worked was itself a plain retry: one that
        # needed a remedy after the retries ran out says so on its own line.
        if output_path and retries and tried[-1] == "retry":
            adjustments.append(
                f"the engine refused this plate {retries} "
                f"time{'s' if retries > 1 else ''} for no reason it would name, "
                f"and sliced it unchanged on run {retries + 1}"
            )

        if not output_path:
            raise SliceFailed(
                f"{self.code} slicing failed"
                + (f" on plate {plate}" if plate else "")
                + self._reason(workdir),
                exit_code=proc.returncode,
                log=self._log(proc),
            )

        usage = self._parse_plate(output_path, plate, palette, facts)
        usage.filament_changes = self._filament_changes(workdir)
        usage.supports = supports.plate_supports(self._plate_gcode(workdir))
        usage.adjustments = adjustments
        # Everything wanted from this plate has been read. An eleven-plate
        # assembly would otherwise hold eleven G-code files at once, and the
        # directory this lands in is a RAM disk in the deployment it was written
        # for.
        shutil.rmtree(workdir, ignore_errors=True)
        return usage, self._log(proc)

    # -- a plate printed in passes ----------------------------------------

    def _split_plan(self, source: str, plate: int) -> list[list[str]] | None:
        """How to print a plate the engine would refuse over layer heights.

        **The rule is the engine's, and narrower than it reads.** Orca — and
        Bambu Studio, which has the same check — refuses a plate whose parts
        have different layer heights only when it builds a prime tower for it,
        that is when the plate loads more than one filament. A one-colour plate
        with parts at 0.16, 0.12 and 0.08 slices without complaint; a plate with
        a 0.24 turtle in one colour and three 0.08 trinkets in another does not.
        The check runs before any slicing, so the refusal itself costs seconds —
        but every plate of the file sliced before it is paid for, and nothing
        the retries do can change the answer.

        **What we do instead is print it in passes**, one per layer height: the
        same plate run once with only the 0.24 parts and once with only the
        0.08 ones. Each pass is either one colour (no tower) or one height (a
        tower it is allowed), and every part keeps its position and the height
        its author gave it. Levelling the heights instead was ruled out by the
        owner (2026-10-15): a thicker layer makes a small part bigger, not just
        coarser.

        **Passes split between models, never inside one** — models as layout.py
        finds them. A plate is refused rather than split (owner, 2026-10-15)
        when it holds a flexi model; when one model's own parts differ in
        height; or when models of different heights have overlapping boxes,
        which is the guard against a joint layout.py did not recognise.

        Returns the object ids of each pass, None when the plate prints as it
        is, and raises the refusal.
        """
        plates, objects = self._layout(source)
        on_plate = plates.get(plate) or []
        if len(on_plate) < 2:
            return None
        if len({e for oid in on_plate for e in objects.get(oid, set())}) < 2:
            return None  # one filament: no tower, any heights

        heights, custom = self._object_heights(source, on_plate)
        if custom or not self._tower_enabled(source):
            return None
        if len({heights[oid] for oid in on_plate}) < 2:
            return None

        def named(oid: str) -> str:
            return f"{self._object_names(source).get(oid, 'object ' + oid)} {heights[oid][0]:g} mm"

        found = layout.plate_models(source, {plate: [int(o) for o in on_plate]}).get(plate)
        models = found.models if found else []
        if not models:
            raise SliceFailed(
                f"{self.code}: plate {plate} needs a prime tower and its parts have "
                f"different layer heights, and its models could not be read to print it "
                f"in passes: " + ", ".join(named(o) for o in on_plate),
                reason=SliceFailed.LAYER_HEIGHTS,
            )

        flexi = [m for m in models if m.kind == "flexi"]
        if flexi:
            parts = ", ".join(named(str(o)) for m in flexi for o in m.object_ids)
            raise SliceFailed(
                f"{self.code}: plate {plate} needs a prime tower and its parts have "
                f"different layer heights; it holds a flexi model ({parts}), so it is "
                f"not printed in passes. Give every part of the plate one layer height, "
                f"or put the parts of each height on a plate of their own.",
                reason=SliceFailed.LAYER_HEIGHTS,
            )
        for model in models:
            own = {heights.get(str(o)) for o in model.object_ids}
            if len(own) > 1:
                raise SliceFailed(
                    f"{self.code}: plate {plate} needs a prime tower and its parts have "
                    f"different layer heights, and one model is made of parts of different "
                    f"heights: " + ", ".join(named(str(o)) for o in model.object_ids)
                    + ". Give that model one layer height.",
                    reason=SliceFailed.LAYER_HEIGHTS,
                )

        by_height: dict[tuple, list] = {}
        for model in models:
            by_height.setdefault(heights[str(model.object_ids[0])], []).append(model)
        for a, b in itertools.combinations(by_height, 2):
            for ma in by_height[a]:
                for mb in by_height[b]:
                    if self._boxes_meet(ma, mb):
                        raise SliceFailed(
                            f"{self.code}: plate {plate} needs a prime tower and its parts "
                            f"have different layer heights, and parts of different heights "
                            f"reach into each other ("
                            + ", ".join(named(str(o)) for o in ma.object_ids + mb.object_ids)
                            + "), so it is not printed in passes.",
                            reason=SliceFailed.LAYER_HEIGHTS,
                        )

        # Objects with no mesh of their own (none on a real plate) would fall
        # out of every pass; they ride with the first.
        groups = [
            sorted({str(o) for m in group for o in m.object_ids}, key=int)
            for _, group in sorted(by_height.items(), reverse=True)
        ]
        placed = {o for g in groups for o in g}
        groups[0] += [o for o in on_plate if o not in placed]
        return groups

    @staticmethod
    def _boxes_meet(a, b) -> bool:
        """Boxes that overlap at all — the conservative reading: two models
        placed side by side never do, and anything closer is not split."""
        if not a.lo or not b.lo:
            return True
        return all(min(a.hi[i], b.hi[i]) > max(a.lo[i], b.lo[i]) for i in range(3))

    def _object_heights(self, source: str, object_ids: list[str]) -> tuple[dict[str, tuple], bool]:
        """Layer height and first-layer height per object — what the engine
        compares — and whether any of them has a variable-height profile, which
        is the engine's own business and left to it."""
        root = self._model_settings(source)
        try:
            with zipfile.ZipFile(source) as zf:
                project = json.loads(zf.read(PROJECT_SETTINGS))
                names = set(zf.namelist())
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return {}, True
        if any("layer_heights_profile" in n for n in names):
            return {}, True

        default = (self._maybe_float(project.get("layer_height")),
                   self._maybe_float(project.get("initial_layer_print_height")))
        wanted = set(object_ids)
        heights: dict[str, tuple] = {oid: default for oid in object_ids}
        custom = False
        for obj in (root.findall("object") if root is not None else []):
            oid = obj.get("id")
            if oid not in wanted:
                continue
            meta = {m.get("key"): m.get("value") for m in obj.findall("metadata")}
            heights[oid] = (
                self._maybe_float(meta.get("layer_height")) or default[0],
                self._maybe_float(meta.get("initial_layer_print_height")) or default[1],
            )
            custom = custom or obj.find("layer_config_ranges") is not None
        return heights, custom

    def _tower_enabled(self, source: str) -> bool:
        try:
            with zipfile.ZipFile(source) as zf:
                project = json.loads(zf.read(PROJECT_SETTINGS))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return False
        return str(project.get("enable_prime_tower", "0")).strip().lower() in ("1", "true")

    def _object_names(self, source: str) -> dict[str, str]:
        root = self._model_settings(source)
        names = {}
        for obj in (root.findall("object") if root is not None else []):
            for meta in obj.findall("metadata"):
                if meta.get("key") == "name" and meta.get("value"):
                    names[obj.get("id")] = meta.get("value")
                    break
        return names

    def _slice_in_passes(
        self,
        request: SliceRequest,
        source: str,
        workdir: str,
        plate: int,
        groups: list[list[str]],
        palette: dict[int, int],
        facts: dict[int, dict],
        learned: list[str],
    ) -> tuple[PlateUsage, str]:
        """Slice each pass from a copy of the file holding only its objects,
        and report the plate as their sum with the passes underneath.

        Removing an object means all of it — its resource, its build item, its
        settings, its plate entry, its assembly entry: with only the build item
        gone the engine refuses the file as unparseable (-6).
        """
        from repair3mf import drop_objects

        on_plate = [o for g in groups for o in g]
        heights, _ = self._object_heights(source, on_plate)
        passes: list[PlateUsage] = []
        log = ""
        for k, group in enumerate(groups, start=1):
            self._local.pass_no = k
            pass_dir = os.path.join(workdir, f"plate-{plate}-pass-{k}")
            os.makedirs(pass_dir, exist_ok=True)
            variant = os.path.join(pass_dir, "pass.3mf")
            drop_objects(source, variant, [o for o in on_plate if o not in group])
            usage, log = self._slice_plate(
                request, variant, os.path.join(pass_dir, "run"), plate, palette, facts, learned
            )
            os.remove(variant)
            passes.append(usage)
        self._local.pass_no = None

        names = self._object_names(source)
        described = "; ".join(
            f"{heights[g[0]][0]:g} mm — " + ", ".join(names.get(o, "object " + o) for o in g)
            for g in groups
        )
        usage = self._sum_passes(plate, passes)
        usage.adjustments = [
            f"printed in {len(passes)} passes, one per layer height, because a plate "
            f"with a prime tower must have one: {described}"
        ] + [f"pass {k}: {note}" for k, p in enumerate(passes, start=1) for note in p.adjustments]
        return usage, log

    def _sum_passes(self, plate: int, passes: list[PlateUsage]) -> PlateUsage:
        times = [p.print_time_sec for p in passes]
        changes = [p.filament_changes for p in passes]
        found = [p.supports for p in passes]
        warnings: list[str] = []
        for p in passes:
            warnings += [w for w in p.warnings if w not in warnings]
        return PlateUsage(
            index=plate,
            weight_g=round(sum(p.weight_g for p in passes), 2),
            print_time_sec=None if None in times else sum(times),
            filaments=self._merge_slots([f for p in passes for f in p.filaments]),
            filament_changes=None if None in changes else sum(changes),
            supports=None if None in found else sorted((g for s in found for g in s), reverse=True),
            warnings=warnings,
            passes=passes,
        )

    # Each named remedy is one thing worth changing about a refused plate,
    # tried at most once and in this order.
    REMEDIES = ("conflict", "tower", "bed", "labels", "supports", "raft", "normal_supports")

    # How many times a plate refused for no nameable reason is simply run
    # again, unchanged. See _remedy() for why this number and not a smaller one.
    RETRIES = 6

    def _remedy(
        self,
        request: SliceRequest,
        source: str,
        workdir: str,
        plate: int,
        proc,
        tried: list[str],
        extra: list[str] | None = None,
    ) -> "_Remedy | None":
        """The next thing worth changing about a plate the binary refused."""
        code = self._result_json(workdir).get("return_code")
        if code == GCODE_CONFLICT:
            # Two toolpaths cross. When one of them is the prime tower it is
            # ours to move — once, to the freest corner of the bed — and when
            # neither is, the file's own layout is what collides and no
            # argument of ours changes that. Either way a second `-101` is
            # final: the same geometry crosses the same way every run.
            parties = self._conflict(workdir)
            if "conflict" not in tried and parties and TOWER_PARTY in parties:
                moved = self._tower_away(request, source, workdir, plate, parties)
                if moved:
                    return moved
            raise self._collision(request, parties, tried)

        if code == GCODE_OUTSIDE_BED:
            # Something is over the edge. Ours to fix only when it is the prime
            # tower, which we placed; anything else is the file's own layout, and
            # the file is what the shop prints. So this is where a refusal stops
            # being worth another run: the same argv on the same geometry lands
            # outside the same bed every time, and six retries of it are six
            # slices spent to reach the same word.
            if "tower" not in tried:
                moved = self._tower_shift(request, source, workdir, plate, extra)
                if moved:
                    return moved
            raise self._off_bed(request, source, workdir, plate, proc)

        if code in (PLATE_NOT_INSIDE, OBJECTS_PARTLY_INSIDE) and self._plate_has_objects(source, plate):
            # The same verdict one step earlier, and only when the file says this
            # plate carries objects — otherwise the binary's other reading of
            # `-50` is the right one, the plate is simply empty, and that is not
            # something to answer with a sentence about the bed. A plate whose
            # parts all fit, but which the file lays out past the edge, is put
            # back on the bed once, whole (1.13.2); anything else is final.
            if "bed" not in tried:
                moved = self._plate_onto_bed(request, source, workdir, plate)
                if moved:
                    return moved
            raise self._off_bed(request, source, workdir, plate, proc)

        if "labels" not in tried:
            # `exclude_object` writes the markers a printer uses to skip one
            # object mid-print. This file is never printed from — it is measured
            # — and on some files the binary cannot follow its own markers,
            # dying on `Unknown label object id!` after the G-code is generated.
            # Switching them off changes annotations, not extrusion.
            if "Unknown label object id" in (self._log(proc) or ""):
                return _Remedy(
                    kind="labels",
                    sticky=True,
                    # `--flag=0`, not `--flag 0`: the CLI reads a bare `0` as a
                    # file name and gives up with "No such file: 0".
                    argv=["--exclude-object=0"],
                    note="object labels switched off, which the file's own ids broke",
                )

        if code == SLICE_FAILED:
            # **An object that starts in mid-air.** The engine checks whether
            # anything on the plate has islands with nothing beneath them and,
            # with supports off, gives up on the whole plate; Bambu Studio only
            # warns. In practice it is small rotated parts — eyes of
            # 8×11×5 mm — and the author's file has supports off for them.
            #
            # The answer is the one the printer would need anyway: supports on
            # this plate, and when the floating island sits in the first layers,
            # where there is nothing to stand a support on, a raft under it
            # (the pig's plate 3: supports alone still refuse, two raft layers
            # slice). Both add grams, and both are what the print would really
            # cost, so they are reported rather than done quietly; the support
            # grams show up in `supports` as on any other plate. Per plate, not
            # sticky: the plates next to it print without either.
            if "supports" not in tried:
                floating = FLOATING_LINE.findall(self._engine_log(workdir))
                if floating:
                    names = ", ".join(f'"{n}"' for n in dict.fromkeys(floating))
                    return _Remedy(
                        kind="supports",
                        argv=["--enable-support=1"],
                        note=f"supports switched on: {names} would start in mid-air",
                    )
            elif "raft" not in tried:
                return _Remedy(
                    kind="raft",
                    argv=["--raft-layers=2"],
                    note="a two-layer raft added: supports alone left a floating "
                    "part with nothing to stand on",
                )

        # Last, and only once nothing above has a name for what went wrong:
        # run the very same command again.
        #
        # **Some refusals come and go.** The same file, the same plate, the same
        # argv: the owl's plate 5 dies inside tree supports on `Not
        # precalculated Placeable areas requested` on three runs out of eight,
        # and slices the other five to the same 49.40 g. There is nothing to fix
        # in the request, because the request that fails is the request that
        # works. The message is not even a tell: those `Error:` lines are in the
        # log of the successful runs too, twenty-five of them, and the run ends
        # in a G-code file anyway.
        #
        # This sits *after* the named remedies and not instead of them, which is
        # the whole discipline of it. The hydra's plate 6 was flaky the same way
        # — one run in twelve — and the right answer there was not persistence
        # but the cause: the brim. A retry that ran first would have found that
        # plate a number eventually and buried the reason it needed one.
        #
        # **The cost is bounded and paid only on refusals.** Six is chosen from
        # the owl: at its measured rate, a plate is left without a number about
        # once in a thousand analyses instead of once in three, which is what it
        # takes for `analyze --all` to stop being a coin toss. A plate that
        # never slices still refuses — six runs slower, which on a refusal of
        # seconds to a couple of minutes is a price worth the catalogue not
        # having holes in it.
        if tried.count("retry") < self.RETRIES:
            # Nothing to change, and no sentence yet: how many runs it took is
            # only worth reporting once one of them works.
            return _Remedy(kind="retry", argv=[], note="")

        # **Trees that never finish.** The goblin's plate 4 refuses with a bare
        # `-100` on every run, seven of seven: the log goes through tree
        # supports and dies at the next stage without a word. Supports off
        # refuses too — the part needs them — and the same supports drawn the
        # classic way, `normal(auto)`, slice two runs of two.
        #
        # This is the one remedy that sits *after* the retries, and on purpose.
        # Tree supports slice fine nearly everywhere, and a plate that refuses
        # them only now and then is better served by the retry above, which
        # keeps the supports the author chose and their grams. Only a plate
        # that refused all the retries gets a different kind of support, and
        # that is reported: the weight of the supports is not the weight of the
        # trees the author would have printed.
        if (
            code == SLICE_FAILED
            and "normal_supports" not in tried
            and TREE_SUPPORT_LINE.search(self._engine_log(workdir))
        ):
            return _Remedy(
                kind="normal_supports",
                argv=["--support-type=normal(auto)"],
                note="tree supports refused on every run; sliced with normal supports instead",
            )
        return None

    def _run_slice(
        self,
        request: SliceRequest,
        source: str,
        workdir: str,
        plate: int,
        extra: list[str] | None = None,
    ) -> tuple[subprocess.CompletedProcess, str | None]:
        output_name = "sliced.gcode.3mf"
        argv = [
            "--slice", str(plate),
            "--allow-newer-file",  # catalogue files come from newer Bambu Studio
            "--no-check",
            "--min-save",          # otherwise the export carries the full mesh
            "--outputdir", workdir,
            "--export-3mf", output_name,
            # Neither stream ever says what `-101` found crossing, or that the
            # plate was moved to fit our bed; the log file does, in a few
            # thousand lines, and both streams stay exactly as they were.
            "--debug", ENGINE_LOG_LEVEL,
            "--logfile", os.path.join(workdir, ENGINE_LOG),
        ]
        if not request.brim:
            # **The brim is off by default, and that is a decision, not a
            # shortcut.** The shop does not print with one: this printer holds
            # its parts down well enough without, and cutting a brim off a
            # finished part is minutes of knife work per part. A weight that
            # includes material nobody extrudes prices the wrong print.
            #
            # It also happens to be the only way some plates slice at all. The
            # hydra's plate 6 carries four dowels of 12–100 mm³ at 27–71k
            # triangles each; a 5 mm brim around a part a few millimetres wide
            # closes on itself, and offsetting outlines that dense blows up
            # inside `Generating skirt & brim` — as `std::bad_alloc` on 957 MB
            # of resident memory with 30 GB free, which reads as a broken model
            # rather than as a brim. It is intermittent, roughly one run in
            # twelve succeeding, which is what makes it so hard to place; the
            # engine says nothing beyond the two words unless it is run with
            # `--debug 5 --logfile`, and only the file gets the 8.6k lines that
            # name the stage. Wider is worse (10 mm never slices, 1 mm
            # sometimes does), off is 10 runs out of 10.
            #
            # Measured cost of switching it off, on files whose own setting is
            # `auto_brim`: none. Plate 4 of the hydra weighs 116.64 g either
            # way, plate 1 weighs 18.47 g against 18.48 g. Where the engine
            # would have laid a brim the difference is that brim's own grams,
            # which is exactly what the shop wants left out.
            argv.append("--brim-type=no_brim")
        if request.machine_profile and request.process_profile:
            argv += [
                "--load-settings",
                f"{self._profile_path(request.machine_profile)};"
                f"{self._profile_path(request.process_profile)}",
            ]
        if request.filament_profiles:
            argv += [
                "--load-filaments",
                ";".join(self._profile_path(p) for p in request.filament_profiles),
            ]
        argv += extra or []
        argv.append(source)

        started = time.monotonic()
        proc = self._run(argv, expect_success=False, cwd=workdir)
        output_path = os.path.join(workdir, output_name)
        sliced = proc.returncode == 0 and os.path.exists(output_path)
        self._note(
            plate, "run",
            "sliced" if sliced else ("refused" + (self._reason(workdir) or f": exit {proc.returncode}")),
            exit_code=proc.returncode,
            seconds=round(time.monotonic() - started, 1),
            flags=list(extra or []),
            log=self._keep_log(plate, argv, proc, workdir),
        )
        if not sliced:
            return proc, None
        return proc, output_path

    def inspect(self, request: SliceRequest, workdir: str) -> ModelInfo:
        if not self.is_available():
            raise EngineUnavailable(f"{self.code}: binary missing at {self.binary}")

        # The same repair a slice needs: `--info` on an untouched Bambu project
        # dies with `return -24` exactly as slicing does.
        source = self.preprocess(request.input_path, workdir, {})

        return self._inspect_file(source, request.scale, models=True)

    # -- internals ---------------------------------------------------------

    def _inspect_file(
        self, path: str, scale: float = 1.0, models: bool = False, only: set[int] | None = None
    ) -> ModelInfo:
        """Ask the binary what the model measures.

        `--allow-newer-file --no-check` are not optional here for the same
        reason they are not optional when slicing: catalogue files are written
        by a newer Bambu Studio than the engine, and without them `--info`
        refuses the file with a bare `return -24` and no explanation.

        `--info` reports the model as it sits in the file; `--scale` is applied
        later, during slicing. Multiplying here is what makes the answer the
        size of the printed object rather than of the drawing.

        **The file's own scale is not applied by `--info` either**, and that
        one is not a request parameter but a fact about the model: a project
        saved with an object at 50% carries the mesh at full size and the 0.5
        on the build item's matrix in `3D/3dmodel.model`. The slice honours it
        — the customer's dragon printed 89 mm long and weighed 25.5 g — while
        `--info` measured the mesh and said 178. Two numbers from one analysis
        describing two different dragons, and the shop showed the customer the
        size of the one they were not buying. So the build item's scale is
        read here and folded into every size the same way the request's is.
        """
        proc = self._run(
            ["--info", "--allow-newer-file", "--no-check", path],
            expect_success=False,
            cwd=os.path.dirname(path) or None,
        )

        factor = scale if scale > 0 else 1.0
        blocks = [
            block
            for block in self._info_blocks(proc.stdout or "")
            if {"size_x", "size_y", "size_z"} <= block.keys()
        ]
        scales = self._build_scales(path, len(blocks))
        objects = [
            self._object(block, factor, scales[n] if scales else None)
            for n, block in enumerate(blocks)
        ]

        if not objects:
            raise SliceFailed(
                f"{self.code}: --info reported no dimensions",
                exit_code=proc.returncode,
                log=self._log(proc),
            )

        plates, parts = self._place_on_plates(path, objects)
        assembly = self._assembly(path, objects, factor)
        if assembly is not None:
            members = set(assembly.members)
            for n, obj in enumerate(objects):
                obj.in_assembly = n in members if obj.plate is not None or not plates else None

        layout, alike = self._layout_kind(objects, plates, assembly, parts)
        if models:
            # Only the plates being printed are worth reading the meshes of: a
            # variants file sliced on one plate of twelve would otherwise pay
            # for all twelve (Foxycat: 100 s of a 115 s slice).
            self._plate_models(path, objects, plates, only)
        return ModelInfo(
            objects=objects,
            assembly=assembly,
            plates=plates,
            layout=layout,
            plates_alike=alike,
        )

    def _place_on_plates(
        self, path: str, objects: list[ModelObject]
    ) -> tuple[list[PlateLayout], dict[int, frozenset[str]]]:
        """Give every object its id and the plate it stands on.

        `model_settings.config` lists each plate's objects by id, and `--info`
        prints its blocks in ascending id — the pairing `_assembly` relies on
        and the only one there is. When the counts disagree the pairing cannot
        be trusted, so nothing is stamped and every object stays where a file
        with no plates puts it: printed, on no named plate.

        An object no plate lists keeps `plate = None`: it stands off the beds,
        nothing prints it, and every aggregate leaves it out. Not one file in
        the shop's catalogue has such an object (28 files, 2026-10-07) — the
        "loose extras" beside an assembly turned out to stand on plates and to
        be printed — but the case is real in Studio and has to read as what it
        is.

        Returns the plates in file order, and per object id the names of its
        parts — what `_layout_kind` compares plates by.
        """
        root = self._model_settings(path)
        if root is None:
            for obj in objects:
                obj.plate = 1
            return [], {}

        ids = sorted(int(o.get("id")) for o in root.findall("object") if (o.get("id") or "").isdigit())
        if len(ids) != len(objects):
            return [], {}

        parts: dict[int, frozenset[str]] = {}
        for node in root.findall("object"):
            if not (node.get("id") or "").isdigit():
                continue
            names = [
                meta.get("value")
                for part in node.findall("part")
                for meta in part.findall("metadata")
                if meta.get("key") == "name" and meta.get("value")
            ]
            if not names:
                names = [
                    meta.get("value")
                    for meta in node.findall("metadata")
                    if meta.get("key") == "name" and meta.get("value")
                ]
            parts[int(node.get("id"))] = frozenset(names)

        plate_of: dict[int, int] = {}
        plates: list[PlateLayout] = []
        for node in root.findall("plate"):
            meta = {m.get("key"): m.get("value") for m in node.findall("metadata")}
            if not (meta.get("plater_id") or "").isdigit():
                continue
            index = int(meta["plater_id"])
            on_plate = [
                int(m.get("value"))
                for instance in node.findall("model_instance")
                for m in instance.findall("metadata")
                if m.get("key") == "object_id" and (m.get("value") or "").isdigit()
            ]
            for object_id in on_plate:
                plate_of.setdefault(object_id, index)
            plates.append(PlateLayout(
                index=index,
                name=(meta.get("plater_name") or "").strip() or None,
                object_count=len(set(on_plate)),
            ))

        for object_id, obj in zip(ids, objects):
            obj.object_id = object_id
            # A project with no plate list at all is one plate, as _plate_count
            # says; one that has a list and leaves the object out of it is not.
            obj.plate = plate_of.get(object_id) if plates else 1

        return plates, parts

    def _plate_models(
        self,
        path: str,
        objects: list[ModelObject],
        plates: list[PlateLayout],
        only: set[int] | None = None,
    ) -> None:
        """What comes off each plate as one thing — see layout.py.

        Seconds to tens of seconds, because it reads the meshes: which is why
        only the answers a caller receives pay for it, and the `--info` runs
        this engine makes for itself (where to put a prime tower, whether a
        plate holds anything) do not.

        A plate's `kind` is "ams" when its objects use more than one palette
        slot — the plate that needs a prime tower — and "simple" otherwise:
        one colour, no tower. Read from the file's slot assignments, so it
        needs no slice.
        """
        if not plates:
            return
        on_plate: dict[int, list[int]] = {}
        for obj in objects:
            if obj.plate is not None and obj.object_id is not None and (only is None or obj.plate in only):
                on_plate.setdefault(obj.plate, []).append(obj.object_id)

        _, extruders = self._layout(path)
        found = layout.plate_models(path, on_plate)
        for plate in plates:
            if only is not None and plate.index not in only:
                continue
            used = {e for oid in on_plate.get(plate.index, []) for e in extruders.get(str(oid), set())}
            plate.kind = "ams" if len(used) > 1 else "simple"
            result = found.get(plate.index)
            if result is None:
                continue
            plate.copies = result.copies
            plate.models = [
                {"kind": m.kind, "object_ids": m.object_ids, "shells": m.shells}
                for m in result.models
            ]

    @staticmethod
    def _model_settings(path: str) -> ET.Element | None:
        try:
            with zipfile.ZipFile(path) as zf:
                if MODEL_SETTINGS not in zf.namelist():
                    return None
                return ET.fromstring(zf.read(MODEL_SETTINGS))
        except (OSError, zipfile.BadZipFile, ET.ParseError):
            return None

    @staticmethod
    def _layout_kind(
        objects: list[ModelObject],
        plates: list[PlateLayout],
        assembly: Assembly | None,
        parts: dict[int, frozenset[str]],
    ) -> tuple[str | None, bool | None]:
        """Whether the plates are one model or several, and a hint about which.

        **The one thing geometry can settle.** Pieces of one model laid out
        across plates meet once they are put back where the author assembled
        them: the hydra's 31 glued pieces stand on all seven of its plates.
        A file whose glued group stays on a single plate — or that has no glued
        group at all — holds a model, or several, on each plate.

        **What it cannot.** Twelve cats on twelve plates are either twelve
        variants to choose one from or a set of twelve to print, and the file
        reads the same either way: that is the author's intent, not geometry.
        So for "separate" this offers only a hint — `alike`, true when every
        plate shares a part name with every other (Foxycat: each plate's cat is
        a "cat body.stl" with its eyes) — and leaves the choice to a person.
        Plate names are the other hint, and are published as they are.
        """
        if not plates:
            # No plate list: one plate if the objects were placed on it, unknown
            # if the pairing with the file's ids could not be made at all.
            return ("single" if objects and all(o.plate == 1 for o in objects) else None), None

        printed = [o for o in objects if o.plate is not None]
        on = sorted({o.plate for o in printed})
        if len(on) <= 1:
            return "single", None

        if assembly is not None:
            spanned = {objects[i].plate for i in assembly.members if objects[i].plate is not None}
            if len(spanned) > 1:
                return "assembly", None

        by_plate: dict[int, set[str]] = {}
        for obj in printed:
            by_plate.setdefault(obj.plate, set()).update(parts.get(obj.object_id, frozenset()))
        if not all(by_plate.get(p) for p in on):
            return "separate", None
        shared = set.intersection(*(by_plate[p] for p in on))
        return "separate", bool(shared)

    def _build_scales(self, path: str, count: int) -> list[tuple[float, float, float]] | None:
        """The scale each object's build item applies to it, in `--info` order.

        `3D/3dmodel.model` places every object on the bed with one `<item>` per
        instance, whose `transform` is the 3MF's 3×4 row-major matrix: a point
        travels as `p · M + t`, so the length of row *i* is what the object's
        own axis *i* is stretched by. Rotation leaves those lengths at one,
        which is what makes them usable on a box `--info` measured in the
        object's own frame: the object is not turned by this, only sized.

        Matched to the `--info` blocks the way `_assembly` matches them — by
        ascending object id, the only link there is — and refused whole when
        the counts disagree, for the same reason. An object with several
        instances takes its first: two instances of one object at two scales
        is a layout nobody has produced yet, and guessing between them would be
        worse than the first.

        None when the file names no build at all (a bare mesh) or cannot be
        read as a 3MF, which leaves the sizes exactly as `--info` said them.
        """
        try:
            with zipfile.ZipFile(path) as zf:
                if MODEL_FILE not in zf.namelist():
                    return None
                root = ET.fromstring(zf.read(MODEL_FILE))
        except (OSError, zipfile.BadZipFile, ET.ParseError):
            return None

        scales: dict[int, tuple[float, float, float]] = {}
        for item in root.iterfind(".//{*}item"):
            object_id = item.get("objectid") or ""
            if not object_id.isdigit() or int(object_id) in scales:
                continue
            matrix = (item.get("transform") or "").split()
            if len(matrix) != 12:
                scales[int(object_id)] = (1.0, 1.0, 1.0)
                continue
            try:
                m = [float(v) for v in matrix]
            except ValueError:
                return None
            scales[int(object_id)] = tuple(
                math.sqrt(sum(m[axis * 3 + n] ** 2 for n in range(3))) for axis in range(3)
            )

        if len(scales) != count:
            return None

        return [scales[object_id] for object_id in sorted(scales)]

    def _assembly(
        self, path: str, objects: list[ModelObject], factor: float = 1.0
    ) -> Assembly | None:
        """How big the model is once its pieces are put together.

        `--info` cannot answer this and never will: it measures each object in
        that object's own coordinates, and where the objects sit on the plates
        says how they were laid out for printing, not how they fit together. The
        file does answer it, in `<assemble>` — one `assemble_item` per piece,
        carrying the matrix that places it in the finished model. Every file in
        the catalogue has the block, single-plate ones included, where it holds
        the single item that is the whole toy.

        So the arithmetic is one rule for both cases and gets no branch: push
        each piece's eight bounding-box corners through its own matrix, then
        take the extent of the pieces that are **glued to each other**.

        **Not the extent of the whole block**, and that distinction is the whole
        of this method. Studio writes `<assemble>` into every project whether or
        not anyone ever assembled anything, and two things routinely sit in it
        that are not part of the toy: alternative pieces (a second pig's head,
        four spare turtle shells) and accessories parked beside the model (the
        retriever's coffee mug, ten millimetres off its paws). Measuring the
        block whole made the retriever 131 mm long against 95 measured by hand,
        the chest 450 mm, and a corgi 884 mm.

        So the pieces are grouped by whether their boxes meet — glued parts
        overlap, that is what glue is — and the group holding the most material
        is the model. What that leaves out is what the customer gets as loose
        extras, and the answer is a lower bound by exactly those. Verified
        against the one physically measured model in the catalogue: 95.0 × 80.6
        × 141.0 against 140 mm of height and "a bit under 100" of length.

        **Corners, not lengths.** The matrices rotate as well as move, and a
        rotated box has to be rebuilt from points; a rotated piece therefore
        yields a slightly larger box than the piece really is, which is the
        error this trades for not reading the mesh. Millimetres on a toy — and
        it errs upwards.

        Returns None when the file cannot be made to answer: no block, a piece
        `--info` did not measure, or — the case this exists for — **nothing
        touching anything at all**, which is Studio's untouched default of laying
        the pieces out in a row and means nobody ever assembled this file. A
        caller needs that told apart from a measurement, because the honest
        response to it is to ask a human. There is deliberately no rule for
        "partly assembled": the chest whose lid is not on it is half a group and
        distinguishing it from a whole one would take a threshold, which would
        be a number invented here rather than found in the file.
        """
        try:
            with zipfile.ZipFile(path) as zf:
                if MODEL_SETTINGS not in zf.namelist():
                    return None
                root = ET.fromstring(zf.read(MODEL_SETTINGS))
        except (OSError, zipfile.BadZipFile, ET.ParseError):
            return None

        items = root.find("assemble")
        if items is None:
            return None

        # `--info` prints one block per object in ascending id order, and that
        # ordering is the only link between a block and the id the assembly
        # names — it prints no id, no name, nothing else to match on. Verified
        # against every file in the catalogue by facet count, which the blocks do
        # carry. A mismatched count means the pairing cannot be trusted and the
        # measurement is refused rather than guessed at.
        ids = sorted(int(o.get("id")) for o in root.findall("object") if (o.get("id") or "").isdigit())
        if len(ids) != len(objects):
            return None
        index_of = {object_id: n for n, object_id in enumerate(ids)}

        placed: list[tuple[list[float], list[float], float]] = []
        scales: list[tuple[float, float, float]] = []
        owners: list[int] = []
        for item in self._assemble_items(items):
            n = index_of.get(int(item.get("object_id") or -1)) if (item.get("object_id") or "").isdigit() else None
            box = objects[n] if n is not None else None
            if box is None or box.file_min is None or box.file_max is None:
                return None
            # Off every plate means not printed, and what is not printed is not
            # part of the toy the customer receives.
            if box.object_id is not None and box.plate is None:
                continue

            matrix = [float(v) for v in (item.get("transform") or "").split()]
            if len(matrix) != 12:
                return None

            # The build item's scale is *not* applied to the corners here.
            # The matrices of this block place unscaled pieces — the customer's
            # 50% dragon has 0.5 on its build item and a bare translation here,
            # and the hydra's parts stand at 1.5 on the bed with the same
            # unit matrices in the assembly — so scaling a piece about its own
            # origin would pull it out of contact with its neighbours and
            # dissolve the glued group (the hydra fell from 31 parts to 1 when
            # that was tried). The assembly is measured in the file's frame
            # and scaled whole at the end, where the translations scale with it.
            corners = itertools.product(*zip(box.file_min, box.file_max))
            unscaled_here = all(
                abs(math.sqrt(sum(matrix[axis * 3 + n] ** 2 for n in range(3))) - 1.0) <= 1e-3
                for axis in range(3)
            )
            scales.append(box.build_scale if unscaled_here and box.build_scale else (1.0, 1.0, 1.0))

            lo = [float("inf")] * 3
            hi = [float("-inf")] * 3
            # Row-vector convention, as the 3MF specification and render.py
            # read it: p' = p · M + t, so output axis `axis` takes column
            # `axis` of the 3×3 block. Until 1.10 this read the rows instead —
            # the transpose, which is the inverse rotation. A translation does
            # not care, and every matrix in the catalogue is one, so no size
            # moved; a rotated piece would have landed mirrored about its own
            # origin, next to the wrong neighbour.
            for corner in corners:
                for axis in range(3):
                    placement = sum(
                        corner[n] * matrix[n * 3 + axis] for n in range(3)
                    ) + matrix[9 + axis]
                    lo[axis] = min(lo[axis], placement)
                    hi[axis] = max(hi[axis], placement)
            # Material, not the box: the box of a hollow shell says how much room
            # a piece takes, and what is wanted here is which cluster *is* the
            # toy. Falls back to the box only where the binary reported no volume.
            #
            # **In the file's frame**, like the corners — not volume_mm3, which
            # carries the build item's scale. Ranking by the scaled figure is
            # what 1.8 shipped, and it turned the hydra into one piece: its
            # largest loose part stands at 1.5 on the bed, which multiplied its
            # material by 3.4 (94 044 → 317 399 mm³) and outweighed the 31
            # glued pieces, half of which stand at 1.0 (115 145 → 235 745).
            volume = box.file_volume_mm3 or max(
                (hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2]), 0.0
            )
            placed.append((lo, hi, volume))
            owners.append(n)

        group = self._glued_group(placed)
        if group is None:
            return None

        lo = [min(placed[i][0][axis] for i in group) for axis in range(3)]
        hi = [max(placed[i][1][axis] for i in group) for axis in range(3)]

        # Scaled at the end rather than on the way in: the matrices are written
        # in the file's own coordinates, so a factor applied to the corners would
        # have to be applied to the translations too. Uniform scaling about the
        # origin makes the two identical, and doing it once here says so.
        #
        # The build item's scale rides the same way, when the assembly's own
        # matrices did not carry it: the piece holding the most material sets
        # it for the whole toy. Most files scale every piece alike, and where
        # one peg is squashed on its own axis the toy's box does not move with
        # it. An estimate for a non-uniformly scaled assembly, exact for the
        # rest — and the printed size, not the drawing's, either way.
        dominant = max(group, key=lambda i: placed[i][2])
        build = scales[dominant]
        return Assembly(
            size_x=round((hi[0] - lo[0]) * build[0] * factor, 3),
            size_y=round((hi[1] - lo[1]) * build[1] * factor, 3),
            size_z=round((hi[2] - lo[2]) * build[2] * factor, 3),
            part_count=len(group),
            members=sorted({owners[i] for i in group}),
        )

    @staticmethod
    def _assemble_items(block: ET.Element) -> list[ET.Element]:
        """One `assemble_item` per placed object instance.

        **Not every element in the block is one.** Most files write exactly one
        per object instance, each carrying an `instance_id`. Some write more:
        Foxycat's every object appears four times — once with `instance_id="0"`
        at the place it was assembled, and three times without one, at offsets
        of a few tens of millimetres that are its parts' positions inside it.
        Counted as pieces, those made one cat 36 parts out of 12 objects.

        So for an object that has items naming their instance, only those count,
        the first per instance. An object whose items name none is read as it
        always was, every item a piece.
        """
        items = block.findall("assemble_item")
        named = {item.get("object_id") for item in items if item.get("instance_id") is not None}

        seen: set[tuple[str | None, str | None]] = set()
        chosen: list[ET.Element] = []
        for item in items:
            key = (item.get("object_id"), item.get("instance_id"))
            if key[0] in named and (key[1] is None or key in seen):
                continue
            seen.add(key)
            chosen.append(item)
        return chosen

    # How far apart two placed boxes may be and still count as touching.
    #
    # Not float noise — a fit clearance. Mating printed parts are modelled with
    # a tenth or two of a millimetre of air between them, because a peg drawn
    # flush with its hole does not go in once both have been printed. Half a
    # millimetre covers that with room to spare and is nowhere near anything it
    # could merge by mistake: the closest thing this must *not* absorb is an
    # accessory parked beside a model, and that is ten millimetres away.
    #
    # It decides real cases. Two pieces of the German shepherd sit between 0.1
    # and 0.5 mm off its body — at a tighter tolerance they read as loose extras
    # and the dog measures 75.9 mm instead of 79.7.
    TOUCH_TOLERANCE_MM = 0.5

    @classmethod
    def _glued_group(
        cls, placed: list[tuple[list[float], list[float], float]]
    ) -> list[int] | None:
        """Indices of the pieces that make up the model, or None if none do.

        Pieces are grouped by whether their placed boxes meet, and the group
        holding the most material wins. A single-piece file is trivially its own
        group and takes this path like any other.

        None when **every** group is a lone piece and there is more than one of
        them: nothing is glued to anything, so the placements are Studio's
        untouched row rather than an assembly. Deliberately the only rejection —
        see _assembly() on why "partly assembled" gets no rule.
        """
        if not placed:
            return None

        parent = list(range(len(placed)))

        def root(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, j in itertools.combinations(range(len(placed)), 2):
            a, b = placed[i], placed[j]
            if all(
                a[0][axis] <= b[1][axis] + cls.TOUCH_TOLERANCE_MM
                and b[0][axis] <= a[1][axis] + cls.TOUCH_TOLERANCE_MM
                for axis in range(3)
            ):
                parent[root(i)] = root(j)

        groups: dict[int, list[int]] = {}
        for i in range(len(placed)):
            groups.setdefault(root(i), []).append(i)

        if len(placed) > 1 and all(len(members) == 1 for members in groups.values()):
            return None

        return max(groups.values(), key=lambda members: sum(placed[i][2] for i in members))

    @staticmethod
    def _info_blocks(stdout: str) -> list[dict[str, str]]:
        """Split `--info` output into one dict per object.

        This is the whole reason the parse is not a single flat dict: the binary
        prints a `[filename]` header and a block of `key = value` lines **for
        every object in the file**, reusing the same key names each time. Folding
        them together keeps whichever object happened to come last — which on a
        four-object keychain meant reporting the 6 mm clip as the size of the
        model.
        """
        blocks: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in stdout.splitlines():
            if line.startswith("["):
                if current:
                    blocks.append(current)
                current = {}
                continue
            match = INFO_LINE.match(line)
            if match:
                current[match.group(1)] = match.group(2)
        if current:
            blocks.append(current)

        return blocks

    @classmethod
    def _object(
        cls,
        values: dict[str, str],
        factor: float,
        build_scale: tuple[float, float, float] | None = None,
    ) -> ModelObject:
        sx, sy, sz = build_scale or (1.0, 1.0, 1.0)
        return ModelObject(
            size_x=round(float(values["size_x"]) * sx * factor, 3),
            size_y=round(float(values["size_y"]) * sy * factor, 3),
            size_z=round(float(values["size_z"]) * sz * factor, 3),
            # Volume scales with the product of the three linear factors — the
            # cube of a uniform one — not with the factor.
            volume_mm3=cls._maybe_float(values.get("volume"), sx * sy * sz * factor ** 3),
            build_scale=build_scale,
            file_volume_mm3=cls._maybe_float(values.get("volume")),
            facet_count=cls._maybe_int(values.get("number_of_facets")),
            manifold=cls._maybe_bool(values.get("manifold")),
            # Unrounded and unscaled, unlike the lengths above: these are what
            # the assembly is built out of, and rounding them would move a piece
            # rather than merely describe it imprecisely.
            file_min=cls._corner(values, "min"),
            file_max=cls._corner(values, "max"),
        )

    @staticmethod
    def _corner(values: dict[str, str], edge: str) -> tuple[float, float, float] | None:
        """`min_x`/`max_x`… as a point, or None when `--info` did not print them."""
        try:
            return tuple(float(values[f"{edge}_{axis}"]) for axis in "xyz")
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _maybe_float(raw: str | None, factor: float = 1.0) -> float | None:
        try:
            return round(float(raw) * factor, 3)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _maybe_int(raw: str | None) -> int | None:
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _maybe_bool(raw: str | None) -> bool | None:
        if raw is None:
            return None
        return raw.strip().lower() in ("yes", "true", "1")

    def _profile_path(self, name: str) -> str:
        """`Snapmaker/machine/Snapmaker U1 (0.4 nozzle)` -> absolute json path."""
        return os.path.join(self.profiles_dir, f"{name}.json")

    # Settings that describe the *machine* and therefore stop being true the
    # moment we load our own machine profile over the file's own. The value is
    # what to write when our own profile has nothing to say about the key;
    # `None` means leave whatever the file had.
    MACHINE_SETTINGS = {
        "single_extruder_multi_material": None,
        "extruder_printable_area": [],
        "extruder_printable_height": [],
        # Which head the machine calls its primary one, 1-based. A file written
        # on a two-head Bambu H2C says 2; on a one-head machine that is a head
        # which does not exist, and the binary dies with SIGSEGV before it writes
        # a single line — on every plate, under `--debug 5`, and run by hand past
        # this service alike. `None` rather than a number, so a profile that says
        # nothing about the concept (the U1: a tool changer has no primary head)
        # keeps the file's value, and every gram already measured against it
        # stays exactly where it was.
        "master_extruder_id": None,
    }

    def _machine_overrides(self, machine_profile: str | None) -> dict:
        """Machine settings taken from the profile we are slicing for.

        A MakerWorld 3MF carries the settings of the machine its author used, and
        those survive `--load-settings` inside project_settings.config. Usually
        harmless, but some combinations are refused outright: a Bambu AMS file
        says `single_extruder_multi_material = 1` (one nozzle fed four filaments)
        while the Snapmaker U1 profile turns ooze prevention on, which Orca only
        allows when that flag is off. The slice then dies with `return -51` and
        the reason on stderr.

        The values come from the machine profile rather than a constant, because
        the right answer differs per machine and we would otherwise be hardcoding
        one printer's truth: resolved through `inherits`, the U1 chain yields 0
        (a tool changer with four independent heads) and the Bambu A1 chain
        yields 1. Hardcoding either would misprice the other.

        **A key our profile is silent about must be cleared, not left alone**,
        and that costs a segfault to learn. `extruder_printable_area` is the
        patch of bed each head can reach; a file written on a two-head Bambu H2C
        carries two of them, our four-head profile declares none, and the plate
        that prints on head 3 reads past the end of that list. The binary dies
        with SIGSEGV inside `outer_inner_brim_area`, with nothing on either
        stream and no result.json — the least informative failure in the whole
        service. An empty list is how a single-head file states it (`[]`) and
        Orca reads it as "no per-head restriction": the plate then slices to
        exactly the same 0.54 g as with the areas spelled out in full.
        """
        if not machine_profile:
            return {}

        overrides = {}
        for key, fallback in self.MACHINE_SETTINGS.items():
            value = self._profile_setting(machine_profile, key)
            if value is None:
                value = fallback
            if value is not None:
                overrides[key] = value
        return overrides

    def _profile_setting(self, name: str, key: str, _depth: int = 0):
        """Read `key` from a profile, following `inherits` until it is found.

        Nearest definition wins, which is what Orca itself does — the U1 chain
        sets the flag to 1 in `fdm_toolchanger` and then back to 0 in `fdm_U1`,
        and only the latter is right. Inherited profiles sit in the same
        directory as the leaf, so the parent's name is resolved against it.
        """
        if _depth > 10:  # a malformed profile must not loop forever
            return None

        path = self._profile_path(name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                profile = json.load(handle)
        except (OSError, ValueError):
            return None

        if key in profile:
            return profile[key]

        parent = profile.get("inherits")
        if not parent:
            return None

        return self._profile_setting(
            f"{os.path.dirname(name)}/{parent}", key, _depth + 1
        )

    def _run(self, argv: list[str], expect_success: bool = True, cwd: str | None = None):
        # The binary drops a `00000.log` in whatever directory it is started
        # from, so it is started from the one we are going to delete anyway —
        # otherwise two concurrent slices write to the same file in the image.
        cmd = [self.env_wrapper, self.binary, *argv]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            cwd=cwd,
            env={**os.environ, "LC_ALL": "C"},
        )
        if expect_success and proc.returncode != 0:
            raise SliceFailed(
                f"{self.code} command failed", proc.returncode, self._log(proc)
            )
        return proc

    @classmethod
    def _log(cls, proc) -> str:
        """Both streams, because the useful line is usually on the one nobody reads.

        Orca prints its progress on stdout but its *reason for giving up* on
        stderr: a rejected settings combination reports nothing but
        `run found error, return -51, exit...` on stdout, while the sentence that
        names the offending setting goes to stderr. Reporting stdout alone once
        cost an afternoon of running the binary by hand to find out what -51
        meant.
        """
        return cls._tail(f"{proc.stdout or ''}\n{proc.stderr or ''}")

    @staticmethod
    def _tail(text: str, lines: int = 25) -> str:
        interesting = [
            ln for ln in (text or "").splitlines()
            if ln.strip() and "[trace]" not in ln and "[debug]" not in ln
        ]
        return "\n".join(interesting[-lines:])

    def _plate_count(self, path: str) -> int:
        """How many plates the file itself declares.

        Asked of the file rather than of a slice, because it is a property of
        how the model was laid out and we need it *before* deciding how many
        times to run the binary. Files with nothing to say about plates (a bare
        STL, or a project without model_settings) count as one.
        """
        try:
            with zipfile.ZipFile(path) as zf:
                if MODEL_SETTINGS not in zf.namelist():
                    return 1
                root = ET.fromstring(zf.read(MODEL_SETTINGS))
        except (OSError, zipfile.BadZipFile, ET.ParseError):
            return 1
        return max(len(root.findall("plate")), 1)

    def _head_count(self, machine_profile: str | None) -> int:
        """How many print heads the machine we slice for has."""
        nozzles = self._profile_setting(machine_profile, "nozzle_diameter") if machine_profile else None
        return len(nozzles) if isinstance(nozzles, list) and nozzles else 1

    def _extruder_remap(
        self, source: str, heads: int
    ) -> tuple[dict[str, dict[str, str]], dict[int, dict[int, int]], dict[str, dict]]:
        """Renumber each plate's filaments into the heads the machine actually has.

        **A file's extruder number is a palette slot, not a head.** An assembly
        laid out across plates numbers its colours 1..13 and prints them one
        plate at a time, so a four-head machine runs it perfectly well — but the
        binary reads those numbers as heads and cannot cope with a part asking
        for head 5 of 4. It does not say so, either: the same file kills the
        slice three different ways depending on the plate — `std::bad_alloc` on
        700 MB of resident memory, a SIGSEGV inside the brim, and a bare
        `-100` — and every one of them looks like a broken model rather than a
        number out of range. Four of the five krackO models in the catalogue
        died here, which is most of what this subtask was about.

        So each plate's colours are renumbered into 1..k for the run, and the
        results are numbered back on the way out. It is also what happens in the
        shop: the operator loads the colour that plate needs into a head that is
        free, and which head it was is not a property of the print.

        **Only when the filaments are interchangeable.** Renumbering moves a
        part onto another palette slot with its own material, density and
        temperature, so it is only sound while those agree — which for a
        multi-plate assembly they invariably do, being one spool of PLA in
        thirteen colours. Where they do not, the file is left alone and the
        slice fails with the binary's own complaint rather than quietly weighing
        the part as the wrong plastic.

        **The paint goes with the base** (1.13, see paint.py). A part's colour
        can also be painted on its triangles, and renumbering only the base left
        a one-colour plate asking for two filaments — a prime tower, and a
        refusal over layer heights the plate was allowed to have. So the paint
        is renumbered with the base, and a part painted all over in one colour
        becomes a part of that colour: base set to it, paint dropped. That is
        done on every plate, renumbered or not, because the base nobody prints
        is a filament the engine loads all the same.

        Returns the rewrite to apply to model_settings.config, per plate a
        `{new number: original number}` map for reading the results back, and
        the paint changes per mesh file for repair3mf.
        """
        plates, bases, painted = self._filaments(source)
        if not plates:
            return {}, {}, {}

        rewrites: dict[str, dict[str, str]] = {}
        palettes: dict[int, dict[int, int]] = {}
        # Per object: the paint change for each of its meshes — a mapping to
        # renumber with, or None to drop the paint.
        wanted: dict[str, tuple[dict[str, str], dict[int, int] | None, dict[str, str]]] = {}
        for index, object_ids in plates.items():
            used = sorted({e for oid in object_ids for e in self._uses(oid, bases, painted)})
            mapping: dict[int, int] = {}
            if used and used[-1] > heads and len(used) <= heads and self._interchangeable(source, used):
                mapping = {old: new for new, old in enumerate(used, start=1)}
                palettes[index] = {new: old for old, new in mapping.items()}
            plain = {str(k): str(v) for k, v in mapping.items()}

            for oid in object_ids:
                found = painted.get(oid)
                colour = found.uniform if found else None
                if colour is not None:
                    target = str(mapping.get(colour, colour))
                    wanted[oid] = ({str(b): target for b in bases.get(oid, ())}, None, plain)
                elif found and mapping:
                    wanted[oid] = (plain, mapping, plain)
                elif mapping:
                    rewrites[oid] = plain

        # A mesh two objects share can take only one change. Where they would
        # want different ones, neither gets its paint touched and each keeps the
        # plain renumbering: the engine may then refuse as before 1.13, which is
        # a refusal rather than a part printed in the wrong colour.
        treatment: dict[tuple[str, str], set] = {}
        for oid, (_, change, _) in wanted.items():
            key = None if change is None else tuple(sorted(change.items()))
            for mesh in painted[oid].meshes:
                treatment.setdefault(mesh, set()).add(key)

        mesh_paint: dict[str, dict] = {}
        for oid, (base, change, plain) in wanted.items():
            meshes = painted[oid].meshes
            if any(len(treatment[m]) > 1 for m in meshes):
                if plain:
                    rewrites[oid] = plain
                continue
            rewrites[oid] = base
            for path, mesh_id in meshes:
                mesh_paint.setdefault(path, {})[mesh_id] = change

        return rewrites, palettes, mesh_paint

    def _filaments(
        self, source: str
    ) -> tuple[dict[int, list[str]], dict[str, set[int]], dict[str, "paint.ObjectPaint"]]:
        """Plates, each object's own extruders, and the paint on its meshes."""
        plates, bases = self._base_layout(source)
        return plates, bases, self._paint(source) if plates else {}

    def _paint(self, source: str) -> dict:
        """paint.read(), once per file: it reads every mesh, which on the
        largest files is seconds, and a slice asks for it more than once."""
        try:
            stat = os.stat(source)
        except OSError:
            return {}
        key = (os.path.realpath(source), stat.st_size, stat.st_mtime_ns)
        cached = getattr(self, "_paint_cached", None)
        if cached and cached[0] == key:
            return cached[1]
        try:
            found = paint.read(source)
        except (OSError, zipfile.BadZipFile, paint.PaintError):
            found = {}
        self._paint_cached = (key, found)
        return found

    @staticmethod
    def _uses(oid: str, bases: dict[str, set[int]], painted: dict) -> set[int]:
        """The filaments an object actually prints with: its base wherever the
        base shows, and every colour painted on it."""
        found = painted.get(oid)
        if not found:
            return bases.get(oid, set())
        return (bases.get(oid, set()) if found.base_shows else set()) | found.states

    def _layout(self, source: str) -> tuple[dict[int, list[str]], dict[str, set[int]]]:
        """Which objects sit on which plate, and which filaments each object
        prints with — paint included, see _extruder_remap."""
        plates, bases, painted = self._filaments(source)
        return plates, {oid: self._uses(oid, bases, painted) for oid in bases}

    def _base_layout(self, source: str) -> tuple[dict[int, list[str]], dict[str, set[int]]]:
        """Which objects sit on which plate, and which extruders each object states.

        An object states one extruder and each of its parts may state another;
        both count, because a plate is only as simple as its busiest piece.
        """
        try:
            with zipfile.ZipFile(source) as zf:
                if MODEL_SETTINGS not in zf.namelist():
                    return {}, {}
                root = ET.fromstring(zf.read(MODEL_SETTINGS))
        except (OSError, zipfile.BadZipFile, ET.ParseError):
            return {}, {}

        objects: dict[str, set[int]] = {}
        for obj in root.findall("object"):
            used = set()
            for node in [obj, *obj.findall("part")]:
                for meta in node.findall("metadata"):
                    if meta.get("key") == "extruder" and (meta.get("value") or "").isdigit():
                        used.add(int(meta.get("value")))
            objects[obj.get("id")] = used

        plates: dict[int, list[str]] = {}
        for plate in root.findall("plate"):
            meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
            index = meta.get("plater_id")
            if not (index or "").isdigit():
                continue
            plates[int(index)] = [
                node.get("value")
                for instance in plate.findall("model_instance")
                for node in instance.findall("metadata")
                if node.get("key") == "object_id"
            ]

        return plates, objects

    # What has to agree before one palette slot may stand in for another: the
    # properties a weight and a print time are computed from.
    INTERCHANGEABLE_KEYS = (
        "filament_type",
        "filament_density",
        "filament_diameter",
        "filament_settings_id",
    )

    def _palette_facts(self, source: str) -> dict[int, dict]:
        """Colour and material per palette slot, as the file states them.

        Needed only where a slot has been renumbered: the slice then reports the
        stand-in's colour, and the file is the one place the real one survives.
        """
        try:
            with zipfile.ZipFile(source) as zf:
                project = json.loads(zf.read(PROJECT_SETTINGS))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return {}

        colours = project.get("filament_colour") or []
        materials = project.get("filament_type") or []
        facts: dict[int, dict] = {}
        for index in range(1, max(len(colours), len(materials)) + 1):
            fact = {}
            if index <= len(colours):
                fact["color"] = colours[index - 1]
            if index <= len(materials):
                fact["material"] = materials[index - 1]
            facts[index] = fact
        return facts

    def _interchangeable(self, source: str, slots: list[int]) -> bool:
        try:
            with zipfile.ZipFile(source) as zf:
                project = json.loads(zf.read(PROJECT_SETTINGS))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return False

        for key in self.INTERCHANGEABLE_KEYS:
            values = project.get(key)
            if not isinstance(values, list):
                continue
            # A list is per filament when it has one entry each; some carry a
            # multiple of that (per variant) and say nothing about this question.
            seen = {values[i - 1] for i in slots if 0 < i <= len(values)}
            if len(seen) > 1:
                return False
        return True

    def _reason(self, workdir: str) -> str:
        """The failure in words, from the result.json the binary leaves behind.

        Worth reaching for because the streams are close to useless on their
        own: a plate whose G-code ends up off the bed reports `return -102` and
        not one word about beds, while result.json spells it out.
        """
        data = self._result_json(workdir)
        message = (data.get("error_string") or "").strip()
        return f": {message}" if message else ""

    @staticmethod
    def _result_json(workdir: str) -> dict:
        try:
            with open(os.path.join(workdir, RESULT_JSON), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _plate_gcode(workdir: str) -> str | None:
        """The G-code of the last slice, which survives even a refused plate."""
        found = sorted(
            os.path.join(workdir, name)
            for name in os.listdir(workdir)
            if name.startswith("plate_") and name.endswith(".gcode")
        )
        return found[-1] if found else None

    def _filament_changes(self, workdir: str) -> int | None:
        """How many times the print swaps filament, straight from the G-code.

        Not in slice_info.config, and the caller cannot infer it: purge on a
        change is per-machine, so charging for it needs the count and the
        machine's own figure per change. Absent on a single-colour plate, where
        the honest answer is also the absent one — nobody swapped anything.
        """
        path = self._plate_gcode(workdir)
        if not path:
            return None
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                head = handle.read(64_000)
                handle.seek(max(0, os.path.getsize(path) - 64_000))
                tail = handle.read()
        except OSError:
            return None
        match = FILAMENT_CHANGES.search(head) or FILAMENT_CHANGES.search(tail)
        return int(match.group(1)) if match else None

    def _tower_shift(
        self,
        request: SliceRequest,
        source: str,
        workdir: str,
        plate: int,
        extra: list[str] | None = None,
    ) -> "_Remedy | None":
        """Move a prime tower that hangs off our bed, or decide not to.

        The tower's place is stored in the file, chosen for the author's bed and
        for the tower *their* machine needed. Ours can be a different size — a
        10 mm wide tower on a four-head machine grew 92 mm deep — so a position
        that was comfortably inside a 256 mm bed can run off a 271 mm one. The
        binary slices the whole plate before noticing and then refuses it whole.

        The G-code it wrote on the way out says exactly how far off it is, so the
        shift is measured rather than guessed, and applied on the command line
        where it costs nothing: rewriting the archive to move one number would
        mean recompressing up to 187 MB of mesh.

        Returns None when the overflow is not the tower's — an object placed off
        the bed is the caller's problem to hear about, not ours to shuffle.
        """
        data = self._result_json(workdir)
        if data.get("return_code") != GCODE_OUTSIDE_BED:
            return None

        bed = self._bed_box(request.machine_profile)
        tower = self._feature_box(self._plate_gcode(workdir), PRIME_TOWER_FEATURE)
        origin = self._tower_origin(source, plate, extra)
        if not bed or not tower or not origin:
            return None

        argv: list[str] = []
        moved = []
        for axis, (low, high), (edge_low, edge_high), start in (
            ("x", tower[0:2], bed[0:2], origin[0]),
            ("y", tower[2:4], bed[2:4], origin[1]),
        ):
            # One extra millimetre so the tower lands inside the edge rather than
            # on it; further than that would push it into the objects it was
            # placed beside.
            if low < edge_low:
                delta = edge_low - low + 1
            elif high > edge_high:
                delta = edge_high - high - 1
            else:
                continue
            argv += [f"--wipe-tower-{axis}", f"{round(start + delta, 3)}"]
            moved.append(f"{axis} by {round(delta, 1)} mm")

        if not argv:
            return None
        return _Remedy(
            kind="tower",
            argv=argv,
            note="prime tower moved " + " and ".join(moved) + " to fit the bed",
        )

    # How far from the bed's edge the tower's origin is put, in millimetres. On
    # the U1 the tower's brim reaches a few millimetres behind its origin; ten
    # covers that and leaves the tower where it was put rather than half off
    # the bed, which would only cost a `-102` and a shift back to here.
    TOWER_EDGE_MARGIN = 10.0

    # What the tower's width is taken to be when the process profile does not
    # say. Orca's own default.
    TOWER_WIDTH_DEFAULT = 35.0

    def _tower_away(
        self,
        request: SliceRequest,
        source: str,
        workdir: str,
        plate: int,
        parties: str,
    ) -> "_Remedy | None":
        """Put a prime tower that crosses the print into the freest corner of the bed.

        **Why it crosses at all.** The tower's place is stored in the file for
        the author's bed. When ours is smaller, the engine moves the plate's
        objects to sit centred on our bed — and leaves the tower where the file
        put it. Its log even says the tower moved with them; the G-code says it
        did not. A dragon laid out on a 350 × 320 H2D bed, with its tower
        beside it, lands on a 270 × 270 U1 bed with the tower across its
        back. And our tower is not the author's either: two filaments on a
        tool changer give a 65 × 12 mm strip, where the file expected a 24 mm
        square.

        **So the tower goes to a corner of *our* bed, in absolute millimetres,
        and grows along an edge.** `wipe_tower_rotation_angle` turns the tower
        about its origin — 0 grows it along +x, 90 along +y, 180 along −x, 270
        along −y, measured on the U1 — so each corner has one rotation that
        keeps the strip on the edge and its depth pointing inward. The corner
        is the one whose tower box sits clearest of the objects as they will
        finally lie: their own boxes from `--info` pushed through the build
        items, plus the centring the engine will apply, recomputed for the
        tower's new place because the engine centres objects *and its
        estimate of the tower* together.

        **Guesses are for ranking only.** The tower's real footprint on this
        machine is not known before it is sliced — a width from the process
        profile times the filaments the file names, and as deep as it is wide,
        is the conservative box the corners are ranked by. The engine then has
        the last word: a corner that still crosses is a second `-101` and a
        refusal with a name, a tower that hangs off the bed is a `-102` and
        `_tower_shift()` walks it back in from where it is now.

        Returns None when the file or the profile cannot be read well enough to
        rank anything, which leaves the refusal as it was.
        """
        bed = self._bed_box(request.machine_profile)
        objects = self._plate_footprint(source, plate)
        if not bed or not objects:
            return None

        log = self._engine_log(workdir)
        width = self._tower_width(request.process_profile, source)
        depth = width
        margin = self.TOWER_EDGE_MARGIN
        x0, x1, y0, y1 = bed

        # (name, origin, rotation, the box the tower is assumed to fill)
        corners = (
            ("bottom-left", (x0 + margin, y0 + margin), 0,
             (x0 + margin, x0 + margin + width, y0 + margin, y0 + margin + depth)),
            ("bottom-right", (x1 - margin, y0 + margin), 90,
             (x1 - margin - depth, x1 - margin, y0 + margin, y0 + margin + width)),
            ("top-right", (x1 - margin, y1 - margin), 180,
             (x1 - margin - width, x1 - margin, y1 - margin - depth, y1 - margin)),
            ("top-left", (x0 + margin, y1 - margin), 270,
             (x0 + margin, x0 + margin + depth, y1 - margin - width, y1 - margin)),
        )

        ranked = []
        for name, origin, angle, box in corners:
            dx, dy = self._recentring(log, bed, objects, origin)
            final = (objects[0] + dx, objects[1] + dx, objects[2] + dy, objects[3] + dy)
            clearance = max(
                final[0] - box[1],
                box[0] - final[1],
                final[2] - box[3],
                box[2] - final[3],
            )
            ranked.append((clearance, name, origin, angle))
        # Ties keep the listed order: the bottom-left corner asks nothing of
        # the guessed size, so it is the one to prefer when nothing separates.
        clearance, name, origin, angle = max(ranked, key=lambda r: r[0])

        other = self._conflict_other(parties)
        return _Remedy(
            kind="conflict",
            argv=[
                "--wipe-tower-x", f"{round(origin[0], 3)}",
                "--wipe-tower-y", f"{round(origin[1], 3)}",
                "--wipe-tower-rotation-angle", str(angle),
            ],
            note=(
                f"prime tower moved to the {name} corner of the bed, "
                f"where the file put it across {other}"
            ),
        )

    def _collision(
        self, request: SliceRequest, parties: str | None, tried: list[str]
    ) -> SliceFailed:
        """The refusal for toolpaths that cross, named so the caller can keep it.

        Final for the same reason `off_bed` is: the same file on the same bed
        crosses the same way every run, and there is nothing left of ours to
        move. Two sentences, because they lead somewhere different — a tower
        that finds no free corner is a bed too full for this print, a file
        whose own parts collide is a file to fix.
        """
        bed = self._bed_limits(request.machine_profile)
        where = f" ({self._bed_words(bed)})" if bed else ""
        if parties and TOWER_PARTY in parties:
            other = self._conflict_other(parties)
            if "conflict" in tried:
                detail = (
                    f"the prime tower crosses {other} where the file puts it "
                    f"and in the freest corner of the bed{where} alike"
                )
            else:
                detail = f"the prime tower crosses {other} and could not be placed elsewhere"
        elif parties:
            detail = f"the file's own layout makes {self._quote(parties)} cross"
        else:
            detail = "two toolpaths cross and the engine did not say which"
        return SliceFailed(
            f"{self.code}: the print cannot be sliced on this bed: {detail}",
            exit_code=256 + GCODE_CONFLICT,
            reason=SliceFailed.CONFLICT,
        )

    def _conflict(self, workdir: str) -> str | None:
        """What the engine's log says crossed, as its own `A and B` phrase."""
        found = CONFLICT_LINE.findall(self._engine_log(workdir))
        return found[-1].strip() if found else None

    def _conflict_other(self, parties: str) -> str:
        """The other party of a tower conflict, quoted. The engine writes
        `A and B`; an object's own name may hold an `and`, so only the tower's
        end of the phrase is trusted to be the separator."""
        head, tail = f"{TOWER_PARTY} and ", f" and {TOWER_PARTY}"
        if parties.startswith(head):
            name = parties[len(head):]
        elif parties.endswith(tail):
            name = parties[: -len(tail)]
        else:
            name = parties
        return self._quote(name.strip())

    @staticmethod
    def _quote(text: str) -> str:
        return f"«{text}»"

    @staticmethod
    def _engine_log(workdir: str) -> str:
        try:
            with open(os.path.join(workdir, ENGINE_LOG), encoding="utf-8", errors="ignore") as handle:
                return handle.read()
        except OSError:
            return ""

    def _recentring(
        self,
        log: str,
        bed: tuple[float, ...],
        objects: tuple[float, float, float, float],
        origin: tuple[float, float],
    ) -> tuple[float, float]:
        """How far the engine will move the plate's objects, for a tower put here.

        Nothing, unless the log says the file's bed is larger than ours. Then
        the engine centres the box around the objects *and its own estimate of
        the tower* on the bed — the estimate is in the log too, as a box
        around the file's tower origin, and it is the same box wherever the
        tower is put and however it is turned. So the box is carried to the
        new origin, the centring is recomputed, and that is where the objects
        will lie; the tower itself stays exactly where it is put.
        """
        if not SHRUNK_LINE.search(log):
            return 0.0, 0.0

        centre = NEW_CENTER_LINE.findall(log)
        if centre:
            cx, cy = float(centre[-1][0]), float(centre[-1][1])
        else:
            cx, cy = (bed[0] + bed[1]) / 2, (bed[2] + bed[3]) / 2

        lo_x, hi_x, lo_y, hi_y = objects
        estimate = TOWER_ESTIMATE_LINE.findall(log)
        placed = TOWER_ORIGIN_LINE.findall(log)
        if estimate and placed:
            ex0, ey0, ex1, ey1 = (float(v) for v in estimate[-1])
            px, py = (float(v) for v in placed[-1])
            lo_x = min(lo_x, origin[0] + (ex0 - px))
            hi_x = max(hi_x, origin[0] + (ex1 - px))
            lo_y = min(lo_y, origin[1] + (ey0 - py))
            hi_y = max(hi_y, origin[1] + (ey1 - py))

        return cx - (lo_x + hi_x) / 2, cy - (lo_y + hi_y) / 2

    def _tower_width(self, process_profile: str | None, source: str) -> float:
        """The tower's width for ranking corners: the profile's, times the
        filaments the file names. A tool changer primes each filament in its
        own segment side by side, which is what makes a two-filament tower on
        the U1 twice as wide as the profile says; on a single-nozzle machine
        the same product overstates it, which for ranking is the safe side."""
        width = None
        if process_profile:
            width = self._profile_setting(process_profile, "prime_tower_width")
        filaments = 1
        try:
            with zipfile.ZipFile(source) as zf:
                project = json.loads(zf.read(PROJECT_SETTINGS))
            if width is None:
                width = project.get("prime_tower_width")
            colours = project.get("filament_colour")
            if isinstance(colours, list) and colours:
                filaments = len(colours)
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            pass
        try:
            width = float(width)
        except (TypeError, ValueError):
            width = self.TOWER_WIDTH_DEFAULT
        return width * max(filaments, 1)

    def _plate_footprint(
        self, source: str, plate: int
    ) -> tuple[float, float, float, float] | None:
        """The box around everything on this plate, in the file's own frame.

        Each object's box from `--info`, in the object's own coordinates,
        pushed through the build item that places it on the bed — the same
        arithmetic `_assembly()` does with the assembly's matrices, on the
        plate's matrices instead. A turned piece yields a box a little larger
        than the piece, which for keeping a tower clear of it is the right
        side to err on. None when any link in that chain is missing: an
        unreadable file, an object `--info` did not measure, a plate that
        names nothing.
        """
        plates, _ = self._layout(source)
        wanted = set(plates.get(plate or 1) or [])
        if not wanted:
            return None

        try:
            objects = self._inspect_file(source).objects
            with zipfile.ZipFile(source) as zf:
                settings = ET.fromstring(zf.read(MODEL_SETTINGS))
                build = ET.fromstring(zf.read(MODEL_FILE))
        except (SliceFailed, OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
            return None

        ids = sorted(
            int(o.get("id")) for o in settings.findall("object") if (o.get("id") or "").isdigit()
        )
        if len(ids) != len(objects):
            return None
        boxes = dict(zip(ids, objects))

        lo = [float("inf")] * 2
        hi = [float("-inf")] * 2
        for item in build.iterfind(".//{*}item"):
            object_id = item.get("objectid") or ""
            if object_id not in wanted or not object_id.isdigit():
                continue
            box = boxes.get(int(object_id))
            if box is None or box.file_min is None or box.file_max is None:
                return None
            matrix = (item.get("transform") or "").split()
            try:
                m = [float(v) for v in matrix]
            except ValueError:
                return None
            if len(m) != 12:
                m = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
            for corner in itertools.product(*zip(box.file_min, box.file_max)):
                for axis in range(2):
                    placed = sum(m[axis + n * 3] * corner[n] for n in range(3)) + m[9 + axis]
                    lo[axis] = min(lo[axis], placed)
                    hi[axis] = max(hi[axis], placed)

        if lo[0] == float("inf"):
            return None
        return lo[0], hi[0], lo[1], hi[1]

    @staticmethod
    def _with_flags(argv: list[str], added: list[str]) -> list[str]:
        """`argv` with `added` appended, minus any earlier value of a flag
        `added` sets: a tower moved twice is at its second place, not at both."""
        flags = {added[n] for n in range(0, len(added) - 1, 2) if added[n].startswith("--")}
        kept: list[str] = []
        skip = False
        for n, token in enumerate(argv):
            if skip:
                skip = False
                continue
            if token in flags and n + 1 < len(argv):
                skip = True
                continue
            kept.append(token)
        return kept + list(added)

    @staticmethod
    def _flag_values(argv: list[str], *flags: str) -> list[str] | None:
        """The value after each of `flags` in `argv`, or None if any is absent."""
        values = []
        for flag in flags:
            try:
                values.append(argv[argv.index(flag) + 1])
            except (ValueError, IndexError):
                return None
        return values

    def _tower_origin(
        self, source: str, plate: int, extra: list[str] | None = None
    ) -> tuple[float, float] | None:
        """Where the prime tower starts from, as the shift's starting point.

        An earlier remedy on this plate may already have put the tower
        somewhere else on the command line, and that is then where it is;
        otherwise it is where the file puts it. Read from the project rather
        than measured off the G-code: the tower section of a G-code file also
        holds the travel moves that fetch and leave it, so its bounding box
        starts wherever the object is, not where the tower does. The position
        is stored per plate, since each plate gets its own tower.
        """
        given = self._flag_values(extra or [], "--wipe-tower-x", "--wipe-tower-y")
        if given:
            try:
                return float(given[0]), float(given[1])
            except (TypeError, ValueError):
                return None
        try:
            with zipfile.ZipFile(source) as zf:
                project = json.loads(zf.read(PROJECT_SETTINGS))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return None

        place = []
        for key in ("wipe_tower_x", "wipe_tower_y"):
            value = project.get(key)
            if isinstance(value, list):
                value = value[max(plate - 1, 0)] if len(value) >= max(plate, 1) else None
            try:
                place.append(float(value))
            except (TypeError, ValueError):
                return None
        return place[0], place[1]

    def _bed_box(self, machine_profile: str | None) -> tuple[float, ...] | None:
        """The bed as (x0, x1, y0, y1), from the profile we slice for."""
        area = self._profile_setting(machine_profile, "printable_area") if machine_profile else None
        points = []
        for corner in area or []:
            try:
                x, y = str(corner).split("x")
                points.append((float(x), float(y)))
            except ValueError:
                return None
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return min(xs), max(xs), min(ys), max(ys)

    def _bed_limits(
        self, machine_profile: str | None
    ) -> tuple[float, float, float | None] | None:
        """The bed as the room a part has to fit inside, in millimetres.

        The extent of `printable_area` rather than the machine's advertised size:
        the U1 is sold as 270 × 270 × 270 and reaches from 0.5 to 270.5, which is
        the same 270 mm shifted half a millimetre off the corner. Height is its
        own key and may be absent, in which case the two other axes are still
        worth comparing — a part refused for being 285 mm wide is answered
        whether or not the profile says how tall the machine is.
        """
        box = self._bed_box(machine_profile)
        if not box:
            return None

        raw = self._profile_setting(machine_profile, "printable_height") if machine_profile else None
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        try:
            height = float(raw)
        except (TypeError, ValueError):
            height = None

        return round(box[1] - box[0], 3), round(box[3] - box[2], 3), height

    def _off_bed(
        self, request: SliceRequest, source: str, workdir: str, plate: int, proc
    ) -> SliceFailed:
        """The refusal for a plate that hangs off the bed, said in parts and
        millimetres.

        Worth the extra `--info` run this costs, because the two answers lead
        somewhere different. A part **larger than the bed** cannot be printed by
        this machine at all, and the shop's move is a different printer or a
        different file; a plate whose parts all fit but which is **laid out past
        the edge** is a file to re-arrange. "It did not fit" tells nobody which
        of the two they are looking at.

        Sizes are compared as the file states them, with no rotation and no
        sorting of the sides. The plate is printed exactly as its author laid it
        out, so a part that fits only when turned does not fit.
        """
        bed = self._bed_limits(request.machine_profile)
        oversize = self._oversize(source, bed) if bed else []

        if oversize:
            detail = "; ".join(oversize)
        elif bed:
            detail = (
                f"no part is larger than the bed ({self._bed_words(bed)}), "
                "so the plate is laid out past its edge"
            )
        else:
            # No profile, or one that does not state its printable area: the
            # refusal is still the engine's own and still final, but naming the
            # part would mean inventing the bed to compare it against. This is
            # the one branch that quotes the binary, because without a bed to
            # measure against its sentence is all there is — and its sentence is
            # worth quoting only there: it says "empty or nothing fully inside",
            # which next to millimetres of ours reads as a second, wrong
            # diagnosis. The full log travels in `log` either way.
            said = self._reason(workdir).lstrip(": ")
            detail = "the machine profile does not say how big the bed is, so the part cannot be named"
            if said:
                detail += f" (engine: {said})"

        return SliceFailed(
            f"{self.code}: the print does not fit the bed"
            + (f" on plate {plate}" if plate else "")
            + f": {detail}",
            exit_code=proc.returncode,
            log=self._log(proc),
            reason=SliceFailed.OFF_BED,
        )

    def _plate_onto_bed(
        self, request: SliceRequest, source: str, workdir: str, plate: int
    ) -> "_Remedy | None":
        """Move a plate the file lays out past the bed's edge back onto it — the
        whole plate, by the least that puts every part on the bed.

        The turtle's plate 5 has a 26 mm part 23 mm past the front edge of its
        author's bed, while the plate as a whole spans 157 × 237 mm and our bed
        is 270 × 270 (1.13.2). Moving the plate rather than the part is the
        owner's choice (2026-10-15): the parts keep their places relative to
        each other and to the tower, so nothing comes any closer to anything
        and no free spot has to be guessed — the bed is put back under the
        print. A plate wider than the bed cannot be, and is refused.

        **Where the bed is, in the file's coordinates.** Orca places the plates
        of a file in a grid a fifth of a bed apart, `ceil(√n)` to a row, and on
        loading moves each plate from the author's bed onto ours centre to
        centre. So the room a plate has, in the file's frame, is our bed's size
        centred on the author's bed — measured against the parts' boxes as
        layout.py places them.
        """
        ours = self._bed_box(request.machine_profile)
        theirs = self._file_bed(source)
        plates, _ = self._layout(source)
        ids = plates.get(plate or 1) or []
        if not ours or not theirs or not ids or not plates:
            return None

        found = layout.plate_models(source, {plate or 1: [int(o) for o in ids]}).get(plate or 1)
        models = found.models if found else []
        if not models or any(not m.lo for m in models):
            return None
        lo = [min(m.lo[i] for m in models) for i in (0, 1)]
        hi = [max(m.hi[i] for m in models) for i in (0, 1)]

        count = max(plates)
        root = math.sqrt(count)
        cols = int(round(root)) + (1 if root > round(root) else 0)
        index = (plate or 1) - 1
        width, depth = theirs[1] - theirs[0], theirs[3] - theirs[2]
        origin = ((index % cols) * width * (1 + PLATE_GAP), -(index // cols) * depth * (1 + PLATE_GAP))
        centre = (origin[0] + (theirs[0] + theirs[1]) / 2, origin[1] + (theirs[2] + theirs[3]) / 2)
        room = (ours[1] - ours[0], ours[3] - ours[2])

        shift = []
        for axis in (0, 1):
            low, high = centre[axis] - room[axis] / 2, centre[axis] + room[axis] / 2
            span = hi[axis] - lo[axis]
            if span > room[axis]:
                return None
            margin = min(1.0, (room[axis] - span) / 2)
            if lo[axis] < low + margin:
                shift.append(low + margin - lo[axis])
            elif hi[axis] > high - margin:
                shift.append(high - margin - hi[axis])
            else:
                shift.append(0.0)
        if not any(shift):
            return None

        from repair3mf import shift_plate

        moved = os.path.join(workdir, "on-bed.3mf")
        shift_plate(source, moved, ids, plate or 1, shift[0], shift[1])
        said = " and ".join(
            f"{axis} by {round(d, 1)} mm" for axis, d in zip("xy", shift) if d
        )
        return _Remedy(
            kind="bed",
            argv=[],
            note=f"plate moved {said}: the file lays it out past the edge of the bed, "
            f"and the whole plate ({round(hi[0] - lo[0], 1)} × {round(hi[1] - lo[1], 1)} mm) fits",
            source=moved,
        )

    def _file_bed(self, source: str) -> tuple[float, ...] | None:
        """The author's bed as (x0, x1, y0, y1), from the file's printable_area."""
        try:
            with zipfile.ZipFile(source) as zf:
                area = json.loads(zf.read(PROJECT_SETTINGS)).get("printable_area")
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return None
        points = []
        for corner in area or []:
            try:
                x, y = str(corner).split("x")
                points.append((float(x), float(y)))
            except ValueError:
                return None
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return min(xs), max(xs), min(ys), max(ys)

    def _plate_has_objects(self, source: str, plate: int) -> bool:
        """Does the file put anything on this plate?

        Plate 0 is the whole-file request a single-plate file still goes through
        (see `_plates_to_slice`), and the file numbers that plate 1. A file whose
        layout cannot be read answers False: "we could not tell" must not become
        a statement about the bed.
        """
        plates, _ = self._layout(source)
        return bool(plates.get(plate or 1))

    def _oversize(self, source: str, bed: tuple[float, float, float | None]) -> list[str]:
        """Each part that is larger than the bed, on each axis it is larger on.

        Numbered rather than named because `--info` prints no names, and the
        number is the position in the `objects` list the caller already has.
        """
        try:
            objects = self._inspect_file(source).objects
        except SliceFailed:
            return []

        found = []
        for number, obj in enumerate(objects, start=1):
            for axis, size, limit in (
                ("x", obj.size_x, bed[0]),
                ("y", obj.size_y, bed[1]),
                ("z", obj.size_z, bed[2]),
            ):
                if limit is not None and size > limit:
                    found.append(f"part {number} is {size} mm on {axis} against {limit}")
        return found

    @staticmethod
    def _bed_words(bed: tuple[float, float, float | None]) -> str:
        sides = [bed[0], bed[1]] + ([bed[2]] if bed[2] is not None else [])
        return " × ".join(f"{side}" for side in sides) + " mm"

    @staticmethod
    def _feature_box(gcode_path: str | None, feature: str) -> tuple[float, ...] | None:
        """Bounding box of one `;TYPE:` section of a G-code file."""
        if not gcode_path or not os.path.exists(gcode_path):
            return None

        inside = False
        x = y = None
        box = [None, None, None, None]
        with open(gcode_path, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.startswith(";TYPE:"):
                    inside = line.startswith(feature)
                    continue
                if not GCODE_MOVE.match(line):
                    continue
                for axis, value in GCODE_XY.findall(line):
                    if axis == "X":
                        x = float(value)
                    else:
                        y = float(value)
                if not inside or x is None or y is None:
                    continue
                box[0] = x if box[0] is None else min(box[0], x)
                box[1] = x if box[1] is None else max(box[1], x)
                box[2] = y if box[2] is None else min(box[2], y)
                box[3] = y if box[3] is None else max(box[3], y)

        return tuple(box) if None not in box else None

    def _parse_plate(
        self,
        output_path: str,
        plate: int,
        palette: dict[int, int] | None = None,
        facts: dict[int, dict] | None = None,
    ) -> PlateUsage:
        result = self._parse(output_path, "")
        for usage in result.filaments:
            # Undo the renumbering _extruder_remap() did, so the caller sees the
            # filament the file names and never our arrangement of it.
            original = (palette or {}).get(usage.slot)
            if original is None:
                continue
            usage.slot = original
            # The colour and material come back describing the *stand-in* slot,
            # which is the one thing the renumbering must not be allowed to
            # report: a caller matches its own filament catalogue against this
            # colour, and the brown part of a toy would go into the cart yellow.
            fact = (facts or {}).get(original, {})
            usage.color = fact.get("color", usage.color)
            usage.material = fact.get("material", usage.material)
        result.filaments.sort(key=lambda f: f.slot)
        return PlateUsage(
            # `--slice 0` is how a one-plate file is asked for, and it is still
            # that file's plate 1 that came back; reporting a plate 0 would make
            # the caller carry our command-line convention.
            index=plate or 1,
            weight_g=result.total_weight_g,
            print_time_sec=result.print_time_sec,
            filaments=result.filaments,
            warnings=result.warnings,
        )

    def _merge(self, plates: list[PlateUsage], plate_count: int) -> SliceResult:
        """One answer for the whole file, with the plates kept underneath it."""
        times = [p.print_time_sec for p in plates if p.print_time_sec is not None]
        return SliceResult(
            engine=self.code,
            engine_version=self.version(),
            filaments=self._merge_slots([f for p in plates for f in p.filaments]),
            total_weight_g=round(sum(p.weight_g for p in plates), 2),
            print_time_sec=sum(times) if times else None,
            plate_count=plate_count,
            warnings=sorted({w for p in plates for w in p.warnings}),
            plates=plates,
        )

    def _parse(self, output_path: str, log: str) -> SliceResult:
        with zipfile.ZipFile(output_path) as zf:
            if SLICE_INFO not in zf.namelist():
                raise SliceFailed(f"{self.code}: no {SLICE_INFO} in output")
            root = ET.fromstring(zf.read(SLICE_INFO))

        filaments: list[FilamentUsage] = []
        total_weight = 0.0
        print_time: int | None = None
        warnings: list[str] = []
        plates = root.findall("plate")

        for plate in plates:
            meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
            if meta.get("weight"):
                total_weight += float(meta["weight"])
            if meta.get("prediction"):
                print_time = (print_time or 0) + int(float(meta["prediction"]))
            for warning in plate.findall("warning"):
                if warning.get("msg"):
                    warnings.append(warning.get("msg"))
            for f in plate.findall("filament"):
                filaments.append(
                    FilamentUsage(
                        slot=int(f.get("id", "1")),
                        used_g=float(f.get("used_g", "0")),
                        used_m=float(f.get("used_m", "0")),
                        material=f.get("type"),
                        color=f.get("color"),
                    )
                )

        return SliceResult(
            engine=self.code,
            engine_version=self.version(),
            filaments=self._merge_slots(filaments),
            total_weight_g=round(total_weight, 2),
            print_time_sec=print_time,
            plate_count=len(plates),
            warnings=sorted(set(warnings)),
            raw=self._tail(log),
        )

    @staticmethod
    def _merge_slots(filaments: list[FilamentUsage]) -> list[FilamentUsage]:
        """A multi-plate job reports each slot once per plate; the cost model
        wants one row per slot.

        Copies rather than accumulating into the row it was handed: the same
        rows are also reported per plate, and adding a total into one of them
        would quietly make plate 1 claim what all the plates used together.
        """
        merged: dict[int, FilamentUsage] = {}
        for f in filaments:
            existing = merged.get(f.slot)
            if existing is None:
                merged[f.slot] = dataclasses.replace(f)
            else:
                existing.used_g = round(existing.used_g + f.used_g, 2)
                existing.used_m = round(existing.used_m + f.used_m, 2)
        return [merged[k] for k in sorted(merged)]
