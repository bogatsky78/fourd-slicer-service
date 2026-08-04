# FourD Slicer Service

Headless 3D-model slicing over HTTP. Post a 3MF, get back how many grams of each
filament the print will consume, how long it will take and how many plates it
needs — the numbers a shop needs to price a printed product.

Built by [4DCode](https://github.com/bogatsky78) for the e-commerce projects we
develop, and useful anywhere a print farm, a quoting page or an order pipeline
has to turn a model file into a cost.

```bash
docker run -d -p 8077:8000 bohatskyi/fourd-slicer-service:latest
curl -s http://127.0.0.1:8077/engines
```

```bash
curl -X POST http://127.0.0.1:8077/engines/orca/slice \
  -F "model=@model.3mf" \
  -F "machine_profile=Snapmaker/machine/Snapmaker U1 (0.4 nozzle)" \
  -F "process_profile=Snapmaker/process/0.16 Optimal @Snapmaker U1 (0.4 nozzle)"
```

```json
{
  "engine": "orca",
  "engine_version": "2.4.2",
  "filaments": [
    { "slot": 1, "used_g": 16.54, "used_m": 5.46, "material": "PLA", "color": "#FFFFFF" },
    { "slot": 2, "used_g": 23.28, "used_m": 7.68, "material": "PLA", "color": "#F99963" },
    { "slot": 3, "used_g": 2.25,  "used_m": 0.74, "material": "PLA", "color": "#000000" }
  ],
  "total_weight_g": 42.07,
  "print_time_sec": 14482,
  "plate_count": 1,
  "filament_count": 3
}
```

## Why this exists

A slicer is a desktop GUI application. Calling one from a web backend means
running a native binary with an OpenGL stack, feeding it files it was never
meant to receive unattended, and parsing whatever it leaves behind. This service
is that boundary, and it absorbs three problems so the caller sees none of them:

- **Files from model sites do not load.** A MakerWorld download is a Bambu Studio
  project carrying settings outside the ranges the slicer enforces, `"nil"`
  placeholders in filament arrays, and inheritance pointers at presets that only
  exist inside Bambu Studio. Every one of those aborts the load. `repair3mf.py`
  fixes them in place — deleting the embedded config is not an option, the
  slicer crashes even earlier without it.
- **The file carries someone else's printer.** Machine settings survive
  `--load-settings`, and some combinations are refused outright. Slicing a Bambu
  AMS file with a Snapmaker U1 profile dies with `return -51` and no explanation
  on stdout.
- **Every slicer differs.** CLI dialect, input requirements and result format all
  vary. Engines normalise those away behind one contract.

The full account of each, with the reasoning and the measurements, is in
[`CLAUDE.md`](CLAUDE.md).

## API

| Method | Purpose |
|---|---|
| `GET /health` | Liveness. |
| `GET /engines` | Engines in this image, their versions and availability. |
| `GET /engines/{code}/profiles` | Machine / process / filament profile names the engine knows. |
| `POST /engines/{code}/slice` | multipart: `model`, optional `machine_profile`, `process_profile`, `filament_profiles` (`;`-separated), `scale`, `plate`. |

Errors: `404` unknown engine, `503` engine missing from the image, `422` slicing
failed — `detail` carries `exit_code` and the tail of **both** output streams,
because the reason for a refusal goes to stderr while progress goes to stdout.

Slicing is synchronous and CPU-heavy: roughly 20 seconds for a multi-colour
model. Call it from a queue, not from a request a person is waiting on.

## Adding a slicer

One fetch stage in the `Dockerfile` unpacking into `/opt/engines/<code>/`, one
class in `engines/`, one line in `engines/registry.py`. Nothing else changes —
not here, and not in whatever is calling the service.

## Releasing

Pushing a tag `vX.Y.Z` builds the image and publishes `X.Y.Z`, `X.Y` and
`latest` to Docker Hub, then starts the container and checks the engine reports
itself available before the tag is trusted. Pull requests build without
publishing.

The workflow needs one repository secret, `DOCKERHUB_TOKEN`, with Read & Write
scope. The account name is not a secret and lives in the workflow's
`REGISTRY_USER`, beside the image reference it is half of.

## Licence

**AGPL-3.0.** See [LICENSE](LICENSE).

This image bundles [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer), which
is itself AGPL-3.0, redistributed unmodified from its official release AppImage
(`ARG ORCA_VERSION` pins the version). Its corresponding source is available
from that repository at the matching tag.

The AppImage carries no copy of its own licence, so the build fetches
`LICENSE.txt` at the same pinned tag and places it beside the binary at
`/opt/engines/orca/LICENSE.txt`. [`NOTICE`](NOTICE) lists what is redistributed
and under what terms.

The wrapper in this repository is licensed the same way on purpose. It exists to
put a slicer behind a network service, which is precisely the case the AGPL's
network clause is written for, and matching the licence removes any question
about it.
