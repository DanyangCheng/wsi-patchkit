"""Small scalar oracle for contract v1; never imports production sampling code."""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np


def gradient(width=8, height=4):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    for row in range(height):
        for col in range(width):
            image[row, col] = (10 * col, 20 * row, 0)
    return image


def impulse(width=7, height=7):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[height // 2, width // 2] = 255
    return image


def reflect_index(index, length):
    if length == 1:
        return 0
    period = 2 * (length - 1)
    folded = index % period
    return folded if folded < length else period - folded


def keys(distance):
    t = abs(distance)
    if t < 1:
        return 1.5 * t**3 - 2.5 * t**2 + 1
    if t < 2:
        return -0.5 * t**3 + 2.5 * t**2 - 4 * t + 2
    return 0.0


def point_weights(position, mode):
    base = math.floor(position)
    if mode == "nearest":
        return [(math.floor(position + 0.5), 1.0)]
    if mode == "bilinear":
        fraction = position - base
        return [(base, 1 - fraction), (base + 1, fraction)]
    if mode == "bicubic":
        return [(i, keys(position - i)) for i in range(base - 1, base + 3)]
    raise ValueError(f"unknown interpolation: {mode}")


def area_weights(start, stop, *, exact=False):
    """Interval coordinates measured in native pixel boundary units."""
    if exact:
        start, stop = Fraction(float(start)), Fraction(float(stop))
    return [
        (i, (min(stop, i + 1) - max(start, i)) / (stop - start))
        for i in range(math.floor(start), math.ceil(stop))
        if min(stop, i + 1) > max(start, i)
    ]


def quantize(value):
    return np.clip(np.floor(np.asarray(value, dtype=np.float64) + 0.5), 0, 255).astype(
        np.uint8
    )


def sample_rgb(
    image,
    *,
    origin,
    target,
    size,
    native=(1.0, 1.0),
    extent=None,
    mode="bilinear",
    padding="constant",
    fill=255,
):
    """Return image, mask, centers, unquantized values for tiny RGB fixtures.

    extent is level-0 physical extent, independently of the selected level.
    This deliberately reads a full test array; it is not an IO planner.
    """
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("oracle requires HWC uint8 RGB")
    if padding not in ("constant", "reflect"):
        raise ValueError("unknown padding")
    if mode not in ("nearest", "bilinear", "bicubic", "area"):
        raise ValueError("unknown interpolation")
    if mode == "area" and any(target[a] < native[a] for a in (0, 1)):
        raise ValueError("area cannot enlarge either axis")
    height, width = image.shape[:2]
    extent = extent if extent is not None else (width * native[0], height * native[1])
    raw = np.empty((size[1], size[0], 3), dtype=np.float64)
    mask = np.empty((size[1], size[0]), dtype=bool)
    centers = np.empty((size[1], size[0], 2), dtype=np.float64)

    def mapped(index, length):
        if padding == "reflect":
            return reflect_index(index, length)
        return min(max(index, 0), length - 1)

    for row in range(size[1]):
        for col in range(size[0]):
            ux = origin[0] + (col + 0.5) * target[0]
            uy = origin[1] + (row + 0.5) * target[1]
            centers[row, col] = ux, uy
            valid = 0 <= ux < extent[0] and 0 <= uy < extent[1]
            mask[row, col] = valid
            if not valid and padding == "constant":
                raw[row, col] = fill
                continue
            if mode == "area":
                wx = area_weights(
                    (origin[0] + col * target[0]) / native[0],
                    (origin[0] + (col + 1) * target[0]) / native[0],
                    exact=True,
                )
                wy = area_weights(
                    (origin[1] + row * target[1]) / native[1],
                    (origin[1] + (row + 1) * target[1]) / native[1],
                    exact=True,
                )
            else:
                wx = point_weights(ux / native[0] - 0.5, mode)
                wy = point_weights(uy / native[1] - 0.5, mode)
            value = np.zeros(3, dtype=np.float64)
            if mode == "area":
                # Rational footprint arithmetic independently resolves exact
                # uint8 half-value ties; the final reference is float64.
                value = np.array(
                    [
                        float(
                            sum(
                                int(
                                    image[
                                        mapped(iy, height), mapped(ix, width), channel
                                    ]
                                )
                                * weight_x
                                * weight_y
                                for iy, weight_y in wy
                                for ix, weight_x in wx
                            )
                        )
                        for channel in range(3)
                    ]
                )
                raw[row, col] = value
                continue
            for iy, weight_y in wy:
                for ix, weight_x in wx:
                    value += (
                        image[mapped(iy, height), mapped(ix, width)].astype(np.float64)
                        * weight_x
                        * weight_y
                    )
            raw[row, col] = value
    return quantize(raw), mask, centers, raw
