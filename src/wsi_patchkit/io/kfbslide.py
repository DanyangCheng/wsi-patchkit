"""Optional KFB reader adapter backed by the OpenSlide-compatible kfbslide API."""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import struct
import threading
import time
from importlib import import_module
from pathlib import Path
from typing import Any

from ._kfb_index import SQLiteTileIndex
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

    def __init__(
        self,
        cache_size: int = 4,
        *,
        index_cache_dir: str | Path | None = None,
        tile_cache_size: int = 256,
    ) -> None:
        if tile_cache_size < 0:
            raise ValueError("KFB tile cache size must be non-negative")
        super().__init__(cache_size=cache_size)
        self.index_cache_dir = index_cache_dir
        self.tile_cache_size = tile_cache_size

    def _open_slide(self, path: str) -> Any:
        backend = self._openslide
        cache_dir = self.index_cache_dir
        if cache_dir is not None and getattr(backend, "__version__", None) not in (
            "0.3.2",
            "0.3.3",
        ):
            raise RuntimeError("persistent KFB indices require kfbslide 0.3.2 or 0.3.3")
        private = import_module("kfbslide._slide")
        tile_cache_size = self.tile_cache_size

        class CompatibleSlide(backend.OpenSlide):
            def __init__(self, filename):
                if cache_dir is None:
                    super().__init__(filename)
                    self._configure_tile_cache()
                    return
                # Initialize the pinned backend without calling _TileIndex,
                # which would materialize every record before our replacement.
                self._filename = filename
                self._closed = False
                self._file_handle = None
                self._pid = os.getpid()
                # Some builds retain the old version string but use the
                # newer per-thread handle registry. Detect the actual layout.
                if hasattr(backend.OpenSlide, "_handles"):
                    self._handles = threading.local()
                    self._open_handles = []
                    self._handles_lock = threading.Lock()
                self._tile_cache = private._LRUCache(tile_cache_size)
                try:
                    self._info = private.parse_kfb_file(filename)
                    self._configure_tile_cache()
                    self._index = SQLiteTileIndex(filename, self._info, cache_dir)
                    self._ensure_open_handle()
                    header = self._info.header
                    props = {
                        "openslide.vendor": "kfbio",
                        "openslide.quickhash-1": "",
                        "openslide.mpp-x": str(header.mpp),
                        "openslide.mpp-y": str(header.mpp),
                        "openslide.objective-power": str(header.scan_scale),
                        "kfbio.vendor": "Kfbio",
                    }
                    for name in (
                        "version",
                        "scan_scale",
                        "tile_size",
                        "tile_count",
                        "width",
                        "height",
                        "spend_time",
                        "scan_time",
                        "mpp",
                    ):
                        props[f"kfbio.{name}"] = str(getattr(header, name))
                    self._properties = private._KfbPropertyMap(props)
                    self._associated_images = private._AssociatedImageMap(
                        filename,
                        self._info.assoc_images,
                        file_handle_getter=lambda: (
                            self._ensure_open_handle() if not self._closed else None
                        ),
                    )
                except BaseException as error:
                    self.close()
                    if isinstance(error, sqlite3.Error):
                        raise OSError(
                            "unable to access persistent KFB index"
                        ) from error
                    raise

            def _configure_tile_cache(self):
                if hasattr(self._tile_cache, "max_bytes"):
                    # Public configuration remains a tile count; decoded
                    # tiles in this backend are RGBA.
                    self._tile_cache.max_bytes = (
                        tile_cache_size * self._info.header.tile_size**2 * 4
                    )
                else:
                    self._tile_cache.capacity = tile_cache_size

            def read_region(self, location, level, size):
                index = self._index
                if isinstance(index, SQLiteTileIndex):
                    if not 0 <= level < index.level_count:
                        raise backend.OpenSlideError(f"Invalid level {level}")
                    scale = index.level_scales[level]
                    ds = self._info.header.scan_scale / scale
                    x, y = int(location[0] / ds), int(location[1] / ds)
                    tile = index.tile_size
                    index.prefetch(
                        scale,
                        (
                            (x // tile) * tile,
                            (y // tile) * tile,
                            ((x + size[0] - 1) // tile + 1) * tile,
                            ((y + size[1] - 1) // tile + 1) * tile,
                        ),
                    )
                return super().read_region(location, level, size)

            def _try_heal_tile(self, idx, exc):
                index = self._index
                if not isinstance(index, SQLiteTileIndex):
                    return super()._try_heal_tile(idx, exc)
                with index.repair_window(idx) as window:
                    self._index = window
                    try:
                        return super()._try_heal_tile(idx, exc)
                    finally:
                        self._index = index

            def close(self):
                index = getattr(self, "_index", None)
                try:
                    super().close()
                finally:
                    if isinstance(index, SQLiteTileIndex):
                        index.close()

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

    def read_region(self, path, location, level, size):
        try:
            return super().read_region(path, location, level, size)
        except sqlite3.Error as error:
            raise OSError("unable to read persistent KFB index") from error

    def __getstate__(self) -> dict[str, Any]:
        return {
            "cache_size": self.cache_size,
            "index_cache_dir": self.index_cache_dir,
            "tile_cache_size": self.tile_cache_size,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__init__(**state)
