import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from phase0_fixtures import (
    LEGACY_CASES,
    ArrayReader,
    level_zero_start,
    pixels,
    write_backend_samples,
)

from wsi_patchkit import (
    ImageReader,
    Patch,
    PatchRequest,
    TiffReader,
    read_aligned_patch_result,
)

FIXTURES = Path(__file__).parent / "fixtures" / "phase0"


@pytest.mark.parametrize("case", LEGACY_CASES, ids=lambda c: c["name"])
def test_legacy_pixel_snapshot(case):
    saved = json.loads((FIXTURES / "legacy_pixels.json").read_text())["cases"][
        case["name"]
    ]
    arguments = dict(case)
    name = arguments.pop("name")
    reader = ArrayReader(arguments.pop("source_width", 8), native=name == "native")
    interpolation = arguments.pop("interpolation")
    color_mode = arguments.pop("color_mode", "rgb")
    request = PatchRequest("fixture", **arguments)
    result = read_aligned_patch_result(
        reader, request, interpolation=interpolation, color_mode=color_mode
    )
    assert str(result.image.dtype) == saved["dtype"]
    np.testing.assert_array_equal(result.image, saved["image"])
    np.testing.assert_array_equal(result.valid_mask, saved["mask"])
    assert len(reader.calls) == saved["read_calls"]
    patch = Patch(request, result.image)
    assert patch.request is request and patch.image is result.image


def test_archived_requirement_digest():
    path = Path(__file__).parents[1] / "docs" / "WSI_PATCHKIT_REQUIREMENTS.md"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "77897a2e7387fb2c17268c8bd1171dceba79b182e7232f0beea90d3867bbc3aa"
    )


@pytest.mark.parametrize("base,reduced", [(1000, 333), (800, 267)])
def test_reproducible_png_and_nonintegral_tiff(tmp_path, base, reduced):
    png, tiff, expected = write_backend_samples(tmp_path, base, reduced)
    with ImageReader() as reader:
        np.testing.assert_array_equal(
            reader.read_region(png, (0, 0), 0, (8, 4)), pixels()
        )
        assert reader.metadata(png, source_mpp=(0.25, 0.5)).mpp == (0.25, 0.5)
    with TiffReader() as reader:
        metadata = reader.metadata(tiff)
        assert metadata.mpp == (0.25, 0.5)
        assert metadata.levels[1].dimensions == (reduced, reduced)
        np.testing.assert_allclose(
            metadata.levels[1].downsample,
            (base / reduced, base / reduced),
            rtol=0,
            atol=1e-12,
        )
        for ix, iy in [(0, 0), (1, 1), (17, 29), (reduced - 3, reduced - 3)]:
            location = (
                level_zero_start(ix, base / reduced),
                level_zero_start(iy, base / reduced),
            )
            actual = reader.read_region(tiff, location, 1, (2, 2))
            np.testing.assert_array_equal(actual, expected[iy : iy + 2, ix : ix + 2])


@pytest.mark.parametrize("ratio", [1000 / 333, 800 / 271])
@pytest.mark.parametrize("index", [0, 1, 2, 17, 29, 270])
def test_native_start_reference_round_trip(ratio, index):
    # 800/271 is not recognized as a pyramid by current tifffile heuristics;
    # retain it as a separate native-coordinate reference fixture.
    assert int(np.floor(level_zero_start(index, ratio) / ratio)) == index
