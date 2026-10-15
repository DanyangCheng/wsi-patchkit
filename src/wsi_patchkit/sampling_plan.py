"""Pure, scalar planning of continuous grids and one bounded native read."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from ._validation import derived_finite, finite_product, integer, physical_pair
from .geometry import native_to_level_zero
from .types import (
    LevelInfo,
    PhysicalPatchRequest,
    SamplingLimits,
    SamplingPlan,
    SlideMetadata,
)

DEFAULT_LIMITS = SamplingLimits()


def _center(origin: float, target: float, index: int) -> float:
    return derived_finite(origin + (index + 0.5) * target, "grid center")


def _valid_indices(origin, target, count, extent):
    """Find the half-open valid output-index interval without a coordinate array."""

    def first_at_least(boundary):
        lo, hi = 0, count
        while lo < hi:
            mid = (lo + hi) // 2
            if _center(origin, target, mid) < boundary:
                lo = mid + 1
            else:
                hi = mid
        return lo

    return first_at_least(0.0), first_at_least(extent)


def _kernel_bounds(request, axis, index, native_mpp):
    origin = request.origin_um[axis]
    target = request.target_mpp[axis]
    if request.interpolation == "area":
        start = derived_finite(
            (origin + index * target) / native_mpp, f"area footprint[{axis}]"
        )
        stop = derived_finite(
            (origin + (index + 1) * target) / native_mpp, f"area footprint[{axis}]"
        )
        if stop <= start:
            raise ValueError(f"area footprint[{axis}] collapses at floating precision")
        derived_finite(stop - start, f"area footprint width[{axis}]")
        return math.floor(start), math.ceil(stop) - 1
    position = derived_finite(
        _center(origin, target, index) / native_mpp - 0.5,
        f"native center index[{axis}]",
    )
    if request.interpolation == "nearest":
        index = math.floor(position + 0.5)
        return index, index
    base = math.floor(position)
    if request.interpolation == "bilinear":
        return base, base + 1
    return base - 1, base + 2


def _reflect_bounds(lo: int, hi: int, length: int) -> tuple[int, int]:
    """Extrema of a reflected integer interval, including very wide footprints."""
    if length == 1:
        return 0, 0
    period = 2 * (length - 1)

    def reflect(index):
        q = index % period
        return q if q < length else period - q

    lower, upper = sorted((reflect(lo), reflect(hi)))
    if lo + (-lo) % period <= hi:
        lower = 0
    if lo + (length - 1 - lo) % period <= hi:
        upper = length - 1
    return lower, upper


def _axis_read_bounds(request, axis, native_mpp, length, valid):
    if request.padding_mode == "constant":
        start, stop = valid
        first, _ = _kernel_bounds(request, axis, start, native_mpp)
        _, last = _kernel_bounds(request, axis, stop - 1, native_mpp)
        return max(0, min(first, length - 1)), max(0, min(last, length - 1))
    if length == 1:
        return 0, 0
    lower, upper = length - 1, 0
    # Point kernels may skip pixels at large strides; reflecting their entire
    # envelope would overestimate the actual read. No HW or axis arrays exist.
    for index in range(request.output_size[axis]):
        lo, hi = _kernel_bounds(request, axis, index, native_mpp)
        lo, hi = _reflect_bounds(lo, hi, length)
        lower, upper = min(lower, lo), max(upper, hi)
        if lower == 0 and upper == length - 1:
            break
    return lower, upper


def _select_level(metadata: SlideMetadata, request: PhysicalPatchRequest) -> LevelInfo:
    if request.level is not None:
        if request.level >= len(metadata.levels):
            raise ValueError(
                f"level={request.level} is unavailable for slide={request.slide!r}; "
                f"available levels=0..{len(metadata.levels) - 1}"
            )
        selected = metadata.levels[request.level]
        if selected.mpp is None:
            raise ValueError("level.mpp is missing for the explicitly selected level")
        return selected
    candidates = [level for level in metadata.levels if level.mpp is not None]
    if not candidates:
        raise ValueError("level.mpp is missing; provide physical-resolution metadata")
    # Difference of logs and sum of logs avoid ratio/product overflow.
    target_logs = tuple(math.log(m) for m in request.target_mpp)
    if request.level_policy == "nearest":
        return min(
            candidates,
            key=lambda level: (
                sum(abs(math.log(level.mpp[a]) - target_logs[a]) for a in (0, 1)),
                level.level,
            ),
        )
    eligible = [
        level
        for level in candidates
        if all(level.mpp[a] <= request.target_mpp[a] for a in (0, 1))
    ]
    if eligible:
        return min(
            eligible,
            key=lambda level: (-sum(math.log(m) for m in level.mpp), level.level),
        )
    return min(
        candidates, key=lambda level: (sum(math.log(m) for m in level.mpp), level.level)
    )


def _validate_pixel_format(selected, request, *, outside):
    pixel_format = selected.pixel_format
    if pixel_format is None:
        if request.color_mode == "native" and outside:
            raise ValueError("pixel_format is required for an outside native request")
        return
    channels = integer(pixel_format.channels, "pixel_format.channels", minimum=1)
    try:
        dtype = np.dtype(pixel_format.dtype)
    except (TypeError, ValueError) as error:
        raise ValueError("pixel_format.dtype is unsupported") from error
    if request.color_mode == "rgb":
        if dtype != np.dtype("uint8") or channels not in (1, 3, 4):
            raise ValueError(
                "pixel_format: RGB requires uint8 with 1, 3, or 4 channels"
            )
    else:
        if dtype.kind not in "biu" or dtype.itemsize not in (1, 2, 4, 8):
            raise ValueError("pixel_format.dtype: native requires bool or integer data")
        minimum, maximum = (
            (0, 1) if dtype.kind == "b" else (np.iinfo(dtype).min, np.iinfo(dtype).max)
        )
        if not minimum <= request.fill_value <= maximum:
            raise ValueError(f"fill_value is outside the native {dtype} range")


def plan_physical_read(
    metadata: SlideMetadata,
    request: PhysicalPatchRequest,
    *,
    limits: SamplingLimits = DEFAULT_LIMITS,
) -> SamplingPlan:
    """Plan exact output centers and native IO without reading or allocating pixels.

    Constant planning uses scalar endpoint/binary searches. Reflect planning
    uses at most O(width + height) scalar work, and O(1) auxiliary memory.
    Zero-weight kernel neighbors may be retained in the single-region budget.
    """
    if not isinstance(request, PhysicalPatchRequest):
        raise ValueError("request must be a PhysicalPatchRequest")
    if not isinstance(metadata, SlideMetadata):
        raise ValueError("metadata must be SlideMetadata")
    if not isinstance(limits, SamplingLimits):
        raise ValueError("limits must be SamplingLimits; use field=None to disable")
    if Path(request.slide).resolve() != Path(metadata.path).resolve():
        raise ValueError("metadata.path does not match request.slide")
    if metadata.mpp is None:
        raise ValueError("metadata.mpp is missing; provide source_mpp metadata")
    if request.source_mpp is not None and request.source_mpp != metadata.mpp:
        raise ValueError("metadata.mpp does not match request.source_mpp")
    selected = _select_level(metadata, request)
    native_mpp = physical_pair(selected.mpp, "level.mpp", scalar=True)
    if request.source_mpp is not None:
        expected = tuple(
            derived_finite(
                request.source_mpp[a] * selected.downsample[a],
                f"source_mpp*downsample[{a}]",
            )
            for a in (0, 1)
        )
        if native_mpp != expected:
            raise ValueError("level.mpp does not match source_mpp*downsample")
    context = (
        f"slide={request.slide!r}, origin_um={request.origin_um}, "
        f"source_level={selected.level}, output_size={request.output_size}"
    )
    output_pixels = request.output_size[0] * request.output_size[1]
    if (
        limits.max_output_pixels is not None
        and output_pixels > limits.max_output_pixels
    ):
        raise ValueError(
            f"max_output_pixels exceeded: {context}, "
            f"output_pixels={output_pixels}, limit={limits.max_output_pixels}"
        )
    if request.interpolation == "area" and any(
        request.target_mpp[a] < native_mpp[a] for a in (0, 1)
    ):
        raise ValueError(f"interpolation=area cannot enlarge either axis: {context}")
    extent = []
    valid = []
    for axis in (0, 1):
        extent.append(
            finite_product(
                metadata.dimensions[axis],
                metadata.mpp[axis],
                f"metadata physical extent[{axis}]",
            )
        )
        derived_finite(
            request.target_mpp[axis] / native_mpp[axis], f"target_mpp/level.mpp[{axis}]"
        )
        # Validate both endpoints even when constant padding skips every pixel.
        for index in (0, request.output_size[axis] - 1):
            _kernel_bounds(request, axis, index, native_mpp[axis])
        valid.append(
            _valid_indices(
                request.origin_um[axis],
                request.target_mpp[axis],
                request.output_size[axis],
                extent[axis],
            )
        )
        if request.interpolation == "area":
            origin = request.origin_um[axis]
            end = origin + request.output_size[axis] * request.target_mpp[axis]
            spacing = request.target_mpp[axis]
            if spacing < math.ulp(max(abs(origin), abs(end))) or spacing / native_mpp[
                axis
            ] < math.ulp(
                max(abs(origin / native_mpp[axis]), abs(end / native_mpp[axis]))
            ):
                start, stop = (
                    valid[-1]
                    if request.padding_mode == "constant"
                    else (0, request.output_size[axis])
                )
                for index in range(start, stop):
                    _kernel_bounds(request, axis, index, native_mpp[axis])
    outside = any(lo == hi for lo, hi in valid)
    no_read = request.padding_mode == "constant" and outside
    _validate_pixel_format(selected, request, outside=outside)
    origin = location = None
    read_size = (0, 0)
    if not no_read:
        bounds = [
            _axis_read_bounds(
                request, a, native_mpp[a], selected.dimensions[a], valid[a]
            )
            for a in (0, 1)
        ]
        origin = tuple(lo for lo, _ in bounds)
        read_size = tuple(hi - lo + 1 for lo, hi in bounds)
        read_pixels = read_size[0] * read_size[1]
        if limits.max_read_pixels is not None and read_pixels > limits.max_read_pixels:
            raise ValueError(
                f"max_read_pixels exceeded: {context}, "
                f"read_size={read_size}, read_pixels={read_pixels}, "
                f"limit={limits.max_read_pixels}"
            )
        location = tuple(
            native_to_level_zero(origin[a], selected.downsample[a]) for a in (0, 1)
        )
    return SamplingPlan(
        request=request,
        grid=request.grid,
        source_level=selected.level,
        native_origin=origin,
        read_location=location,
        read_size=read_size,
        limits=limits,
        native_mpp=native_mpp,
        native_dimensions=selected.dimensions,
        source_extent_um=tuple(extent),
        pixel_format=selected.pixel_format,
    )
