# API reference

Everything the service accepts and everything it answers, for version **1.8**.

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
| `POST` | `/render` | instant to ~40 s | One picture of the model, and where the picture came from. |

`{code}` is an engine code from `GET /engines` — `orca` in every image built so
far. `/render` carries no engine in its path, and that is not an oversight: not
one of the three things it can do involves a slicer binary.

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
| `plates` | string | no | `""` | Several plates, comma-separated and 1-based (`"1,3"`): only these are sliced, and every total is theirs. For a file whose plates are variants and a person chose which to print — see `model.layout`. A number the file does not have is a 422. Takes precedence over `plate`. |
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

### `POST /render`

`multipart/form-data`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model` | file | required | The 3MF. |
| `width` | int | `900` | Widest the picture may be, in pixels. 16 to 4000. |
| `height` | int | `900` | Tallest the picture may be, in pixels. 16 to 4000. |
| `stage` | string | *(cascade)* | Force one stage instead of walking the cascade. |
| `plate` | int | *(none)* | A picture of this plate instead of the model (1.10). Numbers start at 1. |

The picture is fitted inside `width` x `height` keeping its shape, and is never
enlarged past its own size — an embedded 680x510 thumbnail comes back at 680x510
whatever box you ask for. The `width` and `height` in the response are what you
actually got.

```json
{
  "stage": "author_render",
  "assembled": null,
  "format": "png",
  "width": 900,
  "height": 675,
  "bytes": 215627,
  "image_base64": "iVBORw0KGgoAAA…"
}
```

**Always PNG**, whatever was in the archive. The embedded pictures are webp, and
Magento — the first caller — has no webp support at all: its `Image/` framework
carries Gd2 and ImageMagick adapters and not one mention of the format.

**Base64 rather than raw bytes with the stage in a header.** Every other
response here is JSON, and fetching a picture should not need a second kind of
client. The third it adds to the size is nothing beside the 3MF just uploaded to
produce it.

#### The cascade, and why `stage` matters

Three sources, tried in this order, and they are not equally good:

| `stage` | Where it comes from | What it is |
|---|---|---|
| `author_render` | `Auxiliaries/Model Pictures/render.webp` | The studio render the designer published with the model. Best there is, and free. |
| `author_thumbnail` | `Auxiliaries/.thumbnails/thumbnail_middle.png` | The designer's photograph of a print. Always 680x510, and it sometimes carries an overlay the designer stamped on — one file in this catalogue has a crossed-out "AMS" badge across the corner. |
| `geometry` | The meshes, drawn here | Our own render: flat-shaded, in the slot colours the project gives, on a plain ground. The only stage that works on a project somebody saved out of a slicer themselves. |

In a catalogue of 26 MakerWorld files, 14 had `render.webp` and all 26 had
`thumbnail_middle.png`. A project exported from OrcaSlicer out of an STL has
**neither**, nor any `Auxiliaries/` at all — the third stage is not a fallback
for those, it is the only answer. (A *re-export* of a MakerWorld project is a
different thing: the CLI copies `Auxiliaries/` through, so the designer's
pictures survive it.)

The stage is in the response because it changes what the picture **is** — a
studio render of the finished toy, a photograph of somebody else's print, or
untextured geometry — and only the caller can decide what each is good for.
Passing `stage` asks for exactly that one, which is how you get our geometry for
a model that has a perfectly good studio render sitting in it.

#### `assembled`

Only ever set on `geometry`; `null` on the other two, because what a designer
photographed is not knowable from here.

`true` means the pieces in the picture are put together — the toy as it stands on
a shelf. `false` means they are not, and the picture is the largest single piece
of a project that only says how its parts sit on a print bed.

It is measured off the drawn pieces, not read out of the file. A 3MF's
`<assemble>` block is written by Studio into every project whether or not
anybody assembled anything, so its presence proves nothing; and a model that is
one object with eight components carries no `assemble_item` per piece at all and
is nonetheless assembled. What settles it is whether the placed pieces overlap,
within the same 0.5 mm of fit clearance that `model.assembly` uses.

The picture is framed on those touching pieces and not on everything in the
file. Alternative pieces, spare parts and accessories sit beside a model — a
second pig's head, four spare shells, a coffee mug ten millimetres off the paws
— and a project laid out across seven plates spreads them over half a metre of
virtual bed. Framing on all of it renders the toy as a speck. This is the same
cluster `model.assembly` measures, deliberately: the thing the customer is shown
is the thing the shop quoted a size for.

#### Cost

`author_render` and `author_thumbnail` are a zip read and a re-encode —
milliseconds. `geometry` is seconds to tens of seconds, roughly linear in
triangles: 985k triangles in 5 s, 11.1M in 38 s, both at 900x900. Memory goes
the same way and is the real ceiling — 0.8 GB and 1.7 GB for those two. Like
slicing, call it from a queue.

### One plate: `plate` (1.10)

With `plate=N` the picture is of that plate, not of the model, and the cascade is
a different, shorter one:

| `stage` | Source | What it is |
|---|---|---|
| `author_plate` | `Metadata/plate_N.png`, as the plate's `thumbnail_file` in `model_settings.config` names it | The slicer's own picture of the plate as the designer laid it out, cropped to what is drawn and laid on white. Bambu Studio and Orca save one for every plate but do not always redraw it: **a thumbnail another plate shares byte for byte is skipped**, since two plates with different objects cannot look the same. Foxycat's twelve plates carry four distinct pictures, the only such file in the catalogue, and are drawn. |
| `geometry` | The meshes of the objects standing on that plate, drawn here | Every object on the plate, where it stands — no assembling, and no picking "the model" out of the extras: a plate is exactly what is on it. `assembled` is `false`. |

It exists so that a person choosing which plates of a file to print can look at
them; a plate number says nothing without a slicer open next to it. All 29 files
in the shop's catalogue carry `plate_N.png` for every plate (2026-10-07), so the
second stage is for projects saved by something else. A plate the file does not
have is the usual 422, with `plate` added to `detail`. `plate` is echoed in the
response. Timing: `author_plate` 0.1 s; `geometry` 4.5 s for a single-plate
corgi, 9.5 s for a Foxycat plate, 20 s for plate 3 of the hydra, 40 s for
plate 9 of the turtle, at 600x600 (Foxycat at 360x360).

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
| `plates[]` | list | The same numbers, per plate — `index`, `weight_g`, `print_time_sec`, `filaments[]`, `filament_changes`, `supports`, `warnings[]`, `adjustments[]`. |
| `plates_sliced` | list of int | The plates the totals are made of, when the request named some with `plates`; empty for a whole-file slice. |
| `plates[].passes` | list | A plate printed in several passes (1.13): each pass in the same shape as a plate, the plate's own figures being their sum. Empty for a plate printed in one go. |
| `journal` | list | What the slice did, step by step (1.13). See "The journal" below. |
| `engine_logs` | list | Every run of the engine, its full log compressed (1.13). See "The journal" below. |
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

**`plates[].supports`** is the grams of every support the plate prints, largest
first — one entry per support an operator snaps off by hand. Empty is a plate
printed without supports; null, that the G-code could not be read for it. Like
`filament_changes` it comes out of the G-code and nowhere else: the engine's
summary reports a plate's material as one number per filament. Support
extrusions are collected into cells 2 mm square in plan and one support level
tall — the next height at which support is printed, if within 1 mm, not the next
layer number, since a plate of objects with different layer heights interleaves
its Zs — and touching cells are one support — a tree's branches meet its trunk on the way
down, so a tree is one entry; two trees a few millimetres apart are two.
Interface layers count with the support they cap. On the reference corgi: one
tree from 0.2 to 27 mm, 0.93 g, and the count does not move between cells of
0.5 and 4 mm.

**`plates[].adjustments`** lists what had to be changed before that plate would
slice at all — moving a plate the file lays out past the bed's edge back
onto it (1.13.2), moving a purge tower back onto the bed, or into a free corner
of it when the engine found it across a part, or switching supports on (and,
if that is not enough, a raft) for a plate the engine refused because an object
would start in mid-air, or normal supports in place of tree ones the engine
refused on every run, for instance. It is reported
rather than done quietly, because it means the file describes a print this
machine cannot run as the author laid it out.

A plate that took more than one run of the engine to slice says so here too,
with the count. Some refusals come and go: the same plate, the same arguments,
refused three runs out of eight and sliced to the same weight on the other five.
The service simply runs it again — see the note on stubbornness below — and the
line is there so that a price which took six attempts to obtain does not look as
settled as one that sliced first time.

**Renumbering, and why `material` and `color` come from the file.** A file's
extruder number is a palette slot, not a print head: an assembly numbers its
colours 1..13 and prints one plate at a time, and the engine — which reads those
numbers as heads — dies three different ways on a part asking for head 5 of 4.
The service therefore renumbers each plate's colours into 1..k for the run and
numbers the results back on the way out. A slice consequently reports the colour
of the *stand-in* slot, so colour and material are read out of the file instead.
Renumbering happens only where the filaments are interchangeable (same type,
density, diameter and settings id); where they are not, the file is left alone.

**A plate printed in passes (1.13).** The engine refuses a plate whose parts
have different layer heights when it has to build a prime tower for it — that
is, when the plate loads more than one filament; a one-colour plate may mix
heights freely. Such a plate is printed in passes, one per layer height: the
same plate run once per height with only the parts of that height on it, every
part where its author put it and at the height its author gave it. The plate
then reports the sum, `plates[].passes` the passes, and `adjustments` a line
naming the heights and the parts. `max_plate_filaments` and the plate's `kind`
count the busiest **pass**, because that is what a machine loads at once.

A plate is split between models, never inside one (`plates[].models`, below),
and is refused instead — `reason` `layer_heights` — when it holds a flexi model,
when one model's own parts have different heights, or when models of different
heights reach into each other's box.

### The journal

`journal` lists what the slice did, in order, one short entry per step:
`{plate, pass, step, text, …}`. `pass` is null outside a plate printed in
passes. The steps:

| `step` | Extra fields | What it records |
|---|---|---|
| `prepare` | — | A plate's filaments renumbered onto the machine's heads; paint renumbered with them or dropped from parts painted all over in one colour |
| `split` | `reason` on a refusal | A plate printed in passes, and which objects went into each — or why it could not be |
| `run` | `exit_code`, `seconds`, `flags`, `log` | One run of the engine: what it was told beyond the defaults, and `sliced` or `refused: <the engine's sentence>` |
| `remedy` | `flags` | What the service changed before the next run, and why |

