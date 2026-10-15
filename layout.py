"""What stands on each plate: how many models, of which kind, and in how many copies.

`--info` measures objects and `<assemble>` says how pieces glue, but neither says
what comes off a plate as one thing in the operator's hand. That needs the
meshes, in the place each object is printed: an eye sunk into a head is one
model with the head; a print-in-place joint — a pin standing in its socket with
a fifth of a millimetre of air around it — is one *moving* model; two dogs a
centimetre apart are two.

So every object on a plate is placed where its build item puts it on the bed,
split into its shells (the connected pieces of its mesh), and the shells are
related pairwise:

* **touching** — surfaces meet or pass through each other: one model. This is
  what an AMS model's colour regions do, and what glue does;
* **jointed** — the shells interlock (their boxes overlap) yet keep a clearance
  of FLEXI_MIN_MM to FLEXI_MAX_MM everywhere, and are of a size: one model, and
  a flexi one. A small shell held the same way in a big one is a snap-fit
  insert — an eye in its socket — and is one model without a joint;
* anything else — separate models.

The rule and both limits are the owner's (2026-10-07): flexi is a gap of
0.1–1 mm, AMS is a gap of zero or less. Measured on the reference files before
any of this was written: the flexi bear's two joints keep 0.170 and 0.178 mm,
the corgi's pieces touch.

**Distances are vertex to triangle, in both directions.** Vertex to vertex
overstates a gap wherever a mesh is coarse — a CAD part's flat face may carry
no vertex for centimetres — and point-to-triangle in both directions finds the
gap between any face and any corner facing it, which is the shape a clearance
has. An edge crossing an edge without a vertex near either is the one case it
misses, and it errs towards "apart", never towards a joint that is not there.

**Interpenetration is tested separately**, because a coarse part pushed into
another has its intersection curve between vertices and can read as a gap. A
handful of each shell's vertices nearest the other are tested for being inside
it by ray parity; any inside means the two touch.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field

import numpy as np

import render

#: Below this, two surfaces are touching: one model. Owner's rule — a flexi
#: clearance starts at 0.1 mm.
FLEXI_MIN_MM = 0.1
#: Above this, interlocking boxes are two models that happen to sit close.
FLEXI_MAX_MM = 1.0
#: Shells this small are slicer debris rather than pieces anyone removes from
#: a plate: a degenerate sliver, a stray triangle. They follow whatever they
#: touch and are never a model of their own.
MIN_SHELL_TRIANGLES = 12
#: How many of a shell's vertices nearest the other are ray-tested for being
#: inside it.
INSIDE_SAMPLES = 24
#: A shell held in another with a clearance is a joint only when the two are of
#: a size: a flexi model's segments are comparable (0.57–0.83 of each other on
#: the reference bear, seal and cat), while a snap-fit eye sitting in its socket
#: is a small thing inside a big one (0.04–0.19 on the badger and the hydra).
#: Below this ratio of longest edges the pair is an insert — one model, no joint.
JOINT_SIZE_RATIO = 0.35
#: Grid cell for the proximity search. Equal to FLEXI_MAX_MM, so a pair closer
#: than that always shares a cell or a neighbour.
CELL_MM = FLEXI_MAX_MM


@dataclass
class _Shell:
    object_id: int
    slot: int | None
    vertices: np.ndarray  # placed on the bed, float64
    triangles: np.ndarray  # into vertices
    lo: np.ndarray
    hi: np.ndarray
    volume: float


@dataclass
class PlateModel:
    """One thing that comes off a plate in one piece."""

    kind: str  # "part" | "ams" | "flexi"
    object_ids: list[int]
    shells: int
    # A shape key for telling copies apart: shell count, triangle count and
    # material. Not published.
    signature: tuple = field(default=(), repr=False)
    # The model's box on the bed, for telling whether two models could be
    # printed apart without one being inside the other. Not published.
    lo: tuple = field(default=(), repr=False)
    hi: tuple = field(default=(), repr=False)


@dataclass
class PlateModels:
    index: int
    models: list[PlateModel]

    @property
    def copies(self) -> str:
        """"multi" when the plate holds more than one model and all of them are
        the same shape; "one" otherwise — a single model, or a set of different
        ones."""
        if len(self.models) > 1 and len({m.signature for m in self.models}) == 1:
            return "multi"
        return "one"


def plate_models(path: str, plates: dict[int, list[int]]) -> dict[int, PlateModels]:
    """Models per plate, for the plates given as {index: [object ids]}.

    Returns nothing for a file whose meshes cannot be read; the caller then
    simply has no answer, which is the honest one.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if render.ROOT_MODEL not in names:
                return {}
            objects, builds = render._read_model(zf, names)
            slots = render._slots(zf, names)
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        return {}

    result: dict[int, PlateModels] = {}
    for index, object_ids in plates.items():
        wanted = set(object_ids)
        shells: list[_Shell] = []
        for item in builds:
            if item["object_id"] not in wanted:
                continue
            for vertices, triangles, slot in _meshes(objects, item, slots.get(item["object_id"], {})):
                shells.extend(_split(item["object_id"], slot, vertices, triangles))
        result[index] = PlateModels(index=index, models=_group(shells))
    return result


