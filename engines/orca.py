"""Upstream OrcaSlicer engine.

Chosen over the Snapmaker Orca fork because the fork's CLI segfaults on every
3MF project — see the note in ../Dockerfile for the backtrace. Upstream ships
the Snapmaker U1 profiles too, so nothing is lost.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from .base import (
    EngineUnavailable,
    FilamentUsage,
    ModelInfo,
    ModelObject,
    PlateUsage,
    SliceFailed,
    SliceRequest,
    SliceResult,
    SlicerEngine,
)

SLICE_INFO = "Metadata/slice_info.config"
MODEL_SETTINGS = "Metadata/model_settings.config"
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


@dataclasses.dataclass
class _Remedy:
    """One thing to change about a refused plate, and the sentence for it."""

    kind: str
    argv: list[str]
    note: str
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
        repair(input_path, repaired, project_overrides, object_extruders)
        return repaired

    def slice(self, request: SliceRequest, workdir: str) -> SliceResult:
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

        rewrites, palette = self._extruder_remap(
            request.input_path, self._head_count(request.machine_profile)
        )
        facts = self._palette_facts(request.input_path) if palette else {}
        source = self.preprocess(
            request.input_path,
            workdir,
            self._machine_overrides(request.machine_profile),
            rewrites,
        )

        declared = self._plate_count(source)
        wanted = [request.plate] if request.plate else self._plates_to_slice(declared)

        sliced = []
        learned: list[str] = []
        for n in wanted:
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
        # The log of the last plate to run, which is where a warning about the
        # whole job lands; the per-plate detail is in `plates`.
        result.raw = sliced[-1][1] if sliced else None
        # Measured off the same repaired copy the slice just consumed, so the
        # dimensions describe exactly the geometry the weights came from.
        result.model = self._inspect_file(source, request.scale)

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
        # changed, and both are reported rather than done quietly.
        adjustments: list[str] = []
        tried: list[str] = []
        while not output_path and len(tried) < len(self.REMEDIES):
            remedy = self._remedy(request, source, workdir, plate, proc, tried)
            if not remedy:
                break
            extra += remedy.argv
            tried.append(remedy.kind)
            adjustments.append(remedy.note)
            if remedy.sticky and learned is not None:
                learned += remedy.argv
            proc, output_path = self._run_slice(
                request, source, workdir, plate, extra=extra
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
        usage.adjustments = adjustments
        # Everything wanted from this plate has been read. An eleven-plate
        # assembly would otherwise hold eleven G-code files at once, and the
        # directory this lands in is a RAM disk in the deployment it was written
        # for.
        shutil.rmtree(workdir, ignore_errors=True)
        return usage, self._log(proc)

    # Each remedy is one thing worth changing about a refused plate, tried at
    # most once and in this order.
    REMEDIES = ("tower", "labels")

    def _remedy(
        self,
        request: SliceRequest,
        source: str,
        workdir: str,
        plate: int,
        proc,
        tried: list[str],
    ) -> "_Remedy | None":
        """The next thing worth changing about a plate the binary refused."""
        if "tower" not in tried:
            moved = self._tower_shift(request, source, workdir, plate)
            if moved:
                return moved

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
        ]
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

        proc = self._run(argv, expect_success=False, cwd=workdir)
        output_path = os.path.join(workdir, output_name)
        if proc.returncode != 0 or not os.path.exists(output_path):
            return proc, None
        return proc, output_path

    def inspect(self, request: SliceRequest, workdir: str) -> ModelInfo:
        if not self.is_available():
            raise EngineUnavailable(f"{self.code}: binary missing at {self.binary}")

        # The same repair a slice needs: `--info` on an untouched Bambu project
        # dies with `return -24` exactly as slicing does.
        source = self.preprocess(request.input_path, workdir, {})

        return self._inspect_file(source, request.scale)

    # -- internals ---------------------------------------------------------

    def _inspect_file(self, path: str, scale: float = 1.0) -> ModelInfo:
        """Ask the binary what the model measures.

        `--allow-newer-file --no-check` are not optional here for the same
        reason they are not optional when slicing: catalogue files are written
        by a newer Bambu Studio than the engine, and without them `--info`
        refuses the file with a bare `return -24` and no explanation.

        `--info` reports the model as it sits in the file; `--scale` is applied
        later, during slicing. Multiplying here is what makes the answer the
        size of the printed object rather than of the drawing.
        """
        proc = self._run(
            ["--info", "--allow-newer-file", "--no-check", path],
            expect_success=False,
            cwd=os.path.dirname(path) or None,
        )

        factor = scale if scale > 0 else 1.0
        objects = [
            self._object(block, factor)
            for block in self._info_blocks(proc.stdout or "")
            if {"size_x", "size_y", "size_z"} <= block.keys()
        ]

        if not objects:
            raise SliceFailed(
                f"{self.code}: --info reported no dimensions",
                exit_code=proc.returncode,
                log=self._log(proc),
            )

        return ModelInfo(objects=objects)

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
    def _object(cls, values: dict[str, str], factor: float) -> ModelObject:
        return ModelObject(
            size_x=round(float(values["size_x"]) * factor, 3),
            size_y=round(float(values["size_y"]) * factor, 3),
            size_z=round(float(values["size_z"]) * factor, 3),
            # Volume scales with the cube of a linear factor, not with the factor.
            volume_mm3=cls._maybe_float(values.get("volume"), factor ** 3),
            facet_count=cls._maybe_int(values.get("number_of_facets")),
            manifold=cls._maybe_bool(values.get("manifold")),
        )

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
    ) -> tuple[dict[str, dict[str, str]], dict[int, dict[int, int]]]:
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

        Returns the rewrite to apply to the file, and per plate a
        `{new number: original number}` map for reading the results back.
        """
        plates, objects = self._layout(source)
        if not plates:
            return {}, {}

        rewrites: dict[str, dict[str, str]] = {}
        palettes: dict[int, dict[int, int]] = {}
        for index, object_ids in plates.items():
            used = sorted({e for oid in object_ids for e in objects.get(oid, set())})
            if not used or used[-1] <= heads:
                continue
            if len(used) > heads or not self._interchangeable(source, used):
                continue

            mapping = {old: new for new, old in enumerate(used, start=1)}
            palettes[index] = {new: old for old, new in mapping.items()}
            for oid in object_ids:
                rewrites[oid] = {str(k): str(v) for k, v in mapping.items()}

        return rewrites, palettes

    def _layout(self, source: str) -> tuple[dict[int, list[str]], dict[str, set[int]]]:
        """Which objects sit on which plate, and which filaments each object uses.

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
        self, request: SliceRequest, source: str, workdir: str, plate: int
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
        origin = self._tower_origin(source, plate)
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

    def _tower_origin(self, source: str, plate: int) -> tuple[float, float] | None:
        """Where the file puts the prime tower, as the shift's starting point.

        Read from the project rather than measured off the G-code: the tower
        section of a G-code file also holds the travel moves that fetch and
        leave it, so its bounding box starts wherever the object is, not where
        the tower does. The position is stored per plate, since each plate gets
        its own tower.
        """
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
