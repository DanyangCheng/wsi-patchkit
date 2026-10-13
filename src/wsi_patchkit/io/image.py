"""Single-level reader for ordinary eight-bit images."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps

from ..types import LevelInfo, PixelFormat, SlideMetadata, as_mpp

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})


def _mode(image: Image.Image) -> str:
    if image.mode in {"L", "RGB"} and "transparency" in image.info:
        return "RGBA"
    if image.mode in {"L", "RGB", "RGBA"}:
        return image.mode
    if image.mode == "1":
        return "L"
    if image.mode in {"P", "PA", "LA"}:
        return "RGBA" if "A" in image.mode or "transparency" in image.info else "RGB"
    if image.mode in {"CMYK", "YCbCr"}:
        return "RGB"
    raise ValueError(f"ordinary images require eight-bit pixels; got {image.mode}")


class ImageReader:
    """Read EXIF-oriented images, with a bounded worker-local decoded cache.

    Animated images use their first frame. MPP is supplied only by the caller;
    display DPI is not interpreted as physical specimen resolution.
    """

    def __init__(
        self, cache_size: int = 4, *, max_cache_bytes: int = 256 * 1024 * 1024
    ) -> None:
        if cache_size < 1:
            raise ValueError("cache_size must be at least one")
        if max_cache_bytes < 0:
            raise ValueError("max_cache_bytes must be non-negative")
        self.cache_size = int(cache_size)
        self.max_cache_bytes = int(max_cache_bytes)
        self._images: OrderedDict[str, NDArray[np.uint8]] = OrderedDict()
        self._cache_bytes = 0

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
        mpp = None if source_mpp is None else as_mpp(source_mpp, name="source_mpp")
        with Image.open(resolved) as image:
            mode = _mode(image)
            width, height = image.size
            if image.getexif().get(274) in {5, 6, 7, 8}:
                width, height = height, width
        pixel_format = PixelFormat(
            "uint8",
            len(mode) if mode != "L" else 1,
            color_model={"L": "gray", "RGB": "rgb", "RGBA": "rgba"}[mode],
        )
        return SlideMetadata(
            resolved,
            (LevelInfo(0, (width, height), (1.0, 1.0), mpp, pixel_format),),
            mpp=mpp,
            vendor="generic-image",
        )

    def _image(self, path: str) -> NDArray[np.uint8]:
        cached = self._images.get(path)
        if cached is not None:
            self._images.move_to_end(path)
            return cached
        with Image.open(path) as image:
            mode = _mode(image)
            with (
                ImageOps.exif_transpose(image) as oriented,
                oriented.convert(mode) as converted,
            ):
                array = np.array(converted, dtype=np.uint8)
        if array.ndim == 2:
            array = array[..., None]
        if array.nbytes <= self.max_cache_bytes:
            while self._images and (
                len(self._images) >= self.cache_size
                or self._cache_bytes + array.nbytes > self.max_cache_bytes
            ):
                _, evicted = self._images.popitem(last=False)
                self._cache_bytes -= evicted.nbytes
            self._images[path] = array
            self._cache_bytes += array.nbytes
        return array

    def read_region(
        self,
        path: str | Path,
        location: tuple[int, int],
        level: int,
        size: tuple[int, int],
    ) -> NDArray[np.uint8]:
        if level != 0:
            raise ValueError(f"pyramid level {level} is unavailable")
        metadata = self.metadata(path)
        x, y = map(int, location)
        width, height = map(int, size)
        if width < 1 or height < 1:
            raise ValueError("region size must be positive")
        if (
            x < 0
            or y < 0
            or x + width > metadata.dimensions[0]
            or y + height > metadata.dimensions[1]
        ):
            raise ValueError("read_region expects an in-bounds region")
        return self._image(metadata.path)[y : y + height, x : x + width].copy()

    def close(self) -> None:
        self._images.clear()
        self._cache_bytes = 0

    def __enter__(self) -> ImageReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __getstate__(self) -> dict[str, int]:
        return {"cache_size": self.cache_size, "max_cache_bytes": self.max_cache_bytes}

    def __setstate__(self, state: dict[str, int]) -> None:
        self.__init__(**state)
