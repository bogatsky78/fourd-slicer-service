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
    # Grams of every support the plate prints, largest first — one entry per
    # support an operator snaps off. Empty: printed without supports. None: the
    # G-code could not be read for it. See supports.py.
    supports: list[float] | None = None
    warnings: list[str] = field(default_factory=list)
    # What had to be changed before the plate would slice at all — moving a
    # prime tower onto the bed, for instance. Reported rather than done quietly,
    # because it says the file describes a print this machine cannot run as laid
    # out; see OrcaSlicerEngine._remedy().
    adjustments: list[str] = field(default_factory=list)
    # A plate the engine could not print in one go, printed in several: the
    # figures above are their sum and each pass is here in the same shape.
    # Empty for a plate printed in one pass, which is every plate the file laid
    # out so that it could be. See OrcaSlicerEngine._split_plan().
    passes: list["PlateUsage"] = field(default_factory=list)

    @property
    def colours(self) -> int:
        """Filaments loaded at once — on the busiest pass, when there are passes."""
        if self.passes:
            return max(p.colours for p in self.passes)
        return len([f for f in self.filaments if f.used_g > 0])


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
    # The same box as two corners rather than three lengths, in the file's own
    # coordinates and **without** the request's scale — unlike size_* above,
    # which carry it. Both differences are deliberate: an assembly is placed by
    # pushing these corners through matrices written in those same unscaled
    # coordinates, and a piece is not centred on its own origin (one of Stay
    # Golden's runs 2.8..37.6 in x), so a box reconstructed from the lengths
    # alone would land somewhere the part is not.
    #
    # Not published. Where a piece sits in its own frame is the file's private
    # business and answers nothing a caller asked.
    file_min: tuple[float, float, float] | None = None
    file_max: tuple[float, float, float] | None = None
    # What the file's build item scales this object by, per local axis, or
    # None when it names no scale. Already folded into size_* and volume_mm3;
    # carried on its own because the corners above are not scaled, and the
    # assembly has to decide for itself whether to apply it. Not published.
    build_scale: tuple[float, float, float] | None = None
    # Material as `--info` measured it, before either scale. Not published: the
    # assembly ranks its groups by material in the file's frame, the same frame
    # its corners are in, and volume_mm3 above has the build item's scale in
    # it — which differs from piece to piece in a file like the hydra's.
    file_volume_mm3: float | None = None
    # The object's id in the file, which is how the plates name it. Null for a
    # file that has no ids to give (a bare mesh).
    object_id: int | None = None
    # The plate the object stands on, 1-based. Null means **on no plate at
    # all**: the author parked it off the beds, nothing prints it, and it is
    # left out of every aggregate and of the assembly. A file that says nothing
    # about plates prints as one, so everything in it is on plate 1.
    plate: int | None = None
    # Whether the object is one of the pieces the assembly glued together.
    # Null where there is no assembly to belong to.
    in_assembly: bool | None = None

    @property
    def longest_edge(self) -> float:
        return max(self.size_x, self.size_y, self.size_z)


@dataclass
class Assembly:
    """The model as one object: the toy that stands on the shelf once its pieces
    are glued together, as opposed to the pieces that come off the bed.

    A laid-out model has no size anywhere in it — `--info` measures each piece in
    its own coordinates, and how they sit on the plates is a fact about
    compositing, not about the toy. What does say it is the file's `<assemble>`
    block: where the author placed every piece relative to every other. Running
    the pieces' boxes through those placements is the only answer in the file to
    "how big is the thing the customer receives".

    Absent — `None` rather than a number — when the file carries no such block.
    A caller needs to be able to tell "we could not measure it" from a
    measurement, because the honest response to the first is to ask a human.
    """

    size_x: float
    size_y: float
    size_z: float
    part_count: int
    # Indices into ModelInfo.objects of the pieces counted in part_count. Not
    # published as such; each object carries its own `in_assembly` instead.
    members: list[int] = field(default_factory=list)


