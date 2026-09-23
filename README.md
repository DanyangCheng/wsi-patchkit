# wsi-patchkit

`wsi-patchkit` is a small, backend-neutral library for reading patches from
whole-slide images (WSIs). Version 0.1 provides:

- TIFF and optional OpenSlide readers with a common coordinate contract;
- physical-resolution (MPP) metadata and level selection;
- target-MPP-aligned, boundary-padded patch reads;
- deterministic grid, random, indexed, and tissue-filtered sampling;
- lazy patch streams;
- an optional PyTorch `IterableDataset` adapter;
- an optional browser viewer with IIIF tiles, zooming, and panning.

Dataset manifests, labels, augmentation, model execution, and prediction output
formats intentionally remain application concerns.

## Install

The project is managed with `uv`:

```bash
uv sync
```

Install the optional OpenSlide adapter when the native OpenSlide library is
already available:

```bash
uv sync --extra openslide
```

For platforms supported by the `openslide-bin` wheel, the convenience extra is:

```bash
uv sync --extra openslide-binary
```

PyTorch integration is isolated in another extra:

```bash
uv sync --extra torch
```

## Browser WSI viewer

Install the optional web dependencies:

```bash
uv sync --extra web
```

Start the viewer by explicitly registering one or more public slide IDs:

```bash
uv run wsi-patchkit-viewer \
  --slide case-001=/data/slides/case-001.svs \
  --slide case-002=/data/slides/case-002.tif \
  --crop-output-dir /data/crops \
  --reader-pool-size 4
```

To browse every supported WSI in a folder (including subfolders), register the
directory instead. The viewer's searchable slide menu scrolls when the list is
long:

```bash
uv run wsi-patchkit-viewer --slide-dir /data/slides
```

`--slide-dir` is repeatable and can be combined with `--slide`. Recognized file
extensions are `.svs`, `.tif`, `.tiff`, `.ndpi`, `.mrxs`, `.scn`, `.vms`,
`.vmu`, `.bif`, and `.qptiff`.
The slide picker shows discovered files under their relative folders; folders
can be expanded, and search matches both folder names and slide names.

The slide picker also has an **Upload slide** button. Uploaded single-file WSIs
are validated, added to the list immediately, and opened after upload. Files are
saved in `./uploads` by default; use `--upload-dir /path/to/uploads` to choose a
persistent location. Uploads are limited to 32 GiB by default; set
`--max-upload-gb N` to change the limit. The viewer can start without registered
slides and accept its first slide through the browser. Supported upload
extensions are `.svs`,
`.tif`, `.tiff`, `.btf`, `.btiff`, `.ndpi`, `.qptiff`, `.scn`, and `.bif`.

Then open <http://127.0.0.1:8000>. The viewer supports mouse-wheel and pinch
zooming, drag panning, double-click zooming, a navigator, level-0 coordinates,
and an MPP-aware scale bar. TIFF files use the bundled tifffile reader; other
formats are routed to the optional OpenSlide reader.

### Docker Compose

Create the slide directory, then build and start the viewer. WSI files can be
added before or after startup:

```bash
mkdir -p data/slides data/overlays
docker compose up --build -d
```

Open <http://127.0.0.1:8000>. Compose mounts slides and overlays read-only and
stores crops and browser uploads in persistent volumes. Stop the
service with `docker compose down`; add `--volumes` only when you also want to
delete saved crops and uploaded slides.

The slide directory is rescanned when the browser polls the slide list (every
5 seconds). New files appear after two scans with unchanged size and modification
time, so copy large files under a temporary name and rename them after the copy
finishes. Removed files disappear from the list. The viewer can start with an
empty slide directory and will open the first slide when one appears.

Host paths, the published port, and worker counts can be changed without
editing `compose.yaml`:

```bash
WSI_SLIDES_DIR=/mnt/wsi \
WSI_OVERLAYS_DIR=/mnt/predictions \
PATCHKIT_PORT=8080 \
PATCHKIT_READER_POOL_SIZE=8 \
docker compose up --build -d
```

To run the image without Compose:

```bash
docker build -t wsi-patchkit .
docker run --rm -p 8000:8000 \
  -v /path/to/slides:/data/slides:ro \
  -v patchkit-crops:/data/crops \
  wsi-patchkit
```

Select **矩形裁剪** to place a rectangle on the slide, enter its exact level-0
`x`, `y`, width, and height, choose a pyramid level, and save a PNG or JPEG on
the server. Coordinates and dimensions are pixels at the selected level; crops
are read directly from that level without rescaling, at its native resolution.
The server stores them in
`--crop-output-dir` (the default is `./crops`) and never overwrites an existing
file. The viewer remembers the most recently entered crop width and height when
switching slides or reloading the page. Saving submits work to a background
queue, so another crop can be positioned and submitted while earlier jobs are
still running. One crop worker is used by default; use `--crop-workers N` to
change the background concurrency.

