"""Colour painted onto a mesh, and the filaments it really asks for.

A Bambu or Orca project can colour a part two ways: the part's own `extruder`
in model_settings.config, and paint — a `paint_color` attribute on the mesh's
triangles in `3D/Objects/*.model`. The engine counts both. Everything this
service did with filament numbers read only the first, and that was wrong in
two ways a catalogue of laid-out assemblies showed:

* **Renumbering moved the base and left the paint.** A plate whose colour 7 is
  printed on head 1 had its parts' extruder rewritten 7 → 1 while every
  triangle still said 7. The engine saw two filaments where the author used
  one, built a prime tower for them, and refused the plate because its parts
  have different layer heights — which a one-colour plate is allowed to have.
* **A part painted all over still counts its base.** An author who paints a
  whole part in colour 5 leaves its base on 1; nothing of 1 is printed, but the
  engine loads it, and the plate becomes two-colour in the same way.

So the paint is read, renumbered with the base, and a part painted all over in
one colour is made a part *of* that colour: the base takes the paint's number
and the paint is dropped. Nothing printed changes — the base was not printed —
and the file says what the plate actually uses.

**The encoding** is TriangleSelector's: each triangle's string is a bit stream
written as hex nibbles in reverse order. A leaf is two bits of split count (0)
and two bits of state, or `11` and four more bits of state − 3; a split
triangle is two bits of split count, two of special side, and its children.
Every token is nibble-aligned, so the stream is read nibble by nibble. State 0
is "unpainted" (the base shows); state k ≥ 1 is filament k.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field

MODEL = "3D/3dmodel.model"
MODEL_SETTINGS = "Metadata/model_settings.config"

PAINT = re.compile(rb'\spaint_color="([0-9A-Fa-f]*)"')
TRIANGLE = re.compile(rb"<triangle\s")
MESH_OBJECT = re.compile(rb'<object\s+id="(\d+)"[^>]*>(.*?)</object>', re.S)
COMPONENT = re.compile(r'<component\s[^>]*?/>')
ATTR = re.compile(r'([\w:]+)="([^"]*)"')
TOP_OBJECT = re.compile(r'<object\s+id="(\d+)"[^>]*>(.*?)</object>', re.S)


class PaintError(ValueError):
    pass


def _nibbles(code: str) -> list[int]:
    return [int(c, 16) for c in reversed(code)]


def _decode(nibbles: list[int], at: int, states: list[int]) -> int:
    """Walk one triangle's tree from `at`, appending each leaf's state."""
    if at >= len(nibbles):
        raise PaintError("paint string ends inside a triangle")
    n = nibbles[at]
    split = n & 0b11
    if split:
        at += 1
        for _ in range(split + 1):
            at = _decode(nibbles, at, states)
        return at
    state = n >> 2
    if state == 3:
        if at + 1 >= len(nibbles):
            raise PaintError("paint string ends inside a state")
        state = nibbles[at + 1] + 3
        at += 1
    states.append(state)
    return at + 1


def leaf_states(code: str) -> list[int]:
    """The state of every leaf of one triangle's paint."""
    states: list[int] = []
    nibbles = _nibbles(code)
    if nibbles:
        _decode(nibbles, 0, states)
    return states


def _encode_state(state: int) -> list[int]:
    if state < 3:
        return [state << 2]
    if state - 3 > 0xF:
        raise PaintError(f"state {state} does not fit the encoding")
    return [0b1100, state - 3]


def _recode(nibbles: list[int], at: int, mapping: dict[int, int], out: list[int]) -> int:
    n = nibbles[at]
    split = n & 0b11
    if split:
        out.append(n)
        at += 1
        for _ in range(split + 1):
            at = _recode(nibbles, at, mapping, out)
        return at
    state = n >> 2
    width = 1
    if state == 3:
        state = nibbles[at + 1] + 3
        width = 2
    out.extend(_encode_state(mapping.get(state, state)))
    return at + width


def renumber(code: str, mapping: dict[int, int]) -> str:
    """The same paint with every filament state passed through `mapping`."""
    nibbles = _nibbles(code)
    if not nibbles:
        return code
    out: list[int] = []
    _recode(nibbles, 0, mapping, out)
    return "".join(f"{n:X}" for n in reversed(out))


@dataclass
class MeshPaint:
    """What one mesh's paint says: the filaments painted on it, and whether any
    of the mesh shows its base."""

    triangles: int = 0
    states: set[int] = field(default_factory=set)
    base_shows: bool = True

    @property
    def uniform(self) -> int | None:
        """The one filament a mesh is painted in all over, if it is."""
        if not self.base_shows and len(self.states) == 1:
            return next(iter(self.states))
        return None


