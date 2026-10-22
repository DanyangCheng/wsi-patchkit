from __future__ import annotations

import math
import threading
import time
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from wsi_patchkit import (
    LevelInfo,
    SlideMetadata,
    TileRenderer,
    choose_level_for_downsample,
    iiif_scale_factors,
)


class RecordingReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.level0 = np.zeros((8, 10, 3), dtype=np.uint8)
        self.level0[..., 0] = np.arange(10, dtype=np.uint8)
        self.level0[..., 1] = np.arange(8, dtype=np.uint8)[:, None]
        self.level1 = self.level0[::2, ::2]
        self.calls: list[tuple[tuple[int, int], int, tuple[int, int]]] = []
        self.closed = False

    def metadata(self, path: str | Path, *, source_mpp=None) -> SlideMetadata:
        assert Path(path) == self.path
        mpp = (0.25, 0.25) if source_mpp is None else source_mpp
        return SlideMetadata(
            path,
            (
                LevelInfo(0, (10, 8), (1, 1), mpp),
                LevelInfo(1, (5, 4), (2, 2), (mpp[0] * 2, mpp[1] * 2)),
            ),
            mpp=mpp,
        )

    def read_region(self, path, location, level, size):
        self.calls.append((location, level, size))
        array = self.level0 if level == 0 else self.level1
        downsample = 1 if level == 0 else 2
        x, y = location[0] // downsample, location[1] // downsample
        width, height = size
        return array[y : y + height, x : x + width]

    def close(self) -> None:
        self.closed = True


class ConcurrentReaderState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.read_count = 0
        self.closed_count = 0


class ConcurrentReader:
    def __init__(self, path: Path, state: ConcurrentReaderState) -> None:
        self.path = path
        self.state = state

    def metadata(self, path: str | Path, *, source_mpp=None) -> SlideMetadata:
        return SlideMetadata(path, (LevelInfo(0, (64, 64), (1, 1)),))

    def read_region(self, path, location, level, size):
        with self.state.lock:
            self.state.active += 1
            self.state.read_count += 1
            self.state.max_active = max(self.state.max_active, self.state.active)
        try:
            time.sleep(0.03)
            return np.zeros((size[1], size[0], 3), dtype=np.uint8)
        finally:
            with self.state.lock:
                self.state.active -= 1

    def close(self) -> None:
        with self.state.lock:
            self.state.closed_count += 1


def test_iiif_scale_factors_cover_the_smallest_view() -> None:
    assert iiif_scale_factors((1000, 513), tile_size=256) == (1, 2, 4)
    assert iiif_scale_factors((128, 64), tile_size=256) == (1,)


def test_level_selection_prefers_finer_native_data() -> None:
    metadata = SlideMetadata(
        "slide.tif",
        (
            LevelInfo(0, (100, 80), (1, 1)),
            LevelInfo(1, (25, 20), (4, 4)),
            LevelInfo(2, (10, 8), (10, 10)),
        ),
    )

    assert choose_level_for_downsample(metadata, 8).level == 1
    assert choose_level_for_downsample(metadata, 12).level == 2


def test_level_selection_tolerates_integer_pyramid_rounding() -> None:
    metadata = SlideMetadata(
        "slide.svs",
        (
            LevelInfo(0, (84_138, 67_802), (1, 1)),
            LevelInfo(1, (21_034, 16_950), (4.000095, 4.000118)),
            LevelInfo(2, (5_258, 4_237), (16.001902, 16.00236)),
            LevelInfo(3, (1_314, 1_059), (64.031963, 64.024551)),
        ),
    )

    assert choose_level_for_downsample(metadata, 4).level == 1
    assert choose_level_for_downsample(metadata, 16).level == 2
    assert choose_level_for_downsample(metadata, 64).level == 3
    assert choose_level_for_downsample(metadata, 3.9).level == 0


def test_level_selection_validates_tolerance() -> None:
    metadata = SlideMetadata("slide.tif", (LevelInfo(0, (10, 10), (1, 1)),))

    with np.testing.assert_raises_regex(ValueError, "relative_tolerance"):
        choose_level_for_downsample(metadata, 1, relative_tolerance=-0.1)


