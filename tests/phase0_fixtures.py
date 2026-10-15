"""Deterministic backend fixtures and legacy snapshot inputs."""

import math

import numpy as np
import tifffile
from PIL import Image

from wsi_patchkit import LevelInfo, PixelFormat, SlideMetadata

LEGACY_CASES = [
    dict(
        name="aligned",
        x=1,
        y=1,
        width=3,
        height=2,
        target_mpp=(1, 1),
        interpolation="nearest",
    ),
    dict(
        name="downsample",
        x=0,
        y=0,
        width=2,
        height=1,
        target_mpp=(3, 1),
        interpolation="nearest",
    ),
    dict(
        name="fractional_scale",
        x=1,
        y=0,
        width=3,
        height=1,
        target_mpp=(1.5, 1),
        interpolation="bilinear",
    ),
    dict(
        name="padding",
        x=-1,
        y=-1,
        width=3,
        height=2,
        target_mpp=(1, 1),
        interpolation="nearest",
    ),
    dict(
        name="boundary",
        x=0,
        y=0,
        width=2,
        height=1,
        target_mpp=(2, 1),
        interpolation="nearest",
        source_width=3,
    ),
    dict(
        name="box", x=0, y=0, width=3, height=2, target_mpp=(2, 2), interpolation="area"
    ),
    dict(
        name="outside",
        x=-10,
        y=20,
        width=2,
        height=2,
        target_mpp=(1, 1),
        interpolation="bilinear",
    ),
    dict(
        name="native",
        x=1,
        y=1,
        width=2,
        height=2,
        target_mpp=(1, 1),
        interpolation="nearest",
        color_mode="native",
    ),
]


def pixels(width=8, height=4):
    result = np.zeros((height, width, 3), dtype=np.uint8)
    for row in range(height):
        for col in range(width):
            result[row, col] = 10 * col, 20 * row, 0
    return result


class ArrayReader:
    def __init__(self, width=8, native=False):
        self.array = pixels(width)
        if native:
            self.array = self.array[..., :1].astype(np.uint16) * 100
        self.calls = []

    def metadata(self, path, *, source_mpp=None):
        height, width, channels = self.array.shape
        return SlideMetadata(
            str(path),
            (
                LevelInfo(
                    0,
                    (width, height),
                    (1, 1),
                    (1, 1),
                    pixel_format=PixelFormat(str(self.array.dtype), channels),
                ),
            ),
            mpp=(1, 1),
        )

    def read_region(self, path, location, level, size):
        self.calls.append((location, level, size))
        x, y = location
        width, height = size
        return self.array[y : y + height, x : x + width].copy()

    def close(self):
        pass


def write_backend_samples(directory, base=1000, reduced=333):
    """Generate a real PNG and anisotropic nonintegral TIFF SubIFD pyramid."""
    png = directory / "gradient.png"
    Image.fromarray(pixels()).save(png)
    tiff = directory / "nonintegral-pyramid.tif"
    level0 = np.zeros((base, base, 3), dtype=np.uint8)
    yy, xx = np.indices((reduced, reduced))
    level1 = np.stack((xx % 256, yy % 256, (xx + yy) % 256), axis=-1).astype(np.uint8)
    with tifffile.TiffWriter(tiff) as writer:
        writer.write(
            level0,
            photometric="rgb",
            tile=(16, 16),
            subifds=1,
            resolution=(40_000, 20_000),
            resolutionunit="CENTIMETER",
            metadata=None,
        )
        writer.write(
            level1,
            photometric="rgb",
            tile=(16, 16),
            subfiletype=1,
            metadata=None,
        )
    return png, tiff, level1


def level_zero_start(index, ratio):
    return math.ceil(math.nextafter(index * ratio, -math.inf))
