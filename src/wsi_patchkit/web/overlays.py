"""Indexed prediction overlays registered in WSI physical coordinates."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from types import MappingProxyType

import numpy as np
import tifffile
from numpy.typing import NDArray
from PIL import Image

from ..io import TiffReader
from ..tiles import EncodedImage, SlideReaderPool, choose_level_for_downsample
from ..types import MPP, Size, SlideMetadata, as_mpp, as_size
from .registry import valid_public_id

RGBA = tuple[int, int, int, int]


def _single_channel_tiff(path: Path) -> tuple[Size, np.dtype]:
    """Inspect the first TIFF series without decoding its pixels."""
    with tifffile.TiffFile(path) as tif:
        page = tif.series[0].levels[0].pages[0]
        dimensions = int(page.imagewidth), int(page.imagelength)
        channels = int(page.samplesperpixel or 1)
        if channels != 1 or len(page.shape) not in (2, 3):
            raise ValueError(f"overlay TIFF must be single-channel: {path}")
        if page.planarconfig not in (None, 1):
            raise ValueError(f"planar-separate overlay TIFF is unsupported: {path}")
        return dimensions, np.dtype(page.dtype)


@dataclass(frozen=True, slots=True)
class OverlayGeometry:
    """Placement of one raster fragment relative to the WSI level-0 origin."""

    origin_um: tuple[float, float]
    mpp: MPP | float
    dimensions: Size

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) for value in self.origin_um):
            raise ValueError("origin_um must contain two finite values")
        origin = tuple(float(value) for value in self.origin_um)
        if len(origin) != 2 or not all(math.isfinite(value) for value in origin):
            raise ValueError("origin_um must contain two finite values")
        if isinstance(self.mpp, bool) or (
            isinstance(self.mpp, Sequence)
            and any(isinstance(value, bool) for value in self.mpp)
        ):
            raise ValueError("overlay mpp must contain finite positive values")
        if isinstance(self.dimensions, bool) or (
            not isinstance(self.dimensions, int)
            and any(isinstance(value, bool) for value in self.dimensions)
        ):
            raise ValueError("overlay dimensions must contain positive integers")
        object.__setattr__(self, "origin_um", origin)
        object.__setattr__(self, "mpp", as_mpp(self.mpp, name="overlay mpp"))
        object.__setattr__(
            self,
            "dimensions",
            as_size(self.dimensions, name="overlay dimensions"),
        )

    @property
    def bounds_um(self) -> tuple[float, float, float, float]:
        """Return half-open physical bounds as ``(x0, y0, x1, y1)``."""
        return (
            self.origin_um[0],
            self.origin_um[1],
            self.origin_um[0] + self.dimensions[0] * self.mpp[0],
            self.origin_um[1] + self.dimensions[1] * self.mpp[1],
        )


@dataclass(frozen=True, slots=True)
class TiffRasterSource:
    """A single-channel unsigned-integer TIFF used as an indexed raster."""

    path: str | Path
    dimensions: Size = field(init=False)
    dtype: str = field(init=False)

    def __post_init__(self) -> None:
        resolved = Path(self.path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        dimensions, dtype = _single_channel_tiff(resolved)
        if dtype.kind != "u":
            raise ValueError("indexed overlay TIFF must use an unsigned integer dtype")
        object.__setattr__(self, "path", resolved)
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "dtype", str(dtype))


@dataclass(frozen=True, slots=True)
class BinaryRasterSource:
    """A uint8 TIFF whose only valid values are zero and one."""

    path: str | Path
    dimensions: Size = field(init=False)

    def __post_init__(self) -> None:
        resolved = Path(self.path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        dimensions, dtype = _single_channel_tiff(resolved)
        if dtype != np.dtype(np.uint8):
            raise ValueError("coverage TIFF must use uint8")
        object.__setattr__(self, "path", resolved)
        object.__setattr__(self, "dimensions", dimensions)


@dataclass(frozen=True, slots=True)
class OverlayFragment:
    """One spatially placed prediction raster and its optional coverage."""

    source: TiffRasterSource
    geometry: OverlayGeometry
    revision: str
    coverage: BinaryRasterSource | None = None

    def __post_init__(self) -> None:
        if self.source.dimensions != self.geometry.dimensions:
            raise ValueError("prediction TIFF dimensions do not match geometry")
        if self.coverage is not None and (
            self.coverage.dimensions != self.geometry.dimensions
        ):
            raise ValueError("coverage TIFF dimensions do not match prediction")
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("fragment revision must be a non-empty string")


@dataclass(frozen=True, slots=True)
class OverlayClass:
    """Caller-owned display information for one integer ID."""

    id: int
    name: str
    rgba: RGBA

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id < 0:
            raise ValueError("overlay class id must be a non-negative integer")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("overlay class name must be non-empty")
        rgba = tuple(self.rgba)
        if len(rgba) != 4 or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > 255
            for value in rgba
        ):
            raise ValueError("overlay RGBA values must contain four values in [0, 255]")
        object.__setattr__(self, "rgba", rgba)


def _bounds_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        max(first[0], second[0]) < min(first[2], second[2])
        and max(first[1], second[1]) < min(first[3], second[3])
    )


@dataclass(frozen=True, slots=True)
class IndexedOverlay:
    """A display-only indexed overlay composed from non-overlapping fragments."""

    slide_id: str
    overlay_id: str
    classes: Sequence[OverlayClass]
    fragments: Sequence[OverlayFragment]
    display_name: str | None = None
    default_opacity: float = 0.7
    initially_visible: bool = False

    def __post_init__(self) -> None:
        valid_public_id(self.slide_id, name="slide ID")
        valid_public_id(self.overlay_id, name="overlay ID")
        classes = tuple(self.classes)
        fragments = tuple(self.fragments)
        if not classes:
            raise ValueError("indexed overlay must declare at least one class")
        if not fragments:
            raise ValueError("indexed overlay must contain at least one fragment")
        ids = [item.id for item in classes]
        if len(ids) != len(set(ids)):
            raise ValueError("overlay class IDs must be unique")
        for index, first in enumerate(fragments):
            for second in fragments[index + 1 :]:
                if _bounds_overlap(first.geometry.bounds_um, second.geometry.bounds_um):
                    raise ValueError("overlay fragments must not overlap")
        if isinstance(self.default_opacity, bool):
            raise ValueError("default_opacity must be finite and in [0, 1]")
        opacity = float(self.default_opacity)
        if not math.isfinite(opacity) or not 0 <= opacity <= 1:
            raise ValueError("default_opacity must be finite and in [0, 1]")
        display_name = (
            self.overlay_id if self.display_name is None else self.display_name
        )
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("display_name must be non-empty")
        if not isinstance(self.initially_visible, bool):
            raise ValueError("initially_visible must be a boolean")
        object.__setattr__(self, "classes", classes)
        object.__setattr__(self, "fragments", fragments)
        object.__setattr__(self, "default_opacity", opacity)
        object.__setattr__(self, "display_name", display_name)

    @property
    def palette(self) -> Mapping[int, RGBA]:
        return MappingProxyType({item.id: item.rgba for item in self.classes})

    @property
    def revision(self) -> tuple[str, ...]:
        return tuple(fragment.revision for fragment in self.fragments)

    @property
    def cache_token(self) -> str:
        """Change the tile URL when a fragment is replaced in place."""
        fingerprint: list[object] = [
            self.slide_id,
            self.overlay_id,
            tuple((item.id, item.rgba) for item in self.classes),
        ]
        for fragment in self.fragments:
            fingerprint.extend((fragment.revision, fragment.geometry))
            for source in (fragment.source, fragment.coverage):
                if source is not None:
                    stat = Path(source.path).stat()
                    fingerprint.extend((str(source.path), stat.st_size, stat.st_mtime_ns))
        return hashlib.sha256(repr(fingerprint).encode("utf-8")).hexdigest()[:16]

    def public_metadata(self) -> dict[str, object]:
        return {
            "id": self.overlay_id,
            "display_name": self.display_name,
            "encoding": "indexed",
            "default_opacity": self.default_opacity,
            "initially_visible": self.initially_visible,
            "revision": self.cache_token,
            "classes": [
                {"id": item.id, "name": item.name, "rgba": list(item.rgba)}
                for item in self.classes
            ],
        }

    def validate_slide(self, metadata: SlideMetadata) -> None:
        if metadata.mpp is None:
            raise ValueError("slide has no MPP metadata required by its overlay")
        slide_bounds = (
            0.0,
            0.0,
            metadata.dimensions[0] * metadata.mpp[0],
            metadata.dimensions[1] * metadata.mpp[1],
        )
        if not any(
            _bounds_overlap(fragment.geometry.bounds_um, slide_bounds)
            for fragment in self.fragments
        ):
            raise ValueError("overlay does not intersect the registered slide")


class OverlayRegistry:
    """Registered overlays grouped by their public slide and overlay IDs."""

    def __init__(
        self,
        overlays: Mapping[
            str,
            Mapping[str, IndexedOverlay | str | Path],
        ]
        | None,
        *,
        slide_ids: Sequence[str],
        manifest_slide_id_aliases: Mapping[str, str] | None = None,
    ) -> None:
        registered_slides = set(slide_ids)
        aliases = dict(manifest_slide_id_aliases or {})
        sources: dict[str, dict[str, IndexedOverlay]] = {}
        for slide_id, slide_overlays in (overlays or {}).items():
            if slide_id not in registered_slides:
                raise ValueError(f"overlay references unknown slide {slide_id!r}")
            current: dict[str, IndexedOverlay] = {}
            for overlay_id, value in slide_overlays.items():
                overlay = (
                    load_indexed_overlay_manifest(value)
                    if isinstance(value, (str, Path))
                    else value
                )
                manifest_slide_id = aliases.get(overlay.slide_id, overlay.slide_id)
                if (
                    manifest_slide_id != slide_id
                    or overlay.overlay_id != overlay_id
                ):
                    raise ValueError("overlay registry keys must match manifest IDs")
                current[overlay_id] = overlay
            sources[slide_id] = current
        self._sources = sources

    def for_slide(self, slide_id: str) -> Mapping[str, IndexedOverlay]:
        return MappingProxyType(self._sources.get(slide_id, {}))

    def get(self, slide_id: str, overlay_id: str) -> IndexedOverlay:
        return self._sources[slide_id][overlay_id]

    def __iter__(self) -> Iterator[tuple[str, IndexedOverlay]]:
        for slide_id, overlays in self._sources.items():
            for overlay in overlays.values():
                yield slide_id, overlay


def _manifest_path(base: Path, value: object, *, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{name} must be relative to the manifest")
    resolved = (base / relative).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"{name} escapes the manifest directory")
    return resolved


def _pair(value: object, *, name: str, integer: bool = False) -> tuple:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must contain two values")
    if integer:
        if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            raise ValueError(f"{name} must contain two integers")
        return tuple(value)
    if any(isinstance(item, bool) for item in value):
        raise ValueError(f"{name} must contain two numbers")
    try:
        return tuple(float(item) for item in value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain two numbers") from error


def load_indexed_overlay_manifest(path: str | Path) -> IndexedOverlay:
    """Load and validate a ``wsi-patchkit-overlay/v1`` JSON manifest."""
    manifest_path = Path(path).resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid overlay manifest JSON: {manifest_path}") from error
    if not isinstance(payload, dict):
        raise ValueError("overlay manifest must be a JSON object")
    if payload.get("schema") != "wsi-patchkit-overlay/v1":
        raise ValueError("unsupported overlay manifest schema")
    if payload.get("encoding") != "indexed":
        raise ValueError("overlay manifest encoding must be 'indexed'")

    raw_classes = payload.get("classes")
    if not isinstance(raw_classes, list):
        raise ValueError("overlay manifest classes must be a list")
    classes: list[OverlayClass] = []
    for item in raw_classes:
        if not isinstance(item, dict):
            raise ValueError("overlay class entries must be objects")
        rgba = item.get("rgba")
        if not isinstance(rgba, list):
            raise ValueError("overlay class rgba must be a list")
        classes.append(OverlayClass(item.get("id"), item.get("name"), tuple(rgba)))

    raw_fragments = payload.get("fragments")
    if not isinstance(raw_fragments, list):
        raise ValueError("overlay manifest fragments must be a list")
    base = manifest_path.parent
    fragments: list[OverlayFragment] = []
    for item in raw_fragments:
        if not isinstance(item, dict):
            raise ValueError("overlay fragment entries must be objects")
        coverage_value = item.get("coverage")
        full_coverage = item.get("full_coverage")
        if not isinstance(full_coverage, bool):
            raise ValueError("fragment full_coverage must be a boolean")
        if full_coverage == (coverage_value is not None):
            raise ValueError(
                "provide coverage or set full_coverage=true, but not both"
            )
        geometry = OverlayGeometry(
            origin_um=_pair(item.get("origin_um"), name="origin_um"),
            mpp=_pair(item.get("mpp"), name="mpp"),
            dimensions=_pair(
                item.get("dimensions"), name="dimensions", integer=True
            ),
        )
        source = TiffRasterSource(
            _manifest_path(base, item.get("prediction"), name="prediction")
        )
        coverage = (
            None
            if coverage_value is None
            else BinaryRasterSource(
                _manifest_path(base, coverage_value, name="coverage")
            )
        )
        fragments.append(
            OverlayFragment(
                source=source,
                geometry=geometry,
                coverage=coverage,
                revision=item.get("revision"),
            )
        )

    return IndexedOverlay(
        slide_id=payload.get("slide_id"),
        overlay_id=payload.get("overlay_id"),
        classes=classes,
        fragments=fragments,
        display_name=payload.get("display_name"),
        default_opacity=payload.get("default_opacity", 0.7),
        initially_visible=payload.get("initially_visible", False),
    )


class OverlayRenderer:
    """Render indexed overlays into transparent PNG regions."""

    def __init__(
        self,
        *,
        reader_pool_size: int = 4,
        cache_size: int = 512,
        max_output_pixels: int = 16_777_216,
    ) -> None:
        if cache_size < 0:
            raise ValueError("cache_size must be non-negative")
        if max_output_pixels < 1:
            raise ValueError("max_output_pixels must be positive")
        self._readers = SlideReaderPool(TiffReader, size=reader_pool_size)
        self.cache_size = int(cache_size)
        self.max_output_pixels = int(max_output_pixels)
        self._cache: OrderedDict[tuple[object, ...], EncodedImage] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _fingerprint(overlay: IndexedOverlay) -> tuple[object, ...]:
        values: list[object] = [overlay.slide_id, overlay.overlay_id, overlay.revision]
        for fragment in overlay.fragments:
            for source in (fragment.source, fragment.coverage):
                if source is None:
                    continue
                stat = Path(source.path).stat()
                values.extend((str(source.path), stat.st_size, stat.st_mtime_ns))
        values.extend((item.id, item.rgba) for item in overlay.classes)
        return tuple(values)

    def render_region(
        self,
        overlay: IndexedOverlay,
        slide_metadata: SlideMetadata,
        region: tuple[int, int, int, int],
        output_size: tuple[int, int],
        palette_override: Mapping[int, RGBA] | None = None,
    ) -> EncodedImage:
        overlay.validate_slide(slide_metadata)
        if slide_metadata.mpp is None:  # narrowed by validate_slide
            raise ValueError("slide has no MPP metadata required by its overlay")
        x, y, width, height = map(int, region)
        output_width, output_height = map(int, output_size)
        if x < 0 or y < 0 or width < 1 or height < 1:
            raise ValueError("region must contain non-negative coordinates and size")
        if output_width < 1 or output_height < 1:
            raise ValueError("output size must be positive")
        if output_width * output_height > self.max_output_pixels:
            raise ValueError("requested output exceeds max_output_pixels")
        key = (
            self._fingerprint(overlay),
            (
                tuple(sorted(palette_override.items()))
                if palette_override is not None
                else None
            ),
            slide_metadata.mpp,
            (x, y, width, height),
            (output_width, output_height),
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        rgba = self._render_array(
            overlay,
            slide_metadata.mpp,
            (x, y, width, height),
            (output_width, output_height),
            palette_override,
        )
        buffer = BytesIO()
        Image.fromarray(rgba, "RGBA").save(buffer, format="PNG", compress_level=1)
        content = buffer.getvalue()
        result = EncodedImage(
            content,
            "image/png",
            output_width,
            output_height,
            f'"{hashlib.sha256(content).hexdigest()}"',
        )
        with self._lock:
            if self.cache_size:
                self._cache[key] = result
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        return result

    def _render_array(
        self,
        overlay: IndexedOverlay,
        slide_mpp: MPP,
        region: tuple[int, int, int, int],
        output_size: tuple[int, int],
        palette_override: Mapping[int, RGBA] | None = None,
    ) -> NDArray[np.uint8]:
        x, y, width, height = region
        output_width, output_height = output_size
        x_centres = (
            x + (np.arange(output_width, dtype=np.float64) + 0.5) * width / output_width
        ) * slide_mpp[0]
        y_centres = (
            y
            + (np.arange(output_height, dtype=np.float64) + 0.5)
            * height
            / output_height
        ) * slide_mpp[1]
        result = np.zeros((output_height, output_width, 4), dtype=np.uint8)
        max_id = max(item.id for item in overlay.classes)
        palette = np.zeros((max_id + 1, 4), dtype=np.uint8)
        known = np.zeros(max_id + 1, dtype=bool)
        for item in overlay.classes:
            palette[item.id] = (
                palette_override[item.id] if palette_override is not None else item.rgba
            )
            known[item.id] = True

        with self._readers.acquire() as reader:
            for fragment in overlay.fragments:
                x0, y0, x1, y1 = fragment.geometry.bounds_um
                output_x = np.flatnonzero((x_centres >= x0) & (x_centres < x1))
                output_y = np.flatnonzero((y_centres >= y0) & (y_centres < y1))
                if not output_x.size or not output_y.size:
                    continue
                source_x = np.floor(
                    (x_centres[output_x] - x0) / fragment.geometry.mpp[0]
                ).astype(np.int64)
                source_y = np.floor(
                    (y_centres[output_y] - y0) / fragment.geometry.mpp[1]
                ).astype(np.int64)
                np.clip(
                    source_x, 0, fragment.geometry.dimensions[0] - 1, out=source_x
                )
                np.clip(
                    source_y, 0, fragment.geometry.dimensions[1] - 1, out=source_y
                )
                requested_downsample = (
                    width
                    * slide_mpp[0]
                    / output_width
                    / fragment.geometry.mpp[0],
                    height
                    * slide_mpp[1]
                    / output_height
                    / fragment.geometry.mpp[1],
                )
                if fragment.coverage is None:
                    coverage = None
                else:
                    raw_coverage = self._read_sampled(
                        reader,
                        fragment.coverage.path,
                        source_x,
                        source_y,
                        requested_downsample,
                    )
                    invalid_coverage = (raw_coverage != 0) & (raw_coverage != 1)
                    if np.any(invalid_coverage):
                        values = np.unique(raw_coverage[invalid_coverage]).tolist()
                        raise ValueError(
                            f"coverage TIFF contains values outside 0/1: {values}"
                        )
                    coverage = raw_coverage.astype(bool, copy=False)
                    if not np.any(coverage):
                        continue
                sampled = self._read_sampled(
                    reader,
                    fragment.source.path,
                    source_x,
                    source_y,
                    requested_downsample,
                ).astype(np.int64, copy=False)
                safe = np.minimum(sampled, max_id)
                invalid_ids = (sampled > max_id) | ~known[safe]
                if coverage is not None:
                    invalid_ids &= coverage
                if np.any(invalid_ids):
                    values = np.unique(sampled[invalid_ids]).tolist()
                    raise ValueError(
                        f"prediction contains undeclared class IDs: {values}"
                    )
                colored = palette[safe]
                colored = colored.copy()
                if coverage is not None:
                    colored[~coverage, 3] = 0
                result[np.ix_(output_y, output_x)] = colored
        return result

    @staticmethod
    def _read_sampled(
        reader,
        path: str | Path,
        source_x: NDArray[np.int64],
        source_y: NDArray[np.int64],
        requested_downsample: tuple[float, float],
    ) -> NDArray[np.generic]:
        """Nearest-sample one raster using a suitable native pyramid level."""
        metadata = reader.metadata(path)
        level = choose_level_for_downsample(metadata, requested_downsample)
        level_x = np.floor(source_x / level.downsample[0]).astype(np.int64)
        level_y = np.floor(source_y / level.downsample[1]).astype(np.int64)
        np.clip(level_x, 0, level.dimensions[0] - 1, out=level_x)
        np.clip(level_y, 0, level.dimensions[1] - 1, out=level_y)
        lx0, ly0 = int(level_x.min()), int(level_y.min())
        lx1, ly1 = int(level_x.max()) + 1, int(level_y.max()) + 1
        location = (
            math.ceil(math.nextafter(lx0 * level.downsample[0], -math.inf)),
            math.ceil(math.nextafter(ly0 * level.downsample[1], -math.inf)),
        )
        region = reader.read_region(
            path,
            location,
            level.level,
            (lx1 - lx0, ly1 - ly0),
        )[..., 0]
        return region[np.ix_(level_y - ly0, level_x - lx0)]

    def close(self) -> None:
        with self._lock:
            self._cache.clear()
        self._readers.close()
