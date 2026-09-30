"""Optional OpenSlide reader adapter."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from ..types import LevelInfo, PixelFormat, SlideMetadata, as_mpp


class OpenSlideReader:
    """Worker-local OpenSlide-compatible reader with an LRU handle cache."""

    _backend_module = "openslide"
    _missing_backend_message = (
        "OpenSlideReader requires the 'openslide' or 'openslide-binary' extra"
    )

    def __init__(self, cache_size: int = 4) -> None:
        if cache_size < 1:
            raise ValueError("cache_size must be at least one")
        try:
            backend = import_module(self._backend_module)
        except ImportError as error:
            raise ImportError(self._missing_backend_message) from error
        self.cache_size = int(cache_size)
        self._openslide = backend
        # LRU cache queue
        self._slides: OrderedDict[str, Any] = OrderedDict()

    @staticmethod
    def _path(path: str | Path) -> str:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return str(resolved)

    def _slide(self, path: str) -> Any:
        slide = self._slides.get(path)
        if slide is None:
            try:
                slide = self._open_slide(path)
            except self._openslide.OpenSlideError as error:
                raise OSError(f"unable to open slide {path}: {error}") from error
            self._slides[path] = slide
            if len(self._slides) > self.cache_size:
                _, evicted = self._slides.popitem(last=False)
                evicted.close()
        else:
            self._slides.move_to_end(path)
        return slide

    def _open_slide(self, path: str) -> Any:
        return self._openslide.OpenSlide(path)

    def _level_downsample(
        self,
        _slide: Any,
        level: int,
        base_dimensions: tuple[int, int],
        dimensions: tuple[int, int],
    ) -> tuple[float, float]:
        width0, height0 = base_dimensions
        width, height = dimensions
        return width0 / width, height0 / height

    def metadata(
        self,
        path: str | Path,
        *,
        source_mpp: float | Sequence[float] | None = None,
    ) -> SlideMetadata:
        resolved = self._path(path)
        slide = self._slide(resolved)
        properties = {str(key): str(value) for key, value in slide.properties.items()}
        if source_mpp is not None:
            base_mpp = as_mpp(source_mpp, name="source_mpp")
        else:
            x_value = properties.get(self._openslide.PROPERTY_NAME_MPP_X)
            y_value = properties.get(self._openslide.PROPERTY_NAME_MPP_Y)
            base_mpp = (
                None
                if x_value is None or y_value is None
                else as_mpp((float(x_value), float(y_value)))
            )
        width0, height0 = slide.level_dimensions[0]
        levels = []
        for level, dimensions in enumerate(slide.level_dimensions):
            width, height = map(int, dimensions)
            downsample = self._level_downsample(
                slide,
                level,
                (width0, height0),
                (width, height),
            )
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
                    PixelFormat("uint8", 3, color_model="rgb", photometric="rgb"),
                )
            )
        return SlideMetadata(
            resolved,
            tuple(levels),
            mpp=base_mpp,
            vendor=properties.get(self._openslide.PROPERTY_NAME_VENDOR),
            properties=properties,
        )

    def read_region(
        self,
        path: str | Path,
        location: tuple[int, int],
        level: int,
        size: tuple[int, int],
    ) -> NDArray[np.uint8]:
        resolved = self._path(path)
        width, height = map(int, size)
        if width < 1 or height < 1:
            raise ValueError("region size must be positive")
        try:
            image = self._slide(resolved).read_region(
                tuple(map(int, location)),
                int(level),
                (width, height),
            )
        except self._openslide.OpenSlideError as error:
            raise OSError(
                f"unable to read slide region from {resolved}: {error}"
            ) from error
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        alpha = rgba[..., 3:4].astype(np.uint32)
        rgb = (
            rgba[..., :3].astype(np.uint32) * alpha + 255 * (255 - alpha) + 127
        ) // 255
        return rgb.astype(np.uint8)

    def close(self) -> None:
        for slide in self._slides.values():
            slide.close()
        self._slides.clear()

    def __enter__(self) -> OpenSlideReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __getstate__(self) -> dict[str, int]:
        return {"cache_size": self.cache_size}

    def __setstate__(self, state: dict[str, int]) -> None:
        self.__init__(state["cache_size"])

    def __del__(self) -> None:
        if getattr(self, "_slides", None) is not None:
            self.close()
