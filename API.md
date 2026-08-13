# API reference

Everything the service accepts and everything it answers, for version **1.4**.

A running instance serves the machine-readable schema at `/openapi.json` and a
browsable form of it at `/docs`; the endpoint and parameter tables below are
taken from there, so they cannot drift from the code. What the schema cannot
say is written by hand and is most of what this page is for: the endpoints
return plain JSON objects, so the schema knows their shape is "an object" and
nothing else. **What each number means, what unit it is in, why there are three
different sizes and what an empty field is claiming** are all here and nowhere
in the schema.

Base URL is wherever the container is reachable. Inside a compose stack that is
usually `http://slicer:8000`; the examples use `http://127.0.0.1:8077`, which is
what the `docker run` in the README publishes.

## Units, once, for everything below

| Quantity | Unit |
|---|---|
| Lengths, sizes | millimetres |
| Volumes | cubic millimetres |
| Filament consumed | grams, and metres of filament off the spool |
| Times | seconds |
| Colours | `#RRGGBB`, as the file states them |

Nothing here is ever a percentage, a ratio or a machine-specific unit.

## Endpoints

| Method | Path | Cost | Purpose |
|---|---|---|---|
| `GET` | `/health` | instant | Liveness. `{"status": "ok"}` and nothing else. |
| `GET` | `/engines` | instant | Which engines this image carries, their versions, whether the binary is actually present. |
| `GET` | `/engines/{code}/profiles` | instant | The machine / process / filament profile names that engine knows. |
| `POST` | `/engines/{code}/slice` | ~20 s to minutes | Slice the model: what printing it consumes and how long it takes. |
| `POST` | `/engines/{code}/inspect` | ~3 s | Measure the model without slicing it. |

`{code}` is an engine code from `GET /engines` — `orca` in every image built so
far.

**Slicing is synchronous and CPU-heavy.** Roughly 20 seconds for a multi-colour
model on one plate, and a plate at a time for a file laid out across several, so
an eleven-plate assembly is minutes. Call it from a queue, never from a request
somebody is waiting on. `/inspect` is the cheap half and is the right call for
anything that only needs to know how big something is.

### `POST /engines/{code}/slice`

`multipart/form-data`:

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `model` | file | **yes** | — | The 3MF. Bambu Studio projects are repaired on the way in; nothing has to be done to them first. |
| `machine_profile` | string | no | — | A profile name from `/profiles`, e.g. `Snapmaker/machine/Snapmaker U1 (0.4 nozzle)`. |
| `process_profile` | string | no | — | Likewise, e.g. `Snapmaker/process/0.16 Optimal @Snapmaker U1 (0.4 nozzle)`. |
| `filament_profiles` | string | no | `""` | `;`-separated profile names, one per slot. |
| `scale` | number | no | `1.0` | See the warning about it below. |
| `plate` | integer | no | `0` | `0` slices the whole file; a plate number slices only that plate. |
| `brim` | boolean | no | `false` | Off by default, so the grams describe the part and not a skirt around it. See below. |

**The brim is off unless you ask for it.** Whatever the file's own `brim_type`
says, the engine is told `no_brim` — the default answer is the weight of the
part alone. Two reasons, and either is enough. A brim is material the shop does
not extrude: the printer holds its parts without one, and cutting a brim off a
finished part costs minutes of knife work each, so a weight that includes one
prices a print nobody makes. And on some plates it is the only way to get a
number at all — four dowels of 12–100 mm³ at 27–71k triangles apiece kill the
run inside `Generating skirt & brim`, intermittently, under the unhelpful name
`std::bad_alloc`.

Measured cost on files whose own setting is `auto_brim`: **none.** The same
plate weighs 116.64 g with the brim and 116.64 g without; another weighs 18.47 g
against 18.48 g. Where the engine would have laid a brim, the difference is that
brim's own grams. Pass `brim=true` to get the file's behaviour back.

**Pass a `machine_profile` or do not trust the grams.** Without one the engine
slices on the settings the file's author saved into it, which for a Bambu AMS
file means a purge tower between every colour change. The same corgi measures
188.05 g on the file's own Bambu profile and 42.07 g on the Snapmaker U1 one —
a factor of 4.5, and pricing on the wrong figure roughly doubles a product.

**`process_profile` is recorded and largely not applied.** The engine takes
`layer_height`, `wall_loops` and `sparse_infill_density` from the file's embedded
project settings; slicing one file at 0.08, 0.16 and 0.28 mm returns identical
weights and times. Send it — it is the honest record of what was asked for — but
do not expect it to move the numbers.

**`scale` is passed through to the engine and should not be relied on.** The
engine's own scaling grows each piece about its own centre without moving the
pieces apart, so a multi-part model deforms instead of growing: measured at
`--scale 1.5`, volume grew by exactly 1.5³ while `size_y` grew by 1.29. The
geometry in the `model` block below has the factor applied arithmetically and is
therefore the ideal answer, not the engine's.

