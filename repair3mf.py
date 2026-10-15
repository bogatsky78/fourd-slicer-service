"""Repair a Bambu Studio project 3MF so an OrcaSlicer-family CLI will load it.

Both fixes below were derived empirically against the real catalogue files
(CorgiAMS.3mf, Knitted_Puppy_Keychain.3mf, Esun_Filament_Spool_octagon.3mf) —
without them the slicer aborts or crashes:

1. project_settings.config carries five values outside the schema ranges the
   slicer enforces, which abort the load with
   `wall_filament: 0 not in range [1,2147483647]`.
2. filament_settings_*.config uses the literal string "nil" as an array
   placeholder and inherits from a Bambu preset that does not exist outside
   Bambu Studio, which fails preset extraction with
   `Deserializing nil into a non-nullable object`.

3. A single-filament project carries a flush matrix of one cell, and on a
   machine profile with several nozzles the slicer dies with SIGSEGV inside
   the slice, without a word on either stream. A customer's three eggs from
   Snapmaker Orca did exactly that on the U1 profile, and sliced on the
   one-nozzle P2S. Dropped rather than padded: with one filament nothing is
   ever flushed, so no weight can move, and the slicer fills in its own.

4. A plate saved with `filament_map_mode` "Auto For Flush" (every Bambu
   project carries it, per plate in model_settings.config and once for the
   project) lets the slicer re-decide which head prints which filament after
   pre-slicing. On the four-head U1 profile that re-decision corrupts the heap:
   `std::bad_alloc`, SIGSEGV or SIGABRT, right after the log line
   `filament maps changed after pre-slicing`, on seven runs in ten for the same
   plate. So the assignment is fixed here instead ("Manual"), and it has to be
   fixed to something true: the file's own `filament_map` is the author's
   machine, which for a one-nozzle Bambu means every filament on head 1, and a
   two-colour plate read that way becomes a single-nozzle colour swap with a
   purge tower the U1 never builds. Filament k goes to head k, round the heads
   the machine has — what a tool changer does, and with the per-plate
   renumbering in the engine every plate's colours are 1..k anyway.
   `filament_printable` goes with it: it is a bit mask of the heads a filament
   may use, and the author's two-head H2C wrote `3` — heads 1 and 2 — so a
   filament on head 3 is refused with `-68`, "Some filaments cannot be printed
   on the extruder mapped to". Widened to every head the machine has. (Widening
   it alone, leaving the mapping on Auto, does not stop the crash: 1 run in 8.)

5. Settings an object carries for itself in model_settings.config beat the
   ones the command line gives the whole run. `object_settings` overwrites
   such a key wherever an object states it; the one caller uses it for the
   brim, whose `--brim-type=no_brim` five eyes of a pig ignored with
   their own `outer_only` and died inside `Generating skirt & brim`.

Deleting the embedded configs instead of repairing them is NOT an option: with
project_settings.config absent the slicer segfaults even earlier, before it
reaches the file.
"""
from __future__ import annotations

import json
import re
import zipfile

import paint

PROJECT_CONFIG = "Metadata/project_settings.config"
MODEL_CONFIG = "Metadata/model_settings.config"
FILAMENT_CONFIG_PREFIX = "Metadata/filament_settings_"

OBJECT_ID = re.compile(r'<object\s+id="(\d+)"')
EXTRUDER_VALUE = re.compile(r'(key="extruder"\s+value=")(\d+)(")')

# key -> (offending value, replacement)
OUT_OF_RANGE = {
    "wall_filament": ("0", "1"),
    "sparse_infill_filament": ("0", "1"),
    "solid_infill_filament": ("0", "1"),
    "raft_first_layer_expansion": ("-1", "0"),
    "tree_support_wall_count": ("-1", "0"),
}

NIL = "nil"

FLUSH_MATRIX = "flush_volumes_matrix"

MAP_MODE = "filament_map_mode"
MAP_MODE_MANUAL = "Manual"
PLATE_MAP_MODE = re.compile(r'(key="filament_map_mode"\s+value=")([^"]*)(")')
PLATE_MAPS = re.compile(r'(key="filament_maps"\s+value=")([^"]*)(")')


