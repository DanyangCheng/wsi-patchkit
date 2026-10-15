"""Region-efficient TIFF pyramid reader."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from numpy.typing import NDArray

from ..types import LevelInfo, PixelFormat, SlideMetadata, as_mpp


def _tag_number(tag: Any) -> float:
    value = tag.value
    if isinstance(value, tuple):
        return float(value[0]) / float(value[1])
    return float(value)


def _page_mpp(page: tifffile.TiffPage) -> tuple[float, float] | None:
    x_tag = page.tags.get(282)
    y_tag = page.tags.get(283)
    unit_tag = page.tags.get(296)
    if x_tag is None or y_tag is None or unit_tag is None:
        return None
    factor = {2: 25_400.0, 3: 10_000.0}.get(int(unit_tag.value))
    if factor is None:
        return None
    return as_mpp((factor / _tag_number(x_tag), factor / _tag_number(y_tag)))


def _page_pixel_format(page: tifffile.TiffPage) -> PixelFormat:
    photometric_value = int(page.photometric)
    photometric = {
        0: "miniswhite",
        1: "minisblack",
        2: "rgb",
        3: "palette",
        6: "ycbcr",
    }.get(photometric_value, str(page.photometric).lower())
    channels = int(page.samplesperpixel or 1)
    color_model = (
        "gray"
        if photometric_value in (0, 1)
        else "palette"
        if photometric_value == 3
        else "rgba"
        if photometric_value == 2 and channels == 4
        else "rgb"
        if photometric_value in (2, 6)
        else "unknown"
    )
    return PixelFormat(
        str(np.dtype(page.dtype)),
        channels,
        color_model=color_model,
        photometric=photometric,
    )


def _pyramid_pages(tif: tifffile.TiffFile) -> list[tifffile.TiffPage]:
    """Include reduced SubIFDs even for non-integral KFB pyramid ratios.

    tifffile's generic series grouping recognizes integer pyramid factors.
    A KFB can use e.g. 40x/15x; its reduced SubIFDs still form a valid pyramid.
    Only extend single-page image series, retaining the existing behavior for
    multi-page data and rejecting label/macro SubIFDs of unrelated geometry.
    """
    series = tif.series[0]
    pages = [level.pages[0] for level in series.levels]
    base = pages[0]
    if base.subifds and len(series.pages) == 1:
        reduced = sorted(
            (
                page
                for page in base.pages
                if page.is_reduced
                and page.dtype == base.dtype
                and page.samplesperpixel == base.samplesperpixel
                and 0 < page.imagewidth <= base.imagewidth
                and 0 < page.imagelength <= base.imagelength
                and page.imagewidth * page.imagelength
                < base.imagewidth * base.imagelength
            ),
            key=lambda page: page.imagewidth * page.imagelength,
            reverse=True,
        )
        result = [base]
        for page in reduced:
            previous = result[-1]
            if (
                page.imagewidth <= previous.imagewidth
                and page.imagelength <= previous.imagelength
                and page.imagewidth * page.imagelength
                < previous.imagewidth * previous.imagelength
            ):
                result.append(page)
        if len(result) > len(pages):
            return result
    return pages


class _TiffLevelReader:
    def __init__(self, path: str, level: int) -> None:
        self._tif: tifffile.TiffFile | None = tifffile.TiffFile(path)
        levels = _pyramid_pages(self._tif)
        if not 0 <= level < len(levels):
            self.close()
            raise ValueError(
                f"pyramid level {level} is unavailable; "
                f"slide has {len(levels)} level(s)"
            )
        self.page = levels[level]
        if len(self.page.shape) not in (2, 3):
            self.close()
            raise ValueError(f"unsupported TIFF page shape {self.page.shape}")
        if self.page.planarconfig not in (None, 1):
            self.close()
            raise ValueError("planar-separate TIFF pages are unsupported")
        self.width = int(self.page.imagewidth)
        self.height = int(self.page.imagelength)
        self.channels = int(self.page.samplesperpixel or 1)
        self.dtype = np.dtype(self.page.dtype)
        self._jpegtables = self.page.jpegtables
        self._offsets = self.page.dataoffsets
        self._bytecounts = self.page.databytecounts
        if self.page.is_tiled:
            self.segment_height = int(self.page.tilelength)
            self.segment_width = int(self.page.tilewidth)
            self._segments_across = math.ceil(self.width / self.segment_width)
        else:
            self.segment_height = int(self.page.rowsperstrip)
            self.segment_width = self.width
            self._segments_across = 1

    def close(self) -> None:
        if self._tif is not None:
            self._tif.close()
            self._tif = None

    def _decode(self, index: int) -> NDArray[np.generic]:
        if self._tif is None:
            raise RuntimeError("TIFF reader is closed")
        self._tif.filehandle.seek(int(self._offsets[index]))
        encoded = self._tif.filehandle.read(int(self._bytecounts[index]))
        decoded, _, _ = self.page.decode(
            encoded,
            index,
            jpegtables=self._jpegtables,
        )
        if decoded is None:
            return np.zeros(
                (self.segment_height, self.segment_width, self.channels),
                dtype=self.dtype,
            )
        array = np.asarray(decoded)
        while array.ndim > 3 and array.shape[0] == 1:
            array = array[0]
        if array.ndim == 2:
            array = array[..., None]
        return array

    def read_region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> NDArray[np.generic]:
        x, y, width, height = map(int, (x, y, width, height))
        if width < 1 or height < 1:
            raise ValueError("region size must be positive")
        if x < 0 or y < 0 or x + width > self.width or y + height > self.height:
            raise ValueError("read_region expects an in-bounds region")
        result = np.zeros((height, width, self.channels), dtype=self.dtype)
        first_row = y // self.segment_height
        last_row = (y + height - 1) // self.segment_height
        first_col = x // self.segment_width
        last_col = (x + width - 1) // self.segment_width
        for segment_row in range(first_row, last_row + 1):
            for segment_col in range(first_col, last_col + 1):
                index = segment_row * self._segments_across + segment_col
                segment = self._decode(index)
                source_x = segment_col * self.segment_width
                source_y = segment_row * self.segment_height
                x0, y0 = max(x, source_x), max(y, source_y)
                x1 = min(x + width, source_x + segment.shape[1])
                y1 = min(y + height, source_y + segment.shape[0])
                if x1 <= x0 or y1 <= y0:
                    continue
                result[y0 - y : y1 - y, x0 - x : x1 - x] = segment[
                    y0 - source_y : y1 - source_y,
                    x0 - source_x : x1 - source_x,
                    : self.channels,
                ]
        return result


class TiffReader:
    """Worker-local, LRU-cached TIFF/BigTIFF pyramid reader."""

    def __init__(self, cache_size: int = 4) -> None:
        if cache_size < 1:
            raise ValueError("cache_size must be at least one")
        self.cache_size = int(cache_size)
        self._readers: OrderedDict[tuple[str, int], _TiffLevelReader] = OrderedDict()
        self._metadata: OrderedDict[
            tuple[str, tuple[float, float] | None], SlideMetadata
        ] = OrderedDict()

    @staticmethod
    def _path(path: str | Path) -> str:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return str(resolved)

    def metadata(
        self,
        path: str | Path,
        *,
        source_mpp: float | Sequence[float] | None = None,
    ) -> SlideMetadata:
        resolved = self._path(path)
        override = None if source_mpp is None else as_mpp(source_mpp, name="source_mpp")
        key = resolved, override
        cached = self._metadata.get(key)
        if cached is not None:
            self._metadata.move_to_end(key)
            return cached
        with tifffile.TiffFile(resolved) as tif:
            pages = _pyramid_pages(tif)
            dimensions = [
                (int(page.imagewidth), int(page.imagelength)) for page in pages
            ]
            pixel_formats = [_page_pixel_format(page) for page in pages]
            base_mpp = override if override is not None else _page_mpp(pages[0])
            description = pages[0].description or ""
        width0, height0 = dimensions[0]
        levels: list[LevelInfo] = []
        for level, ((width, height), pixel_format) in enumerate(
            zip(dimensions, pixel_formats, strict=True)
        ):
            downsample = width0 / width, height0 / height
            level_mpp = (
                None
                if base_mpp is None
                else (
                    base_mpp[0] * downsample[0],
                    base_mpp[1] * downsample[1],
                )
            )
            levels.append(
                LevelInfo(
                    level,
                    (width, height),
                    downsample,
                    level_mpp,
                    pixel_format,
                )
            )
        metadata = SlideMetadata(
            resolved,
            tuple(levels),
            mpp=base_mpp,
            vendor="generic-tiff",
            properties={"tiff.description": description} if description else {},
        )
        self._metadata[key] = metadata
        if len(self._metadata) > self.cache_size:
            self._metadata.popitem(last=False)
        return metadata

    def _reader(self, path: str, level: int) -> _TiffLevelReader:
        key = path, int(level)
        reader = self._readers.get(key)
        if reader is None:
            reader = _TiffLevelReader(path, level)
            self._readers[key] = reader
            if len(self._readers) > self.cache_size:
                _, evicted = self._readers.popitem(last=False)
                evicted.close()
        else:
            self._readers.move_to_end(key)
        return reader

    def read_region(
        self,
        path: str | Path,
        location: tuple[int, int],
        level: int,
        size: tuple[int, int],
    ) -> NDArray[np.generic]:
        resolved = self._path(path)
        metadata = self.metadata(resolved)
        if not 0 <= level < len(metadata.levels):
            raise ValueError(f"pyramid level {level} is unavailable")
        level_info = metadata.levels[level]
        x = math.floor(int(location[0]) / level_info.downsample[0])
        y = math.floor(int(location[1]) / level_info.downsample[1])
        width, height = map(int, size)
        return self._reader(resolved, level).read_region(x, y, width, height)

    def close(self) -> None:
        for reader in self._readers.values():
            reader.close()
        self._readers.clear()
        self._metadata.clear()

    def __enter__(self) -> TiffReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __getstate__(self) -> dict[str, int]:
        return {"cache_size": self.cache_size}

    def __setstate__(self, state: dict[str, int]) -> None:
        self.cache_size = state["cache_size"]
        self._readers = OrderedDict()
        self._metadata = OrderedDict()

    def __del__(self) -> None:
        if getattr(self, "_readers", None) is not None:
            self.close()
