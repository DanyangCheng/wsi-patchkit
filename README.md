# wsi-patchkit

`wsi-patchkit` is a small, backend-neutral library for reading patches from
whole-slide images (WSIs). Version 0.2 provides:

- ordinary image (Pillow), TIFF, KFB, and OpenSlide readers with a common coordinate contract;
- physical-resolution (MPP) metadata and level selection;
- strict continuous physical requests, output grids, and IO-free sampling plans;
- exact synchronous nearest/bilinear/bicubic/area reads with physical-center valid masks;
- target-MPP-aligned, boundary-padded patch reads;
- deterministic grid, random, indexed, and tissue-filtered sampling;
- lazy patch streams;
- an optional PyTorch `IterableDataset` adapter;
- an optional browser viewer with IIIF tiles, zooming, and panning.

Dataset manifests, labels, augmentation, model execution, and prediction output
formats intentionally remain application concerns.

## Physical sampling plans

Phase 1 exposes `PhysicalPatchRequest`, `OutputGrid`, `SamplingLimits`,
`SamplingPlan`, and `plan_physical_read`. Planning preserves negative and
fractional origins, selects the native level, and budgets one native region
without reading or allocating pixel arrays. Exact synchronous reads support
nearest/bilinear/bicubic/area with constant or reflect padding. Complete
physical result streams are scheduled for phase 4.

```python
from wsi_patchkit import ImageReader, PhysicalPatchRequest, SamplingLimits, plan_physical_read

request = PhysicalPatchRequest(
    "slide.png",
    origin_um=(0.125, -0.5),
    output_size=(256, 128),  # width, height
    target_mpp=(0.5, 0.75),
    source_mpp=(0.25, 0.5),
    level_policy="finer",   # an explicit level takes precedence
    interpolation="bilinear",
    fill_value=(255, 255, 255),
)
with ImageReader() as reader:
    metadata = reader.metadata(request.slide, source_mpp=request.source_mpp)
    plan = plan_physical_read(metadata, request, limits=SamplingLimits())

print(plan.source_level, plan.read_location, plan.read_size)
print(plan.grid.output_to_slide_um)  # [col, row, 1] -> pixel center in micrometres
```

Supplied metadata must match the slide path and any `source_mpp` override.
Defaults cap output pixels at 16,777,216 and native read pixels at 67,108,864,
including interpolation neighbors and unused pixels inside the read rectangle.
Set either limit to `None` independently to disable it. `0` is invalid. The
planner retains zero-weight point-kernel neighbors in its budget. Constant
planning uses scalar searches; reflect planning uses at most O(width + height)
scalar work and O(1) auxiliary memory. Neither allocates an HW coordinate array.

The new request rejects bool, fractional or floating-point integer fields,
nonfinite coordinates, and nonpositive MPP. Physical coordinates and MPP may
be fractional; integer fields accept Python and NumPy integers. Native mode
requires nearest. Area planning requires both axes to avoid enlargement and
follows physical footprint weights; it does not invoke the legacy BOX resize.
See the [sampling contract](docs/PHYSICAL_SAMPLING_CONTRACT.md) for details.

Use `PhysicalPatchRequest.from_legacy(request)` to convert an old request's
origin to `(x * target_mpp_x, y * target_mpp_y)`. Previously truncated fractions
cannot be recovered. Legacy fractional/bool integer inputs and bool MPP emit
`FutureWarning` in 0.2.0 and will be rejected in 0.3.0. Valid integer inputs
and old helper pixel results remain compatible.

`SlideSpec.from_metadata(..., canvas_rounding="ceil")` propagates its strategy
through grid, random, and tissue samplers into `PatchRequest`. Old helpers use
the request's strategy, with an optional explicit `canvas_rounding` override;
`virtual_canvas_size` accepts the same option. The default `"round"` retains
ties-to-even behavior. Canvas coverage and exact physical-center validity are
separate contracts.

### Exact synchronous reads

```python
from wsi_patchkit import ImageReader, PhysicalPatchRequest, read_physical_patch_result

request = PhysicalPatchRequest(
    "slide.png",
    origin_um=(0.125, -0.5),
    output_size=(256, 128),
    target_mpp=(0.5, 0.75),
    source_mpp=(0.25, 0.5),
    interpolation="bilinear",
    padding_mode="constant",
    fill_value=(255, 255, 255),
)
with ImageReader() as reader:
    result = read_physical_patch_result(reader, request)

print(result.image.shape, result.valid_mask.shape)  # (128, 256, 3), (128, 256)
print(result.source_level, result.grid.output_to_slide_um)
# Arrays remain valid after the reader closes.
```