def _fix_ranges(cfg: dict) -> list[str]:
    changed = []
    for key, (bad, good) in OUT_OF_RANGE.items():
        if key not in cfg:
            continue
        value = cfg[key]
        if isinstance(value, list):
            fixed = [good if str(v) == bad else v for v in value]
            if fixed != value:
                cfg[key] = fixed
                changed.append(key)
        elif str(value) == bad:
            cfg[key] = good
            changed.append(key)
    return changed


def _fix_flush_matrix(cfg: dict) -> list[str]:
    """Drop a one-cell flush matrix; see point 3 in the module docstring.

    One cell is what a one-filament project writes (N filaments give N×N), and
    it is the only size found to crash. Bigger matrices are left exactly as
    they are: they size the prime tower, and the tower is weight.
    """
    matrix = cfg.get(FLUSH_MATRIX)
    if isinstance(matrix, list) and len(matrix) == 1:
        del cfg[FLUSH_MATRIX]
        return [FLUSH_MATRIX]
    return []


def _head_map(count: int, heads: int) -> list[str]:
    """Filament k on head k, wrapping round the heads; see point 4 above."""
    return [str(i % heads + 1) for i in range(count)]


def _fix_map_mode(cfg: dict, heads: int | None) -> list[str]:
    """Pin the project's filament-to-head assignment; see point 4 above."""
    if not heads or MAP_MODE not in cfg:
        return []
    count = len(cfg.get("filament_colour") or cfg.get("filament_map") or [])
    cfg[MAP_MODE] = MAP_MODE_MANUAL
    cfg["filament_map"] = _head_map(count, heads)
    changed = [MAP_MODE, "filament_map"]
    if "filament_printable" in cfg:
        cfg["filament_printable"] = [str((1 << heads) - 1)] * count
        changed.append("filament_printable")
    return changed


def _fix_plate_map_mode(settings: str, heads: int | None) -> str:
    """The same per plate, which is the one that matters: the project value
    alone set to Manual leaves the crash exactly as frequent."""
    if not heads:
        return settings
    settings = PLATE_MAP_MODE.sub(lambda m: f"{m.group(1)}{MAP_MODE_MANUAL}{m.group(3)}", settings)
    return PLATE_MAPS.sub(
        lambda m: m.group(1) + " ".join(_head_map(len(m.group(2).split()), heads)) + m.group(3),
        settings,
    )


def _fix_filament_config(cfg: dict) -> dict:
    """Replace "nil" placeholders with the first real value in the same array and
    drop the inherits pointer at a preset the target slicer does not have."""
    for key, value in list(cfg.items()):
        if not isinstance(value, list) or NIL not in value:
            continue
        fallback = next((v for v in value if v != NIL), "0")
        cfg[key] = [fallback if v == NIL else v for v in value]
    cfg.pop("inherits", None)
    return cfg


def _apply_overrides(cfg: dict, overrides: dict) -> list[str]:
    """Force `overrides` onto the project config, reporting what actually moved.

    Kept deliberately dumb: it has no opinion about which keys are worth
    overriding, because the caller is the only one that knows what it is slicing
    for. See OrcaSlicerEngine.slice() for the one caller and its reasoning.
    """
    changed = []
    for key, value in overrides.items():
        if str(cfg.get(key)) == str(value):
            continue
        cfg[key] = value
        changed.append(key)
    return changed


SETTINGS_OBJECT = re.compile(r'<object\s+id="\d+">.*?</object>', re.S)
PART_OPEN = re.compile(r'<part\s[^>]*>')


def _single_part_colour(settings: str) -> str:
    """An object of one part prints in its part's colour, as Bambu Studio has it.

    A Bambu project states an extruder twice — on the object and on each part —
    and when the object has a single part the engine reads the object's and
    drops the part's. The author set the part: the zen eagle's body is part
    extruder 4 (brown) under an object left on 1 (black), and it sliced black,
    one colour for a plate that has two and a slot the product never got
    (1.13.3; the same in 1.10). So the object takes its part's number, which
    is what the author sees in the slicer the file was made in. Line-based, for
    the reason _rewrite_extruders() gives.
    """

    def one(match: re.Match) -> str:
        block = match.group(0)
        parts = PART_OPEN.findall(block)
        if len(parts) != 1 or 'subtype="normal_part"' not in parts[0]:
            return block
        head, _, tail = block.partition(parts[0])
        found = EXTRUDER_VALUE.search(tail)
        if not found or found.group(2) == "0":
            return block
        return EXTRUDER_VALUE.sub(
            lambda m: f"{m.group(1)}{found.group(2)}{m.group(3)}", head, count=1
        ) + parts[0] + tail

    return SETTINGS_OBJECT.sub(one, settings)


