"""Slicer engine abstraction.

Engines differ in three ways that callers must never see:

* CLI dialect — flag names and semantics diverge between Orca, Bambu and Prusa.
* Input requirements — the Orca/Bambu family needs the Bambu project 3MF
  repaired before it will load; PrusaSlicer does not.
* Result format — Orca/Bambu write Metadata/slice_info.config into the exported
  .gcode.3mf, PrusaSlicer only emits `; filament used [g] = ...` gcode comments.

Every engine normalises those away and returns a SliceResult.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class FilamentUsage:
    """Per-slot consumption. `slot` is 1-based, matching the extruder numbers the
    caller addresses them by."""

    slot: int
    used_g: float
    used_m: float
    material: str | None = None
    color: str | None = None


@dataclass
class SliceResult:
    engine: str
    engine_version: str
    filaments: list[FilamentUsage]
    total_weight_g: float
    print_time_sec: int | None = None
    plate_count: int = 1
    warnings: list[str] = field(default_factory=list)
    raw: str | None = None

    @property
    def filament_count(self) -> int:
        return len([f for f in self.filaments if f.used_g > 0])


@dataclass
class SliceRequest:
    """What to slice. Profile names are engine-specific strings taken from
    profiles(); the caller picks them in the admin, we do not invent them."""

    input_path: str
    machine_profile: str | None = None
    process_profile: str | None = None
    filament_profiles: list[str] = field(default_factory=list)
    scale: float = 1.0
    plate: int = 0  # 0 = all plates


class EngineUnavailable(RuntimeError):
    pass


class SliceFailed(RuntimeError):
    def __init__(self, message: str, exit_code: int | None = None, log: str | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.log = log


class SlicerEngine(abc.ABC):
    """One installed slicer binary."""

    code: str
    label: str

    @abc.abstractmethod
    def is_available(self) -> bool:
        """False when the binary is absent from this image build."""

    @abc.abstractmethod
    def version(self) -> str:
        ...

    @abc.abstractmethod
    def profiles(self) -> dict[str, list[str]]:
        """{'machine': [...], 'process': [...], 'filament': [...]}"""

    def preprocess(self, input_path: str, workdir: str) -> str:
        """Return a path the engine can actually load. Default: unchanged."""
        return input_path

    @abc.abstractmethod
    def slice(self, request: SliceRequest, workdir: str) -> SliceResult:
        ...
