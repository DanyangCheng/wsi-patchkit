# Changelog

## Unreleased

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