### `POST /engines/{code}/inspect`

`multipart/form-data`: `model` (file, required) and `scale` (number, default
`1.0`). Takes no printer profile, because none of what it answers depends on the
machine.

Returns `engine`, `engine_version` and the same `model` block a slice returns.

## The slice response

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
  "filament_count": 3,
  "max_plate_filaments": 3,
  "warnings": [],
  "raw": "…tail of the engine log…",
  "plates": [
    {
      "index": 1,
      "weight_g": 42.07,
      "print_time_sec": 14482,
      "filament_changes": 416,
      "filaments": [ { "slot": 1, "used_g": 16.54, "used_m": 5.46, "material": "PLA", "color": "#FFFFFF" } ],
      "warnings": [],
      "adjustments": []
    }
  ],
  "model": { "…": "see below" }
}
```

| Field | Type | Meaning |
|---|---|---|
| `engine` / `engine_version` | string | Which binary produced this. **Record it**: grams depend on the engine version, so an upgrade that nobody wrote down turns into a price change nobody can explain. |
| `filaments[]` | list | One entry per palette slot the model uses, merged across plates. |
| `filaments[].slot` | int | The extruder number **the file names**, 1-based. Sparse by design: an assembly may use 1, 2, 4, 5 … 13 with gaps, and a slot missing from this list is a slot the model does not print. |
| `filaments[].used_g` / `used_m` | number | Grams of plastic, and metres off the spool. |
| `filaments[].material` / `color` | string, nullable | As the **file** states them, never as the slice reported them — see "Renumbering" below. |
| `total_weight_g` | number | Every plate summed. |
| `print_time_sec` | int, nullable | Every plate summed. Null when the engine did not say. |
| `plate_count` | int | Plates the file declares. |
| `filament_count` | int | How many slots actually consume something. |
| `max_plate_filaments` | int | **Colours standing on the busiest single plate.** |
| `warnings[]` | list of string | What the engine complained about without refusing. |
| `raw` | string, nullable | The tail of the engine's log for the last plate run. Diagnostics; do not parse it. |
| `plates[]` | list | The same numbers, per plate — `index`, `weight_g`, `print_time_sec`, `filaments[]`, `filament_changes`, `warnings[]`, `adjustments[]`. |
| `model` | object | Geometry. See below. |

**`max_plate_filaments`, not `filament_count`, is what a head count is compared
against.** Plates print one after another, so a model laid out across nine of
them asks the machine for no more colours at once than stand on its busiest
plate: an eleven-colour assembly whose plates hold one colour each prints
perfectly well on a single-head machine. `filament_count` answers a different
question — how many spools the job needs in total — and on a laid-out model it is
routinely far larger.

**`plates[].filament_changes`** is the count of tool changes in that plate's
G-code, and it comes from nowhere else — not from the engine's summary, not from
its metadata. A caller that charges for purge needs it: how many grams a change
costs is a property of the machine, and how many changes there will be is a
property of the print, and only the thing that sliced it knows the second half.

**`plates[].adjustments`** lists what had to be changed before that plate would
slice at all — moving a purge tower back onto the bed, for instance. It is
reported rather than done quietly, because it means the file describes a print
this machine cannot run as the author laid it out.

**Renumbering, and why `material` and `color` come from the file.** A file's
extruder number is a palette slot, not a print head: an assembly numbers its
colours 1..13 and prints one plate at a time, and the engine — which reads those
numbers as heads — dies three different ways on a part asking for head 5 of 4.
The service therefore renumbers each plate's colours into 1..k for the run and
numbers the results back on the way out. A slice consequently reports the colour
of the *stand-in* slot, so colour and material are read out of the file instead.
Renumbering happens only where the filaments are interchangeable (same type,
density, diameter and settings id); where they are not, the file is left alone.

## The `model` block

Returned identically by `/slice` and `/inspect`.

```json
{
  "model": {
    "objects": [
      { "size_x": 60.96, "size_y": 80.644, "size_z": 81.382,
        "volume_mm3": 144072.25, "facet_count": 1159988, "manifold": true }
    ],
    "object_count": 16,
    "total_volume_mm3": 276484.7,
    "size_x": 60.96, "size_y": 80.644, "size_z": 81.382,
    "assembly": { "size_x": 94.95, "size_y": 80.64, "size_z": 141.0, "part_count": 15 }
  }
}
```

**There are three different sizes in there and they answer three different
questions.** Picking the wrong one is the single easiest mistake to make against
this API, so:

| You want to know | Read |
|---|---|
| Will a piece fit on the print bed? | `objects[].size_*` — every piece, individually |
| What carton do the printed pieces ship in? | `objects[]` — each box must fit, and `total_volume_mm3` must fit |
| How big is the toy the customer receives? | `assembly.size_*` |
| One number, for a rough sort or a listing | `size_*` at the top level — the largest single piece |

| Field | Type | Meaning |
|---|---|---|
| `objects[]` | list | One entry per printable object in the file, **each measured in its own coordinates**. A 3MF routinely holds several: a keychain file is a puppy, two rings and a clip; a laid-out assembly is forty-two pieces. |
| `objects[].size_*` | number | That piece's bounding box. |
| `objects[].volume_mm3` | number, nullable | Material, not the box. |
| `objects[].facet_count` | int, nullable | Triangles. |
| `objects[].manifold` | bool, nullable | **False means the volume, and therefore any weight derived from it, cannot be trusted.** |
| `object_count` | int | `len(objects)`. |
| `total_volume_mm3` | number, nullable | Every piece's material summed. |
| `size_x` / `size_y` / `size_z` | number, nullable | The **largest single object**, as a convenience for callers that want one number without sorting the list. Not the model. |
| `assembly` | object, **nullable** | The model with its pieces put together. See below. |

**`objects[]` is a list and not a box on purpose.** Reporting one bounding box
over all of them would answer a question nobody asks: the pieces are printed
apart and packed apart, and the extent of their arrangement on the print bed is
an artefact of how somebody laid them out. Their coordinates are local to each
object and carry no layout, so they cannot be unioned into anything meaningful
anyway.

### `assembly` — and what `null` there means

`assembly` is the model as one object: the thing that stands on a shelf once the
pieces are glued together. For a toy printed in one piece it is that piece. For a
model laid out across plates it is the answer that exists nowhere else in the
file — `objects[]` measures pieces, and the plates say how they were arranged for
printing, not how they fit together.

It is computed from the file's own `<assemble>` block, which records where the
author placed each piece relative to every other. The pieces are grouped by
whether their placed boxes meet — glued parts overlap, that is what glue is — and
the group holding the most material is the model. `part_count` says how many
pieces went into the answer; compare it against `object_count` to see whether
anything was left out.

**Left out are loose extras**, and there are usually some: alternative pieces the
author shipped in the same file (a second head, four spare shells) and
accessories parked beside the model in the assembly view. Counting them would
overstate the model badly — one retriever measures 95 mm assembled and 131 mm
with its coffee mug parked ten millimetres off its paws, and a decorative chest
goes from 99 mm to 450 mm. So `assembly.size_*` is a **lower bound on what
ships**: it is the model, not the model plus everything else in the box.

Accuracy, on the one model in the reference catalogue that has been measured with
a ruler: computed 94.95 × 80.64 × 141.0 mm against a printed and glued 140 mm of
height, 80 mm of width and a little under 100 mm of length. Expect a millimetre
or two over, because a rotated piece's box is rebuilt from its corners and comes
out slightly larger than the piece.

**`null` means the file does not say, and it must not be read as zero, nor
quietly replaced with the largest piece.** It happens for a real and common
reason: the writing tool puts an `<assemble>` block into every project whether or
not anybody assembled anything, and until somebody does, it holds the default
row of pieces laid out side by side. The service detects that — nothing touching
anything at all — and refuses rather than answering 884 mm for a corgi. The only
honest response to `null` is to ask a human for the number.

There is deliberately no verdict for a *partly* assembled file: a chest whose lid
was never put on it comes back as the chest. Telling that from a whole model
would take a threshold, and a threshold here would be a number invented by this
service rather than found in the file.

## Errors

| Status | When | Body |
|---|---|---|
| `404` | No such engine code | `detail`: string |
| `503` | The engine exists in the registry but its binary is not in this image | `detail`: string |
| `422` | The slice failed, **or** a request field failed validation | See below |

A failed slice returns `detail` as an object: `message`, `exit_code`, and `log` —
the tail of **both** output streams. Both, because the engine writes progress to
stdout and the reason for a refusal to stderr, and reading only the first is how
`return -51` looked like a mystery for a week. Where the engine wrote its own
verdict, that sentence is in `message` along with the plate it was on.

A malformed request instead returns FastAPI's own validation shape, `detail` as
a list of `{loc, msg, type}`.

## Compatibility

The service versions its own contract; the engine inside it versions separately
and is reported in every response.

- **1.1** added the `model` block and `/inspect`.
- **1.2** added `plates[]`, `max_plate_filaments` and `filament_changes`, and
  started slicing a plate at a time.
- **1.3** added `model.assembly`.
- **1.4** turned the brim off by default and added `brim` to ask for it back.
  This one **changes numbers a caller already had**: a file the engine would
  have brimmed now weighs its own grams and no more.

Fields are added, not repurposed. The one thing a caller must handle is the
difference between a field being **absent** — an older service that has never
heard of the question — and being **present and null**, which is this service
saying the file does not answer it. Those are different states and they call for
different behaviour; `assembly` is the field where it matters today.

**Pin the tag, never `:latest`.** Grams depend on the engine version, and a
silently updated image changes what everything downstream of it costs.
