"""HTTP wrapper around the slicer engines.

Callers never talk to a slicer binary directly — they post a model here and get
a normalised result back, so swapping or upgrading the engine stays invisible to
whatever is asking.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from engines import registry
from engines.base import EngineUnavailable, SliceFailed, SliceRequest

app = FastAPI(title="FourD Slicer Service", version="1.0")

WORKDIR_ROOT = os.environ.get("SLICER_WORKDIR", "/work")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/engines")
def list_engines() -> dict:
    return {"engines": registry.describe()}


@app.get("/engines/{code}/profiles")
def list_profiles(code: str) -> dict:
    engine = _engine(code)
    return {"engine": engine.code, "profiles": engine.profiles()}


@app.post("/engines/{code}/slice")
async def slice_model(
    code: str,
    model: UploadFile = File(...),
    machine_profile: str | None = Form(None),
    process_profile: str | None = Form(None),
    filament_profiles: str = Form(""),
    scale: float = Form(1.0),
    plate: int = Form(0),
) -> dict:
    engine = _engine(code)

    os.makedirs(WORKDIR_ROOT, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=WORKDIR_ROOT, prefix="slice-")
    try:
        source = os.path.join(workdir, os.path.basename(model.filename or "model.3mf"))
        with open(source, "wb") as fh:
            shutil.copyfileobj(model.file, fh)

        request = SliceRequest(
            input_path=source,
            machine_profile=machine_profile or None,
            process_profile=process_profile or None,
            filament_profiles=[p for p in filament_profiles.split(";") if p],
            scale=scale,
            plate=plate,
        )
        try:
            result = engine.slice(request, workdir)
        except EngineUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except SliceFailed as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": str(exc), "exit_code": exc.exit_code, "log": exc.log},
            ) from exc

        payload = dataclasses.asdict(result)
        payload["filament_count"] = result.filament_count
        return payload
    finally:
        # A slice leaves hundreds of MB behind; never let it accumulate.
        shutil.rmtree(workdir, ignore_errors=True)


def _engine(code: str):
    try:
        return registry.get(code)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
