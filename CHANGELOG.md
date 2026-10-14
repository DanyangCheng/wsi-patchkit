# Changelog

## Unreleased

### 0.2.0 — phase 1 physical sampling plans

- Added strict `PhysicalPatchRequest`, `OutputGrid`, `SamplingLimits`, and
  `SamplingPlan`, including center affines and explicit legacy conversion.
- Added IO-free `plan_physical_read` with per-request level/policy, native
  neighborhood bounds, constant/reflect planning, area footprint budgets,
  overflow checks, and output/read pixel limits before allocation or IO.
- Added shared `round`/`ceil` canvas policies carried from slide specifications
  through grid/random/tissue samplers and aligned read helpers.
- Added `FutureWarning` for implicit legacy integer conversions and bool MPP;
  these inputs will be rejected in 0.3.0. Fractional MPP remains valid.
- Reject invalid explicit helper levels and policies before region IO.
- Preserved legacy pixel snapshots and `Patch(request, image)` construction.
  Exact pixel resampling and physical result streams remain later milestones.

### Other changes

- Added detection-manifest registration, disk-backed spatial indexing, and
  high-magnification local point rendering with adjustable radius, class
  colors, filled/hollow styles, and opacity.

- Added browser slide upload with validation, immediate listing, persistent
  server-side storage, and a dedicated Docker volume.
- Renamed domain-level sampling APIs to `*PatchRequestSampler` and the PyTorch
  adapter argument to `request_sampler`, distinguishing them from PyTorch
  `DataLoader` samplers.
- Added deterministic, fixed-length tissue-aware random sampling for distributed
  training and lazy random-access request sources for indexed sampling.
- Added `PatchReadResult` and geometric valid masks for aligned patch reads.
- Added optional finer-only pyramid-level selection for training workloads.
- Added per-level reader pixel-format metadata and downstream adapter guidance.
- Fixed aligned reads at non-integral pyramid ratios and added area-averaged RGB
  downsampling.
- Added physically registered, partial indexed-prediction overlays with explicit
  coverage, palettes, manifest validation, transparent IIIF tiles, and viewer
  layer controls.
- Added the `patchkit-web` CLI alias and `--overlay-root` discovery for
  registered slides, including Segment's `{slide_id}` output layout.
- Added sampling and patch-I/O benchmark tooling.

## 0.1.0 - 2026-09-15

- Added TIFF and optional OpenSlide WSI readers.
- Added MPP-aware pyramid selection and aligned patch reads.
- Added grid, deterministic random, and indexed samplers.
- Added low-resolution tissue-mask filtering.
- Added lazy patch streaming and an optional PyTorch iterable dataset.
- Added an optional IIIF tile server and browser viewer with zooming, panning,
  navigation, coordinates, and an MPP-aware scale bar.
- Accounted for whole-pixel pyramid rounding when selecting native viewer levels.
- Added concurrent reader and tile-worker pools while keeping cache bookkeeping
  locks outside WSI decoding, resizing, and image encoding.