def _mesh_paint(body: bytes, cache: dict[bytes, tuple[int, ...]]) -> MeshPaint:
    result = MeshPaint(triangles=len(TRIANGLE.findall(body)))
    painted = 0
    unpainted_leaf = False
    for match in PAINT.finditer(body):
        code = match.group(1)
        painted += 1
        states = cache.get(code)
        if states is None:
            states = tuple(leaf_states(code.decode()))
            cache[code] = states
        result.states.update(s for s in states if s)
        unpainted_leaf = unpainted_leaf or 0 in states
    # A triangle with no paint attribute at all shows the base as surely as a
    # leaf painted 0 does.
    result.base_shows = painted < result.triangles or unpainted_leaf
    return result


@dataclass
class ObjectPaint:
    """Paint on one object of model_settings.config, over all its meshes."""

    meshes: list[tuple[str, str]] = field(default_factory=list)  # (archive path, mesh object id)
    states: set[int] = field(default_factory=set)
    base_shows: bool = True

    @property
    def uniform(self) -> int | None:
        if not self.base_shows and len(self.states) == 1:
            return next(iter(self.states))
        return None


def _components(model: str) -> dict[str, list[tuple[str, str]]]:
    """Each top-level object's meshes as (archive path, object id in it)."""
    found: dict[str, list[tuple[str, str]]] = {}
    for match in TOP_OBJECT.finditer(model):
        oid, body = match.group(1), match.group(2)
        meshes = []
        for comp in COMPONENT.findall(body):
            attrs = dict(ATTR.findall(comp))
            path = (attrs.get("p:path") or "").lstrip("/") or MODEL
            if attrs.get("objectid"):
                meshes.append((path, attrs["objectid"]))
        if "<mesh" in body:
            meshes.append((MODEL, oid))
        found[oid] = meshes
    return found


def read(source: str) -> dict[str, ObjectPaint]:
    """Paint per object id of model_settings.config. Objects without paint are
    absent, so a file nobody painted costs one pass over its mesh files and
    returns nothing."""
    with zipfile.ZipFile(source) as zf:
        names = set(zf.namelist())
        if MODEL not in names:
            return {}
        components = _components(zf.read(MODEL).decode("utf-8", errors="ignore"))
        by_file: dict[str, set[str]] = {}
        for meshes in components.values():
            for path, mesh_id in meshes:
                by_file.setdefault(path, set()).add(mesh_id)

        cache: dict[bytes, tuple[int, ...]] = {}
        meshes_paint: dict[tuple[str, str], MeshPaint] = {}
        for path, ids in by_file.items():
            if path not in names:
                continue
            data = zf.read(path)
            if b"paint_color" not in data:
                continue
            for match in MESH_OBJECT.finditer(data):
                mesh_id = match.group(1).decode()
                if mesh_id in ids:
                    meshes_paint[(path, mesh_id)] = _mesh_paint(match.group(2), cache)

    objects: dict[str, ObjectPaint] = {}
    for oid, meshes in components.items():
        painted = [meshes_paint[m] for m in meshes if m in meshes_paint and meshes_paint[m].states]
        if not painted:
            continue
        obj = ObjectPaint(meshes=list(meshes))
        for mesh in painted:
            obj.states |= mesh.states
        # The base shows wherever a mesh is unpainted or partly painted.
        obj.base_shows = len(painted) < len(meshes) or any(m.base_shows for m in painted)
        objects[oid] = obj
    return objects


def rewrite_mesh_file(data: bytes, per_mesh: dict[str, dict | None]) -> bytes:
    """Apply paint changes to one mesh file.

    `per_mesh` maps a mesh object id to `{old: new}` (renumber its paint) or to
    None (drop its paint — the part has become the colour it was painted).
    Meshes not named are left byte for byte.
    """
    if not per_mesh:
        return data

    def one_object(match: re.Match) -> bytes:
        mesh_id = match.group(1).decode()
        if mesh_id not in per_mesh:
            return match.group(0)
        mapping = per_mesh[mesh_id]
        if mapping is None:
            return PAINT.sub(b"", match.group(0))
        cache: dict[bytes, bytes] = {}

        def one_triangle(m: re.Match) -> bytes:
            code = m.group(1)
            new = cache.get(code)
            if new is None:
                new = renumber(code.decode(), mapping).encode()
                cache[code] = new
            return b' paint_color="' + new + b'"'

        return PAINT.sub(one_triangle, match.group(0))

    return MESH_OBJECT.sub(one_object, data)