def _meshes(objects: dict, item: dict, assignment: dict):
    """Every mesh of one build item, placed on the bed, with its palette slot."""
    top = objects.get(item["key"])
    if top is None:
        return
    parts = assignment.get("parts") or []
    default = assignment.get("default")
    components = top.get("components") or []
    if components:
        for n, component in enumerate(components):
            slot = (parts[n] if n < len(parts) else None) or default
            yield from _walk(objects, component["key"], render._compose(component["matrix"], item["matrix"]), slot)
    else:
        yield from _walk(objects, item["key"], item["matrix"], default)


def _walk(objects: dict, key, matrix: np.ndarray, slot, depth: int = 0):
    if depth > 8:
        return
    node = objects.get(key)
    if node is None:
        return
    if node.get("triangles") is not None:
        yield render._apply(node["vertices"].astype(np.float64), matrix), node["triangles"], slot
    for child in node.get("components") or []:
        yield from _walk(objects, child["key"], render._compose(child["matrix"], matrix), slot, depth + 1)


def _split(object_id: int, slot, vertices: np.ndarray, triangles: np.ndarray) -> list[_Shell]:
    """A mesh's connected pieces.

    Vertices are welded by position first: a mesh written triangle by triangle
    repeats every corner, and connectivity through indices alone would make
    each triangle a shell of its own.
    """
    if len(triangles) == 0:
        return []
    keys = np.round(vertices / 1e-4).astype(np.int64)
    _, weld = np.unique(keys, axis=0, return_inverse=True)
    weld = weld.reshape(-1)
    tri = weld[triangles]

    labels = _components(int(weld.max()) + 1, tri)
    tri_label = labels[tri[:, 0]]
    order = np.argsort(tri_label, kind="stable")
    bounds = np.flatnonzero(np.diff(tri_label[order])) + 1

    shells = []
    for group in np.split(order, bounds):
        t = triangles[group]
        used = np.unique(t)
        remap = np.full(len(vertices), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        v = vertices[used]
        shells.append(_Shell(
            object_id=object_id,
            slot=slot,
            vertices=v,
            triangles=remap[t],
            lo=v.min(axis=0),
            hi=v.max(axis=0),
            volume=abs(_signed_volume(v, remap[t])),
        ))
    return shells


def _components(n: int, triangles: np.ndarray) -> np.ndarray:
    """Connected components of a vertex graph, by min-label propagation with
    pointer jumping — numpy only, no Python loop over vertices."""
    labels = np.arange(n)
    edges = np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]]])
    a, b = edges[:, 0], edges[:, 1]
    while True:
        m = np.minimum(labels[a], labels[b])
        before = labels.copy()
        np.minimum.at(labels, a, m)
        np.minimum.at(labels, b, m)
        for _ in range(4):
            labels = labels[labels]
        if np.array_equal(labels, before):
            return labels