@dataclass
class PlateLayout:
    """One plate as the file lays it out — before anything is sliced.

    `name` is the author's own label for the plate, when there is one. It is
    the best hint the file holds about *why* a plate exists ("AMS", "1C
    Keyring - Mini"), and nothing more than a hint: it is free text.
    """

    index: int
    name: str | None = None
    object_count: int = 0
    # "ams" — the plate's objects use more than one palette slot, so it prints
    # with a prime tower; "simple" — one colour, no tower.
    kind: str | None = None
    # "multi" — several models on the plate, all the same shape; "one" — a
    # single model, or several different ones. Null when the meshes were not
    # read.
    copies: str | None = None
    # What comes off the plate as one thing each: {"kind": "part" | "ams" |
    # "flexi", "object_ids": [...], "shells": n}. See layout.py.
    models: list[dict] | None = None


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
    assembly: Assembly | None = None
    plates: list[PlateLayout] = field(default_factory=list)
    # How the plates relate to one another — see OrcaSlicerEngine._layout_kind:
    # "single" (one plate), "assembly" (pieces on different plates glue into
    # one model) or "separate" (every plate holds models of its own). Null
    # when the file could not be read for it.
    layout: str | None = None
    # Only for "separate": whether every plate holds the same kind of model —
    # a hint that the plates are variants of one model rather than a set.
    # A hint and no more; which plates to print is a person's call.
    plates_alike: bool | None = None

    @property
    def printed(self) -> list[ModelObject]:
        """The objects that stand on a plate. Every aggregate counts only these."""
        return [o for o in self.objects if o.plate is not None or not self.plates]

    @property
    def object_count(self) -> int:
        return len(self.printed)

    @property
    def total_volume_mm3(self) -> float | None:
        volumes = [o.volume_mm3 for o in self.printed if o.volume_mm3 is not None]
        return round(sum(volumes), 3) if volumes else None

    @property
    def largest(self) -> ModelObject | None:
        return max(self.printed, key=lambda o: o.longest_edge, default=None)

    @property
    def size_x(self) -> float | None:
        return self.largest.size_x if self.largest else None

    @property
    def size_y(self) -> float | None:
        return self.largest.size_y if self.largest else None

    @property
    def size_z(self) -> float | None:
        return self.largest.size_z if self.largest else None

    # What an object publishes. Listed rather than taken from `asdict`, because
    # ModelObject also carries the corners the assembly is built from, and those
    # are coordinates in a frame the caller has no way to interpret.
    OBJECT_FIELDS = (
        "size_x", "size_y", "size_z", "volume_mm3", "facet_count", "manifold",
        "object_id", "plate", "in_assembly",
    )
    ASSEMBLY_FIELDS = ("size_x", "size_y", "size_z", "part_count")

    def to_payload(self) -> dict:
        """Serialised by hand because `dataclasses.asdict` cannot see properties,
        and the aggregates are the part most callers actually read."""
        return {
            # Every object the file holds, off-plate ones included and marked
            # so; the aggregates after it count only what is printed.
            "objects": [
                {name: getattr(o, name) for name in self.OBJECT_FIELDS}
                for o in self.objects
            ],
            "object_count": self.object_count,
            "total_volume_mm3": self.total_volume_mm3,
            "size_x": self.size_x,
            "size_y": self.size_y,
            "size_z": self.size_z,
            # Null means the file does not say, and must not be read as zero or
            # quietly replaced with the largest piece — see Assembly.
            "assembly": (
                {name: getattr(self.assembly, name) for name in self.ASSEMBLY_FIELDS}
                if self.assembly else None
            ),
            "plates": [dataclasses.asdict(p) for p in self.plates],
            "layout": self.layout,
            "plates_alike": self.plates_alike,
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
    # Which plates the totals above are made of, when the request named some;
    # empty for a whole-file slice.
    plates_sliced: list[int] = field(default_factory=list)
    # What the slice did, step by step, and every engine run's full log —
    # see OrcaSlicerEngine.slice(). A refusal carries the same two on SliceFailed.
    journal: list[dict] = field(default_factory=list)
    engine_logs: list[dict] = field(default_factory=list)

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
        return max((p.colours for p in self.plates), default=0)


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
    # Several plates, 1-based, when only some are to be printed — the variants
    # case, where a person picked which of a file's plates the product is.
    # Takes precedence over `plate`; empty means `plate` decides.
    plates: list[int] = field(default_factory=list)
    brim: bool = False  # see OrcaSlicerEngine._run_slice for why off is the default


class EngineUnavailable(RuntimeError):
    pass


class SliceFailed(RuntimeError):
    """A refusal, with an optional name for what kind of refusal it is.

    `reason` is for the few refusals a caller can *act* on, and it is deliberately
    not filled in for the rest: a sentence and a log tail are all anyone can do
    with a segfault, while "this plate does not fit the bed" is a fact about the
    product that has to survive the wire as something other than prose. It stays
    None unless a refusal has earned a name — an unnamed one means only that the
    slicer said no.
    """

    #: The plate hangs off the bed and no argument of ours would change that.
    OFF_BED = "off_bed"
    #: Two toolpaths cross — the prime tower and a part, once the tower has
    #: been given the freest corner of the bed and still crosses, or two parts
    #: in the file's own layout — and no argument of ours would change that.
    CONFLICT = "conflict"
    #: The plate needs a prime tower and its parts have different layer
    #: heights, which the engine refuses, and the plate cannot be printed in
    #: passes either: a flexi model on it, a model whose own parts differ, or
    #: parts of different heights inside each other's reach.
    LAYER_HEIGHTS = "layer_heights"

    def __init__(
        self,
        message: str,
        exit_code: int | None = None,
        log: str | None = None,
        reason: str | None = None,
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.log = log
        self.reason = reason
        # Filled in on the way out of OrcaSlicerEngine.slice().
        self.journal: list[dict] = []
        self.engine_logs: list[dict] = []


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