Centers follow `origin_um + (col + 0.5, row + 0.5) * target_mpp`.
`valid_mask` is HW bool and tests those centers against the level-0 physical
half-open extent. Right/bottom boundary centers are invalid. An invalid center
receives the entire constant fill; interpolation neighbors of valid centers
clamp at native edges. A fully outside request makes no pixel IO calls.

Nearest chooses `floor(s + 0.5)` in native center coordinates; bilinear uses
float64 weights and `clip(floor(value + 0.5), 0, 255)` quantization. RGB requires
uint8, expands grayscale, and composites RGBA on white before sampling.
`color_mode="native"` requires nearest and preserves bool/integer dtype and
channel count (2D data becomes HWC with one channel). Native fills must fit the
source dtype; fully outside native requests require reliable `PixelFormat`
metadata. Unsupported shape, dtype, or metadata/region mismatch raises an error.

The same `SamplingLimits` apply to synchronous reads before output allocation
and pixel IO. Supplied metadata can avoid repeated metadata queries. An optional
`plan=plan` is verified against metadata/request/limits before IO; when planning
with custom limits, pass `limits=plan.limits` to the read too. Results contain
`request/image/valid_mask/grid/source_level/plan`, and both arrays own their data.
The synchronous reader is borrowed and remains caller-owned on success or error.

RGB sampling supports:

| Kernel   | Sampling rule                                                                                     |
| -------- | ------------------------------------------------------------------------------------------------- |
| nearest  | Center index `floor(s + 0.5)`                                                                   |
| bilinear | Two neighbors per axis, float64 separable weights                                                 |
| bicubic  | Four neighbors per axis, Keys cubic with `a=-0.5`, no added antialias filter                    |
| area     | Physical footprint overlap weights; both target MPP axes must be at least the selected native MPP |

Each kernel accepts `padding_mode="constant"` or `"reflect"`. Reflect maps
each native kernel neighbor on the entire original axis, with NumPy's
nonrepeating endpoint rule; a one-pixel axis repeats its sole pixel. Entirely
outside reflect requests may read the mapped native region, but their center
masks remain false. Far origins and very wide area footprints use integer
period mapping and bulk cell counts, so their distance does not enlarge IO.
Native mode still permits only nearest and requires reliable pixel-format
metadata for completely outside requests under either padding mode.

Area uses the output pixel's physical rectangle, includes all overlapping
native cells, and preserves fractional phase. Constant fills invalid centers
as whole pixels; for valid centers its footprint beyond native edges clamps
to edge pixels. Reflect folds each footprint cell. Coverage accumulates in
float64 before normalization and uint8 quantization. Very large counts are
scaled before multiplication to keep values finite. If floating precision
collapses a footprint, planning rejects it before IO. All modes share the same
output/read limits, including the actual single-region neighbor rectangle.

The existing `read_aligned_patch*`, `PatchStream`, and torch adapter keep their
legacy sampling path; physical result streams arrive in phase 4.

## Install

The project is managed with `uv`:

```bash
uv sync
```

PyTorch integration is isolated in another extra:

```bash
uv sync --extra torch
```

## KFB to TIFF conversion

Convert a KFB file to tiled BigTIFF with its native pyramid and MPP metadata:

```bash
uv run --extra tiff-codecs kfb-to-tiff /data/slides/sample.kfb /data/tiff/sample.tiff --workers 4
```

Compatible JPEG tiles are copied without decoding or re-encoding. Other tiles
are converted using independent worker readers with a bounded tile queue.
Use `--compression deflate` for lossless decoded RGB pixels, or provide input
and output directories with `--recursive --jobs 2` for parallel batch conversion.
See [KFB conversion usage and performance options](docs/KFB_TO_TIFF.md).

## Browser WSI viewer

Install the optional web dependencies:

```bash
uv sync --extra web
```

The CLI uses a SQLite user database and server-side sessions, and serves HTTP by
default. The first startup requires an administrator password hash. Set
`PATCHKIT_AUTH_DB` to choose the database location (default `data/auth.sqlite3`).
Users and unexpired sessions survive restarts. Users apply at `/register`;
administrators review accounts and assign file groups at `/admin/users`. A group
is a top-level directory under a scanned slide root. Ordinary users see only their
authorized groups and their own uploads and crops. Password changes revoke all
sessions for that user. See
[administrator login and HTTPS deployment](docs/SECURITY_DEPLOYMENT.md) to generate
the password hash, configure cookies, and deploy Caddy. For local development
without authentication, explicitly add `--no-auth` to the commands below.

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
extensions are `.svs`, `.tif`, `.tiff`, `.kfb`, `.ndpi`, `.mrxs`, `.scn`,
`.vms`, `.vmu`, `.bif`, `.qptiff`, `.jpg`, `.jpeg`, `.png`, `.bmp`, and `.webp`.
The slide picker shows discovered files under their relative folders; folders
can be expanded, and search matches both folder names and slide names.