def _signed_volume(v: np.ndarray, t: np.ndarray) -> float:
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def _group(shells: list[_Shell]) -> list[PlateModel]:
    """Union shells into models and name each model's kind."""
    big = [s for s in shells if len(s.triangles) >= MIN_SHELL_TRIANGLES]
    small = [s for s in shells if len(s.triangles) < MIN_SHELL_TRIANGLES]
    shells = big + small

    parent = list(range(len(shells)))
    jointed = [False] * len(shells)

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in _candidate_pairs(shells):
        relation = _relation(shells[i], shells[j])
        if relation is None:
            continue
        ri, rj = root(i), root(j)
        if ri != rj:
            parent[ri] = rj
        if relation == "joint":
            jointed[root(j)] = True

    groups: dict[int, list[int]] = {}
    for i in range(len(big)):
        groups.setdefault(root(i), []).append(i)
    for i in range(len(big), len(shells)):
        r = root(i)
        if r in groups:
            groups[r].append(i)

    flexi_roots = {root(i) for i in range(len(shells)) if jointed[i]}
    models = []
    for r, members in groups.items():
        slots = {shells[i].slot for i in members if shells[i].slot is not None}
        kind = "flexi" if r in flexi_roots else ("ams" if len(slots) > 1 else "part")
        signature = (
            len(members),
            sum(len(shells[i].triangles) for i in members),
            round(sum(shells[i].volume for i in members), 0),
        )
        models.append(PlateModel(
            kind=kind,
            object_ids=sorted({shells[i].object_id for i in members}),
            shells=len(members),
            signature=signature,
            lo=tuple(np.min([shells[i].lo for i in members], axis=0).tolist()),
            hi=tuple(np.max([shells[i].hi for i in members], axis=0).tolist()),
        ))
    models.sort(key=lambda m: (m.object_ids, -m.shells))
    return models


def _candidate_pairs(shells: list[_Shell]):
    """Shell pairs whose boxes come within FLEXI_MAX_MM of each other."""
    if len(shells) < 2:
        return
    lo = np.array([s.lo for s in shells]) - FLEXI_MAX_MM
    hi = np.array([s.hi for s in shells]) + FLEXI_MAX_MM
    for i in range(len(shells)):
        near = np.all((lo[i + 1:] <= hi[i]) & (lo[i] <= hi[i + 1:]), axis=1)
        for j in np.flatnonzero(near) + i + 1:
            yield i, int(j)


def _relation(a: _Shell, b: _Shell) -> str | None:
    """"touch", "joint" or None for two shells."""
    gap = _gap(a, b)
    if gap >= FLEXI_MIN_MM:
        gap = min(gap, _gap(b, a))
    if gap < FLEXI_MIN_MM:
        return "touch"
    if gap > FLEXI_MAX_MM:
        return None
    if _inside_either(a, b):
        return "touch"
    if _boxes_overlap(a, b):
        return "joint" if _comparable(a, b) else "touch"
    return None


def _comparable(a: _Shell, b: _Shell) -> bool:
    """Whether two shells are of a size — segments, not an insert in a socket."""
    small, big = sorted(((a.hi - a.lo).max(), (b.hi - b.lo).max()))
    return big > 0 and small / big >= JOINT_SIZE_RATIO


def _boxes_overlap(a: _Shell, b: _Shell) -> bool:
    """Interlocking: the boxes overlap with real volume, not merely meet."""
    overlap = np.minimum(a.hi, b.hi) - np.maximum(a.lo, b.lo)
    return bool(np.all(overlap > FLEXI_MAX_MM))


_BASE = np.int64(1 << 20)


def _key(cells: np.ndarray) -> np.ndarray:
    """One int64 per (n, 3) cell index — 20 bits an axis, a kilometre of bed."""
    x = cells + (1 << 19)
    return (x[:, 0] * _BASE + x[:, 1]) * _BASE + x[:, 2]


_OFFSETS = np.array(np.meshgrid([-1, 0, 1], [-1, 0, 1], [-1, 0, 1])).T.reshape(-1, 3)


