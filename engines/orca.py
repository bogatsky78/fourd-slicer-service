"""Upstream OrcaSlicer engine.

Chosen over the Snapmaker Orca fork because the fork's CLI segfaults on every
3MF project — see the note in ../Dockerfile for the backtrace. Upstream ships
the Snapmaker U1 profiles too, so nothing is lost.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from .base import (
    EngineUnavailable,
    FilamentUsage,
    ModelInfo,
    ModelObject,
    SliceFailed,
    SliceRequest,
    SliceResult,
    SlicerEngine,
)

SLICE_INFO = "Metadata/slice_info.config"

# `--info` prints one `key = value` per line under a `[filename]` header.
INFO_LINE = re.compile(r"^\s*([a-z_]+)\s*=\s*(.+?)\s*$")


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
    ) -> str:
        """Bambu project files need repairing before Orca will load them. Files
        that are already clean pass through untouched."""
        from repair3mf import repair

        if not input_path.lower().endswith(".3mf"):
            return input_path
        with zipfile.ZipFile(input_path) as zf:
            if "Metadata/project_settings.config" not in zf.namelist():
                return input_path

        repaired = os.path.join(workdir, "repaired.3mf")
        repair(input_path, repaired, project_overrides)
        return repaired

    def slice(self, request: SliceRequest, workdir: str) -> SliceResult:
        if not self.is_available():
            raise EngineUnavailable(f"{self.code}: binary missing at {self.binary}")

        source = self.preprocess(
            request.input_path, workdir, self._machine_overrides(request.machine_profile)
        )
        output_name = "sliced.gcode.3mf"

        argv = [
            "--slice", str(request.plate),
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
        argv.append(source)

        proc = self._run(argv, expect_success=False)
        output_path = os.path.join(workdir, output_name)
        if proc.returncode != 0 or not os.path.exists(output_path):
            raise SliceFailed(
                f"{self.code} slicing failed",
                exit_code=proc.returncode,
                log=self._log(proc),
            )

        result = self._parse(output_path, self._log(proc))
        # Measured off the same repaired copy the slice just consumed, so the
        # dimensions describe exactly the geometry the weights came from.
        result.model = self._inspect_file(source, request.scale)

        return result

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
    # moment we load our own machine profile over the file's own.
    MACHINE_SETTINGS = ("single_extruder_multi_material",)

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
        """
        if not machine_profile:
            return {}

        overrides = {}
        for key in self.MACHINE_SETTINGS:
            value = self._profile_setting(machine_profile, key)
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

    def _run(self, argv: list[str], expect_success: bool = True):
        cmd = [self.env_wrapper, self.binary, *argv]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
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
        wants one row per slot."""
        merged: dict[int, FilamentUsage] = {}
        for f in filaments:
            existing = merged.get(f.slot)
            if existing is None:
                merged[f.slot] = f
            else:
                existing.used_g = round(existing.used_g + f.used_g, 2)
                existing.used_m = round(existing.used_m + f.used_m, 2)
        return [merged[k] for k in sorted(merged)]
