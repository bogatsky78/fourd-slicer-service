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
import dataclasses
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
class PlateUsage:
    """What one plate of a multi-plate job consumes.

    A file laid out across plates is not one print but several, run one after
    another, and the caller prices them as such: which colour sits on which
    plate decides whether the job needs a four-head machine or four passes of a
    one-head one. Summing the plates away would throw that away, so the totals
    and the breakdown are both reported.
    """

    index: int
    weight_g: float
    print_time_sec: int | None = None
    filaments: list[FilamentUsage] = field(default_factory=list)
    # None means the slicer did not say, which is not the same as no changes.
    filament_changes: int | None = None
    warnings: list[str] = field(default_factory=list)
    # What had to be changed before the plate would slice at all — moving a
    # prime tower onto the bed, for instance. Reported rather than done quietly,
    # because it says the file describes a print this machine cannot run as laid
    # out; see OrcaSlicerEngine._remedy().
    adjustments: list[str] = field(default_factory=list)


@dataclass
class ModelObject:
    """One printable object out of a file.

    Its own bounding box, in its own coordinates. A 3MF routinely holds several:
    a keychain file in this catalogue carries the puppy, two rings and a clip,
    each of which is printed and packed as a separate piece.
    """

    size_x: float
    size_y: float
    size_z: float
    volume_mm3: float | None = None
    facet_count: int | None = None
    manifold: bool | None = None

    @property
    def longest_edge(self) -> float:
        return max(self.size_x, self.size_y, self.size_z)


@dataclass
class ModelInfo:
    """What the model *is*, as opposed to what printing it would consume.

    Geometry, so it needs no slicing and no printer profile — which is why it is
    reachable on its own (`/inspect`) as well as folded into a slice. Sizes are
    millimetres, volumes are cubic millimetres, and both already have the
    request's scale applied: a caller asking about a print wants the size of the
    thing that comes off the bed, not of the file.

    **A list, not a box.** Reporting one bounding box for a multi-object file
    would be answering a question nobody asked: the pieces are printed apart and
    packed apart, so what a shipping box has to satisfy is every piece
    individually plus their combined volume — never the extent of their
    arrangement on the print bed, which is an artefact of how they were laid
    out. The convenience `size_*` below is the largest single object, kept so a
    caller that only wants one number does not have to sort the list itself.

    `manifold` is carried through because a model that is not watertight is one
    whose volume, and therefore whose weight estimate, cannot be trusted.
    """

    objects: list[ModelObject] = field(default_factory=list)

    @property
    def object_count(self) -> int:
        return len(self.objects)

    @property
    def total_volume_mm3(self) -> float | None:
        volumes = [o.volume_mm3 for o in self.objects if o.volume_mm3 is not None]
        return round(sum(volumes), 3) if volumes else None

    @property
    def largest(self) -> ModelObject | None:
        return max(self.objects, key=lambda o: o.longest_edge, default=None)

    @property
    def size_x(self) -> float | None:
        return self.largest.size_x if self.largest else None

    @property
    def size_y(self) -> float | None:
        return self.largest.size_y if self.largest else None

    @property
    def size_z(self) -> float | None:
        return self.largest.size_z if self.largest else None

    def to_payload(self) -> dict:
        """Serialised by hand because `dataclasses.asdict` cannot see properties,
        and the aggregates are the part most callers actually read."""
        return {
            "objects": [dataclasses.asdict(o) for o in self.objects],
            "object_count": self.object_count,
            "total_volume_mm3": self.total_volume_mm3,
            "size_x": self.size_x,
            "size_y": self.size_y,
            "size_z": self.size_z,
        }


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
    model: ModelInfo | None = None
    plates: list[PlateUsage] = field(default_factory=list)

    @property
    def filament_count(self) -> int:
        return len([f for f in self.filaments if f.used_g > 0])

    @property
    def max_plate_filaments(self) -> int:
        """Colours on the busiest plate — what decides whether the job needs a
        machine with that many heads. The total colour count answers a different
        question and is routinely much larger: an eleven-colour assembly whose
        plates hold one colour each prints on a single-head machine."""
        if not self.plates:
            return self.filament_count
        return max(
            (len([f for f in p.filaments if f.used_g > 0]) for p in self.plates),
            default=0,
        )


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

    @abc.abstractmethod
    def inspect(self, request: SliceRequest, workdir: str) -> ModelInfo:
        """Measure the model without slicing it.

        Separate from slice() because the two answer different questions at
        wildly different prices: geometry is seconds, a slice is minutes. A
        caller that only needs to know whether a model fits in a box, or what
        size to show a customer, must not have to pay for a slice to find out.
        """