Applications can embed the viewer server instead of using the CLI:

```python
from wsi_patchkit.web import SlideSource, create_app

app = create_app(
    {
        "case-001": SlideSource("/data/slides/case-001.svs"),
        # Supply an override when the file has no reliable MPP metadata.
        "case-002": SlideSource("/data/slides/case-002.tif", source_mpp=0.5),
    },
    crop_output_dir="/data/crops",
)
```

### Indexed prediction overlays

The viewer can place one or more partial indexed prediction rasters in WSI
physical coordinates. Each overlay uses a caller-owned palette, and an optional
binary coverage TIFF distinguishes a valid class (including class zero) from a
region that was never predicted. See the complete
[Segment prediction-mask contract](docs/SEGMENT_PREDICTION_MASK_CONTRACT.md).

Register a manifest from the CLI:

```bash
uv run wsi-patchkit-viewer \
  --slide case-001=/data/case-001.svs \
  --overlay /data/case-001/prediction.json
```

For a shared downstream output directory, `patchkit-web` can discover only
the manifests whose `slide_id` is among the served slides. This supports the
Segment layout `/home/cdy/Projects/Segment/output/{slide_id}/`:

```bash
uv run patchkit-web /data/slides \
  --overlay-root /home/cdy/Projects/Segment/output
```

It recursively locates `prediction.json`, then uses its declared `slide_id`
and `overlay_id`; directory names are not treated as metadata. When slide IDs
were discovered from filenames, a manifest may use the unique filename stem
(for example `case-001` for `case-001.svs`). Results for unserved slides are
ignored, while duplicate `(slide_id, overlay_id)` pairs are rejected.
`wsi-patchkit-viewer --slide-dir /data/slides` remains supported.

Or register it programmatically:

```python
from wsi_patchkit.web import create_app

app = create_app(
    {"case-001": "/data/case-001.svs"},
    overlays={
        "case-001": {
            "prediction": "/data/case-001/prediction.json",
        }
    },
)
```

Overlay tiles are transparent PNGs served separately from the WSI. The browser
can toggle each layer, adjust opacity, change class colors, and show or hide
classes individually or with Select All and Invert Selection controls.
Patchkit never infers class semantics, background IDs, coverage, or registration
from the mask pixels.

The server exposes a conservative subset of IIIF Image API 3 at
`/iiif/3/{slide_id}`. Paths are registered server-side and are never accepted
from request URLs. Tiles are rendered from the closest suitable native pyramid
level and returned with an ETag and private cache headers. By default, four
independent readers process different tiles concurrently; tune
`--reader-pool-size` to match available CPU, memory, and storage throughput.

## Coordinate contract

Sampler coordinates live on a virtual canvas at `target_mpp`. For example, an
`x` coordinate of 100 at 0.5 MPP means 50 micrometres from the level-0 origin.
Reader-level `read_region()` calls use OpenSlide-compatible semantics: the
location is in level-0 pixels and the requested size is in pixels of the chosen
pyramid level. `read_aligned_patch()` performs the conversion explicitly. All
rectangles use half-open `(x0, y0, x1, y1)` coverage semantics.

`read_aligned_patch()` keeps the original array-only API. Use
`read_aligned_patch_result()` when a downstream application also needs the
geometric coverage mask created by boundary padding:

```python
result = read_aligned_patch_result(reader, request, level_policy="finer")
image = result.image
geometry_valid = result.valid_mask
```

`geometry_valid` reports only pixels backed by the WSI. It has no tissue, ROI,
or annotation meaning. `level_policy="nearest"` remains the default;
`"finer"` avoids selecting a native level coarser than the target MPP when a
finer level is available. Pass `interpolation="area"` when reducing an RGB
region with area averaging; label and indexed-mask reads should continue to use
`interpolation="nearest"` with `color_mode="native"`.

## Sliding-window inference

```python
from wsi_patchkit import (
    GridPatchRequestSampler,
    PatchStream,
    SlideSpec,
    TiffReader,
)

reader = TiffReader()
metadata = reader.metadata("slide.tif", source_mpp=0.25)
slide = SlideSpec.from_metadata(metadata, target_mpp=0.5)
requests = GridPatchRequestSampler(patch_size=512, stride=256).sample([slide])

try:
    for patch in PatchStream(reader, requests):
        # patch.image is uint8 HWC; patch.request contains placement metadata.
        prediction = model(patch.image)
finally:
    reader.close()
```

The grid sampler aligns its final window with the far edge, so an inference
canvas is fully covered. Use `edge="drop"` if incomplete boundary regions should
be omitted instead.

## Reproducible random training samples

