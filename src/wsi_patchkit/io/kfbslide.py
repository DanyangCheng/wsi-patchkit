"""Optional KFB reader adapter backed by the OpenSlide-compatible kfbslide API."""

from __future__ import annotations

import logging
import math
import struct
import time
from typing import Any

from .openslide import OpenSlideReader

_LOGGER = logging.getLogger(__name__)


def _restore_index_offsets(path: str, info: Any) -> None:
    """Restore the 64-bit index range truncated by kfbslide 0.3.2.

    The header stores end/start as little-endian uint64 values at payload
    offsets 56/64. The backend reads only their low 32-bit words. Apply this
    before it builds the index, and validate the complete table range.
    """
    with open(path, "rb") as handle:
        prefix = handle.read(92)
        file_size = handle.seek(0, 2)
    if len(prefix) != 92 or prefix[1:4] != b"\x01\xee\xee":
        raise ValueError("unsupported KFB header layout for index offset correction")
    index_end, index_start = struct.unpack_from("<QQ", prefix, 4 + 56)
    expected_size = info.header.tile_count * 64
    if (
        expected_size <= 0
        or index_end - index_start != expected_size
        or index_start < info.tile_data_offset
        or index_end > file_size
    ):
        raise ValueError(
            f"invalid KFB tile index range {index_start}:{index_end}; "
            f"expected {expected_size} bytes within file size {file_size}"
        )
    if (index_start, index_end) != (
        info.header.tile_index_start,
        info.header.tile_index_end,
    ):
        _LOGGER.warning(
            "Correcting KFB 64-bit index offsets path=%s start=%s->%s end=%s->%s",
            path,
            info.header.tile_index_start,
            index_start,
            info.header.tile_index_end,
            index_end,
        )
        info.header.tile_index_start = index_start
        info.header.tile_index_end = index_end


class KfbSlideReader(OpenSlideReader):
    """Worker-local KFB reader with an LRU handle cache."""

    _backend_module = "kfbslide"
    _missing_backend_message = "KfbSlideReader requires the 'kfbslide' package"

    def _open_slide(self, path: str) -> Any:
        backend = self._openslide

        class CompatibleSlide(backend.OpenSlide):
            # Intercept parsed file info before the backend constructs its
            # index. Keep the backend constructor/decoder and avoid changing
            # module globals shared by concurrent readers.
            @property
            def _info(self):
                return self._patchkit_info

            @_info.setter
            def _info(self, value):
                if getattr(backend, "__version__", None) == "0.3.2":
                    _restore_index_offsets(path, value)
                self._patchkit_info = value

        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return self._checked_slide(CompatibleSlide(path))

        # Optional diagnostics for kfbslide 0.3.x. Delegate decoding and repair
        # unchanged; these private hooks only report which tile is in progress.
        class TracedSlide(CompatibleSlide):
            def _read_decoded_tile(self, idx):
                cached = idx in getattr(self._tile_cache, "_cache", {})
                if cached:
                    return super()._read_decoded_tile(idx)
                entry = self._index.entries[idx]
                _LOGGER.debug(
                    "KFB tile BEGIN path=%s index=%s offset=%s bytes=%s size=%s",
                    path,
                    idx,
                    self._index.offsets[idx],
                    entry["size"],
                    (entry["width"], entry["height"]),
                )
                start = time.perf_counter()
                try:
                    result = super()._read_decoded_tile(idx)
                except Exception:
                    _LOGGER.exception(
                        "KFB tile FAILED path=%s index=%s elapsed=%.3fs",
                        path,
                        idx,
                        time.perf_counter() - start,
                    )
                    raise
                _LOGGER.debug(
                    "KFB tile END path=%s index=%s elapsed=%.3fs",
                    path,
                    idx,
                    time.perf_counter() - start,
                )
                return result

            def _try_heal_tile(self, idx, exc):
                _LOGGER.warning(
                    "KFB repair BEGIN path=%s index=%s remaining_offsets=%s error=%r",
                    path,
                    idx,
                    len(self._index.offsets) - idx - 1,
                    exc,
                )
                start = time.perf_counter()
                try:
                    result = super()._try_heal_tile(idx, exc)
                except Exception:
                    _LOGGER.exception(
                        "KFB repair FAILED path=%s index=%s elapsed=%.3fs",
                        path,
                        idx,
                        time.perf_counter() - start,
                    )
                    raise
                _LOGGER.warning(
                    "KFB repair END path=%s index=%s elapsed=%.3fs",
                    path,
                    idx,
                    time.perf_counter() - start,
                )
                return result

        return self._checked_slide(TracedSlide(path))

    @staticmethod
    def _checked_slide(slide: Any) -> Any:
        """Reject invalid pyramid metadata before it reaches the renderer/UI."""
        try:
            dimensions = slide.level_dimensions
            downsamples = slide.level_downsamples
            if not 1 <= len(dimensions) <= 64 or len(dimensions) != len(downsamples):
                raise ValueError("invalid KFB pyramid level count")
            base = dimensions[0]
            header = getattr(getattr(slide, "_info", None), "header", None)
            if header is not None and base != (header.width, header.height):
                raise ValueError("KFB base dimensions disagree with file header")
            previous = 0.0
            for dims, ds in zip(dimensions, downsamples, strict=True):
                ds = float(ds)
                if not math.isfinite(ds) or ds < 1 or ds <= previous:
                    raise ValueError("invalid KFB pyramid downsample values")
                if any(value < 1 for value in dims) or any(
                    abs(value - base_value / ds) > 1
                    for value, base_value in zip(dims, base, strict=True)
                ):
                    raise ValueError("invalid KFB pyramid dimensions")
                previous = ds
            if not math.isclose(float(downsamples[0]), 1.0):
                raise ValueError("KFB base pyramid level must have downsample 1")
        except Exception:
            slide.close()
            raise
        return slide

    def _level_downsample(
        self,
        slide: Any,
        level: int,
        _base_dimensions: tuple[int, int],
        _dimensions: tuple[int, int],
    ) -> tuple[float, float]:
        downsample = float(slide.level_downsamples[level])
        return downsample, downsample

    def __enter__(self) -> KfbSlideReader:
        return self
