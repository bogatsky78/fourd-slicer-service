"""HTTP wrapper around the slicer engines.

Callers never talk to a slicer binary directly — they post a model here and get
a normalised result back, so swapping or upgrading the engine stays invisible to
whatever is asking.
"""
from __future__ import annotations

import base64
import dataclasses
import os
import shutil
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

import render
from engines import registry
from engines.base import EngineUnavailable, SliceFailed, SliceRequest

app = FastAPI(title="FourD Slicer Service", version="1.13")

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
    plates: str = Form(""),
    brim: bool = Form(False),
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
            # "1,3,5": only these plates, for a file whose plates are variants
            # and a person chose which to print. Empty means `plate` decides.
            plates=_plate_list(plates),
            brim=brim,
        )
        try:
            result = engine.slice(request, workdir)
        except EngineUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except SliceFailed as exc:
            raise HTTPException(
                status_code=422,
                detail=_refusal(exc),
            ) from exc

        payload = dataclasses.asdict(result)
        payload["filament_count"] = result.filament_count
        # Colours on the busiest plate, which is the number a caller has to
        # compare against a machine's head count; the total is a different
        # question and is routinely much larger on a laid-out assembly.
        payload["max_plate_filaments"] = result.max_plate_filaments
        # asdict() flattens the nested ModelInfo without its properties, and the
        # aggregates are the part callers read; re-serialise it properly.
        payload["model"] = result.model.to_payload() if result.model else None
        return payload
    finally:
        # A slice leaves hundreds of MB behind; never let it accumulate.
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/engines/{code}/inspect")
async def inspect_model(
    code: str,
    model: UploadFile = File(...),
    scale: float = Form(1.0),
) -> dict:
    """Measure a model without slicing it.

    Its own endpoint because geometry costs seconds where a slice costs minutes,
    and plenty of questions — what size to show a customer, which box it ships
    in, whether it fits the bed at all — need only the former. Takes no printer
    profile: none of this depends on the machine.
    """
    engine = _engine(code)

    os.makedirs(WORKDIR_ROOT, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=WORKDIR_ROOT, prefix="inspect-")
    try:
        source = os.path.join(workdir, os.path.basename(model.filename or "model.3mf"))
        with open(source, "wb") as fh:
            shutil.copyfileobj(model.file, fh)

        request = SliceRequest(input_path=source, scale=scale)
        try:
            info = engine.inspect(request, workdir)
        except EngineUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except SliceFailed as exc:
            raise HTTPException(
                status_code=422,
                detail=_refusal(exc),
            ) from exc

        return {"engine": engine.code, "engine_version": engine.version(), "model": info.to_payload()}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/render")
async def render_model(
    model: UploadFile = File(...),
    width: int = Form(900),
    height: int = Form(900),
    stage: str | None = Form(None),
    plate: int | None = Form(None),
) -> dict:
    """One picture of the model, and the name of where it came from.

    With `plate`, one picture of that plate instead (1.10): the slicer's own
    thumbnail of it out of the archive, else our drawing of what stands on it.
    A plate the file does not have is a 422 like any other file nothing could
    draw.

    **Not under `/engines/{code}/`**, unlike everything else that reads a model:
    not one stage of the cascade touches a slicer binary. Two lift a picture the
    designer put in the archive and the third draws the meshes; asking for an
    engine would be asking for something that is then never used, and would
    imply a choice of renderer that does not exist.

    The stage travels with the image because it changes what the image *is* —
    the designer's studio render, the designer's photograph of a print, or our
    own untextured geometry — and only the caller can decide what to do with each.

    Base64 rather than raw bytes with the stage in a header: every other response
    from this service is JSON, and a caller that has already written a JSON
    client should not need a second code path to fetch a picture. A 900x900 PNG
    is a few hundred kilobytes, and a third on top of that is nothing next to the
    3MF that was just uploaded to produce it.
    """
    width, height = _picture_size(width), _picture_size(height)
    if plate is not None and plate < 1:
        raise HTTPException(status_code=422, detail=f"plate numbers start at 1, got {plate}")

    os.makedirs(WORKDIR_ROOT, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=WORKDIR_ROOT, prefix="render-")
    try:
        source = os.path.join(workdir, os.path.basename(model.filename or "model.3mf"))
        with open(source, "wb") as fh:
            shutil.copyfileobj(model.file, fh)

        try:
            picture = render.render(source, width, height, stage, plate)
        except render.RenderFailed as exc:
            raise HTTPException(
                status_code=422, detail={"message": str(exc), "stage": stage, "plate": plate}
            ) from exc

        return {
            "stage": picture.stage,
            "plate": plate,
            "assembled": picture.assembled,
            "format": "png",
            "width": picture.width,
            "height": picture.height,
            "bytes": len(picture.png),
            "image_base64": base64.b64encode(picture.png).decode("ascii"),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


#: Widest picture this will draw. Not a policy about what a shop should display —
#: a guard on the one request parameter that costs memory quadratically, so a
#: typo cannot ask for a 100000-pixel canvas and take the service down with it.
MAX_PICTURE_PX = 4000


def _plate_list(value: str) -> list[int]:
    try:
        return [int(p) for p in value.replace(" ", "").split(",") if p]
    except ValueError:
        raise HTTPException(status_code=400, detail=f"plates must be a comma-separated list of numbers, got {value!r}")


def _picture_size(value: int) -> int:
    if value < 16 or value > MAX_PICTURE_PX:
        raise HTTPException(
            status_code=422,
            detail=f"picture size must be between 16 and {MAX_PICTURE_PX} pixels, got {value}",
        )

    return value


def _refusal(exc: SliceFailed) -> dict:
    """What a 422 carries.

    `reason` is present on every refusal and null on most of them, so a caller
    can branch on it without first asking whether the field exists. A name means
    the refusal is a fact about the model worth storing — `off_bed` is one — and
    null means the slicer simply said no, which only the sentence and the log
    tail can describe.
    """
    return {
        "message": str(exc),
        "reason": exc.reason,
        "exit_code": exc.exit_code,
        "log": exc.log,
        "journal": exc.journal,
        "engine_logs": exc.engine_logs,
    }


def _engine(code: str):
    try:
        return registry.get(code)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
