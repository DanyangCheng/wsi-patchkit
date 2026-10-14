"""Physical-resolution geometry and aligned patch reads."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from ._canvas import CanvasRounding, canvas_size
from ._validation import derived_finite, integer, option
from .io.base import SlideReader
from .types import LevelInfo, PatchRequest, SlideMetadata, as_mpp

LevelSelectionPolicy = Literal["nearest", "finer"]
Interpolation = Literal["nearest", "bilinear", "area"]


@dataclass(frozen=True, slots=True)
class ReadPlan:
    """Mapping from one virtual-canvas patch to one pyramid-level read."""

    level: int
    level_zero_location: tuple[int, int]
    level_size: tuple[int, int]
    destination_box: tuple[int, int, int, int]
    output_size: tuple[int, int]


@dataclass(frozen=True, slots=True)
class PatchReadResult:
    """A materialized patch together with its geometric coverage information.

    ``valid_mask`` is true only where output pixels originate from the source
    WSI. It deliberately has no tissue, ROI, or annotation semantics.
    """

    image: NDArray[np.generic]
    valid_mask: NDArray[np.bool_]
    plan: ReadPlan | None


def choose_level(
    metadata: SlideMetadata,
    target_mpp: tuple[float, float],
    *,
    policy: LevelSelectionPolicy = "nearest",
) -> LevelInfo:
    """Choose a pyramid level for a requested physical resolution.

    ``nearest`` preserves the original behaviour. ``finer`` chooses the
    coarsest level that is no coarser than the requested MPP, avoiding an
    unnecessary loss of detail caused by upsampling a coarser level.
    """
    candidates = [level for level in metadata.levels if level.mpp is not None]
    if not candidates:
        raise ValueError("slide has no physical-resolution metadata")
    if policy not in ("nearest", "finer"):
        raise ValueError("policy must be 'nearest' or 'finer'")
    if policy == "finer":
        eligible = [
            level
            for level in candidates
            if level.mpp[0] <= target_mpp[0] and level.mpp[1] <= target_mpp[1]
        ]
        if eligible:
            return max(
                eligible,
                key=lambda level: level.mpp[0] * level.mpp[1],  # type: ignore[index]
            )
        return min(
            candidates,
            key=lambda level: level.mpp[0] * level.mpp[1],  # type: ignore[index]
        )
    return min(
        candidates,
        key=lambda level: sum(
            abs(math.log(level.mpp[axis] / target_mpp[axis]))  # type: ignore[index]
            for axis in (0, 1)
        ),
    )


def virtual_canvas_size(
    metadata: SlideMetadata,
    target_mpp: tuple[float, float],
    *,
    canvas_rounding: CanvasRounding = "round",
) -> tuple[int, int]:
    """Return the level-0 physical extent sampled at ``target_mpp``."""
    if metadata.mpp is None:
        raise ValueError("slide has no MPP metadata; provide source_mpp")
    target = as_mpp(target_mpp, name="target_mpp")
    return canvas_size(metadata.dimensions, metadata.mpp, target, canvas_rounding)


def native_to_level_zero(index: int, downsample: float) -> int:
    """Stable integer location for readers mapping floor(location/downsample)."""
    try:
        product = derived_finite(index * downsample, "read_location")
    except OverflowError as error:
        raise ValueError("read_location derived range overflow") from error
    return math.ceil(math.nextafter(product, -math.inf))


def plan_aligned_read(
    metadata: SlideMetadata,
    request: PatchRequest,
    *,
    level: int | None = None,
    level_policy: LevelSelectionPolicy = "nearest",
    canvas_rounding: CanvasRounding | None = None,
) -> ReadPlan | None:
    """Plan an MPP-aligned, clipped read for one request."""
    if metadata.mpp is None:
        raise ValueError("slide has no MPP metadata; set PatchRequest.source_mpp")
    option(level_policy, "level_policy", ("nearest", "finer"))
    if level is not None:
        level = integer(level, "level", minimum=0)
        if level >= len(metadata.levels):
            raise ValueError(f"level {level} is unavailable for {metadata.path!r}")
    selected = (
        choose_level(metadata, request.target_mpp, policy=level_policy)
        if level is None
        else metadata.levels[level]
    )
    if selected.mpp is None:
        raise ValueError("selected level has no MPP metadata")
    canvas_width, canvas_height = virtual_canvas_size(
        metadata,
        request.target_mpp,
        canvas_rounding=(
            request.canvas_rounding if canvas_rounding is None else canvas_rounding
        ),
    )
    x0, y0 = max(0, request.x), max(0, request.y)
    x1 = min(canvas_width, request.x + request.width)
    y1 = min(canvas_height, request.y + request.height)
    if x1 <= x0 or y1 <= y0:
        return None

    level_width, level_height = selected.dimensions
    lx0 = max(0, math.floor(x0 * request.target_mpp[0] / selected.mpp[0]))
    ly0 = max(0, math.floor(y0 * request.target_mpp[1] / selected.mpp[1]))
    lx1 = min(
        level_width,
        math.ceil(x1 * request.target_mpp[0] / selected.mpp[0]),
    )
    ly1 = min(
        level_height,
        math.ceil(y1 * request.target_mpp[1] / selected.mpp[1]),
    )
    if lx1 <= lx0 or ly1 <= ly0:
        return None
    # ``read_region`` locations use level-0 pixels, while ``lx0``/``ly0`` are
    # exact coordinates on the selected level.  Rounding down here can make a
    # reader's floor(location / downsample) select the preceding level pixel
    # for non-integral pyramid ratios (for example 1000 / 333).  Ceil preserves
    # the selected-level coordinate.  nextafter keeps mathematically integral
    # products stable in the presence of a one-ULP floating-point overshoot.
    level_zero_location = (
        native_to_level_zero(lx0, selected.downsample[0]),
        native_to_level_zero(ly0, selected.downsample[1]),
    )
    destination = (
        x0 - request.x,
        y0 - request.y,
        x1 - request.x,
        y1 - request.y,
    )
    return ReadPlan(
        selected.level,
        level_zero_location,
        (lx1 - lx0, ly1 - ly0),
        destination,
        (request.width, request.height),
    )


def _as_rgb(array: NDArray[np.generic]) -> NDArray[np.uint8]:
    if array.dtype != np.uint8:
        raise ValueError("RGB patch reads currently require an 8-bit WSI")
    if array.ndim == 2:
        array = array[..., None]
    if array.ndim != 3:
        raise ValueError(f"reader returned unsupported shape {array.shape}")
    if array.shape[2] == 1:
        array = np.repeat(array, 3, axis=2)
    elif array.shape[2] == 4:
        alpha = array[..., 3:4].astype(np.uint32)
        array = (
            array[..., :3].astype(np.uint32) * alpha + 255 * (255 - alpha) + 127
        ) // 255
    elif array.shape[2] >= 3:
        array = array[..., :3]
    else:
        raise ValueError(f"reader returned unsupported channel count {array.shape[2]}")
    return np.asarray(array, dtype=np.uint8)


def _resize_nearest(
    array: NDArray[np.generic],
    size: tuple[int, int],
) -> NDArray[np.generic]:
    width, height = size
    source_height, source_width = array.shape[:2]
    xs = np.minimum(
        (np.arange(width, dtype=np.int64) * source_width) // width,
        source_width - 1,
    )
    ys = np.minimum(
        (np.arange(height, dtype=np.int64) * source_height) // height,
        source_height - 1,
    )
    return array[np.ix_(ys, xs)]


def _resize_rgb(
    array: NDArray[np.uint8],
    size: tuple[int, int],
    interpolation: Interpolation,
) -> NDArray[np.uint8]:
    if interpolation not in ("nearest", "bilinear", "area"):
        raise ValueError("interpolation must be 'nearest', 'bilinear', or 'area'")
    if (array.shape[1], array.shape[0]) == size:
        return array
    if interpolation == "nearest":
        return np.asarray(_resize_nearest(array, size), dtype=np.uint8)
    if interpolation == "area" and (
        size[0] > array.shape[1] or size[1] > array.shape[0]
    ):
        raise ValueError("area interpolation only supports downsampling")
    image = Image.fromarray(array, mode="RGB")
    resample = (
        Image.Resampling.BOX if interpolation == "area" else Image.Resampling.BILINEAR
    )
    return np.asarray(image.resize(size, resample=resample))


def read_aligned_patch_result(
    reader: SlideReader,
    request: PatchRequest,
    *,
    metadata: SlideMetadata | None = None,
    level: int | None = None,
    level_policy: LevelSelectionPolicy = "nearest",
    interpolation: Interpolation = "bilinear",
    color_mode: Literal["rgb", "native"] = "rgb",
    canvas_rounding: CanvasRounding | None = None,
) -> PatchReadResult:
    """Materialize a request and report which output pixels came from the WSI."""
    metadata = metadata or reader.metadata(
        request.slide,
        source_mpp=request.source_mpp,
    )
    plan = plan_aligned_read(
        metadata,
        request,
        level=level,
        level_policy=level_policy,
        canvas_rounding=canvas_rounding,
    )
    if color_mode == "rgb":
        result: NDArray[np.generic] = np.full(
            (request.height, request.width, 3),
            request.fill_value,
            dtype=np.uint8,
        )
    else:
        result = np.full(
            (request.height, request.width, 1),
            request.fill_value,
            dtype=np.uint8,
        )
    valid_mask = np.zeros((request.height, request.width), dtype=bool)
    if plan is None:
        return PatchReadResult(result, valid_mask, None)

    region = reader.read_region(
        request.slide,
        plan.level_zero_location,
        plan.level,
        plan.level_size,
    )
    if region.ndim == 2:
        region = region[..., None]
    dx0, dy0, dx1, dy1 = plan.destination_box
    destination_size = dx1 - dx0, dy1 - dy0
    if color_mode == "rgb":
        resized = _resize_rgb(_as_rgb(region), destination_size, interpolation)
    else:
        resized = (
            region
            if (region.shape[1], region.shape[0]) == destination_size
            else _resize_nearest(region, destination_size)
        )
        if result.shape[2] != resized.shape[2] or result.dtype != resized.dtype:
            result = np.full(
                (request.height, request.width, resized.shape[2]),
                request.fill_value,
                dtype=resized.dtype,
            )
    result[dy0:dy1, dx0:dx1] = resized
    valid_mask[dy0:dy1, dx0:dx1] = True
    return PatchReadResult(result, valid_mask, plan)


def read_aligned_patch(
    reader: SlideReader,
    request: PatchRequest,
    *,
    metadata: SlideMetadata | None = None,
    level: int | None = None,
    level_policy: LevelSelectionPolicy = "nearest",
    interpolation: Interpolation = "bilinear",
    color_mode: Literal["rgb", "native"] = "rgb",
    canvas_rounding: CanvasRounding | None = None,
) -> NDArray[np.generic]:
    """Materialize a target-MPP request as a padded HWC array.

    Use :func:`read_aligned_patch_result` when geometric coverage is required.
    """
    return read_aligned_patch_result(
        reader,
        request,
        metadata=metadata,
        level=level,
        level_policy=level_policy,
        interpolation=interpolation,
        color_mode=color_mode,
        canvas_rounding=canvas_rounding,
    ).image