The slide picker also has an **Upload slide** button. Select multiple single-file
WSIs to upload them sequentially. Each successful upload is validated and added
to the list, and the last successful slide opens when the batch finishes.
Files are saved in `./uploads` by default. Use `--upload-dir /path/to/uploads`
to choose a persistent location. Uploads are limited to 32 GiB by default; set
`--max-upload-gb N` to change the limit. The viewer can start without registered
slides and accept its first slide through the browser. Supported upload
extensions are `.svs`, `.tif`, `.tiff`, `.btf`, `.btiff`, `.kfb`, `.ndpi`,
`.qptiff`, `.scn`, `.bif`, `.jpg`, `.jpeg`, `.png`, `.bmp`, and `.webp`.

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000). The viewer supports mouse-wheel and pinch
zooming, drag panning, double-click zooming, a navigator, level-0 coordinates,
and an MPP-aware scale bar. TIFF files use the bundled tifffile reader, `.kfb`
files use `kfbslide`, ordinary images use Pillow, and other WSI formats use
OpenSlide.

### Personal workspace

Open **个人管理** in the viewer toolbar, or visit `/personal`. The workspace
includes a dashboard, slide library, resumable upload queue, crop tasks, result
library, recycle bin, and account settings. See the
[personal workspace guide](docs/PERSONAL_WORKSPACE.md) for the full workflow.

Each account can organize its accessible slides with private projects, tags and
notes. Open **裁剪任务** in the slide viewer to select rectangle or polygon ROIs
on the slide, preview patch grids, and reuse crop templates. The personal result
library exports images with coordinate and resolution metadata. Crop in native-level
pixels or set a target MPP when the slide's physical resolution is known.
Account settings allow profile updates, password changes and signing out all
devices. Password changes require the current password and revoke every session.

### Ordinary images

`AutoSlideReader` also reads JPEG, PNG, BMP, and WebP. They expose a single
native level (`level=0`, `downsample=(1, 1)`) and work with the viewer's virtual
zoom pyramid and server-side crops. EXIF orientation is applied consistently to
metadata dimensions, displayed pixels, and crop coordinates. Animated WebP uses
its first frame.

The reader supports eight-bit grayscale, RGB, and RGBA. Bilevel images become
grayscale; palette, grayscale-alpha, and CMYK images become RGB or RGBA as
appropriate. Transparency is retained by the reader and composited onto white
by the viewer. Higher-bit-depth images are rejected rather than silently reduced.

Ordinary images have no MPP by default; display DPI is not treated as specimen
resolution. Browsing and cropping work without MPP, while physical scale bars,
physical-coordinate overlays, and target-MPP patch sampling require a known
`source_mpp`:

```python
from wsi_patchkit import AutoSlideReader, SlideSpec

with AutoSlideReader() as reader:
    metadata = reader.metadata("image.png", source_mpp=0.5)
    slide = SlideSpec.from_metadata(metadata, target_mpp=0.5)
    region = reader.read_region("image.png", (10, 20), 0, (128, 128))
```

For the viewer, pass `--source-mpp 0.25` (or `--source-mpp 0.25,0.5` for
an X/Y pair). This overrides embedded MPP for registered, scanned, and uploaded
images. All images in that invocation should share the specified resolution.
For images with detection overlays, use the actual source resolution matching
`provenance.source_mpp` in the prediction manifest. The Python API accepts
`create_app(..., source_mpp=0.25)`; a per-image `SlideSource.source_mpp` takes
precedence over that default.

`ImageReader` caches decoded images per reader, bounded by both `cache_size`
(default 4) and `max_cache_bytes` (default 256 MiB). Images exceeding the byte
budget are decoded on each read without being cached. JPEG and PNG generally
require whole-image decoding; the cache budget does not bound the memory needed
to decode one image. With multiple reader workers, each has its own cache.

On Windows, the viewer can run as a background service managed by WinSW. See
[Windows service setup](docs/WINDOWS_SERVICE.md) for installation and service
control commands.

### Docker Compose

Generate the administrator password hash and configure `.env` first using the
[HTTPS deployment guide](docs/SECURITY_DEPLOYMENT.md). Then start the viewer:

```bash
docker compose -f compose.yaml -f compose.https.yaml up --build -d
```