```python
from wsi_patchkit import RandomPatchRequestSampler, SamplingContext, SlideSpec

slides = [
    SlideSpec("a.svs", canvas_size=(120_000, 80_000), target_mpp=(0.5, 0.5)),
    SlideSpec("b.svs", canvas_size=(90_000, 70_000), target_mpp=(0.5, 0.5)),
]
request_sampler = RandomPatchRequestSampler(
    num_samples=20_000, patch_size=512, seed=2026
)
requests = request_sampler.sample(slides, context=SamplingContext(epoch=3))
```

Random samples are derived from `(seed, epoch, global sample index)`. Changing
DataLoader worker count does not change the global sample set; each worker only
receives its deterministic shard.

## Tissue filtering

```python
from wsi_patchkit import TissueFilter, TissueMask

masks = {
    "a.svs": TissueMask(mask_array, canvas_size=(120_000, 80_000)),
}
filtered = TissueFilter(masks, minimum_fraction=0.05).filter(requests)
```

The tissue mask may be low resolution; it is mapped over the corresponding
target-MPP canvas. Version 0.1 consumes caller-provided masks and does not impose
a tissue-detection algorithm.

For distributed training, use `TissueRandomPatchRequestSampler` instead of filtering an
already-sharded request stream. It retries deterministically until every global
sample meets the threshold, so `pad` and `drop` shard policies retain equal
lengths:

```python
from wsi_patchkit import TissueRandomPatchRequestSampler

request_sampler = TissueRandomPatchRequestSampler(
    masks,
    num_samples=20_000,
    patch_size=512,
    minimum_fraction=0.05,
    max_attempts=100,
    seed=2026,
)
```

## Indexed sampling

`IndexedPatchRequestSampler` samples precomputed `PatchRequest` objects. Index serialization
is deliberately left to the caller in v0.1 so projects can use NPZ, Parquet, or
a database without coupling the core library to one storage format. Its input
only needs `__len__()` and integer `__getitem__()`, allowing lazy wrappers over
memory-mapped arrays and database-backed coordinate indexes.

## Downstream reader adapters

Applications may adapt `SlideReader` to their own dataset backend without
depending on patchkit task types. `metadata()` supplies level dimensions, MPP,
and optional `LevelInfo.pixel_format`; `read_region()` always accepts a level-0
location and a size in the selected level's pixels. A typical adapter exposes
those methods through the application's own `info/read_region/close` protocol.

Coordinate indexes remain application-owned. An adapter converts each index row
from its declared coordinate space into a `PatchRequest`, then uses
`read_aligned_patch()` or `read_aligned_patch_result()`. Labels, masks, split
membership, and target encoding stay in the downstream application.

## PyTorch adapter

```python
from wsi_patchkit import RandomPatchRequestSampler, SlideSpec
from wsi_patchkit.io import TiffReader
from wsi_patchkit.torch import WSIPatchIterableDataset

dataset = WSIPatchIterableDataset(
    slides,
    request_sampler=RandomPatchRequestSampler(
        num_samples=20_000, patch_size=512, seed=2026
    ),
    reader_factory=TiffReader,
)
```

Each worker creates and closes its own reader. Items contain a float32 CHW
`image` tensor in `[0, 1]` plus the original `PatchRequest`.

`PatchRequestSampler` is a domain-level request generator, not a PyTorch
`torch.utils.data.Sampler`: the latter yields integer indices to a map-style
dataset, while this adapter is an `IterableDataset` that generates and shards
`PatchRequest` values inside each worker.

### Distributed training and checkpoints

`WSIPatchIterableDataset` pads the final global sampling round by default, so
every distributed rank and DataLoader worker receives the same number of
requests. Padding repeats deterministic requests from the start of the epoch.
Pass `even_shards="drop"` to discard the short final round, or
`even_shards="none"` to retain the historical uneven behaviour.

Call `dataset.set_epoch(epoch)` before each new DataLoader iteration. The epoch
is stored in shared memory, so the new random stream is visible when PyTorch
uses `persistent_workers=True`.

The dataset exposes `state_dict()` / `load_state_dict()` and
`SamplingContext` exposes `state_dict()` / `from_state_dict()` for saving the
epoch and a virtual global resume cursor. Recreate the same slides and sampler
configuration before restoring the state. A training loop must set the cursor
itself at a synchronized optimizer-step boundary; worker prefetching prevents
a data iterator from knowing which prefetched samples were actually committed
by the optimizer.

`TissueFilter` deliberately remains a post-sampling filter. It can produce an
unequal number of samples per shard, so the dataset rejects it together with
the default equal-shard modes. Use `even_shards="none"` for exploratory runs or
provide a tissue-aware sampler that generates a fixed number of valid requests.

## Development

```bash
uv sync --all-extras
uv run ruff check .
uv run pytest --cov
uv build
```
