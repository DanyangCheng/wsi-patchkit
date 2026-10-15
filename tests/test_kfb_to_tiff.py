from __future__ import annotations

import struct
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image

from wsi_patchkit.io.kfbslide import KfbSlideReader
from wsi_patchkit.io.tiff import TiffReader
from wsi_patchkit.kfb_to_tiff import (
    _bounded_map,
    convert_kfb_to_tiff,
    main,
)


def _write_kfb(
    path: Path,
    *,
    dimensions: tuple[int, int] = (149, 101),
    scales: tuple[int, ...] = (40, 15),
    sampling: int = 2,
    tile: int = 32,
    bad_size: bool = False,
    missing: bool = False,
) -> list[bytes]:
    """Small real KFB container, parsed by the installed backend without mocks."""
    pytest.importorskip("kfbslide")
    payload = bytearray(88)
    payload[:4] = b"KFB\0"
    struct.pack_into("<f", payload, 8, 1.6)
    struct.pack_into("<III", payload, 16, dimensions[1], dimensions[0], 40)
    payload[28:32] = b"JPEG"
    struct.pack_into("<I", payload, 48, 96)
    struct.pack_into("<f", payload, 72, 0.25)
    struct.pack_into("<I", payload, 84, tile)
    macro = BytesIO()
    Image.new("RGB", (8, 8), "white").save(macro, "JPEG")
    macro_bytes = macro.getvalue()
    assoc = bytearray(44)
    struct.pack_into("<II", assoc, 4, 8, 8)
    struct.pack_into("<II", assoc, 16, len(macro_bytes), 52)
    images: list[bytes] = []
    records: list[bytes] = []
    rng = np.random.default_rng(25)
    for scale in scales:
        ds = 40 / scale
        width, height = (int(value / ds) for value in dimensions)
        for y in range(0, height, tile):
            for x in range(0, width, tile):
                if missing and x == tile and y == tile:
                    continue
                w, h = min(tile, width - x), min(tile, height - y)
                pixels = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
                encoded = BytesIO()
                Image.fromarray(pixels).save(
                    encoded, "JPEG", quality=95, subsampling=sampling
                )
                image = encoded.getvalue()
                record = bytearray(64)
                struct.pack_into("<IIII", record, 4, x, y, w, h)
                struct.pack_into("<f", record, 20, scale)
                size = len(image)
                if bad_size:
                    size += -10 if not records else 10 if len(records) == 1 else 0
                struct.pack_into("<I", record, 32, size)
                records.append(bytes(record))
                images.append(image)
    struct.pack_into("<I", payload, 12, len(records))
    start = 96 + 52 + len(macro_bytes) + sum(map(len, images))
    struct.pack_into("<QQ", payload, 56, start + 64 * len(records), start)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\xf1\x01\xee\xee"
        + payload
        + b"\xff\x01\xee\xee"
        + b"\xf1\x02\xee\xee"
        + assoc
        + b"\xff\x02\xee\xee"
        + macro_bytes
        + b"".join(images)
        + b"".join(records)
    )
    return images


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("compression", ["none", "deflate"])
def test_lossless_pyramid_matches_kfb_pixels(tmp_path, workers, compression):
    source, output = tmp_path / "slide.kfb", tmp_path / "slide.tiff"
    _write_kfb(source, missing=True)
    result = convert_kfb_to_tiff(
        source, output, workers=workers, buffer_tiles=2, compression=compression
    )
    assert result.tiles == 24
    assert result.copied_tiles == 0
    with KfbSlideReader() as native, TiffReader() as converted:
        expected = native.metadata(source)
        actual = converted.metadata(output)
        assert actual.mpp == expected.mpp
        assert [item.dimensions for item in actual.levels] == [
            item.dimensions for item in expected.levels
        ]
        for level in expected.levels:
            np.testing.assert_array_equal(
                converted.read_region(output, (0, 0), level.level, level.dimensions),
                native.read_region(source, (0, 0), level.level, level.dimensions),
            )
    with tifffile.TiffFile(output) as tif:
        assert tif.is_bigtiff
        assert tif.pages[0].is_tiled
        assert len(tif.pages[0].subifds) == 1
        assert "kfbio.scan_scale" in tif.pages[0].description
        assert int(tif.pages[0].pages[0].subfiletype) == 1
        numerator, denominator = tif.pages[0].pages[0].tags[282].value
        assert numerator / denominator == pytest.approx(10_000 / (0.25 * 40 / 15))
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("sampling", [0, 1, 2])
def test_jpeg_copy_preserves_original_bitstreams_and_pixels(tmp_path, sampling):
    pytest.importorskip("imagecodecs")
    source, output = tmp_path / "slide.kfb", tmp_path / "slide.tiff"
    originals = _write_kfb(source, dimensions=(64, 64), scales=(40,), sampling=sampling)
    result = convert_kfb_to_tiff(source, output, workers=3, verify_jpeg=True)
    assert result.copied_tiles == result.tiles == 4
    with tifffile.TiffFile(output) as tif, output.open("rb") as handle:
        page = tif.pages[0]
        for offset, size, original in zip(
            page.dataoffsets, page.databytecounts, originals, strict=True
        ):
            handle.seek(offset)
            assert handle.read(size) == original
        actual = page.asarray()
    with KfbSlideReader() as reader:
        expected = reader.read_region(source, (0, 0), 0, (64, 64))
    np.testing.assert_allclose(actual, expected, atol=1, rtol=0)
    with TiffReader() as reader:
        assert reader.metadata(output).levels[0].pixel_format.color_model == "rgb"


