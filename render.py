"""One picture of a model, by whatever means the file allows.

A caller that wants to *show* a 3MF has three sources for a picture and they are
not equally good, so this is a cascade rather than a choice:

1. `author_render` — `Auxiliaries/Model Pictures/render.webp`, the studio render
   the designer published with the model. Best by a wide margin and free.
2. `author_thumbnail` — `Auxiliaries/.thumbnails/thumbnail_middle.png`, the
   designer's photograph. Present in every MakerWorld file, but it is a
   photograph of a print and sometimes carries an overlay the designer added
   (Stay Golden's has a crossed-out "AMS" badge stamped across it).
3. `geometry` — our own render of the meshes. The only stage that works on a
   project a customer saved out of Orca themselves: such a file has no
   `Auxiliaries/` directory at all, only `Metadata/plate_N.png`, which is a
   picture of a print bed and not of a model.

**Why the whole cascade lives here** and not half of it in the caller: pulling
the embedded pictures out is three lines, and splitting a cascade across two
languages so that one language owns steps 1–2 and the other step 3 buys nothing
and costs a caller the ability to say "give me a picture" and be done.

The stage is reported alongside the image because it changes what the picture
*is*: a studio render of the finished toy, a photo of somebody's print, or our
own untextured geometry. A caller deciding whether to show it to a customer
needs to know which of the three it got.

Output is **always PNG**. The embedded pictures are webp, and Magento — the first
caller — carries no webp support at all: `vendor/magento/framework/Image/` has
Gd2 and ImageMagick adapters and not one mention of the format. Re-encoding here
costs milliseconds and saves every caller from finding that out the hard way.
"""
from __future__ import annotations

import io
import itertools
import json
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import numpy as np
from PIL import Image

#: In cascade order. `author_*` come out of the archive, `geometry` is drawn.
STAGES = ("author_render", "author_thumbnail", "geometry")

AUTHOR_RENDER = "Auxiliaries/Model Pictures/render.webp"
AUTHOR_THUMBNAIL = "Auxiliaries/.thumbnails/thumbnail_middle.png"

#: How many parsed numbers between sweeps of the XML tree, in _read_part.
HARVEST = 60_000

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
MODEL_SETTINGS = "Metadata/model_settings.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
ROOT_MODEL = "3D/3dmodel.model"


class RenderFailed(RuntimeError):
    """No stage could produce a picture of this file."""


@dataclass
class Render:
    stage: str
    png: bytes
    width: int
    height: int
    #: Only ever true for `geometry`, and then only when the file says how its
    #: pieces fit together — see `_assembled_placements`. None for the stages
    #: where the question does not arise, because a studio render is whatever
    #: the designer photographed and we cannot tell.
    assembled: bool | None = None


def render(path: str, width: int = 900, height: int = 900, stage: str | None = None) -> Render:
    """Best available picture of `path`, at most `width` x `height`.

    `stage` forces one stage instead of walking the cascade — the way to ask
    "what would our own renderer make of this file" about a model that has a
    perfectly good studio render sitting in it.
    """
    if stage is not None and stage not in STAGES:
        raise RenderFailed(f"unknown render stage: {stage}")

    wanted = (stage,) if stage else STAGES
    problems: list[str] = []
    for name in wanted:
        try:
            result = _STAGES[name](path, width, height)
        except Exception as exc:  # noqa: BLE001 - a stage that fails is a stage we skip
            problems.append(f"{name}: {exc}")
            continue
        if result is not None:
            return result
        problems.append(f"{name}: not in this file")

    raise RenderFailed("; ".join(problems) or "nothing to render")


def _embedded(name: str):
    """A stage that lifts one picture straight out of the archive."""

    def stage(path: str, width: int, height: int) -> Render | None:
        with zipfile.ZipFile(path) as zf:
            if name not in zf.namelist():
                return None
            raw = zf.read(name)

        with Image.open(io.BytesIO(raw)) as image:
            return _encode(image.convert("RGB"), width, height, _EMBEDDED_STAGE[name])

    return stage


def _encode(image: Image.Image, width: int, height: int, stage: str) -> Render:
    """Fit inside the box, never past the source's own size, and write PNG.

    Fitting rather than cropping or padding: what to do with the leftover space
    is the caller's decision — a shop pads its own thumbnails to whatever shape
    its grid wants — and a crop here would throw away pixels nobody can get back.
    Never upscaling for the same reason: 680x510 stretched to 900x900 is not more
    picture, only a bigger file.
    """
    image.thumbnail((width, height), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)

    return Render(stage=stage, png=buffer.getvalue(), width=image.width, height=image.height)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