def _rewrite_object_settings(settings: str, values: dict) -> str:
    """Overwrite `values` wherever the file states them; see point 5 above.

    Only keys already present are touched: an object that says nothing about a
    key inherits it from the run, which is the behaviour being asked for.
    """
    for key, value in values.items():
        settings = re.sub(
            r'(key="%s"\s+value=")[^"]*(")' % re.escape(key),
            lambda m: f"{m.group(1)}{value}{m.group(2)}",
            settings,
        )
    return settings


def _rewrite_extruders(settings: str, per_object: dict, heads: int | None = None) -> str:
    """Renumber the extruder each object's parts print with.

    Line-based rather than through an XML parser, and deliberately so: this file
    is written and read by the slicer, and re-serialising it would rewrite every
    line in the archive we hand back to it for the sake of changing a handful.

    `per_object` is `{object id: {old index: new index}}`; an object not named
    there is left exactly as it was.
    """
    if not per_object:
        return settings

    out = []
    current = None
    for line in settings.splitlines(keepends=True):
        opened = OBJECT_ID.search(line)
        if opened:
            current = opened.group(1)
        elif "</object>" in line:
            current = None

        mapping = per_object.get(current) if current else None
        if mapping:
            found = EXTRUDER_VALUE.search(line)
            if found and found.group(2) in mapping:
                line = EXTRUDER_VALUE.sub(
                    lambda m: f"{m.group(1)}{mapping[m.group(2)]}{m.group(3)}", line
                )
            elif found and heads and int(found.group(2)) > heads:
                # A number nothing on this object prints with — the object's
                # own under parts that all state theirs, or a negative part's —
                # still has to name a head the machine has, or the engine dies
                # on it as on any other (1.2). Where it lands does not matter.
                line = EXTRUDER_VALUE.sub(
                    lambda m: f"{m.group(1)}{min(mapping.values(), key=int)}{m.group(3)}", line
                )
        out.append(line)
    return "".join(out)


def repair(
    src: str,
    dst: str,
    project_overrides: dict | None = None,
    object_extruders: dict | None = None,
    object_settings: dict | None = None,
    heads: int | None = None,
    mesh_paint: dict | None = None,
) -> dict:
    """Write a repaired copy of `src` to `dst`. Returns a small report.

    `project_overrides` are applied to project_settings.config in the same pass,
    and `object_extruders` and `object_settings` to model_settings.config, so none costs anything
    extra — rewriting the archive a second time would mean recompressing tens of
    megabytes of mesh for the sake of one JSON key.

    `mesh_paint` is `{archive path: {mesh object id: {old: new} or None}}` — the
    paint that has to follow a renumbered base, or be dropped from a part that
    has become the colour it was painted (see paint.py).
    """
    with zipfile.ZipFile(src) as zin:
        names = set(zin.namelist())
        if PROJECT_CONFIG not in names:
            raise ValueError(
                f"{src}: no {PROJECT_CONFIG} — not a slicer project 3MF, "
                "and removing/adding one is not a supported repair"
            )
        project = json.loads(zin.read(PROJECT_CONFIG))

    fixed_keys = _fix_ranges(project) + _fix_flush_matrix(project) + _fix_map_mode(project, heads)
    overridden_keys = _apply_overrides(project, project_overrides or {})
    touched_filament_configs = []

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        dst, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for item in zin.infolist():
            name = item.filename
            if name == PROJECT_CONFIG:
                continue  # rewritten below
            if name == MODEL_CONFIG:
                settings = _fix_plate_map_mode(zin.read(name).decode("utf-8"), heads)
                settings = _single_part_colour(settings)
                settings = _rewrite_object_settings(settings, object_settings or {})
                zout.writestr(item, _rewrite_extruders(settings, object_extruders or {}, heads))
                continue
            if mesh_paint and name in mesh_paint:
                zout.writestr(item, paint.rewrite_mesh_file(zin.read(name), mesh_paint[name]))
                continue
            if name.startswith(FILAMENT_CONFIG_PREFIX):
                cfg = _fix_filament_config(json.loads(zin.read(name)))
                zout.writestr(name, json.dumps(cfg, indent=4))
                touched_filament_configs.append(name)
                continue
            zout.writestr(item, zin.read(name))
        zout.writestr(PROJECT_CONFIG, json.dumps(project, indent=4))

    return {
        "range_keys_fixed": fixed_keys,
        "filament_configs_fixed": touched_filament_configs,
        "project_overrides_applied": overridden_keys,
        "mesh_files_repainted": sorted(mesh_paint or {}),
    }