def _dilated_cells(shell: _Shell) -> np.ndarray:
    """Sorted keys of every cell within one cell of the shell's vertices. Cached."""
    cached = getattr(shell, "_dilated", None)
    if cached is None:
        cells = np.unique(_key(np.floor(shell.vertices / CELL_MM).astype(np.int64)))
        x = cells
        z = x % _BASE
        y = (x // _BASE) % _BASE
        xx = x // (_BASE * _BASE)
        grid = np.stack([xx, y, z], axis=1)
        cached = np.unique(_key((grid[:, None, :] + _OFFSETS[None, :, :]).reshape(-1, 3) - (1 << 19)))
        shell._dilated = cached
    return cached


def _near_vertices(a: _Shell, b: _Shell) -> np.ndarray:
    """Indices of a's vertices within roughly FLEXI_MAX_MM of b's vertices."""
    lo = b.lo - FLEXI_MAX_MM
    hi = b.hi + FLEXI_MAX_MM
    inside = np.flatnonzero(np.all((a.vertices >= lo) & (a.vertices <= hi), axis=1))
    if len(inside) == 0:
        return inside
    keys = _key(np.floor(a.vertices[inside] / CELL_MM).astype(np.int64))
    table = _dilated_cells(b)
    pos = np.clip(np.searchsorted(table, keys), 0, len(table) - 1)
    return inside[table[pos] == keys]


#: A triangle whose expanded box covers more cells than this is checked against
#: every point instead of being registered cell by cell.
MAX_CELLS_PER_TRIANGLE = 4096


def _triangle_index(shell: _Shell):
    """b's triangles registered in every cell their box, grown by FLEXI_MAX_MM,
    covers — so a point within that distance of a triangle finds it in its own
    cell. Cached per shell."""
    cached = getattr(shell, "_tindex", None)
    if cached is not None:
        return cached
    tri = shell.vertices[shell.triangles]
    lo = np.floor((tri.min(axis=1) - FLEXI_MAX_MM) / CELL_MM).astype(np.int64)
    hi = np.floor((tri.max(axis=1) + FLEXI_MAX_MM) / CELL_MM).astype(np.int64)
    extent = hi - lo + 1
    counts = extent.prod(axis=1)
    huge = np.flatnonzero(counts > MAX_CELLS_PER_TRIANGLE)
    keep = np.flatnonzero(counts <= MAX_CELLS_PER_TRIANGLE)

    c = counts[keep]
    owner = np.repeat(keep, c)
    local = np.arange(len(owner)) - np.repeat(np.cumsum(c) - c, c)
    ext = extent[owner]
    dz = local % ext[:, 2]
    dy = (local // ext[:, 2]) % ext[:, 1]
    dx = local // (ext[:, 2] * ext[:, 1])
    cells = lo[owner] + np.stack([dx, dy, dz], axis=1)
    keys = _key(cells)
    order = np.argsort(keys, kind="stable")
    cached = (tri, keys[order], owner[order], huge)
    shell._tindex = cached
    return cached


def _gap(a: _Shell, b: _Shell) -> float:
    """Smallest distance from a's vertices to b's triangles, or inf when none
    comes within FLEXI_MAX_MM — beyond that the exact figure answers nothing."""
    near = _near_vertices(a, b)
    if len(near) == 0:
        return np.inf
    points = a.vertices[near]
    tri, keys, owners, huge = _triangle_index(b)

    best = np.inf
    if len(huge):
        best = min(best, float(_min_point_triangle(points, tri[huge]).min()))

    q = _key(np.floor(points / CELL_MM).astype(np.int64))
    start = np.searchsorted(keys, q, side="left")
    stop = np.searchsorted(keys, q, side="right")
    counts = stop - start
    total = int(counts.sum())
    if total:
        pi = np.repeat(np.arange(len(points)), counts)
        first = np.repeat(start, counts)
        offset = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
        ti = owners[first + offset]
        for s in range(0, total, 2_000_000):
            d = _point_triangle(points[pi[s:s + 2_000_000]], tri[ti[s:s + 2_000_000]])
            best = min(best, float(d.min()))
            if best < FLEXI_MIN_MM:
                break
    return best if best <= FLEXI_MAX_MM else np.inf


def _min_point_triangle(points: np.ndarray, tris: np.ndarray) -> np.ndarray:
    best = np.full(len(points), np.inf)
    step = max(1, 2_000_000 // max(len(tris), 1))
    for s in range(0, len(points), step):
        p = points[s:s + step]
        d = _point_triangle(
            np.repeat(p, len(tris), axis=0),
            np.tile(tris, (len(p), 1, 1)),
        ).reshape(len(p), len(tris))
        best[s:s + step] = d.min(axis=1)
    return best


def _point_triangle(p: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Distance from each point to the paired triangle (Ericson, Real-Time
    Collision Detection, 5.1.5), vectorised."""
    a, b, c = t[:, 0], t[:, 1], t[:, 2]
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2

    with np.errstate(divide="ignore", invalid="ignore"):
        denom = va + vb + vc
        v = np.where(denom != 0, vb / denom, 0.0)
        w = np.where(denom != 0, vc / denom, 0.0)
        closest = a + ab * v[:, None] + ac * w[:, None]

        t_ab = np.where(d1 - d3 != 0, d1 / (d1 - d3), 0.0)
        on_ab = a + ab * t_ab[:, None]
        t_ac = np.where(d2 - d6 != 0, d2 / (d2 - d6), 0.0)
        on_ac = a + ac * t_ac[:, None]
        t_bc = np.where((d4 - d3) + (d5 - d6) != 0, (d4 - d3) / ((d4 - d3) + (d5 - d6)), 0.0)
        on_bc = b + (c - b) * t_bc[:, None]

    region_a = (d1 <= 0) & (d2 <= 0)
    region_b = (d3 >= 0) & (d4 <= d3)
    region_c = (d6 >= 0) & (d5 <= d6)
    region_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    region_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    region_bc = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)

    closest = np.where(region_bc[:, None], on_bc, closest)
    closest = np.where(region_ac[:, None], on_ac, closest)
    closest = np.where(region_ab[:, None], on_ab, closest)
    closest = np.where(region_c[:, None], c, closest)
    closest = np.where(region_b[:, None], b, closest)
    closest = np.where(region_a[:, None], a, closest)
    return np.linalg.norm(p - closest, axis=1)


def _inside_either(a: _Shell, b: _Shell) -> bool:
    return _any_inside(a, b) or _any_inside(b, a)


def _any_inside(a: _Shell, b: _Shell) -> bool:
    """Whether any of a's vertices nearest b lies inside b (ray parity along +z)."""
    near = _near_vertices(a, b)
    if len(near) == 0:
        return False
    points = a.vertices[near]
    if len(points) > INSIDE_SAMPLES:
        points = points[np.linspace(0, len(points) - 1, INSIDE_SAMPLES).astype(int)]
    tri = b.vertices[b.triangles]
    for p in points:
        # Triangles whose xy box holds the point and that lie above it.
        x0 = tri[:, :, 0].min(axis=1) <= p[0]
        x1 = tri[:, :, 0].max(axis=1) >= p[0]
        y0 = tri[:, :, 1].min(axis=1) <= p[1]
        y1 = tri[:, :, 1].max(axis=1) >= p[1]
        cand = tri[x0 & x1 & y0 & y1]
        if len(cand) == 0:
            continue
        crossings = _ray_crossings(p, cand)
        if crossings % 2 == 1:
            return True
    return False


def _ray_crossings(p: np.ndarray, tris: np.ndarray) -> int:
    """Möller–Trumbore against a +z ray from p, counting hits above it."""
    direction = np.array([0.0, 0.0, 1.0])
    # A hair off the axes, so a ray down a shared edge is not counted twice.
    origin = p + np.array([1.1e-6, 1.3e-6, 0.0])
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    h = np.cross(direction, e2)
    det = np.einsum("ij,ij->i", e1, h)
    ok = np.abs(det) > 1e-12
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    s = origin - tris[:, 0]
    u = inv * np.einsum("ij,ij->i", s, h)
    q = np.cross(s, e1)
    v = inv * (q @ direction)
    t = inv * np.einsum("ij,ij->i", e2, q)
    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0)
    return int(hit.sum())
