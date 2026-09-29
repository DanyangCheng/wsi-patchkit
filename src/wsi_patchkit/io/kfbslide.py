"""Optional KFB reader adapter backed by the OpenSlide-compatible kfbslide API."""

from __future__ import annotations

from typing import Any

from .openslide import OpenSlideReader


class KfbSlideReader(OpenSlideReader):
    """Worker-local KFB reader with an LRU handle cache."""

    _backend_module = "kfbslide"
    _missing_backend_message = "KfbSlideReader requires the 'kfbslide' package"

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
