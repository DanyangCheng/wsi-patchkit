"""Exact synchronous patch reads over the existing native reader protocol."""

from __future__ import annotations

import numpy as np

from .geometry import _as_rgb
from .io.base import SlideReader
from .resampling import sample_patch
from .sampling_plan import DEFAULT_LIMITS, plan_physical_read
from .types import (
    PhysicalPatchRequest,
    PhysicalPatchResult,
    SamplingLimits,
    SamplingPlan,
    SlideMetadata,
)


def _check_shape(plan, channels, dtype, *, read=False):
    """Catch address-space shape overflow before attempting NumPy allocation."""
    size = plan.read_size if read else plan.grid.output_size
    field = "read_size" if read else "output_size"
    width, height = size
    maximum = np.iinfo(np.intp).max
    if (
        max(width, height, channels) > maximum
        or width * height * channels * dtype.itemsize > maximum
    ):
        raise ValueError(
            f"{field} cannot be addressed: slide={plan.request.slide!r}, "
            f"source_level={plan.source_level}, {field}={size}, "
            f"channels={channels}, dtype={dtype}"
        )


def _native_fill(dtype, fill):
    if dtype.kind not in "biu" or dtype.itemsize not in (1, 2, 4, 8):
        raise ValueError("image.dtype: native requires bool or integer data")
    lo, hi = (0, 1) if dtype.kind == "b" else (np.iinfo(dtype).min, np.iinfo(dtype).max)
    if not lo <= fill <= hi:
        raise ValueError(f"fill_value is outside the native {dtype} range")


def _region_format(region, plan):
    if not isinstance(region, np.ndarray):
        raise ValueError("image: reader must return a numpy array")
    if region.ndim == 2:
        region = region[..., None]
    if (
        region.ndim != 3
        or region.shape[:2] != (plan.read_size[1], plan.read_size[0])
        or region.shape[2] < 1
    ):
        raise ValueError(
            f"image.shape: reader returned {region.shape}, "
            f"expected read_size={plan.read_size} HWC"
        )
    pixel_format = plan.pixel_format
    if pixel_format is not None and (
        region.dtype != np.dtype(pixel_format.dtype)
        or region.shape[2] != pixel_format.channels
    ):
        raise ValueError("image.dtype/channels do not match pixel_format metadata")
    if plan.request.color_mode == "rgb":
        if region.dtype != np.uint8 or region.shape[2] not in (1, 3, 4):
            raise ValueError(
                "image.dtype/channels: RGB requires uint8 with 1, 3, or 4 channels"
            )
        return _as_rgb(region)
    _native_fill(region.dtype, plan.request.fill_value)
    return region


def read_physical_patch_result(
    reader: SlideReader,
    request: PhysicalPatchRequest,
    *,
    metadata: SlideMetadata | None = None,
    limits: SamplingLimits = DEFAULT_LIMITS,
    plan: SamplingPlan | None = None,
) -> PhysicalPatchResult:
    """Read an exact physical patch with an independent center-valid mask.

    Metadata IO is allowed even for fully outside requests; pixel IO is not.
    A supplied plan must equal the plan for this metadata/request/limits; pass
    its limits explicitly when using custom limits. Readers remain borrowed.
    RGB supports nearest/bilinear/bicubic/area; native supports nearest.
    """
    if not isinstance(request, PhysicalPatchRequest):
        raise ValueError("request must be a PhysicalPatchRequest")
    if plan is not None and not isinstance(plan, SamplingPlan):
        raise ValueError("plan must be a SamplingPlan")
    if metadata is None:
        metadata = reader.metadata(request.slide, source_mpp=request.source_mpp)
    canonical = plan_physical_read(metadata, request, limits=limits)
    if plan is not None and plan != canonical:
        raise ValueError("plan does not match request, metadata geometry, or limits")
    plan = canonical if plan is None else plan
    if request.color_mode == "rgb":
        channels, dtype = 3, np.dtype("uint8")
    elif plan.pixel_format is not None:
        channels, dtype = plan.pixel_format.channels, np.dtype(plan.pixel_format.dtype)
    else:
        channels, dtype = 1, np.dtype("uint8")  # Refined after an in-bounds read.
    _check_shape(plan, channels, dtype)
    if plan.pixel_format is not None:
        _check_shape(
            plan,
            plan.pixel_format.channels,
            np.dtype(plan.pixel_format.dtype),
            read=True,
        )
    else:
        _check_shape(plan, channels, dtype, read=True)
    if plan.read_pixels == 0:
        # Native outside format was checked by the planner; never infer via IO.
        image = np.empty(
            (request.output_size[1], request.output_size[0], channels), dtype=dtype
        )
        image[...] = request.fill_value
        mask = np.zeros(image.shape[:2], dtype=bool)
    else:
        region = reader.read_region(
            request.slide, plan.read_location, plan.source_level, plan.read_size
        )
        region = _region_format(region, plan)
        _check_shape(plan, region.shape[2], region.dtype)
        image, mask = sample_patch(region, plan)
    return PhysicalPatchResult(request, image, mask, plan.grid, plan.source_level, plan)
