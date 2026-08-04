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

Deleting the embedded configs instead of repairing them is NOT an option: with
project_settings.config absent the slicer segfaults even earlier, before it
reaches the file.
"""
from __future__ import annotations

import json
import zipfile

PROJECT_CONFIG = "Metadata/project_settings.config"
FILAMENT_CONFIG_PREFIX = "Metadata/filament_settings_"

# key -> (offending value, replacement)
OUT_OF_RANGE = {
    "wall_filament": ("0", "1"),
    "sparse_infill_filament": ("0", "1"),
    "solid_infill_filament": ("0", "1"),
    "raft_first_layer_expansion": ("-1", "0"),
    "tree_support_wall_count": ("-1", "0"),
}

NIL = "nil"


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


def repair(src: str, dst: str, project_overrides: dict | None = None) -> dict:
    """Write a repaired copy of `src` to `dst`. Returns a small report.

    `project_overrides` are applied to project_settings.config in the same pass,
    so overriding a setting costs nothing extra — rewriting the archive a second
    time would mean recompressing tens of megabytes of mesh for the sake of one
    JSON key.
    """
    with zipfile.ZipFile(src) as zin:
        names = set(zin.namelist())
        if PROJECT_CONFIG not in names:
            raise ValueError(
                f"{src}: no {PROJECT_CONFIG} — not a slicer project 3MF, "
                "and removing/adding one is not a supported repair"
            )
        project = json.loads(zin.read(PROJECT_CONFIG))

    fixed_keys = _fix_ranges(project)
    overridden_keys = _apply_overrides(project, project_overrides or {})
    touched_filament_configs = []

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        dst, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for item in zin.infolist():
            name = item.filename
            if name == PROJECT_CONFIG:
                continue  # rewritten below
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
    }


if __name__ == "__main__":
    import sys

    print(repair(sys.argv[1], sys.argv[2]))
