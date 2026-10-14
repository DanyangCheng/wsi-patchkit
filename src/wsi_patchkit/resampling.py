"""Center-based point sampling of a planned native block.

This module samples globally defined physical centers. A native read rectangle
only supplies pixels; it never sets the output phase or resize ratio.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from .types import SamplingPlan


def _local_indices(indices, origin: int, length: int):
    """Clamp global integer neighbors before converting to local intp indices."""
    end = origin + length - 1
    if abs(origin) > 2**53 or abs(end) > 2**53:
        # Preserve integer clipping when a metadata edge is not float64-exact.
        return np.fromiter(
            (min(max(int(i), origin), end) - origin for i in indices),
            dtype=np.intp,
            count=len(indices),
        )
    return (np.clip(indices, origin, end) - origin).astype(np.intp)


def sample_constant(
    source: NDArray[np.generic],
    plan: SamplingPlan,
) -> tuple[NDArray[np.generic], NDArray[np.bool_]]:
    """Nearest or bilinear into an owned constant-padded output array."""
    request = plan.request
    width, height = request.output_size
    output = np.empty((height, width, source.shape[2]), dtype=source.dtype)
    output[...] = request.fill_value
    xs = (
        request.origin_um[0]
        + (np.arange(width, dtype=np.float64) + 0.5) * request.target_mpp[0]
    )
    ys = (
        request.origin_um[1]
        + (np.arange(height, dtype=np.float64) + 0.5) * request.target_mpp[1]
    )
    valid_x = (xs >= 0) & (xs < plan.source_extent_um[0])
    valid_y = (ys >= 0) & (ys < plan.source_extent_um[1])
    mask = valid_y[:, None] & valid_x[None, :]
    x_columns = np.flatnonzero(valid_x)
    y_rows = np.flatnonzero(valid_y)
    if not len(x_columns) or not len(y_rows):
        return output, mask
    # Only valid centers enter native-coordinate arithmetic. Far outside
    # centers need no integer conversion and cannot overflow an intp index.
    sx = xs[valid_x] / plan.native_mpp[0] - 0.5
    sy = ys[valid_y] / plan.native_mpp[1] - 0.5
    origin_x, origin_y = plan.native_origin
    read_width, read_height = plan.read_size
    x_start, x_stop = int(x_columns[0]), int(x_columns[-1]) + 1
    if request.interpolation == "nearest":
        ix = _local_indices(np.floor(sx + 0.5), origin_x, read_width)
        iy = _local_indices(np.floor(sy + 0.5), origin_y, read_height)
        # Bound temporary pixel gathers; each strip uses the same global axes.
        for offset in range(0, len(y_rows), 64):
            rows = y_rows[offset : offset + 64]
            output[int(rows[0]) : int(rows[-1]) + 1, x_start:x_stop] = source[
                iy[offset : offset + 64, None], ix[None, :]
            ]
        return output, mask
    floor_x, floor_y = np.floor(sx), np.floor(sy)
    fx, fy = sx - floor_x, sy - floor_y
    ix = [_local_indices(floor_x + shift, origin_x, read_width) for shift in (0, 1)]
    iy = [_local_indices(floor_y + shift, origin_y, read_height) for shift in (0, 1)]
    wx, wy = (1 - fx, fx), (1 - fy, fy)
    for offset in range(0, len(y_rows), 64):
        rows = y_rows[offset : offset + 64]
        stop = offset + len(rows)
        value = np.zeros((len(rows), len(x_columns), 3), dtype=np.float64)
        # Match the scalar contract's multiplication order (pixel * wx * wy).
        for y_neighbor in (0, 1):
            for x_neighbor in (0, 1):
                value += (
                    source[
                        iy[y_neighbor][offset:stop, None], ix[x_neighbor][None, :]
                    ].astype(np.float64)
                    * wx[x_neighbor][None, :, None]
                    * wy[y_neighbor][offset:stop, None, None]
                )
        output[int(rows[0]) : int(rows[-1]) + 1, x_start:x_stop] = np.clip(
            np.floor(value + 0.5), 0, 255
        ).astype(np.uint8)
    return output, mask