ITEM = re.compile(r'\s*<item\s[^>]*objectid="(\d+)"[^>]*/>')
RESOURCE = re.compile(r'\s*<object\s+id="(\d+)"[^>]*>.*?</object>', re.S)
INSTANCE = re.compile(r'\s*<model_instance>.*?</model_instance>', re.S)
INSTANCE_OBJECT = re.compile(r'key="object_id"\s+value="(\d+)"')
ASSEMBLE_ITEM = re.compile(r'\s*<assemble_item\s[^>]*object_id="(\d+)"[^>]*/>')


def drop_objects(src: str, dst: str, object_ids: list[str]) -> None:
    """A copy of `src` without the named objects — all of each: the resource
    and build item in 3dmodel.model, and the object, plate entry and assembly
    entry in model_settings.config. Their meshes in 3D/Objects stay, unreferenced:
    the engine ignores them, and rewriting them would cost more than it saves.

    Used to print a plate in passes (see OrcaSlicerEngine._split_plan). Removing
    only the build item leaves the settings naming an object that is not there,
    and the engine refuses the whole file as unparseable (-6).
    """
    drop = set(object_ids)

    def unless(pattern_id):
        return lambda m: "" if pattern_id(m) in drop else m.group(0)

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "3D/3dmodel.model":
                text = data.decode("utf-8")
                text = ITEM.sub(unless(lambda m: m.group(1)), text)
                text = RESOURCE.sub(unless(lambda m: m.group(1)), text)
                data = text.encode("utf-8")
            elif item.filename == MODEL_CONFIG:
                text = data.decode("utf-8")
                text = INSTANCE.sub(unless(lambda m: (INSTANCE_OBJECT.search(m.group(0)) or [None, None])[1]), text)
                text = RESOURCE.sub(unless(lambda m: m.group(1)), text)
                text = ASSEMBLE_ITEM.sub(unless(lambda m: m.group(1)), text)
                data = text.encode("utf-8")
            zout.writestr(item, data)


ITEM_TRANSFORM = re.compile(r'(<item\s[^>]*objectid="(\d+)"[^>]*\btransform=")([^"]+)(")')


def shift_plate(src: str, dst: str, object_ids: list[str], plate: int, dx: float, dy: float) -> None:
    """A copy of `src` with one plate's objects — and its prime tower — moved by
    (dx, dy) millimetres on the bed.

    The whole plate moves as one, so nothing on it comes any closer to anything
    else: it is the bed that is put back under the print, not the print that
    is rearranged (see OrcaSlicerEngine._plate_onto_bed). Objects move by the
    translation of their build item; the tower's place is stored per plate,
    relative to the plate, so it moves by the same amount.
    """
    wanted = set(object_ids)

    def moved(match: re.Match) -> str:
        if match.group(2) not in wanted:
            return match.group(0)
        values = match.group(3).split()
        if len(values) != 12:
            return match.group(0)
        values[9] = _num(float(values[9]) + dx)
        values[10] = _num(float(values[10]) + dy)
        return match.group(1) + " ".join(values) + match.group(4)

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "3D/3dmodel.model":
                data = ITEM_TRANSFORM.sub(moved, data.decode("utf-8")).encode("utf-8")
            elif item.filename == PROJECT_CONFIG:
                project = json.loads(data)
                for key, delta in (("wipe_tower_x", dx), ("wipe_tower_y", dy)):
                    values = project.get(key)
                    if isinstance(values, list) and 0 < plate <= len(values):
                        try:
                            values[plate - 1] = _num(float(values[plate - 1]) + delta)
                        except (TypeError, ValueError):
                            pass
                data = json.dumps(project, indent=4).encode("utf-8")
            zout.writestr(item, data)


def _num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


if __name__ == "__main__":
    import sys

    print(repair(sys.argv[1], sys.argv[2]))