Open your configured HTTPS domain and log in. Compose mounts slides and overlays
read-only and stores crops and browser uploads in bind-mounted directories.
Use the same `-f` options when stopping the service. The base Compose file alone
binds to loopback port 8081 for local testing; HTTP requires the cookie setting
described in the deployment guide.

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

Select **裁剪任务** in the viewer to choose a rectangle or polygon directly on
the slide. Coordinates and dimensions use pixels at the selected native level.
Choose an output format and optional target MPP, save named regions and parameter
templates, and preview patch grids on the slide before submitting background jobs.
Without a target MPP, crops retain the selected level's native resolution.
The viewer remembers the most recently entered crop width and height when
switching slides or reloading the page. The server stores images in
`--crop-output-dir` (default `./crops`) and never overwrites an existing file.
One crop worker is used by default; use `--crop-workers N` to change concurrency.
The viewer panel displays current-slide tasks and supports cancelling queued jobs
and retrying failures. Open **个人管理 → 裁剪管理** to browse saved crops, download
images and coordinate metadata, or delete selections.

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

The upstream contract for nucleus-center and box detections is in
[Detection prediction contract](docs/DETECTION_PREDICTION_CONTRACT.md).
Detection manifests use the same `--overlay` and `--overlay-root` options.
The viewer imports JSONL records into a temporary disk-backed SQLite R-tree and
queries the visible region for Canvas point rendering. Points appear only at
native level 0–1 resolutions or higher zoom; lower zoom clears the layer and
stops point queries. Box-only records use their box center. Layer controls
adjust class colors, visibility, opacity, screen-space radius (1–24 px), and
filled or hollow circles. Boxes and instance contours are not drawn.

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

Large overview regions are resampled in blocks rather than materializing the
whole native region as one array. `TileRenderer(max_read_pixels=1_048_576)`
limits the pixels requested in each native read during `render_region()`;
bilinear sampling uses one global output grid across blocks. The encoded
output remains cached. This bounds temporary conversion arrays, but the
backend's index and tile caches also consume memory, and the first overview
still needs to decode its contributing tiles. Exact crop methods retain
their existing direct-read behavior.

`KfbSlideReader` corrects truncated tile-index offsets in kfbslide 0.3.2:
KFB header index offsets are 64-bit values, while that backend reads their
low 32-bit words. For files whose index lies beyond 4 GiB, this otherwise
interprets image data as an index and produces invalid pyramid metadata.
The adapter restores the offsets before index construction, checks that the
index range matches the tile count and file bounds, and rejects invalid
pyramid dimensions/downsamples. The correction does not modify the file or
the installed package.

The browser fetches slide metadata and IIIF `info.json` without caching,
checks that their dimensions agree, and opens the fetched IIIF description.
The base IIIF description uses `Cache-Control: no-store`; each newly opened
slide resets the viewport to its home bounds. Rendered image tiles retain
their separate cache policy.

For a slow KFB request, run the same region through the reader and renderer
without the web server. For example, on Windows, from the project directory:

```powershell
.venv\Scripts\python.exe -u benchmarks\diagnose_kfb.py "D:\slides\example.kfb" --region 98304,98304,32768,2315 --size 512,37
```

Use the actual slide path and the region/output size from the slow IIIF URL.
The script prints file opening, metadata, reading, and rendering timings, plus
the selected native level and read size. Two renders use the same reader with
encoded response caching disabled, so the second pass measures reuse of the
reader's internal cache. Every 15 seconds it dumps the current Python stack to
stderr, including during a stall. A slow standalone read narrows the problem to
the reader or storage; fast standalone renders point toward server scheduling
or a difference in the request workload. It does not measure HTTP queue time.

If standalone rendering is fast but HTTP requests stall, append
`--debug-stack-interval 15` to the viewer command to dump all server thread
stacks every 15 seconds. Capture a dump while the browser request is pending.
Add `--debug-render` to log each uncached request's region, output size, selected
native level, read size, and elapsed time. Replay the last unfinished region
with `diagnose_kfb.py --worker-thread` to compare background thread decoding
with the script's default main thread decoding. A quick read of one small
region does not establish the speed of a full-slide overview or other tiles.
The viewer initializes Pillow's image plugins before accepting requests, so
initial plugin imports happen during startup rather than the first tile read.
For kfbslide 0.3.x, `--debug-render` also traces uncached tile decoding and
index repairs, delegating both operations to the installed backend. The last
`KFB tile BEGIN` identifies the tile being decoded; `KFB repair BEGIN` records
the original decode error and the number of subsequent offsets involved.
Use `diagnose_kfb.py --trace-tiles --preload-pillow --worker-thread` to collect
the same details without HTTP requests. These tile diagnostics use private
backend hooks and may need updating for future kfbslide versions.
To compare without periodic directory discovery, launch the viewer with just
`--slide "sample=/path/to/example.kfb"`, omitting slide directory arguments.

