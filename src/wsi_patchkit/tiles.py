"""Protocol-neutral rendering of browser-friendly WSI image tiles."""

from __future__ import annotations

import hashlib
import logging
import math
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from .io.base import SlideReader
from .types import MPP, LevelInfo, SlideMetadata, as_mpp

ImageFormat = Literal["jpg", "png"]
ReaderFactory = Callable[[], SlideReader]
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EncodedImage:
    """An encoded image response and its cache metadata."""

    content: bytes
    media_type: str
    width: int
    height: int
    etag: str


def iiif_scale_factors(
    dimensions: tuple[int, int],
    tile_size: int = 256,
) -> tuple[int, ...]:
    """Return a power-of-two virtual pyramid covering an image."""
    width, height = map(int, dimensions)
    if width < 1 or height < 1 or tile_size < 1:
        raise ValueError("dimensions and tile_size must be positive")
    factors = [1]
    while math.ceil(width / factors[-1]) > tile_size or math.ceil(
        height / factors[-1]
    ) > tile_size:
        factors.append(factors[-1] * 2)
    return tuple(factors)


def choose_level_for_downsample(
    metadata: SlideMetadata,
    requested: float | Sequence[float],
    *,
    relative_tolerance: float = 0.01,
) -> LevelInfo:
    """Choose the coarsest native level suitable for the requested view.

    Reading a slightly finer level and reducing it avoids visible blur caused by
    upscaling a coarser native WSI level. A small tolerance accounts for pyramid
    dimensions rounded to whole pixels, such as an intended 4x level reported as
    4.000095x.
    """
    requested_pair = as_mpp(requested, name="requested_downsample")
    tolerance = float(relative_tolerance)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("relative_tolerance must be finite and non-negative")
    upper_bound = tuple(value * (1 + tolerance) for value in requested_pair)
    eligible = [
        level
        for level in metadata.levels
        if level.downsample[0] <= upper_bound[0]
        and level.downsample[1] <= upper_bound[1]
    ]
    if not eligible:
        return metadata.levels[0]
    return max(eligible, key=lambda item: math.prod(item.downsample))


def _as_rgb(array: NDArray[np.generic]) -> NDArray[np.uint8]:
    if array.dtype != np.uint8:
        raise ValueError("web tiles currently require an 8-bit WSI")
    if array.ndim == 2:
        array = array[..., None]
    if array.ndim != 3:
        raise ValueError(f"reader returned unsupported shape {array.shape}")
    if array.shape[2] == 1:
        array = np.repeat(array, 3, axis=2)
    elif array.shape[2] == 4:
        alpha = array[..., 3:4].astype(np.uint32)
        array = (
            array[..., :3].astype(np.uint32) * alpha
            + 255 * (255 - alpha)
            + 127
        ) // 255
    elif array.shape[2] >= 3:
        array = array[..., :3]
    else:
        raise ValueError(f"reader returned unsupported channel count {array.shape[2]}")
    return np.asarray(array, dtype=np.uint8)


class SlideReaderPool:
    """A bounded pool of independent readers for concurrent tile requests."""

    def __init__(self, reader_factory: ReaderFactory, size: int = 4) -> None:
        if size < 1:
            raise ValueError("reader pool size must be positive")
        readers: list[SlideReader] = []
        try:
            for _ in range(size):
                readers.append(reader_factory())
        except BaseException:
            for reader in readers:
                reader.close()
            raise
        self._readers = tuple(readers)
        self._available: queue.LifoQueue[SlideReader] = queue.LifoQueue(size)
        for reader in self._readers:
            self._available.put(reader)
        self._closed = False
        self._state_lock = threading.Lock()

    @property
    def size(self) -> int:
        return len(self._readers)

    @contextmanager
    def acquire(self) -> Iterator[SlideReader]:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("reader pool is closed")
        reader = self._available.get()
        try:
            yield reader
        finally:
            self._available.put(reader)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        borrowed = [self._available.get() for _ in self._readers]
        for reader in borrowed:
            reader.close()