`engine_logs` holds one entry per run of the engine: `name` (the `log` a `run`
step points to, `plate-4-pass-2-run-1.log`), `plate`, `pass`, `run`, and
`gzip_base64` — the command line, both output streams and the engine's own log
file, gzip-compressed and base64-encoded. About 15–30 KB a run once encoded.
Both fields are on a refusal too (in `detail`), which is where they matter
most: the service keeps nothing after it answers, so this is the only copy.

## The `model` block

Returned identically by `/slice` and `/inspect`.

```json
{
  "model": {
    "objects": [
      { "size_x": 60.96, "size_y": 80.644, "size_z": 81.382,
        "volume_mm3": 144072.25, "facet_count": 1159988, "manifold": true,
        "object_id": 2, "plate": 1, "in_assembly": true }
    ],
    "object_count": 16,
    "total_volume_mm3": 276484.7,
    "size_x": 60.96, "size_y": 80.644, "size_z": 81.382,
    "assembly": { "size_x": 94.95, "size_y": 80.64, "size_z": 141.0, "part_count": 15 },
    "plates": [ { "index": 1, "name": null, "object_count": 3 } ],
    "layout": "assembly",
    "plates_alike": null
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
| `objects[].object_id` | int, nullable | The object's id in the file — the id `plates` and `<assemble>` name it by. Null for a file with no ids (a bare mesh). |
| `objects[].plate` | int, nullable | The plate the object stands on, 1-based. **Null means on no plate**: the author parked it off the beds, nothing prints it, and it is left out of every aggregate below and of `assembly`. A file that says nothing about plates prints as one, so its objects are on plate 1. |
| `objects[].in_assembly` | bool, nullable | Whether the object is one of the `assembly.part_count` pieces. False marks a loose extra that is still printed; null, that there is no assembly to belong to. |
| `object_count` | int | Objects on a plate — `objects[]` minus the ones with `plate: null`. |
| `total_volume_mm3` | number, nullable | Every printed piece's material summed. |
| `size_x` / `size_y` / `size_z` | number, nullable | The **largest single object**, as a convenience for callers that want one number without sorting the list. Not the model. |
| `assembly` | object, **nullable** | The model with its pieces put together. See below. |
| `plates[]` | list | The plates as the file lays them out, before anything is sliced: `index`, the author's `name` for the plate (nullable, free text), `object_count`, `kind`, `copies` and `models[]` — see below. Empty for a file with no plate list. |
| `layout` | string, nullable | How the plates relate — see below: `single`, `assembly` or `separate`. Null when the file could not be read for it. |
| `plates_alike` | bool, nullable | For `separate` only: every plate holds the same kind of model. **A hint, not an answer** — see below. |

**`objects[]` is a list and not a box on purpose.** Reporting one bounding box
over all of them would answer a question nobody asks: the pieces are printed
apart and packed apart, and the extent of their arrangement on the print bed is
an artefact of how somebody laid them out. Their coordinates are local to each
object and carry no layout, so they cannot be unioned into anything meaningful
anyway.

### `layout` — one model across the plates, or a model on each

Three answers, and only the first two are settled by geometry:

| `layout` | Means | Catalogue example |
|---|---|---|
| `single` | One plate. | every `*AMS.3mf` |
| `assembly` | Pieces standing on **different** plates glue into one model: the `assembly` group spans more than one plate. | the hydra — 31 glued pieces over 7 plates |
| `separate` | Every plate holds a model, or models, of its own: the glued group stays on one plate, or there is none. | Foxycat — 12 plates, one cat on each |

**`separate` is where geometry stops.** Twelve cats on twelve plates are either
twelve variants to pick one from or a set of twelve to print, and the file reads
the same either way: that is the author's intent, not a fact about the shapes.
So the service does not decide it. It offers two hints and leaves the choice to
whoever is selling or buying the print:

- `plates_alike` — true when every plate shares a part name with every other
  (each Foxycat plate holds a `cat body.stl` with its eyes); false when they
  share none;
- `plates[].name` — the author's label for each plate (`AMS`, `1C Keyring -
  Mini`), as written.

A file whose pieces are a real assembly but whose `<assemble>` block was never
filled in also reads as `separate`, with `plates_alike: false` — the split corgi
in the reference catalogue is one. The file does not say how its pieces go
together, so neither can the answer.

### `plates[].models` — what comes off a plate as one thing

Each plate's objects are placed where the file prints them, split into their
shells (the connected pieces of their meshes), and the shells related pairwise:

| Two shells… | Are |
|---|---|
| touch or pass through each other (gap under 0.1 mm) | one model — an AMS model's colour regions, an eye sunk into a head |
| interlock — boxes overlapping — with 0.1 to 1 mm of air everywhere | one **flexi** model — a print-in-place joint |
| anything else | separate models |

Both limits are the shop's (flexi is a gap of 0.1–1 mm, AMS a gap of zero or
less). The reference flexi bear's two joints keep 0.170 and 0.178 mm.

| Field | Type | Meaning |
|---|---|---|
| `plates[].kind` | string | `ams` — the plate's objects use more than one palette slot, so it prints with a prime tower; `simple` — one colour, no tower. Read from the file, no slice needed. |
| `plates[].copies` | string, nullable | `multi` — several models, all the same shape (same shells, triangles and material); `one` — a single model, or several different ones. |
| `plates[].models[]` | list, nullable | One entry per thing that comes off the plate in one piece: `kind` (`part`, `ams` or `flexi`), `object_ids` it is made of, and `shells`. |

Distances are vertex to triangle, both ways round, which finds the gap between
any face and any corner facing it — the shape a clearance has — even on a coarse
CAD mesh whose flat faces carry no vertices. A part pushed into another is caught
separately, by testing a few of its vertices for being inside the other. This
reads the meshes, so it costs seconds: 11–13 s on the corgi and the bear, most
of it parsing.

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

**Left out of the model is not left off the plate.** In every file of the
reference catalogue the extras stand on a plate like any other piece — the
hydra's nine are printed, weighed and timed with the rest. They are the objects
with `in_assembly: false` and a `plate`; only `plate: null` means not printed.
Pieces off every plate never enter the grouping at all.

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
| `422` | The slice failed, a render found nothing to draw, **or** a request field failed validation | See below |

A failed slice returns `detail` as an object: `message`, `reason`, `exit_code`,
and `log` — the tail of **both** output streams. Both, because the engine writes
progress to stdout and the reason for a refusal to stderr, and reading only the
first is how `return -51` looked like a mystery for a week. Where the engine
wrote its own verdict, that sentence is in `message` along with the plate it was
on.

**`reason` is present on every refusal and null on most of them.** A name means
the refusal is a fact about the model, worth storing against the product and
worth acting on; null means the slicer simply said no, and only the sentence and
the log describe it. Three names exist today:

| `reason` | What it means | What the caller can do |
|---|---|---|
| `off_bed` | The print does not fit the bed of the machine it was sliced for — a part is larger than the bed, or the plate is laid out past its edge | Nothing that involves trying again: a different printer, or a different file |
| `conflict` | Two toolpaths cross. Either the prime tower crosses a part both where the file puts it and in the freest corner of the bed — the service tried that corner first — or the file's own parts cross each other | Nothing that involves trying again either: a looser layout with a free corner for the tower, or a smaller model |
| `layer_heights` (1.13) | A plate loading several filaments has parts of different layer heights, which the engine refuses, and it cannot be printed in passes: it holds a flexi model, one model's own parts differ in height, or parts of different heights reach into each other. The message names the plate and each part with its height | One layer height for every part of the plate, or the parts of each height on a plate of their own |

`message` says which of the two it is, and in millimetres: `part 1 is 68.837 mm
on y against 60.0`, or `no part is larger than the bed (270.0 × 270.0 × 270.05
mm), so the plate is laid out past its edge`. Parts are numbered by their
position in the `objects` list of the `model` block. Sides are compared as the
file states them: the plate is printed as its author laid it out, so a part that
would fit rotated does not fit.

**A 422 with no `reason` means the engine refused the same plate seven times**,
not once. Past the fixes it has names for, the service runs a refused plate again
— up to six more times — because some of these refusals are intermittent. Two
consequences worth planning for: a slice that fails now takes several times
longer to say so, and a timeout on the caller's side has to allow for it. A file
that cannot be sliced at all still cannot be sliced; it only takes longer to hear
that. A **named** refusal is the exception and arrives at once: `off_bed` and
`conflict` are deterministic, so repeating them would only spend slices to
reach the same word. `layer_heights` is found before the engine runs at all.

A refusal's `detail` also carries `journal` and `engine_logs` (1.13) — see
"The journal".

A render that produces nothing returns `detail` as `{"message", "stage", "plate"}`, where
`message` lists what each stage was asked and what it said. It takes a file with
no pictures in it **and** no geometry we could read, so in practice it means the
upload is not a 3MF at all. An out-of-range `width` or `height` returns `detail`
as a plain string naming the bounds.

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
- **1.5** runs a refused plate again instead of giving up on it, and says in
  `plates[].adjustments` when it had to. No field changed shape; what changed is
  how long a failure takes to arrive.
- **1.6** added `reason` to the 422 body and the first name in it, `off_bed`,
  which also arrives without the retries of 1.5. Callers that only read `message`
  are unaffected.

- **1.7** added `POST /render`. Nothing existing changed shape; the service
  gained `numpy` and `pillow`, and the image grew by what those weigh.
- **1.8** applies the file's own scale to `model`. A project saved with an
  object scaled on the bed keeps the mesh at full size and the factor on the
  build item; the slice always honoured it, `objects[].size_*`,
  `volume_mm3` and `assembly` did not. No field changed shape; a file whose
  objects stand at 100% answers exactly as before, one whose objects do not
  now answers with the size of the print rather than of the drawing.
- **1.9** added the second name in `reason`, `conflict`, and one more thing
  `plates[].adjustments` can say: that the prime tower was moved into a corner
  of the bed. A one-filament project whose flush matrix is a single cell no
  longer crashes the engine on a multi-nozzle machine; its weight is what the
  same file always gave on a one-nozzle one. No field changed shape.

- **1.10** says what stands on each plate: `objects[].object_id`, `plate` and
  `in_assembly`; `model.plates[]` with names, `kind`, `copies` and `models[]`;
  `model.layout` and `plates_alike`; `plates[].supports`; the `plates`
  request parameter of the slice; and `plate` on `/render`, a picture of one
  plate. Two fixes move numbers. The assembly ranks its groups by
  material in the file's frame again — 1.8 ranked by scaled material, which
  turned the hydra into one piece (part_count 1, 69.7 × 105.6 × 109.3 mm) instead
  of 31 (98.2 × 107.5 × 183.9) — and a file that lists an object several times
  in `<assemble>` counts it once per instance. A rotated assembly piece is now
  turned the way the 3MF specification turns it; every matrix in the reference
  catalogue is a translation, so no size there moved. Objects on no plate are
  left out of `object_count`, `total_volume_mm3`, `size_*` and `assembly`; none
  of the 29 reference files has one.
- **1.11** makes sixteen refused plates of one author's catalogue slice. A
  Bambu project's filament-to-head assignment is pinned (filament k on head k)
  instead of left to the engine, which corrupted its heap re-deciding it on a
  four-head machine; an object's own brim setting is overwritten with the
  run's, so `brim=false` now holds for objects that brim themselves — this
  **moves numbers**: one reference file with a per-object brim on every plate
  went from 138.46 g to 137.28 g. And `plates[].adjustments` can say two more
  things: supports were switched on for a plate the engine refused over an
  object starting in mid-air, and a two-layer raft was added when supports
  alone were not enough. Both add the grams the print would really take. No
  field changed shape.
- **1.12** adds one more thing `plates[].adjustments` can say: tree supports
  refused the plate on every run, and it was sliced with normal supports
  instead. Only a plate that refused all retries is changed this way, so no
  number a caller already had moves. No field changed shape.
- **1.13** reads the paint on a file's meshes. Paint is renumbered with the
  base when a plate's filaments are moved onto the machine's heads, and a part
  painted all over in one colour is treated as a part of that colour. Before,
  a one-colour plate could reach the engine asking for two filaments, get a
  prime tower and be refused over layer heights it was allowed to have — four
  plates of one author's catalogue. This **moves numbers** where a file paints
  whole parts over a different base: a plate that loaded a filament it never
  printed loses its tower (one reference file, plate 3). A plate that does
  need a tower and mixes layer heights is printed in passes
  (`plates[].passes`) or refused with the third `reason`, `layer_heights`.
  Every response and every refusal now carries `journal` and `engine_logs`.
  **Use 1.13.1**: the 1.13.0 image was published without `paint.py` and does
  not start.
- **1.13.2** puts back on the bed a plate the file lays out past its edge —
  the whole plate, by the least distance, with its tower — when the plate as a
  whole fits; `plates[].adjustments` says by how much. A plate that does not
  fit is refused with `off_bed` at once, where `-52` (some object only partly
  on the bed) used to be retried six times and refused with no name. No field
  changed shape; only plates that used to be refused are affected.
- **1.13.3** slices an object of a single part in that part's colour, as the
  slicer the file was made in shows it; the engine used the object's colour
  instead, so a plate could come back in one colour where the author put
  several. This **moves colours, not weights**: `filaments[]` and
  `plates[].filaments` of eight reference files name different slots, and
  some name more of them. A plate's colours are now those of the parts that
  print — not of negative parts or modifiers — which stops 1.13 from refusing
  a one-colour plate as a mixed-height tower plate.
- **1.13.4** slices a plate whose file has a variable layer height and tree
  supports in the default style, which the engine refused with `-51` on every
  run. The trees are drawn as `tree_hybrid` — what Bambu Studio, where such
  files are made, picks for that combination itself — and
  `plates[].adjustments` says so. No field changed shape; only plates that
  used to be refused are affected.

Fields are added, not repurposed. The one thing a caller must handle is the
difference between a field being **absent** — an older service that has never
heard of the question — and being **present and null**, which is this service
saying the file does not answer it. Those are different states and they call for
different behaviour; `assembly` is the field where it matters today.

**Pin the tag, never `:latest`.** Grams depend on the engine version, and a
silently updated image changes what everything downstream of it costs.