#: Orca's interface accent colour, which it writes into `filament_colour` for a
#: slot the author never assigned a colour to. Painting a model in it would be
#: inventing a colour decision the author did not make, so it is drawn as the
#: neutral grey that "unset" actually means.
ORCA_PLACEHOLDER = "#26A69A"
UNSET_COLOUR = (0.72, 0.72, 0.74)
DEFAULT_COLOUR = (0.78, 0.78, 0.80)
BACKGROUND = (0.945, 0.945, 0.945)

#: How far apart two placed pieces may be and still count as touching. Same
#: figure, and the same reasoning, as OrcaSlicerEngine.TOUCH_TOLERANCE_MM: mating
#: printed parts are drawn with a tenth or two of clearance so they go together
#: once printed.
TOUCH_TOLERANCE_MM = 0.5

#: Three-quarter view, azimuth then elevation, in degrees. A product shot rather
#: than a print-bed shot: straight down would show a plate of parts even for a
#: model that is properly assembled.
AZIMUTH = -40.0
ELEVATION = 22.0

#: Drawn at this multiple of the requested size and averaged down. Cheaper than
#: any smarter antialiasing, and the edges of a model against a plain ground are
#: the whole of what a viewer notices.
SUPERSAMPLE = 2

#: Ceiling on scattered samples. Measured need at 900x900: 11.9M for the knitted
#: corgi and 10.5M for the eleven-million-triangle shepherd — the two converge
#: because past a triangle per pixel it is the covered area that decides, not the
#: mesh. So the cap only bites on a model with a few enormous flat faces, where
#: the density is lowered instead and the picture is no worse for it.
SAMPLE_BUDGET = 24_000_000


@dataclass
class _Mesh:
    #: In the coordinates of the **built object** it belongs to: component
    #: transforms are already folded in, the build item's own placement is not.
    #: That split is what lets the same meshes be drawn in either pose.
    vertices: np.ndarray  # (n, 3) float32
    triangles: np.ndarray  # (m, 3) int32
    colour: tuple[float, float, float]
    instance: int


def _geometry(path: str, width: int, height: int) -> Render | None:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if ROOT_MODEL not in names:
            return None

        palette = _palette(zf, names)
        objects, builds = _read_model(zf, names)
        if not objects or not builds:
            return None

        meshes = _flatten(zf, names, objects, builds, palette)
        assemble = _assemble(zf, names)

    if not meshes:
        return None

    placements = _pose(builds, meshes, assemble)
    subject, assembled = _subject(meshes, placements)
    image = _draw(subject, placements, width, height)

    result = _encode(image, width, height, "geometry")
    result.assembled = assembled

    return result


def _palette(zf: zipfile.ZipFile, names: set[str]) -> list[tuple[float, float, float]]:
    """Slot colours as the project settings give them, 1-based on the wire.

    Index 0 of the returned list is slot 1, matching the `extruder` numbers in
    `model_settings.config`. Those numbers address a palette entry, not a print
    head — a distinction that has already cost this project one round of bugs.
    """
    if PROJECT_SETTINGS not in names:
        return []
    try:
        settings = json.loads(zf.read(PROJECT_SETTINGS))
    except (ValueError, OSError):
        return []

    colours = settings.get("filament_colour") or []

    return [_rgb(str(c)) for c in colours]


def _rgb(value: str) -> tuple[float, float, float]:
    value = value.strip()
    if value.upper() == ORCA_PLACEHOLDER:
        return UNSET_COLOUR
    if not value.startswith("#") or len(value) < 7:
        return DEFAULT_COLOUR
    try:
        return tuple(int(value[i:i + 2], 16) / 255.0 for i in (1, 3, 5))  # type: ignore[return-value]
    except ValueError:
        return DEFAULT_COLOUR


IDENTITY = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]])


