"""Physical-center point kernels and footprint area sampling of native blocks."""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from .types import SamplingPlan


def _mapped(index: int, length: int, reflect: bool) -> int:
    if not reflect:
        return min(max(index, 0), length - 1)
    if length == 1:
        return 0
    period = 2 * (length - 1)
    q = index % period
    return q if q < length else period - q


def _indices(base, shift, plan, axis):
    origin = plan.native_origin[axis]
    length = plan.native_dimensions[axis]
    reflect = plan.request.padding_mode == "reflect"
    if length > 2**52 or np.any(np.abs(base) > 2**52):
        # Python integer modulo preserves huge negative/positive phases and
        # each distinct kernel neighbor before mapping, without int64 overflow.
        return np.fromiter(
            (_mapped(int(i) + shift, length, reflect) - origin for i in base),
            dtype=np.intp,
            count=len(base),
        )
    indices = base.astype(np.int64) + shift
    if reflect and length > 1:
        period = 2 * (length - 1)
        indices %= period
        indices = np.minimum(indices, period - indices)
    elif reflect:
        indices = np.zeros_like(indices)
    else:
        indices = np.clip(indices, 0, length - 1)
    return (indices - origin).astype(np.intp)


def _keys(distance):
    t = np.abs(distance)
    return np.where(
        t < 1,
        1.5 * t**3 - 2.5 * t**2 + 1,
        np.where(t < 2, -0.5 * t**3 + 2.5 * t**2 - 4 * t + 2, 0.0),
    )


def _point_axis(centers, plan, axis):
    position = centers / plan.native_mpp[axis] - 0.5
    if plan.request.interpolation == "nearest":
        return [_indices(np.floor(position + 0.5), 0, plan, axis)], [
            np.ones(len(centers))
        ]
    base = np.floor(position)
    fraction = position - base
    if plan.request.interpolation == "bilinear":
        shifts, weights = (0, 1), (1 - fraction, fraction)
    else:
        shifts = (-1, 0, 1, 2)
        # Fraction-relative distance avoids cancellation at far outside origins.
        weights = [_keys(fraction - shift) for shift in shifts]
    return [_indices(base, shift, plan, axis) for shift in shifts], weights


def _area_weights(start, stop, plan, axis):
    """Coalesce reflected/clamped footprint cells in O(mapped support) work.

    Whole-cell counts use Python integers. Complete reflect periods and far
    constant tails do not cause an iteration over the physical footprint.
    """
    length = plan.native_dimensions[axis]
    origin = plan.native_origin[axis]
    reflect = plan.request.padding_mode == "reflect"
    width = stop - start
    first, last = math.floor(start), math.ceil(stop) - 1
    weights = {}

    def add(index, weight):
        local = _mapped(index, length, reflect) - origin
        weights[local] = weights.get(local, 0.0) + weight

    if first == last or length == 1:
        add(first, width)
    else:
        add(first, 1 - (start - first))
        fraction = stop - math.floor(stop)
        add(last, fraction if fraction else 1.0)
        lo, hi = first + 1, last - 1
        if lo <= hi:
            if not reflect:
                if lo < 0:
                    add(0, min(hi, -1) - lo + 1)
                if hi >= length:
                    add(length - 1, hi - max(lo, length) + 1)
                for i in range(max(lo, 0), min(hi, length - 1) + 1):
                    add(i, 1)
            elif hi - lo + 1 <= plan.read_size[axis]:
                for i in range(lo, hi + 1):
                    add(i, 1)
            else:
                period = 2 * (length - 1)

                def count(residue):
                    return (hi - residue) // period - (lo - 1 - residue) // period

                for i in range(origin, origin + plan.read_size[axis]):
                    occurrences = count(i)
                    if i not in (0, length - 1):
                        occurrences += count(period - i)
                    if occurrences:
                        add(i, occurrences)
    ordered = sorted(weights.items())
    return (
        np.fromiter((i for i, _ in ordered), dtype=np.intp),
        np.fromiter((w for _, w in ordered), dtype=np.float64),
    )


def _sample_area(source, plan, output, x_columns, y_rows):
    request = plan.request
    # Generate one pixel's support at a time; no W x native-width matrix.
    # Sum unnormalized coverage before dividing to preserve half-value ties.
    for row in y_rows:
        start_y = (
            request.origin_um[1] + int(row) * request.target_mpp[1]
        ) / plan.native_mpp[1]
        stop_y = (
            request.origin_um[1] + (int(row) + 1) * request.target_mpp[1]
        ) / plan.native_mpp[1]
        iy, wy = _area_weights(start_y, stop_y, plan, 1)
        for col in x_columns:
            start_x = (
                request.origin_um[0] + int(col) * request.target_mpp[0]
            ) / plan.native_mpp[0]
            stop_x = (
                request.origin_um[0] + (int(col) + 1) * request.target_mpp[0]
            ) / plan.native_mpp[0]
            ix, wx = _area_weights(start_x, stop_x, plan, 0)
            width_x, width_y = stop_x - start_x, stop_y - start_y
            if width_x > 1e150 or width_y > 1e150:
                # Scale huge coverage counts before products can overflow.
                wx = wx / width_x
                y_weights = wy / width_y
                denominator = 1.0
            else:
                y_weights = wy
                denominator = width_x * width_y
            value = np.zeros(3, dtype=np.float64)
            for offset in range(0, len(iy), 64):
                stop = offset + 64
                values = (
                    source[iy[offset:stop, None], ix[None, :]].astype(np.float64)
                    * wx[None, :, None]
                    * y_weights[offset:stop, None, None]
                )
                value += values.sum(axis=(0, 1), dtype=np.float64)
            output[int(row), int(col)] = np.clip(
                np.floor(value / denominator + 0.5), 0, 255
            ).astype(np.uint8)


def sample_patch(source: NDArray[np.generic], plan: SamplingPlan):
    """Sample into an owned output, with a mask independent of padding/kernels."""
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
    if request.padding_mode == "reflect":
        x_columns, y_rows = np.arange(width), np.arange(height)
    else:
        x_columns, y_rows = np.flatnonzero(valid_x), np.flatnonzero(valid_y)
    if not len(x_columns) or not len(y_rows):
        return output, mask
    if request.interpolation == "area":
        _sample_area(source, plan, output, x_columns, y_rows)
        return output, mask
    ix, wx = _point_axis(xs[x_columns], plan, 0)
    iy, wy = _point_axis(ys[y_rows], plan, 1)
    x_start, x_stop = int(x_columns[0]), int(x_columns[-1]) + 1
    for offset in range(0, len(y_rows), 64):
        rows = y_rows[offset : offset + 64]
        stop = offset + len(rows)
        destination = output[int(rows[0]) : int(rows[-1]) + 1, x_start:x_stop]
        if request.interpolation == "nearest":
            destination[...] = source[iy[0][offset:stop, None], ix[0][None, :]]
            continue
        value = np.zeros((len(rows), len(x_columns), 3), dtype=np.float64)
        for y_neighbor in range(len(iy)):
            for x_neighbor in range(len(ix)):
                value += (
                    source[
                        iy[y_neighbor][offset:stop, None], ix[x_neighbor][None, :]
                    ].astype(np.float64)
                    * wx[x_neighbor][None, :, None]
                    * wy[y_neighbor][offset:stop, None, None]
                )
        destination[...] = np.clip(np.floor(value + 0.5), 0, 255).astype(np.uint8)
    return output, mask
