from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from wsi_patchkit import AutoSlideReader, ImageReader, PatchRequest, read_aligned_patch


@pytest.mark.parametrize("extension", ["png", "jpg", "jpeg", "bmp", "webp"])
def test_auto_reads_ordinary_images(tmp_path: Path, extension: str) -> None:
    path = tmp_path / f"image.{extension.upper()}"
    Image.new("RGB", (20, 16), (80, 100, 120)).save(path)
    with AutoSlideReader() as reader:
        metadata = reader.metadata(path)
        assert metadata.dimensions == (20, 16)
        assert metadata.mpp is None
        assert metadata.vendor == "generic-image"
        assert len(metadata.levels) == 1
        assert metadata.levels[0].downsample == (1, 1)
        assert reader.read_region(path, (2, 3), 0, (4, 5)).shape == (5, 4, 3)
        assert reader._openslide is None
        backend = reader._image
    assert backend is not None and not backend._images


@pytest.mark.parametrize(
    "mode,channels",
    [("1", 1), ("L", 1), ("RGB", 3), ("RGBA", 4), ("LA", 4), ("P", 3), ("CMYK", 3)],
)
def test_color_modes(tmp_path: Path, mode: str, channels: int) -> None:
    path = tmp_path / ("image.jpg" if mode == "CMYK" else "image.png")
    Image.new(mode, (6, 4)).save(path)
    with ImageReader() as reader:
        metadata = reader.metadata(path)
        array = reader.read_region(path, (0, 0), 0, (6, 4))
        assert array.shape == (4, 6, channels)
        assert array.dtype == np.uint8
        assert metadata.levels[0].pixel_format.channels == channels


def test_palette_transparency(tmp_path: Path) -> None:
    path = tmp_path / "palette.png"
    image = Image.new("P", (4, 3))
    image.putpalette([255, 0, 0] + [0] * 765)
    image.save(path, transparency=0)
    with ImageReader() as reader:
        pixels = reader.read_region(path, (0, 0), 0, (4, 3))
        np.testing.assert_array_equal(pixels[0, 0], [255, 0, 0, 0])
        assert reader.metadata(path).levels[0].pixel_format.color_model == "rgba"


@pytest.mark.parametrize("orientation", range(1, 9))
def test_exif_dimensions_and_crop_coordinates(tmp_path: Path, orientation: int) -> None:
    path = tmp_path / "oriented.jpg"
    pixels = np.arange(6 * 8 * 3, dtype=np.uint8).reshape(6, 8, 3)
    exif = Image.Exif()
    exif[274] = orientation
    Image.fromarray(pixels).save(path, exif=exif)
    with Image.open(path) as image:
        expected = np.array(ImageOps.exif_transpose(image))
    with ImageReader() as reader:
        assert reader.metadata(path).dimensions == (
            expected.shape[1],
            expected.shape[0],
        )
        np.testing.assert_array_equal(
            reader.read_region(path, (1, 2), 0, (3, 2)), expected[2:4, 1:4]
        )


def test_mpp_override_and_aligned_padding(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    Image.new("RGB", (8, 6), (20, 30, 40)).save(path, dpi=(300, 300))
    with ImageReader() as reader:
        assert reader.metadata(path).mpp is None
        metadata = reader.metadata(path, source_mpp=(0.5, 0.75))
        assert metadata.levels[0].mpp == (0.5, 0.75)
        request = PatchRequest(path, -1, -1, 4, 3, target_mpp=0.5, source_mpp=0.5)
        result = read_aligned_patch(reader, request)
        np.testing.assert_array_equal(result[0], np.full((4, 3), 255, dtype=np.uint8))
        np.testing.assert_array_equal(result[1, 1], [20, 30, 40])
        assert reader.metadata(path).mpp is None


def test_validation(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    Image.new("RGB", (4, 3)).save(path)
    with ImageReader() as reader:
        for location, level, size in [
            ((0, 0), 1, (1, 1)),
            ((-1, 0), 0, (1, 1)),
            ((3, 2), 0, (2, 2)),
            ((0, 0), 0, (0, 1)),
        ]:
            with pytest.raises(ValueError):
                reader.read_region(path, location, level, size)
        with pytest.raises(FileNotFoundError):
            reader.metadata(tmp_path / "missing.png")
        with pytest.raises(ValueError):
            reader.metadata(path, source_mpp=0)
        sixteen_bit = tmp_path / "sixteen.png"
        Image.fromarray(np.zeros((3, 4), dtype=np.uint16)).save(sixteen_bit)
        with pytest.raises(ValueError, match="eight-bit"):
            reader.metadata(sixteen_bit)


def test_cache_limits_copies_and_pickle(tmp_path: Path) -> None:
    paths = [tmp_path / f"image-{index}.png" for index in range(3)]
    for path in paths:
        Image.new("RGB", (4, 3), (20, 30, 40)).save(path)
    with ImageReader(cache_size=2, max_cache_bytes=72) as reader:
        first = reader.read_region(paths[0], (0, 0), 0, (1, 1))
        first[:] = 0
        np.testing.assert_array_equal(
            reader.read_region(paths[0], (0, 0), 0, (1, 1))[0, 0], [20, 30, 40]
        )
        for path in paths[1:]:
            reader.read_region(path, (0, 0), 0, (1, 1))
        assert list(reader._images) == [str(p.resolve()) for p in paths[1:]]
        assert reader._cache_bytes == 72
        with pickle.loads(pickle.dumps(reader)) as restored:
            assert not restored._images
            assert restored.max_cache_bytes == 72
            assert restored.read_region(paths[0], (0, 0), 0, (1, 1)).shape == (1, 1, 3)
    assert reader._cache_bytes == 0
    with ImageReader(max_cache_bytes=1) as reader:
        reader.read_region(paths[0], (0, 0), 0, (1, 1))
        assert not reader._images