def _matrix(raw: str | None) -> np.ndarray:
    """A 3MF transform, as the file writes it and the spec reads it.

    Twelve numbers, applied to a **row** vector: `p' = p @ M[:3] + M[3]`. The
    order in the attribute is m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32,
    so a naive reshape into (4, 3) is already the right thing and the transpose
    is the wrong one. It makes no difference to a translation, which is what
    almost every matrix in this catalogue is, and all the difference to a
    rotation.
    """
    if not raw:
        return IDENTITY
    values = [float(v) for v in raw.split()]
    if len(values) != 12:
        return IDENTITY

    return np.array(values, dtype=np.float64).reshape(4, 3)


def _compose(inner: np.ndarray, outer: np.ndarray) -> np.ndarray:
    """`inner` applied first, then `outer`."""
    composed = np.empty((4, 3))
    composed[:3] = inner[:3] @ outer[:3]
    composed[3] = inner[3] @ outer[:3] + outer[3]

    return composed


def _apply(vertices: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    return vertices @ matrix[:3] + matrix[3]


def _placed_bounds(vertices: np.ndarray, matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bounding box of a mesh once placed, without materialising the placement.

    The boxes are wanted for every piece of every file, and building a second
    copy of a five-million-vertex mesh to take its minimum and maximum is the
    kind of arithmetic that decides whether a render fits in memory.
    """
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    for chunk in np.array_split(vertices, max(1, len(vertices) // 500_000)):
        placed = _apply(chunk.astype(np.float64), matrix)
        lo = np.minimum(lo, placed.min(axis=0))
        hi = np.maximum(hi, placed.max(axis=0))

    return lo, hi


def _read_model(zf: zipfile.ZipFile, names: set[str]):
    """Objects and build items out of the root model part.

    Objects are keyed by (part path, id) because the production extension lets a
    component point into another part file, and ids are only unique within one.
    A catalogue file routinely keeps every mesh in a single `3D/Objects/*.model`
    and references it eight times.
    """
    objects: dict[tuple[str, int], dict] = {}
    builds: list[dict] = []

    _read_part(zf, names, ROOT_MODEL, objects, set())

    root = ET.fromstring(zf.read(ROOT_MODEL))
    build = root.find(f"{{{CORE_NS}}}build")
    if build is None:
        return objects, builds

    seen: dict[int, int] = {}
    for item in build.findall(f"{{{CORE_NS}}}item"):
        raw_id = item.get("objectid")
        if raw_id is None or not raw_id.isdigit():
            continue
        object_id = int(raw_id)
        # Studio numbers repeats of one object from zero, and `<assemble>`
        # addresses them by that number; nothing in the file states it, so it is
        # counted the same way here.
        instance_id = seen.get(object_id, 0)
        seen[object_id] = instance_id + 1
        builds.append(
            {
                "key": (item.get(f"{{{PRODUCTION_NS}}}path") or ROOT_MODEL, object_id),
                "object_id": object_id,
                "instance_id": instance_id,
                "matrix": _matrix(item.get("transform")),
            }
        )

    return objects, builds


def _read_part(zf: zipfile.ZipFile, names: set[str], part: str, objects: dict, seen: set[str]) -> None:
    """Parse one model part, following component references into the others.

    Meshes are read with `iterparse` and dropped into flat Python lists before
    numpy sees them: a 985k-triangle object is 81 MB of XML, and building the
    arrays element by element costs more than the parse itself.
    """
    part = part.lstrip("/")
    if part in seen or part not in names:
        return
    seen.add(part)

    current: dict | None = None
    container: ET.Element | None = None
    coordinates: list[float] = []
    indices: list[int] = []
    components: list[dict] = []
    follow: list[str] = []

    for event, element in ET.iterparse(zf.open(part), events=("start", "end")):
        tag = element.tag.split("}")[-1]
        if event == "start":
            if tag == "object":
                raw_id = element.get("id")
                current = {"id": int(raw_id)} if raw_id and raw_id.isdigit() else None
                coordinates, indices, components = [], [], []
            elif tag in ("vertices", "triangles"):
                container = element
            elif current is None:
                continue
            elif tag == "vertex":
                coordinates.append(float(element.get("x", 0.0)))
                coordinates.append(float(element.get("y", 0.0)))
                coordinates.append(float(element.get("z", 0.0)))
            elif tag == "triangle":
                indices.append(int(element.get("v1", 0)))
                indices.append(int(element.get("v2", 0)))
                indices.append(int(element.get("v3", 0)))
            elif tag == "component":
                child = (element.get(f"{{{PRODUCTION_NS}}}path") or part).lstrip("/")
                raw_id = element.get("objectid")
                if raw_id and raw_id.isdigit():
                    components.append({"key": (child, int(raw_id)), "matrix": _matrix(element.get("transform"))})
                    if child != part:
                        follow.append(child)
            if container is not None and (len(coordinates) + len(indices)) % HARVEST == 0:
                # The numbers are already out; the elements they came from are
                # not, and iterparse keeps every one of them hanging off its
                # parent until the parent ends. On a 187 MB file that is around
                # three million live Element objects and 2.4 GB of resident
                # memory — an order of magnitude more than the arrays being
                # built. Emptying the parent as we go costs nothing and brings
                # the same file down to a few hundred megabytes.
                container.clear()
        elif tag == "object" and current is not None:
            if indices:
                current["vertices"] = np.asarray(coordinates, dtype=np.float32).reshape(-1, 3)
                current["triangles"] = np.asarray(indices, dtype=np.int32).reshape(-1, 3)
            current["components"] = components
            objects[(part, current["id"])] = current
            current = None
            container = None
            coordinates, indices, components = [], [], []
            element.clear()

    for child in dict.fromkeys(follow):
        _read_part(zf, names, child, objects, seen)


def _slots(zf: zipfile.ZipFile, names: set[str]) -> dict[int, dict]:
    """Which palette slot paints which piece, per object.

    `model_settings.config` gives an object a default `extruder` and then one
    `<part>` per component, each able to override it. Parts are matched to
    components **by position**: the file states the correspondence nowhere else,
    and in every catalogue file the two lists are the same length and in the same
    order.
    """
    if MODEL_SETTINGS not in names:
        return {}
    try:
        root = ET.fromstring(zf.read(MODEL_SETTINGS))
    except (ET.ParseError, OSError):
        return {}

    slots: dict[int, dict] = {}
    for element in root.findall("object"):
        raw_id = element.get("id")
        if not raw_id or not raw_id.isdigit():
            continue
        slots[int(raw_id)] = {
            "default": _slot(element),
            "parts": [_slot(part) for part in element.findall("part")],
        }

    return slots


def _slot(element: ET.Element) -> int | None:
    for metadata in element.findall("metadata"):
        if metadata.get("key") == "extruder":
            raw = (metadata.get("value") or "").strip()
            if raw.isdigit():
                return int(raw)

    return None


def _flatten(zf, names, objects: dict, builds: list[dict], palette: list) -> list[_Mesh]:
    """Every mesh in the file, placed in its own object's frame and coloured."""
    slots = _slots(zf, names)
    meshes: list[_Mesh] = []

    for instance, item in enumerate(builds):
        top = objects.get(item["key"])
        if top is None:
            continue
        assignment = slots.get(item["object_id"], {})
        parts = assignment.get("parts") or []
        default = assignment.get("default")

        components = top.get("components") or []
        if components:
            for index, component in enumerate(components):
                # `or default` and not a length check: a `<part>` exists for
                # every component but routinely leaves `extruder` off, and the
                # object's own slot is the answer then. Reading the missing part
                # entry as "no colour" painted every multi-object model grey.
                slot = (parts[index] if index < len(parts) else None) or default
                _collect(objects, component["key"], component["matrix"], _colour(palette, slot), instance, meshes)
        else:
            _collect(objects, item["key"], IDENTITY, _colour(palette, default), instance, meshes)

    return meshes


def _collect(objects: dict, key, matrix: np.ndarray, colour, instance: int, meshes: list[_Mesh], depth: int = 0) -> None:
    """One component's meshes, with nested components folded in.

    The depth limit is not defensive tidiness: a component graph with a cycle in
    it parses fine and then recurses until the interpreter gives up, and a
    picture is not worth crashing a service over.
    """
    if depth > 8:
        return
    node = objects.get(key)
    if node is None:
        return

    if node.get("triangles") is not None:
        meshes.append(
            _Mesh(
                vertices=_apply(node["vertices"].astype(np.float64), matrix).astype(np.float32),
                triangles=node["triangles"],
                colour=colour,
                instance=instance,
            )
        )

    for child in node.get("components") or []:
        _collect(objects, child["key"], _compose(child["matrix"], matrix), colour, instance, meshes, depth + 1)


def _colour(palette: list, slot: int | None):
    if slot is None or slot < 1 or slot > len(palette):
        return DEFAULT_COLOUR

    return palette[slot - 1]


def _assemble(zf: zipfile.ZipFile, names: set[str]) -> dict[tuple[int, int], np.ndarray]:
    """Where `<assemble>` puts each instance, keyed the way build items are."""
    if MODEL_SETTINGS not in names:
        return {}
    try:
        root = ET.fromstring(zf.read(MODEL_SETTINGS))
    except (ET.ParseError, OSError):
        return {}

    block = root.find("assemble")
    if block is None:
        return {}

    placed: dict[tuple[int, int], np.ndarray] = {}
    for item in block.findall("assemble_item"):
        raw_object = item.get("object_id") or ""
        raw_instance = item.get("instance_id") or "0"
        if not raw_object.isdigit():
            continue
        placed[(int(raw_object), int(raw_instance) if raw_instance.isdigit() else 0)] = _matrix(
            item.get("transform")
        )

    return placed


def _pose(builds: list[dict], meshes: list[_Mesh], assemble: dict) -> list[np.ndarray]:
    """Where to put each instance: assembled if the file really says, else laid out.

    Studio writes an `<assemble>` block into every project whether or not anybody
    assembled anything, so its presence proves nothing — the same trap the size
    measurement in `OrcaSlicerEngine._assembly` had to work around. It is used
    only when it covers every instance and actually brings them together;
    otherwise the pieces are drawn where the plate has them.
    """
    layout = [item["matrix"] for item in builds]
    if len(builds) < 2:
        return layout

    try:
        candidate = [assemble[(item["object_id"], item["instance_id"])] for item in builds]
    except KeyError:
        return layout

    if len(_cluster(_boxes(meshes, candidate, "instance", len(builds)))) < 2:
        return layout

    return candidate


def _subject(meshes: list[_Mesh], placements: list[np.ndarray]) -> tuple[list[_Mesh], bool]:
    """The pieces that are the model, and whether they add up to an assembled one.

    A 3MF holds more than the toy. Alternative pieces, spare parts and
    accessories sit beside it — a second pig's head, four spare turtle shells,
    the retriever's coffee mug ten millimetres off its paws — and a project laid
    out across seven plates spreads them over half a metre of virtual bed. Framing
    on all of that is what turned the hydra into four specks on an empty field.

    So the picture is framed on the pieces **glued to each other**, exactly the
    cluster `OrcaSlicerEngine._assembly` measures for size. Same rule and same
    reason in both places, which is worth more than either choice on its own: the
    thing the customer is shown is the thing the shop quoted a size for.

    What it costs is a laid-out multi-plate project, where nothing touches
    anything and the cluster is a single piece — the biggest one, drawn alone.
    That is the honest limit of such a file: it says how its parts sit on a bed
    and never how they go together. `assembled` is False there, and a caller that
    cares can say so.
    """
    if len(meshes) < 2:
        return meshes, True

    cluster = _cluster(_boxes(meshes, placements, "piece", len(meshes)))

    return [meshes[i] for i in cluster], len(cluster) > 1


def _boxes(meshes: list[_Mesh], placements: list[np.ndarray], by: str, count: int):
    """Placed bounding boxes, grouped either per build instance or per piece.

    Taken from the meshes rather than from `--info`, which is the one thing this
    module has that the size measurement does not: real vertices, so a rotated
    piece gets its own box and not the box of its rotated box.
    """
    lo = np.full((count, 3), np.inf)
    hi = np.full((count, 3), -np.inf)
    for index, mesh in enumerate(meshes):
        slot = mesh.instance if by == "instance" else index
        low, high = _placed_bounds(mesh.vertices, placements[mesh.instance])
        lo[slot] = np.minimum(lo[slot], low)
        hi[slot] = np.maximum(hi[slot], high)

    return lo, hi


def _cluster(boxes) -> list[int]:
    """Indices of the largest group of pieces whose placed boxes meet.

    Pieces are glued by overlapping — that is what glue is — and laid out with
    millimetres between them, because a nozzle has to get past. Largest by the
    volume its boxes enclose rather than by how many pieces it holds, so a toy
    does not lose to a tray of small spares.
    """
    lo, hi = boxes
    count = len(lo)
    parent = list(range(count))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in itertools.combinations(range(count), 2):
        if np.isinf(lo[i]).any() or np.isinf(lo[j]).any():
            continue
        if np.all(lo[i] <= hi[j] + TOUCH_TOLERANCE_MM) and np.all(lo[j] <= hi[i] + TOUCH_TOLERANCE_MM):
            parent[root(i)] = root(j)

    groups: dict[int, list[int]] = {}
    for i in range(count):
        groups.setdefault(root(i), []).append(i)

    def bulk(members: list[int]) -> float:
        extent = hi[members].max(axis=0) - lo[members].min(axis=0)
        return float(np.prod(np.where(np.isfinite(extent), extent, 0.0)))

    return max(groups.values(), key=bulk, default=[])


def _camera() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Right, up and towards-the-camera axes of the three-quarter view.

    Models are Z-up — they are printed — so the world's up is the model's up and
    the view is built around it rather than around the screen.
    """
    azimuth = np.radians(AZIMUTH)
    elevation = np.radians(ELEVATION)

    towards = np.array(
        [
            np.cos(elevation) * np.cos(azimuth),
            np.cos(elevation) * np.sin(azimuth),
            np.sin(elevation),
        ]
    )
    right = np.cross(np.array([0.0, 0.0, 1.0]), towards)
    right /= np.linalg.norm(right)
    up = np.cross(towards, right)

    # float32 so that projecting a mesh does not silently promote it: an
    # eleven-million-triangle file is gigabytes either way, and twice that is
    # the difference between rendering and being killed.
    return right.astype(np.float32), up.astype(np.float32), towards.astype(np.float32)


def _draw(meshes: list[_Mesh], placements: list[np.ndarray], width: int, height: int) -> Image.Image:
    """Rasterise the placed meshes, orthographically, with one light.

    Orthographic on purpose: a perspective camera would have to be positioned,
    and every position is a decision about how much the near end of the model
    should flare, which is a taste question nobody asked this service to answer.

    The rasteriser scatters points rather than walking scanlines. At the sizes
    that matter here that is not an approximation worth apologising for — a
    catalogue model carries about a million triangles against under a million
    pixels, so a triangle is already smaller than a pixel — and it is the only
    shape of the problem numpy can do at full speed. Triangles bigger than a
    pixel get proportionally more samples, so a plain cube fills in too.
    """
    scale = max(1, SUPERSAMPLE)
    canvas_w, canvas_h = width * scale, height * scale

    vertices, triangles, colours = _assemble_arrays(meshes, placements)
    right, up, towards = _camera()

    normals = _normals(vertices, triangles)
    triangles, colours, normals = _cull(triangles, colours, normals, towards)

    screen = np.column_stack((vertices @ right, vertices @ up))
    depth = vertices @ towards

    span = screen.max(axis=0) - screen.min(axis=0)
    span[span <= 0] = 1.0
    # 0.94 leaves a hair of margin so the silhouette never touches the edge.
    pixels_per_mm = 0.94 * min(canvas_w / span[0], canvas_h / span[1])
    centre = (screen.max(axis=0) + screen.min(axis=0)) / 2.0
    screen = (screen - centre) * pixels_per_mm
    screen[:, 0] += canvas_w / 2.0
    # Screen y grows downwards, the view's up does not.
    screen[:, 1] = canvas_h / 2.0 - screen[:, 1]

    face = np.clip(colours * _shading(normals, right, up, towards)[:, None], 0.0, 1.0)

    buffer = _rasterise(screen, depth, triangles, canvas_w, canvas_h)

    image = np.empty((canvas_h * canvas_w, 3), dtype=np.float32)
    image[:] = BACKGROUND
    hit = buffer >= 0
    image[hit] = face[buffer[hit]]

    picture = Image.fromarray((image.reshape(canvas_h, canvas_w, 3) * 255).astype(np.uint8), "RGB")
    if scale > 1:
        picture = picture.resize((width, height), Image.LANCZOS)

    return picture


def _assemble_arrays(meshes: list[_Mesh], placements: list[np.ndarray]):
    """One vertex array, one triangle array, one colour per triangle."""
    vertices: list[np.ndarray] = []
    triangles: list[np.ndarray] = []
    colours: list[np.ndarray] = []
    offset = 0

    for mesh in meshes:
        # float32 throughout, and 32-bit indices with it. A tenth of a micron of
        # resolution over half a metre is far more than a picture needs, and the
        # arrays are the largest thing in the process: the 187 mm shepherd file
        # carries millions of triangles, and every doubled word is hundreds of
        # megabytes of it.
        placed = _apply(mesh.vertices, placements[mesh.instance]).astype(np.float32)
        vertices.append(placed)
        triangles.append(mesh.triangles.astype(np.int32) + offset)
        colours.append(np.tile(np.asarray(mesh.colour, dtype=np.float32), (len(mesh.triangles), 1)))
        offset += len(placed)

    return np.concatenate(vertices), np.concatenate(triangles), np.concatenate(colours)


def _normals(vertices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Unit face normal per triangle."""
    a = vertices[triangles[:, 0]]
    normals = np.cross(vertices[triangles[:, 1]] - a, vertices[triangles[:, 2]] - a)
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0] = 1.0

    return normals / lengths[:, None]


#: Below this share of triangles surviving, the cull is assumed to have been
#: wrong rather than the model to have been strange, and is abandoned.
CULL_FLOOR = 0.25


def _cull(triangles: np.ndarray, colours: np.ndarray, normals: np.ndarray, towards: np.ndarray):
    """Drop the triangles facing away from the camera.

    Not an optimisation, though it is one — it halves the work. Without it the
    far wall of a shell wins pixels wherever it lands within depth-quantisation
    distance of the near one, and a printed toy is full of places where the two
    are a fraction of a millimetre apart. That showed up as fine dark salt over
    the whole model: 8.3% of drawn pixels on the knitted corgi were the inside of
    the surface, shaded as if unlit.

    Abandoned when it would remove nearly everything, which is what an inverted
    or inconsistent winding looks like from here. A model drawn with its back
    faces is worse than one drawn with a bit of salt; a model drawn empty is
    worse than both.
    """
    keep = normals @ towards > 0
    if keep.mean() < CULL_FLOOR:
        return triangles, colours, normals

    return triangles[keep], colours[keep], normals[keep]


def _shading(normals: np.ndarray, right, up, towards) -> np.ndarray:
    """Flat per-face Lambert, lit from over the viewer's left shoulder.

    Two-sided — the absolute value, not the clipped one — because a triangle
    wound the wrong way round is a fact of life in files assembled out of parts
    from different sources, and shading it as if it faced into the model turns
    a smooth surface into noise. Back-face culling removes most of them first;
    this is what makes the rest harmless instead of visible.
    """
    light = 0.45 * -right + 0.35 * up + 0.82 * towards
    light /= np.linalg.norm(light)

    return 0.30 + 0.66 * np.abs(normals @ light)


#: Depth resolution of the z-buffer, in steps across the whole model. Two
#: million of them over a 100 mm toy is a twentieth of a micron — far finer than
#: anything the geometry means, which is the point: the depth test should decide
#: on depth, and a tie should be vanishingly rare rather than routinely broken by
#: whichever triangle happens to be numbered highest.
DEPTH_LEVELS = 2_000_000

#: Samples per square pixel of a triangle's screen area.
#:
#: Four rather than the two that a stratified sequence nominally needs, because
#: a triangle's samples are stratified within *that triangle* and neighbouring
#: triangles know nothing of each other. Doubling costs half a second on a
#: million-triangle model and settles the last of the seams between them; eight
#: changes nothing further.
SAMPLE_DENSITY = 4.0

#: Triangles are scattered in batches of this many samples. Bounds peak memory
#: at a few hundred megabytes on a model of any size; the depth buffer they
#: write into is shared, so batching changes nothing about the result.
SAMPLE_BATCH = 4_000_000


def _rasterise(screen: np.ndarray, depth: np.ndarray, triangles: np.ndarray, width: int, height: int) -> np.ndarray:
    """Index of the nearest triangle at each pixel, -1 where nothing was hit.

    Depth and triangle index travel through the z-buffer as **one integer** —
    quantised depth in the high bits, index in the low ones — so a single
    `np.maximum.at` resolves both which sample is in front and what was drawn
    there. Two passes over tens of millions of samples would otherwise be needed
    to answer the second question, and the second pass is the expensive one.
    """
    corners = [screen[triangles[:, i]] for i in range(3)]
    depths = [depth[triangles[:, i]] for i in range(3)]

    edge1 = corners[1] - corners[0]
    edge2 = corners[2] - corners[0]
    area = 0.5 * np.abs(edge1[:, 0] * edge2[:, 1] - edge1[:, 1] * edge2[:, 0])

    counts = np.ceil(area * SAMPLE_DENSITY).astype(np.int64)
    np.clip(counts, 1, 1 << 20, out=counts)
    total = int(counts.sum())
    if total > SAMPLE_BUDGET:
        # Thinning rather than truncating: dropping the tail would leave whole
        # triangles undrawn, while a lower density only coarsens the big flat
        # faces that caused the overflow in the first place.
        counts = np.maximum((counts * (SAMPLE_BUDGET / total)).astype(np.int64), 1)

    low, high = float(depth.min()), float(depth.max())
    quantum = DEPTH_LEVELS / (high - low) if high > low else 0.0

    buffer = np.full(width * height, -1, dtype=np.int64)
    offsets = np.concatenate(([0], np.cumsum(counts)))

    start = 0
    while start < len(counts):
        stop = _batch_end(counts, start)
        index = np.repeat(np.arange(start, stop, dtype=np.int64), counts[start:stop])

        weights = _barycentric(np.arange(offsets[start], offsets[stop]) - offsets[index], counts[index])

        x = sum(w * corners[i][index, 0] for i, w in enumerate(weights))
        y = sum(w * corners[i][index, 1] for i, w in enumerate(weights))
        z = sum(w * depths[i][index] for i, w in enumerate(weights))

        column = np.floor(x).astype(np.int64)
        row = np.floor(y).astype(np.int64)
        drawn = (column >= 0) & (column < width) & (row >= 0) & (row < height)

        # Widened for this one expression: the samples are a bounded batch, and
        # two million depth steps need more mantissa than float32 has left after
        # subtracting the near plane.
        key = ((z[drawn].astype(np.float64) - low) * quantum).astype(np.int64) << np.int64(32) | index[drawn]
        np.maximum.at(buffer, row[drawn] * width + column[drawn], key)

        start = stop

    hit = buffer >= 0
    buffer[hit] &= 0xFFFFFFFF

    return buffer


def _barycentric(rank: np.ndarray, counts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample positions inside a triangle, spread evenly rather than at random.

    This is the difference between a picture and a picture with dust on it.
    Independent random samples at a density of two per pixel miss `e**-2` of the
    pixels — thirteen in every hundred — and each miss shows whatever surface is
    behind: the corgi's nose came out sprayed with the head it plugs into. A
    stratified sequence covers at the density it promises, so the same two
    samples per pixel actually cover.

    The pair is a Hammersley point — the sample's rank spread along one axis, its
    bit-reversal along the other — mapped onto the triangle by the usual square
    root, which keeps the spread and makes it uniform by area.
    """
    along = (rank + 0.5) / counts
    across = _bit_reversed(rank)

    edge = np.sqrt(along)

    return 1.0 - edge, across * edge, (1.0 - across) * edge


def _bit_reversed(rank: np.ndarray) -> np.ndarray:
    """The van der Corput sequence in base two, as fractions in [0, 1)."""
    bits = rank.astype(np.uint32)
    bits = ((bits & np.uint32(0x55555555)) << np.uint32(1)) | ((bits >> np.uint32(1)) & np.uint32(0x55555555))
    bits = ((bits & np.uint32(0x33333333)) << np.uint32(2)) | ((bits >> np.uint32(2)) & np.uint32(0x33333333))
    bits = ((bits & np.uint32(0x0F0F0F0F)) << np.uint32(4)) | ((bits >> np.uint32(4)) & np.uint32(0x0F0F0F0F))
    bits = (bits << np.uint32(24)) | ((bits & np.uint32(0xFF00)) << np.uint32(8)) \
        | ((bits >> np.uint32(8)) & np.uint32(0xFF00)) | (bits >> np.uint32(24))

    return bits.astype(np.float64) * 2.3283064365386963e-10


def _batch_end(counts: np.ndarray, start: int) -> int:
    """First triangle past a batch of roughly SAMPLE_BATCH samples."""
    running = np.cumsum(counts[start:])
    within = np.searchsorted(running, SAMPLE_BATCH, side="left") + 1

    return min(start + max(int(within), 1), len(counts))


_EMBEDDED_STAGE = {AUTHOR_RENDER: "author_render", AUTHOR_THUMBNAIL: "author_thumbnail"}

_STAGES = {
    "author_render": _embedded(AUTHOR_RENDER),
    "author_thumbnail": _embedded(AUTHOR_THUMBNAIL),
    "geometry": _geometry,
}