def test_tile_renderer_selects_level_encodes_and_caches(tmp_path: Path) -> None:
    path = tmp_path / "slide.tif"
    path.touch()
    reader = RecordingReader(path)
    renderer = TileRenderer(reader, cache_size=2)

    first = renderer.render_region(path, (0, 0, 10, 8), (5, 4), image_format="png")
    second = renderer.render_region(path, (0, 0, 10, 8), (5, 4), image_format="png")

    assert first is second
    assert first.media_type == "image/png"
    assert (first.width, first.height) == (5, 4)
    assert reader.calls == [((0, 0), 1, (5, 4))]
    decoded = np.asarray(Image.open(BytesIO(first.content)))
    np.testing.assert_array_equal(decoded, reader.level1)
    renderer.close()
    assert reader.closed


def test_tile_renderer_crops_edge_regions(tmp_path: Path) -> None:
    path = tmp_path / "slide.tif"
    path.touch()
    reader = RecordingReader(path)
    renderer = TileRenderer(reader)

    result = renderer.render_region(path, (8, 6, 4, 4), (4, 4), image_format="png")

    assert (result.width, result.height) == (2, 2)
    assert reader.calls == [((8, 6), 0, (2, 2))]
    renderer.close()


def test_tile_renderer_can_force_a_pixel_exact_level0_crop(tmp_path: Path) -> None:
    path = tmp_path / "slide.tif"
    path.touch()
    reader = RecordingReader(path)
    renderer = TileRenderer(reader)

    result = renderer.render_level0_region(
        path,
        (2, 1, 4, 3),
        image_format="png",
    )

    assert (result.width, result.height) == (4, 3)
    assert reader.calls == [((2, 1), 0, (4, 3))]
    decoded = np.asarray(Image.open(BytesIO(result.content)))
    np.testing.assert_array_equal(decoded, reader.level0[1:4, 2:6])
    renderer.close()


def test_tile_renderer_reads_different_tiles_concurrently(tmp_path: Path) -> None:
    path = tmp_path / "slide.tif"
    path.touch()
    state = ConcurrentReaderState()
    renderer = TileRenderer(
        reader_factory=lambda: ConcurrentReader(path, state),
        reader_pool_size=3,
    )
    errors: list[BaseException] = []

    def render(x: int) -> None:
        try:
            renderer.render_region(path, (x, 0, 8, 8), (8, 8), image_format="png")
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=render, args=(x,)) for x in (0, 8, 16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert state.max_active == 3
    assert state.read_count == 3
    renderer.close()
    assert state.closed_count == 3


def test_tile_renderer_coalesces_concurrent_identical_tiles(tmp_path: Path) -> None:
    path = tmp_path / "slide.tif"
    path.touch()
    state = ConcurrentReaderState()
    renderer = TileRenderer(
        reader_factory=lambda: ConcurrentReader(path, state),
        reader_pool_size=3,
        cache_size=0,
    )
    results = []

    def render() -> None:
        results.append(
            renderer.render_region(path, (0, 0, 8, 8), (8, 8), image_format="png")
        )

    threads = [threading.Thread(target=render) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state.read_count == 1
    assert len(results) == 3
    assert results[0] is results[1] is results[2]
    renderer.close()


@pytest.mark.parametrize("max_read_pixels", [16, 1000])
def test_fractional_pyramid_render_uses_correct_native_origin(
    tmp_path, max_read_pixels
):
    class FractionalReader:
        def metadata(self, path, *, source_mpp=None):
            return SlideMetadata(
                path,
                (
                    LevelInfo(0, (30, 30), (1, 1)),
                    LevelInfo(1, (9, 9), (10 / 3, 10 / 3)),
                ),
            )

        def read_region(self, path, location, level, size):
            assert level == 1
            x, y = (math.floor(value / (10 / 3)) for value in location)
            return native[y : y + size[1], x : x + size[0]]

        def close(self):
            pass

    (tmp_path / "slide.tif").touch()
    native = np.zeros((9, 9, 3), dtype="uint8")
    native[..., 0] = np.arange(9, dtype="uint8") * 20
    native[..., 1] = np.arange(9, dtype="uint8")[:, None] * 20
    renderer = TileRenderer(FractionalReader(), max_read_pixels=max_read_pixels)
    try:
        encoded = renderer.render_region(
            tmp_path / "slide.tif", (14, 14, 14, 14), (4, 4), image_format="png"
        )
        with Image.open(BytesIO(encoded.content)) as image:
            actual = np.array(image)
        expected = Image.fromarray(native).transform(
            (4, 4),
            Image.Transform.EXTENT,
            (4.2, 4.2, 8.4, 8.4),
            resample=Image.Resampling.BILINEAR,
        )
        # Local floating-point extents can differ by one quantization value.
        np.testing.assert_allclose(actual, np.array(expected), atol=1, rtol=0)
    finally:
        renderer.close()