def test_jpeg_edges_missing_tiles_and_reencode(tmp_path):
    pytest.importorskip("imagecodecs")
    source = tmp_path / "slide.kfb"
    _write_kfb(source, missing=True)
    for reencode in (False, True):
        output = tmp_path / f"{reencode}.tiff"
        result = convert_kfb_to_tiff(source, output, workers=3, reencode=reencode)
        assert (result.copied_tiles == 0) == reencode
        with tifffile.TiffFile(output) as tif:
            assert [
                page.asarray().shape for page in [tif.pages[0], *tif.pages[0].pages]
            ] == [
                (101, 149, 3),
                (37, 55, 3),
            ]
            np.testing.assert_array_equal(
                tif.asarray()[32:64, 32:64], np.full((32, 32, 3), 255)
            )


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("compression", ["deflate", "jpeg"])
def test_corrupt_index_is_repaired(tmp_path, workers, compression):
    if compression == "jpeg":
        pytest.importorskip("imagecodecs")
    pristine, source = tmp_path / "pristine.kfb", tmp_path / "bad.kfb"
    _write_kfb(pristine)
    _write_kfb(source, bad_size=True)
    output = tmp_path / "repaired.tiff"
    result = convert_kfb_to_tiff(
        source, output, compression=compression, workers=workers
    )
    with KfbSlideReader() as reader, TiffReader() as converted:
        actual = converted.read_region(output, (0, 0), 0, (149, 101))
        if compression == "deflate":
            np.testing.assert_array_equal(
                reader.read_region(pristine, (0, 0), 0, (149, 101)), actual
            )
        else:
            assert actual.shape == (101, 149, 3)
            assert result.copied_tiles > 0


def test_failed_conversion_keeps_existing_output_and_removes_partial(tmp_path):
    source, output = tmp_path / "slide.kfb", tmp_path / "slide.tiff"
    _write_kfb(source)
    output.write_bytes(b"existing result")
    with pytest.raises(FileExistsError):
        convert_kfb_to_tiff(source, output, compression="none")

    def fail(_):
        raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError, match="simulated failure"):
        convert_kfb_to_tiff(
            source, output, compression="deflate", overwrite=True, progress=fail
        )
    assert output.read_bytes() == b"existing result"
    assert not list(tmp_path.glob("*.partial"))
    convert_kfb_to_tiff(source, output, compression="none", overwrite=True)
    with tifffile.TiffFile(output) as tif:
        assert tif.is_bigtiff


def test_retiling_and_level0_only(tmp_path):
    source, output = tmp_path / "slide.kfb", tmp_path / "slide.tiff"
    _write_kfb(source)
    result = convert_kfb_to_tiff(
        source, output, tile_size=64, compression="none", level0_only=True
    )
    assert result.tiles == 6
    with tifffile.TiffFile(output) as tif, KfbSlideReader() as reader:
        assert len(tif.series[0].levels) == 1
        assert tif.pages[0].tilewidth == 64
        np.testing.assert_array_equal(
            tif.asarray(), reader.read_region(source, (0, 0), 0, (149, 101))
        )


def test_batch_cli_preserves_subdirectories_and_reports_errors(tmp_path):
    source, output = tmp_path / "input", tmp_path / "output"
    _write_kfb(source / "a.kfb")
    _write_kfb(source / "nested" / "a.KFB")
    assert (
        main(
            [
                str(source),
                str(output),
                "--recursive",
                "--jobs",
                "2",
                "--workers",
                "2",
                "--compression",
                "deflate",
            ]
        )
        == 0
    )
    assert (output / "a.tiff").is_file()
    assert (output / "nested" / "a.tiff").is_file()
    assert main([str(source), str(output), "--compression", "none"]) == 1


def test_bounded_map_does_not_eagerly_submit_entire_slide():
    pulled = []

    def items():
        for index in range(100):
            pulled.append(index)
            yield index

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = _bounded_map(executor, lambda value: value * 2, items(), 3)
        assert next(results) == 0
        assert len(pulled) == 3
        assert next(results) == 2
        assert len(pulled) == 4
        results.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"workers": 0},
        {"buffer_tiles": 0},
        {"tile_size": 17},
        {"compression": "invalid"},
        {"quality": 101},
    ],
)
def test_invalid_options(tmp_path, kwargs):
    with pytest.raises(ValueError):
        convert_kfb_to_tiff(tmp_path / "input.kfb", tmp_path / "output.tiff", **kwargs)
