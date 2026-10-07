"""The supports a plate prints, one by one, out of its G-code.

A support is what the operator snaps off by hand, and what that costs depends on
how many there are and how big each one is — a few small props are a minute, a
tree under a dog's chin is several. The slice already knows both and says
neither: slice_info.config reports a plate's material as one number per
filament. The G-code does say it, in the `;TYPE:` sections the engine writes
around every extrusion.

So the support extrusions are collected into cells — CELL_XY_MM square in plan,
one layer tall — and cells that touch, in plan or in the layer above or below,
are one support — "above or below" meaning the next height at which support is
printed, within MAX_LEVEL_GAP_MM. A tree's branches meet its trunk on the way down, so the whole
tree comes out as one; two trees a few millimetres apart come out as two. Each
support's weight is its extruded filament length times the filament's
cross-section and density, read from the G-code's own header.

Interface layers (`;TYPE:Support interface`) belong to the support they cap and
are counted with it.
"""
from __future__ import annotations

import math
import re

SUPPORT_FEATURES = (";TYPE:Support", ";TYPE:Support interface", ";TYPE:Support transition")
#: Cell size in plan. Two supports closer than this merge into one — a tree's
#: twigs are millimetres apart and are one support to the hand that removes it.
CELL_XY_MM = 2.0
#: Below this a "support" is a stray extrusion between two cells, not a thing
#: anyone snaps off.
MIN_SUPPORT_G = 0.02
#: Two support levels this close in height are neighbours. Not "the next layer":
#: a plate of objects with different layer heights interleaves its Zs — the
#: turtle's run 0.28, 0.32, 0.36, 0.44 — and a support printed on every other
#: one of them, counted by layer number, fell apart into dozens of crumbs.
MAX_LEVEL_GAP_MM = 1.0

_MOVE = re.compile(r"^G[01] ")
_AXIS = re.compile(r"\b([XYE])(-?\d*\.?\d+)")
_TOOL = re.compile(r"^T(\d+)\s*$")


def plate_supports(gcode_path: str | None) -> list[float] | None:
    """Grams of every support on the plate, largest first.

    An empty list is a plate printed without supports. None means the G-code
    could not be read, which is not the same answer.
    """
    if not gcode_path:
        return None
    try:
        handle = open(gcode_path, encoding="utf-8", errors="ignore")
    except OSError:
        return None

    density: list[float] = []
    diameter: list[float] = []
    relative = True
    tool = 0
    in_support = False
    x = y = None
    z = 0.0
    cells: dict[tuple[int, int, int], float] = {}

    with handle:
        for line in handle:
            if line.startswith(";"):
                if line.startswith(";TYPE:"):
                    in_support = line.rstrip() in SUPPORT_FEATURES
                elif line.startswith(";Z:"):
                    try:
                        z = float(line[3:].strip())
                    except ValueError:
                        pass
                elif line.startswith("; filament_density:"):
                    density = _floats(line)
                elif line.startswith("; filament_diameter:"):
                    diameter = _floats(line)
                continue
            if line.startswith("M83"):
                relative = True
                continue
            if line.startswith("M82"):
                relative = False
                continue
            match = _TOOL.match(line)
            if match:
                tool = int(match.group(1))
                continue
            if not _MOVE.match(line):
                continue

            nx, ny, e = x, y, None
            for axis, value in _AXIS.findall(line):
                if axis == "X":
                    nx = float(value)
                elif axis == "Y":
                    ny = float(value)
                else:
                    e = float(value)
            if in_support and relative and e and e > 0 and x is not None and nx is not None:
                _deposit(cells, round(z * 1000), x, y, nx, ny, e * _grams_per_mm(tool, density, diameter))
            x, y = nx, ny

    if not relative:
        # Absolute extrusion would need the running total, and no profile this
        # service slices with writes it; refuse rather than guess.
        return None
    return _components(cells)


def _floats(line: str) -> list[float]:
    values = []
    for raw in line.split(":", 1)[1].split(","):
        try:
            values.append(float(raw))
        except ValueError:
            values.append(math.nan)
    return values


def _grams_per_mm(tool: int, density: list[float], diameter: list[float]) -> float:
    rho = density[tool] if tool < len(density) and not math.isnan(density[tool]) else 1.24
    d = diameter[tool] if tool < len(diameter) and not math.isnan(diameter[tool]) else 1.75
    return math.pi * (d / 2) ** 2 * rho / 1000.0


def _deposit(cells, layer, x0, y0, x1, y1, grams) -> None:
    """Spread one segment's grams over the cells it crosses."""
    length = math.hypot(x1 - x0, y1 - y0)
    steps = max(1, int(length / CELL_XY_MM) + 1)
    share = grams / steps
    for n in range(steps):
        t = (n + 0.5) / steps
        key = (layer, int(math.floor((x0 + (x1 - x0) * t) / CELL_XY_MM)),
               int(math.floor((y0 + (y1 - y0) * t) / CELL_XY_MM)))
        cells[key] = cells.get(key, 0.0) + share


def _components(cells: dict[tuple[int, int, int], float]) -> list[float]:
    """Union touching cells. Cells are keyed by height in micrometres; the
    support's own levels are renumbered in order, and two consecutive levels
    are neighbours when they are at most MAX_LEVEL_GAP_MM apart."""
    heights = sorted({h for h, _, _ in cells})
    level = {h: n for n, h in enumerate(heights)}
    joined = {
        n for n in range(len(heights) - 1)
        if heights[n + 1] - heights[n] <= MAX_LEVEL_GAP_MM * 1000
    }
    cells = {(level[h], cx, cy): g for (h, cx, cy), g in cells.items()}
    parent = {key: key for key in cells}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for (layer, cx, cy) in cells:
        for dl in (0, 1):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dl == 0 and (dx, dy) <= (0, 0):
                        continue
                    if dl == 1 and layer not in joined:
                        continue
                    other = (layer + dl, cx + dx, cy + dy)
                    if other in parent:
                        a, b = root((layer, cx, cy)), root(other)
                        if a != b:
                            parent[a] = b

    totals: dict[tuple[int, int, int], float] = {}
    for key, grams in cells.items():
        r = root(key)
        totals[r] = totals.get(r, 0.0) + grams
    return sorted((round(g, 2) for g in totals.values() if g >= MIN_SUPPORT_G), reverse=True)