### Viewer performance and persistent KFB indices

The viewer caches one slide per reader by default (`--reader-cache-size 1`),
with 128 decoded KFB tiles per open slide (`--kfb-tile-cache-size 128`). The
reader count remains configurable through `--reader-pool-size`. Reducing it
can help when storage or memory, rather than CPU, limits concurrent reads.

KFB indices are streamed into SQLite in batches of 4096 records on first use.
The default cache directory is `./.cache/kfb-index`; put it on a local SSD even
when slides reside on a slower drive:

```powershell
uv run wsi-patchkit-viewer --slide-dir "D:\slides" --kfb-index-cache-dir "C:\wsi-cache" --reader-cache-size 1
```

Subsequent opens reuse the database and query only contributing tile records.
Each open index keeps at most 512 records in its Python cache and requests a
2 MiB SQLite page-cache budget. This is not a cap on total process memory:
decoded image caches, rendering buffers, and OS file caches also use memory.
The first database build may take longer than the previous in-memory parser;
later opens avoid rebuilding its large Python dictionaries. Cache identity
includes the source path, size, nanosecond modification time, parsed header
fields, and cache schema version. Source changes create a new cache; completed
builds and index repairs are transactional, and simultaneous builders serialize.
Old cache files can be removed when the viewer is stopped. Corrupt cache files
can likewise be removed to rebuild them from their unchanged source slides.

The adapter preserves the backend's JPEG decoder and index repair behavior.
It supports the private index interface of kfbslide 0.3.2/0.3.3; future backend
versions require compatibility verification. Library readers opt in with
`KfbSlideReader(index_cache_dir=...)` or
`AutoSlideReader(kfb_index_cache_dir=...)`. The web app enables it by default;
embedded applications can pass `kfb_index_cache_dir=None` to use native indices.
Docker stores index databases in `/data/cache/kfb-index`, backed by a persistent
writable volume in Compose.

Directory scanning, metadata, and tile rendering use separate work queues.
`--scan-interval` sets the minimum rescan interval (default: 5 seconds); browser
polling still happens every 5 seconds. Rendering queues apply backpressure,
and disconnected clients cancel work that has not started. The browser limits
tile requests to four, clears unstarted jobs when the viewport changes, and
retains at most 128 image-cache entries. Already-running synchronous reads
finish normally. RGBA images are composited over white using Pillow, preserving
the original integer rounding without large uint32 conversion buffers.

Compare opening, working-set growth on Windows, and uncached overview rendering:

```powershell
.venv\Scripts\python.exe -u benchmarks\benchmark_web_kfb.py "D:\slides\sample.kfb" --output .cache\benchmark.json
```

The benchmark verifies exact JPEG equality between the legacy conversion and
the optimized renderer. `sqlite_first` builds only if its source cache does not
already exist; `index_built` reports whether a build actually happened. Native
file-system caches are not cleared, so these are not cold-disk measurements.

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

For parallel reading, supply a factory that creates an independent reader for
each thread:

```python
from contextlib import closing

stream = PatchStream(
    None, requests, reader_factory=TiffReader, num_workers=4, max_pending=8
)
with closing(iter(stream)) as patches:
    for patch in patches:
        prediction = model(patch.image)
```

`num_workers=1` keeps the default synchronous behavior. Parallel mode preserves
request order and queues at most `max_pending` requests (default: twice the
thread count), including completed patches waiting to be consumed. It requires
`reader_factory` rather than a shared reader, since backend handles and caches
are not safe to share between threads. The iterator closes all readers it
creates on exhaustion, failure, or explicit iterator closure, after waiting for
running reads to finish. Each thread has its own metadata and handle caches,
so memory and open-file usage grow with the thread count. With PyTorch
DataLoader, process concurrency remains controlled by `num_workers` on the
DataLoader; this option applies to direct `PatchStream` use.

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
from wsi_patchkit.io import AutoSlideReader
from wsi_patchkit.torch import WSIPatchIterableDataset

dataset = WSIPatchIterableDataset(
    slides,
    request_sampler=RandomPatchRequestSampler(
        num_samples=20_000, patch_size=512, seed=2026
    ),
    reader_factory=AutoSlideReader,
)
```

`AutoSlideReader` routes KFB files to `kfbslide` and TIFF files to tifffile, so
one training dataset can contain slides in both formats. Each worker creates
and closes its own reader. Items contain a float32 CHW `image` tensor in
`[0, 1]` plus the original `PatchRequest`.

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
