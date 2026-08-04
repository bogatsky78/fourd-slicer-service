"""Engine registry.

Adding a slicer is one class plus one line here (and its binary in the image).
Nothing else in the service, or in whatever calls it, needs to change.
"""
from __future__ import annotations

from .base import SlicerEngine
from .orca import OrcaSlicerEngine

_ENGINES: dict[str, SlicerEngine] = {}


def register(engine: SlicerEngine) -> None:
    _ENGINES[engine.code] = engine


def get(code: str) -> SlicerEngine:
    try:
        return _ENGINES[code]
    except KeyError:
        raise KeyError(f"unknown slicer engine: {code}") from None


def all_engines() -> list[SlicerEngine]:
    return list(_ENGINES.values())


def describe() -> list[dict]:
    """Engines this image carries, for a caller building a picker."""
    described = []
    for engine in all_engines():
        available = engine.is_available()
        described.append(
            {
                "code": engine.code,
                "label": engine.label,
                "available": available,
                "version": engine.version() if available else None,
            }
        )
    return described


register(OrcaSlicerEngine())