class TileRenderer:
    """Render arbitrary level-0 regions with native-level selection and caching.

    Each reader handle is used by at most one request at a time. Different tiles
    can read concurrently when a ``reader_factory`` and pool size greater than one
    are supplied. Shared locks only protect short cache bookkeeping operations.
    """

    def __init__(
        self,
        reader: SlideReader | None = None,
        *,
        reader_factory: ReaderFactory | None = None,
        reader_pool_size: int = 1,
        cache_size: int = 512,
        jpeg_quality: int = 85,
        max_output_pixels: int = 16_777_216,
        max_read_pixels: int = 1_048_576,
    ) -> None:
        if cache_size < 0:
            raise ValueError("cache_size must be non-negative")
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be in [1, 100]")
        if max_output_pixels < 1:
            raise ValueError("max_output_pixels must be positive")
        if max_read_pixels < 16:
            raise ValueError("max_read_pixels must be at least 16")
        if reader is not None and reader_factory is not None:
            raise ValueError("provide reader or reader_factory, not both")
        if reader is None and reader_factory is None:
            raise ValueError("reader or reader_factory is required")
        if reader is not None:
            self._readers = SlideReaderPool(lambda: reader, size=1)
        else:
            assert reader_factory is not None
            self._readers = SlideReaderPool(reader_factory, size=reader_pool_size)
        self.cache_size = int(cache_size)
        self.jpeg_quality = int(jpeg_quality)
        self.max_output_pixels = int(max_output_pixels)
        self.max_read_pixels = int(max_read_pixels)
        self._cache: OrderedDict[tuple[object, ...], EncodedImage] = OrderedDict()
        self._metadata_cache: dict[tuple[object, ...], SlideMetadata] = {}
        self._inflight: dict[tuple[object, ...], Future[EncodedImage]] = {}
        self._state_lock = threading.Lock()

    def metadata(
        self,
        path: str | Path,
        *,
        source_mpp: float | Sequence[float] | None = None,
    ) -> SlideMetadata:
        source_mpp_pair: MPP | None = (
            None if source_mpp is None else as_mpp(source_mpp, name="source_mpp")
        )
        fingerprint = self._fingerprint(path)
        key: tuple[object, ...] = fingerprint, source_mpp_pair
        with self._state_lock:
            cached = self._metadata_cache.get(key)
        if cached is not None:
            return cached
        with self._readers.acquire() as reader:
            metadata = reader.metadata(path, source_mpp=source_mpp_pair)
        with self._state_lock:
            return self._metadata_cache.setdefault(key, metadata)

    @staticmethod
    def _fingerprint(path: str | Path) -> tuple[str, int, int]:
        resolved = Path(path).resolve()
        stat = resolved.stat()
        return str(resolved), stat.st_size, stat.st_mtime_ns

    def render_region(
        self,
        path: str | Path,
        region: tuple[int, int, int, int],
        output_size: tuple[int, int],
        *,
        image_format: ImageFormat = "jpg",
        source_mpp: float | Sequence[float] | None = None,
    ) -> EncodedImage:
        """Render a level-0 ``(x, y, width, height)`` region."""
        x, y, width, height = map(int, region)
        output_width, output_height = map(int, output_size)
        if x < 0 or y < 0 or width < 1 or height < 1:
            raise ValueError("region must contain non-negative coordinates and size")
        if output_width < 1 or output_height < 1:
            raise ValueError("output size must be positive")
        if output_width * output_height > self.max_output_pixels:
            raise ValueError("requested output exceeds max_output_pixels")
        if image_format not in ("jpg", "png"):
            raise ValueError("image_format must be 'jpg' or 'png'")

        source_mpp_pair: MPP | None = (
            None if source_mpp is None else as_mpp(source_mpp, name="source_mpp")
        )
        fingerprint = self._fingerprint(path)
        key: tuple[object, ...] = (
            fingerprint,
            source_mpp_pair,
            x,
            y,
            width,
            height,
            output_width,
            output_height,
            image_format,
            self.jpeg_quality,
        )
        with self._state_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            future = self._inflight.get(key)
            owns_render = future is None
            if future is None:
                future = Future()
                self._inflight[key] = future
        if not owns_render:
            return future.result()

        try:
            result = self._render_uncached(
                path,
                (x, y, width, height),
                (output_width, output_height),
                image_format=image_format,
                source_mpp=source_mpp_pair,
            )
        except BaseException as error:
            with self._state_lock:
                self._inflight.pop(key, None)
                future.set_exception(error)
            raise
        with self._state_lock:
            if self.cache_size:
                self._cache[key] = result
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            self._inflight.pop(key, None)
            future.set_result(result)
        return result

    def render_level0_region(
        self,
        path: str | Path,
        region: tuple[int, int, int, int],
        *,
        image_format: ImageFormat = "png",
        source_mpp: float | Sequence[float] | None = None,
    ) -> EncodedImage:
        """Read and encode an in-bounds region directly from pyramid level 0.

        Unlike :meth:`render_region`, this method never selects another pyramid
        level, rescales the image, clips the requested rectangle, or pads it.
        It is intended for lossless, pixel-exact server-side crops.
        """
        x, y, width, height = map(int, region)
        if x < 0 or y < 0 or width < 1 or height < 1:
            raise ValueError("region must contain non-negative coordinates and size")
        if width * height > self.max_output_pixels:
            raise ValueError("requested output exceeds max_output_pixels")
        if image_format not in ("jpg", "png"):
            raise ValueError("image_format must be 'jpg' or 'png'")

        source_mpp_pair: MPP | None = (
            None if source_mpp is None else as_mpp(source_mpp, name="source_mpp")
        )
        metadata = self.metadata(path, source_mpp=source_mpp_pair)
        full_width, full_height = metadata.dimensions
        if x + width > full_width or y + height > full_height:
            raise ValueError("region must be fully inside the level-0 image")

        with self._readers.acquire() as reader:
            array = reader.read_region(path, (x, y), 0, (width, height))
        image = Image.fromarray(_as_rgb(array), "RGB")
        if image.size != (width, height):
            raise ValueError("reader returned an unexpected level-0 crop size")
        return self._encode_image(image, image_format)

    def render_level_region(
        self,
        path: str | Path,
        region: tuple[int, int, int, int],
        level: int,
        *,
        image_format: ImageFormat = "png",
        source_mpp: float | Sequence[float] | None = None,
    ) -> EncodedImage:
        """Read an in-bounds region directly at one native pyramid level.

        ``region`` is expressed in pixels of ``level``. The returned image is
        read without resampling, so its dimensions exactly equal ``region``'s
        requested width and height.
        """
        x, y, width, height = map(int, region)
        if x < 0 or y < 0 or width < 1 or height < 1:
            raise ValueError("region must contain non-negative coordinates and size")
        if isinstance(level, bool) or not isinstance(level, int):
            raise ValueError("level must be an integer")
        if image_format not in ("jpg", "png"):
            raise ValueError("image_format must be 'jpg' or 'png'")

        source_mpp_pair: MPP | None = (
            None if source_mpp is None else as_mpp(source_mpp, name="source_mpp")
        )
        metadata = self.metadata(path, source_mpp=source_mpp_pair)
        if level < 0 or level >= len(metadata.levels):
            raise ValueError("level does not exist for this slide")
        level_info = metadata.levels[level]
        full_width, full_height = level_info.dimensions
        if x + width > full_width or y + height > full_height:
            raise ValueError("region must be fully inside the selected level")
        if width * height > self.max_output_pixels:
            raise ValueError("requested output exceeds max_output_pixels")
        level_zero_location = (
            round(x * level_info.downsample[0]),
            round(y * level_info.downsample[1]),
        )
        with self._readers.acquire() as reader:
            array = reader.read_region(
                path,
                level_zero_location,
                level_info.level,
                (width, height),
            )
        image = Image.fromarray(_as_rgb(array), "RGB")
        if image.size != (width, height):
            raise ValueError("reader returned an unexpected level crop size")
        return self._encode_image(image, image_format)

    def _render_uncached(
        self,
        path: str | Path,
        region: tuple[int, int, int, int],
        output_size: tuple[int, int],
        *,
        image_format: ImageFormat,
        source_mpp: MPP | None,
    ) -> EncodedImage:
        x, y, width, height = region
        output_width, output_height = output_size
        start = time.perf_counter()
        _LOGGER.debug(
            "Render BEGIN path=%s region=%s output=%s", path, region, output_size
        )
        metadata = self.metadata(path, source_mpp=source_mpp)
        full_width, full_height = metadata.dimensions
        if x >= full_width or y >= full_height:
            raise ValueError("region starts outside the image")
        clipped_width = min(width, full_width - x)
        clipped_height = min(height, full_height - y)
        rendered_width = max(1, round(output_width * clipped_width / width))
        rendered_height = max(1, round(output_height * clipped_height / height))
        requested_downsample = (
            clipped_width / rendered_width,
            clipped_height / rendered_height,
        )
        level = choose_level_for_downsample(metadata, requested_downsample)

        lx0 = max(0, math.floor(x / level.downsample[0]))
        ly0 = max(0, math.floor(y / level.downsample[1]))
        lx1 = min(
            level.dimensions[0],
            math.ceil((x + clipped_width) / level.downsample[0]),
        )
        ly1 = min(
            level.dimensions[1],
            math.ceil((y + clipped_height) / level.downsample[1]),
        )
        level_zero_location = (
            round(lx0 * level.downsample[0]),
            round(ly0 * level.downsample[1]),
        )
        if (lx1 - lx0) * (ly1 - ly0) > self.max_read_pixels:
            image = self._render_in_blocks(
                path,
                level,
                (lx0, ly0, lx1, ly1),
                (
                    x / level.downsample[0],
                    y / level.downsample[1],
                    (x + clipped_width) / level.downsample[0],
                    (y + clipped_height) / level.downsample[1],
                ),
                (rendered_width, rendered_height),
            )
            encoded = self._encode_image(image, image_format)
            _LOGGER.debug(
                "Render END path=%s elapsed=%.3fs", path, time.perf_counter() - start
            )
            return encoded
        _LOGGER.debug(
            "Read BEGIN path=%s level=%s downsample=%s location=%s size=%s",
            path,
            level.level,
            level.downsample,
            level_zero_location,
            (lx1 - lx0, ly1 - ly0),
        )
        read_start = time.perf_counter()
        with self._readers.acquire() as reader:
            array = reader.read_region(
                path,
                level_zero_location,
                level.level,
                (lx1 - lx0, ly1 - ly0),
            )
        _LOGGER.debug(
            "Read END path=%s elapsed=%.3fs", path, time.perf_counter() - read_start
        )
        image = Image.fromarray(_as_rgb(array), "RGB")
        extent = (
            (x / level.downsample[0]) - lx0,
            (y / level.downsample[1]) - ly0,
            ((x + clipped_width) / level.downsample[0]) - lx0,
            ((y + clipped_height) / level.downsample[1]) - ly0,
        )
        image = image.transform(
            (rendered_width, rendered_height),
            Image.Transform.EXTENT,
            extent,
            resample=Image.Resampling.BILINEAR,
        )

        encoded = self._encode_image(image, image_format)
        _LOGGER.debug(
            "Render END path=%s elapsed=%.3fs", path, time.perf_counter() - start
        )
        return encoded

    def _render_in_blocks(
        self,
        path: str | Path,
        level: LevelInfo,
        bounds: tuple[int, int, int, int],
        extent: tuple[float, float, float, float],
        output_size: tuple[int, int],
    ) -> Image.Image:
        """Resample bounded native reads onto one output pixel grid.

        Include both neighbors of each bilinear sample. Using the global
        output grid keeps sampling positions consistent at block boundaries.
        This limits temporary arrays even when the native pyramid stops far
        above the requested overview resolution.
        """
        width, height = output_size
        dx = (extent[2] - extent[0]) / width
        dy = (extent[3] - extent[1]) / height
        side = math.isqrt(self.max_read_pixels)
        block_width = min(width, max(1, math.floor((side - 3) / dx) + 1))
        block_height = min(height, max(1, math.floor((side - 3) / dy) + 1))
        image = Image.new("RGB", output_size)
        _LOGGER.debug(
            "Block render path=%s level=%s output_block=%s max_read_pixels=%s",
            path,
            level.level,
            (block_width, block_height),
            self.max_read_pixels,
        )
        with self._readers.acquire() as reader:
            for oy in range(0, height, block_height):
                oh = min(block_height, height - oy)
                ry0 = min(
                    bounds[3] - 1,
                    max(bounds[1], math.floor(extent[1] + (oy + 0.5) * dy - 0.5)),
                )
                ry1 = max(
                    ry0 + 1,
                    min(
                        bounds[3],
                        math.floor(extent[1] + (oy + oh - 0.5) * dy - 0.5) + 2,
                    ),
                )
                for ox in range(0, width, block_width):
                    ow = min(block_width, width - ox)
                    rx0 = min(
                        bounds[2] - 1,
                        max(bounds[0], math.floor(extent[0] + (ox + 0.5) * dx - 0.5)),
                    )
                    rx1 = max(
                        rx0 + 1,
                        min(
                            bounds[2],
                            math.floor(extent[0] + (ox + ow - 0.5) * dx - 0.5) + 2,
                        ),
                    )
                    location = (
                        round(rx0 * level.downsample[0]),
                        round(ry0 * level.downsample[1]),
                    )
                    size = (rx1 - rx0, ry1 - ry0)
                    _LOGGER.debug(
                        "Read BEGIN path=%s level=%s location=%s size=%s",
                        path,
                        level.level,
                        location,
                        size,
                    )
                    read_start = time.perf_counter()
                    array = reader.read_region(path, location, level.level, size)
                    _LOGGER.debug(
                        "Read END path=%s elapsed=%.3fs",
                        path,
                        time.perf_counter() - read_start,
                    )
                    block = Image.fromarray(_as_rgb(array), "RGB")
                    block = block.transform(
                        (ow, oh),
                        Image.Transform.EXTENT,
                        (
                            extent[0] + ox * dx - rx0,
                            extent[1] + oy * dy - ry0,
                            extent[0] + (ox + ow) * dx - rx0,
                            extent[1] + (oy + oh) * dy - ry0,
                        ),
                        resample=Image.Resampling.BILINEAR,
                    )
                    image.paste(block, (ox, oy))
        return image

    def _encode_image(
        self,
        image: Image.Image,
        image_format: ImageFormat,
    ) -> EncodedImage:
        buffer = BytesIO()
        if image_format == "jpg":
            image.save(buffer, format="JPEG", quality=self.jpeg_quality)
            media_type = "image/jpeg"
        else:
            image.save(buffer, format="PNG", compress_level=1)
            media_type = "image/png"
        content = buffer.getvalue()
        return EncodedImage(
            content,
            media_type,
            image.width,
            image.height,
            f'"{hashlib.sha256(content).hexdigest()}"',
        )

    def close(self) -> None:
        with self._state_lock:
            self._cache.clear()
            self._metadata_cache.clear()
        self._readers.close()
