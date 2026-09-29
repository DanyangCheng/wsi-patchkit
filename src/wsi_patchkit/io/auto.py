"""Automatic routing between bundled WSI reader backends."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import tifffile
from numpy.typing import NDArray

from ..types import SlideMetadata
from .base import SlideReader
from .kfbslide import KfbSlideReader
from .openslide import OpenSlideReader
from .tiff import TiffReader

_TIFF_SUFFIXES = {".tif", ".tiff", ".btf", ".btiff"}
_KFB_SUFFIXES = {".kfb"}


class AutoSlideReader:
    """Route TIFF to tifffile, KFB to kfbslide, and other WSIs to OpenSlide.

    Optional readers are imported lazily, so TIFF-only applications do not need
    their additional dependencies.
    """

    def __init__(self, cache_size: int = 4) -> None:
        self.cache_size = int(cache_size)
        self._tiff = TiffReader(cache_size=cache_size)
        self._kfb: KfbSlideReader | None = None
        self._openslide: OpenSlideReader | None = None
        self._tiff_routes: dict[str, bool] = {}

    @staticmethod
    def _is_aperio_tiff(path: str | Path) -> bool:
        """Return whether a TIFF carries the Aperio WSI signature.

        Aperio SVS files are TIFF containers and are sometimes exported with a
        ``.tif`` or ``.tiff`` suffix.  tifffile treats their reduced-resolution
        IFDs as separate images, while OpenSlide exposes them as one pyramid.
        Inspecting the first IFD avoids decoding image data or importing the
        optional OpenSlide dependency for ordinary TIFF files.
        """
        with tifffile.TiffFile(path) as tif:
            description = tif.pages[0].description or ""
            return bool(tif.is_svs or description.lstrip().startswith("Aperio"))

    def _reader(self, path: str | Path) -> SlideReader:
        if Path(path).suffix.lower() in _KFB_SUFFIXES:
            if self._kfb is None:
                self._kfb = KfbSlideReader(cache_size=self.cache_size)
            return self._kfb
        if Path(path).suffix.lower() in _TIFF_SUFFIXES:
            resolved = str(Path(path).resolve())
            is_aperio = self._tiff_routes.get(resolved)
            if is_aperio is None:
                is_aperio = self._is_aperio_tiff(resolved)
                self._tiff_routes[resolved] = is_aperio
            if not is_aperio:
                return self._tiff
        if self._openslide is None:
            self._openslide = OpenSlideReader(cache_size=self.cache_size)
        return self._openslide

    def metadata(
        self,
        path: str | Path,
        *,
        source_mpp: float | Sequence[float] | None = None,
    ) -> SlideMetadata:
        return self._reader(path).metadata(path, source_mpp=source_mpp)

    def read_region(
        self,
        path: str | Path,
        location: tuple[int, int],
        level: int,
        size: tuple[int, int],
    ) -> NDArray:
        return self._reader(path).read_region(path, location, level, size)

    def close(self) -> None:
        self._tiff.close()
        self._tiff_routes.clear()
        if self._kfb is not None:
            self._kfb.close()
            self._kfb = None
        if self._openslide is not None:
            self._openslide.close()
            self._openslide = None

    def __enter__(self) -> AutoSlideReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
