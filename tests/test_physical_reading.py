"""Phase 2: exact synchronous results against scalar reference and real IO."""

from dataclasses import replace

import numpy as np
import pytest
from phase0_fixtures import write_backend_samples
from phase0_reference import gradient, impulse, sample_rgb
from PIL import Image

from wsi_patchkit import (
    ImageReader,
    LevelInfo,
    PatchRequest,
    PhysicalPatchRequest,
    PhysicalPatchResult,
    PixelFormat,
    SamplingLimits,
    SlideMetadata,
    TiffReader,
    plan_physical_read,
    read_physical_patch_result,
)


class Reader:
    def __init__(self, image=None, *, mpp=(1, 1), describe=True):
        self.image = gradient() if image is None else image
        self.mpp = mpp
        self.describe = describe
        self.calls = []
        self.metadata_calls = []
        self.closed = False

    def metadata(self, path, *, source_mpp=None):
        self.metadata_calls.append((path, source_mpp))
        mpp = source_mpp or self.mpp
        channels = 1 if self.image.ndim == 2 else self.image.shape[2]
        return SlideMetadata(
            path,
            (
                LevelInfo(
                    0,
                    (self.image.shape[1], self.image.shape[0]),
                    (1, 1),
                    mpp,
                    pixel_format=PixelFormat(str(self.image.dtype), channels)
                    if self.describe
                    else None,
                ),
            ),
            mpp=mpp,
        )

    def read_region(self, path, location, level, size):
        self.calls.append((path, location, level, size))
        x, y = location
        width, height = size
        # Deliberately borrow the buffer to exercise result ownership.
        return self.image[y : y + height, x : x + width]

    def close(self):
        self.closed = True
        self.image[...] = 0


def request(**overrides):
    values = dict(
        origin_um=(0, 0), output_size=(3, 2), target_mpp=1, interpolation="bilinear"
    )
    values.update(overrides)
    return PhysicalPatchRequest("fixture", **values)


@pytest.mark.parametrize(
    "origin,target,size,mode,red",
    [
        ((1, 1), (1, 1), (3, 2), "nearest", [10, 20, 30]),
        ((0, 0), (3, 1), (2, 1), "nearest", [10, 40]),
        ((1.5, 0), (1.5, 1), (3, 1), "bilinear", [18, 33, 48]),
        ((0.5, 0), (1, 1), (3, 1), "bilinear", [5, 15, 25]),
        ((0.5, 0), (1, 1), (3, 1), "nearest", [10, 20, 30]),
    ],
)
def test_fixed_requirement_examples(origin, target, size, mode, red):
    reader = Reader()
    patch = request(
        origin_um=origin, target_mpp=target, output_size=size, interpolation=mode
    )
    result = read_physical_patch_result(reader, patch)
    assert isinstance(result, PhysicalPatchResult)
    assert result.request is patch
    assert result.plan.request is patch
    assert result.grid == patch.grid
    assert result.source_level == 0
    np.testing.assert_array_equal(result.image[0, :, 0], red)
    expected = sample_rgb(
        reader.image, origin=origin, target=target, size=size, mode=mode
    )
    np.testing.assert_array_equal(result.image, expected[0])
    np.testing.assert_array_equal(result.valid_mask, expected[1])
    assert len(reader.calls) == 1 and not reader.closed


@pytest.mark.parametrize("mode", ["nearest", "bilinear"])
@pytest.mark.parametrize("image", [gradient(), impulse()], ids=["gradient", "impulse"])
def test_fractional_anisotropic_grids_match_independent_reference(mode, image):
    rng = np.random.default_rng(20261014)
    reader = Reader(image.copy(), mpp=(0.25, 0.5))
    for _ in range(30):
        origin = tuple(rng.uniform(-1, 3, 2))
        target = tuple(rng.uniform(0.1, 1.5, 2))
        size = tuple(int(v) for v in rng.integers(1, 8, 2))
        patch = request(
            origin_um=origin,
            target_mpp=target,
            output_size=size,
            interpolation=mode,
            source_mpp=(0.25, 0.5),
            fill_value=(1, 2, 3),
        )
        result = read_physical_patch_result(reader, patch)
        expected = sample_rgb(
            image,
            origin=origin,
            target=target,
            size=size,
            native=(0.25, 0.5),
            mode=mode,
            fill=(1, 2, 3),
        )
        np.testing.assert_array_equal(result.image, expected[0])
        np.testing.assert_array_equal(result.valid_mask, expected[1])


@pytest.mark.parametrize("mode", ["nearest", "bilinear"])
def test_overlapping_requests_preserve_phase_across_io_rectangles(mode):
    reader = Reader()
    a = read_physical_patch_result(
        reader,
        request(origin_um=(1.25, 0.25), target_mpp=(0.5, 1.5), interpolation=mode),
    )
    b = read_physical_patch_result(
        reader,
        request(origin_um=(2.25, 0.25), target_mpp=(0.5, 1.5), interpolation=mode),
    )
    assert a.plan.native_origin != b.plan.native_origin
    np.testing.assert_array_equal(a.image[:, 2], b.image[:, 0])
    np.testing.assert_array_equal(a.valid_mask[:, 2], b.valid_mask[:, 0])
    np.testing.assert_allclose(
        np.asarray(a.grid.output_to_slide_um) @ [2, 0, 1],
        np.asarray(b.grid.output_to_slide_um) @ [0, 0, 1],
        rtol=0,
        atol=1e-12,
    )


@pytest.mark.parametrize(
    "origin",
    [
        (-1, 0),
        (7, 0),
        (0, -1),
        (0, 3),
        (-1, -1),
        (7, -1),
        (-1, 3),
        (7, 3),
        (-100, -100),
        (100, 100),
    ],
)
@pytest.mark.parametrize("fill", [17, (1, 2, 3)])
@pytest.mark.parametrize("mode", ["nearest", "bilinear"])
def test_four_edges_corners_and_outside_constant(origin, fill, mode):
    reader = Reader()
    result = read_physical_patch_result(
        reader,
        request(
            origin_um=origin, output_size=(2, 2), fill_value=fill, interpolation=mode
        ),
    )
    expected = sample_rgb(
        reader.image, origin=origin, target=(1, 1), size=(2, 2), fill=fill, mode=mode
    )
    assert result.image.shape == (2, 2, 3) and result.valid_mask.shape == (2, 2)
    assert result.valid_mask.dtype == np.bool_
    np.testing.assert_array_equal(result.image, expected[0])
    np.testing.assert_array_equal(result.valid_mask, expected[1])
    assert len(reader.calls) == int(result.valid_mask.any())


@pytest.mark.parametrize(
    "center,valid",
    [
        ((0, 0.5), True),
        ((-1e-9, 0.5), False),
        ((3 - 1e-9, 0.5), True),
        ((3, 0.5), False),
        ((3 + 1e-9, 0.5), False),
        ((0.5, 0), True),
        ((0.5, -1e-9), False),
        ((0.5, 3 - 1e-9), True),
        ((0.5, 3), False),
        ((0.5, 3 + 1e-9), False),
    ],
)
@pytest.mark.parametrize("mode", ["nearest", "bilinear"])
def test_exact_half_open_centers_and_clamped_kernel(center, valid, mode):
    reader = Reader(gradient(3, 3))
    origin = tuple(c - 0.5 for c in center)
    result = read_physical_patch_result(
        reader, request(origin_um=origin, output_size=(1, 1), interpolation=mode)
    )
    assert bool(result.valid_mask[0, 0]) is valid
    expected = sample_rgb(
        reader.image, origin=origin, target=(1, 1), size=(1, 1), mode=mode
    )
    np.testing.assert_array_equal(result.image, expected[0])


def test_width_three_target_two_last_center_invalid():
    result = read_physical_patch_result(
        Reader(gradient(3, 1)),
        request(output_size=(2, 1), target_mpp=(2, 1), interpolation="nearest"),
    )
    np.testing.assert_array_equal(result.image[0, :, 0], [10, 255])
    np.testing.assert_array_equal(result.valid_mask, [[True, False]])


@pytest.mark.parametrize("rounding", ["round", "ceil"])
def test_legacy_canvas_policy_does_not_change_physical_center_mask(rounding):
    old = PatchRequest("fixture", 0, 0, 2, 1, (2, 1), canvas_rounding=rounding)
    patch = replace(PhysicalPatchRequest.from_legacy(old), interpolation="nearest")
    result = read_physical_patch_result(Reader(gradient(3, 1)), patch)
    np.testing.assert_array_equal(result.image[0, :, 0], [10, 255])
    np.testing.assert_array_equal(result.valid_mask, [[True, False]])


@pytest.mark.parametrize("origin", [(-1e200, -1e200), (1e200, 1e200)])
def test_far_constant_skips_pixel_io_and_native_integer_conversion(origin):
    reader = Reader()
    result = read_physical_patch_result(reader, request(origin_um=origin))
    assert not result.valid_mask.any() and not reader.calls
    assert np.all(result.image == 255)


@pytest.mark.parametrize(
    "image,expected",
    [
        (np.array([[23]], dtype=np.uint8), [23, 23, 23]),
        (np.array([[[23]]], dtype=np.uint8), [23, 23, 23]),
        (np.array([[[100, 50, 0, 128]]], dtype=np.uint8), [177, 152, 127]),
        (np.array([[[100, 50, 0, 0]]], dtype=np.uint8), [255, 255, 255]),
        (np.array([[[100, 50, 0, 255]]], dtype=np.uint8), [100, 50, 0]),
    ],
)
@pytest.mark.parametrize("mode", ["nearest", "bilinear"])
def test_rgb_gray_and_white_rgba(image, expected, mode):
    result = read_physical_patch_result(
        Reader(image), request(output_size=(1, 1), interpolation=mode)
    )
    np.testing.assert_array_equal(result.image, [[expected]])


def test_rgba_is_composited_before_bilinear_interpolation():
    array = np.array([[[100, 50, 0, 128], [10, 20, 30, 255]]], dtype=np.uint8)
    result = read_physical_patch_result(
        Reader(array), request(origin_um=(0.5, 0), output_size=(1, 1))
    )
    np.testing.assert_array_equal(result.image, [[[94, 86, 79]]])


@pytest.mark.parametrize(
    "dtype,fill",
    [(np.bool_, 1), (np.uint16, 65535), (np.uint64, 2**64 - 1), (np.int64, -(2**63))],
)
@pytest.mark.parametrize("outside", [False, True])
def test_native_nearest_preserves_dtype_channels_and_fill(dtype, fill, outside):
    array = np.array([[[0, 1], [1, 0]]], dtype=dtype)
    result = read_physical_patch_result(
        Reader(array),
        request(
            color_mode="native",
            interpolation="nearest",
            fill_value=fill,
            output_size=(2, 1),
            origin_um=(-100, -100) if outside else (0, 0),
        ),
    )
    assert result.image.dtype == dtype and result.image.shape == (1, 2, 2)
    np.testing.assert_array_equal(
        result.image, np.full_like(array, fill) if outside else array
    )
    assert result.valid_mask.all() != outside


def test_native_without_format_infers_from_inbounds_read_only():
    array = np.array([[40000, 50000]], dtype=np.uint16)
    reader = Reader(array, describe=False)
    result = read_physical_patch_result(
        reader,
        request(
            color_mode="native",
            interpolation="nearest",
            output_size=(2, 1),
            fill_value=0,
        ),
    )
    np.testing.assert_array_equal(result.image[..., 0], array)
    with pytest.raises(ValueError, match="pixel_format"):
        read_physical_patch_result(
            reader,
            request(
                color_mode="native",
                interpolation="nearest",
                origin_um=(-100, -100),
                fill_value=0,
            ),
        )
    assert len(reader.calls) == 1


@pytest.mark.parametrize("mode", ["nearest", "bilinear"])
def test_returned_arrays_survive_reader_reuse_and_close(mode):
    reader = Reader()
    iterator = (
        read_physical_patch_result(reader, request(interpolation=mode))
        for _ in range(2)
    )
    first = next(iterator)
    image, mask = first.image.copy(), first.valid_mask.copy()
    second = next(iterator)
    assert not np.shares_memory(first.image, reader.image)
    assert first.image.flags.owndata and first.valid_mask.flags.owndata
    reader.close()
    iterator.close()
    np.testing.assert_array_equal(first.image, image)
    np.testing.assert_array_equal(first.valid_mask, mask)
    first.image[...] = 0
    np.testing.assert_array_equal(second.image, image)
    first.valid_mask[...] = False
    np.testing.assert_array_equal(second.valid_mask, mask)


def test_metadata_cache_and_precomputed_plan_validation():
    reader = Reader()
    patch = request()
    meta = reader.metadata("fixture")
    limits = SamplingLimits(max_output_pixels=10)
    plan = plan_physical_read(meta, patch, limits=limits)
    result = read_physical_patch_result(
        reader, patch, metadata=meta, plan=plan, limits=limits
    )
    assert result.plan is plan and len(reader.metadata_calls) == 1
    before = len(reader.calls)
    for invalid in (
        replace(plan, read_size=(1, 1)),
        replace(plan, source_level=1),
        replace(plan, grid=request(origin_um=(0.5, 0)).grid),
    ):
        with pytest.raises(ValueError, match="plan"):
            read_physical_patch_result(
                reader, patch, metadata=meta, plan=invalid, limits=limits
            )
    with pytest.raises(ValueError, match="plan"):
        read_physical_patch_result(reader, patch, metadata=meta, plan=plan)
    assert len(reader.calls) == before


@pytest.mark.parametrize(
    "changes,field",
    [
        (dict(interpolation="bicubic"), "interpolation"),
        (dict(interpolation="area"), "interpolation"),
        (dict(padding_mode="reflect"), "padding_mode"),
    ],
)
def test_unimplemented_modes_rejected_before_metadata_io(changes, field):
    reader = Reader()
    with pytest.raises(ValueError, match=field):
        read_physical_patch_result(reader, request(**changes))
    assert not reader.calls and not reader.metadata_calls


def test_limits_and_invalid_level_fail_before_allocation_or_pixel_io(monkeypatch):
    reader = Reader()
    meta = reader.metadata("fixture")
    patch = request(output_size=(10**12, 2))

    def forbidden(*args, **kwargs):
        raise AssertionError("allocated before validation")

    for name in ("empty", "zeros", "arange"):
        monkeypatch.setattr(np, name, forbidden)
    for kwargs, expected in [
        (dict(request=patch), "max_output_pixels"),
        (
            dict(request=request(), limits=SamplingLimits(max_read_pixels=1)),
            "max_read_pixels",
        ),
        (dict(request=request(level=1)), "level"),
        (
            dict(
                request=request(output_size=(10**20, 2)),
                limits=SamplingLimits(max_output_pixels=None),
            ),
            "output_size",
        ),
    ]:
        with pytest.raises(ValueError, match=expected):
            read_physical_patch_result(reader, metadata=meta, **kwargs)
    assert not reader.calls


@pytest.mark.parametrize(
    "array,field",
    [
        (np.zeros((2, 3, 3), dtype=np.uint16), "dtype"),
        (np.zeros((2, 3, 3), dtype=np.float32), "dtype"),
        (np.zeros((2, 3, 2), dtype=np.uint8), "channels"),
        (np.zeros((2, 3, 5), dtype=np.uint8), "channels"),
    ],
)
def test_unsupported_rgb_without_metadata_format(array, field):
    with pytest.raises(ValueError, match=field):
        read_physical_patch_result(Reader(array, describe=False), request())


def test_reader_shape_format_mismatch_and_raw_error():
    reader = Reader()
    meta = reader.metadata("fixture")
    reader.image = np.zeros((1, 1, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="image.shape"):
        read_physical_patch_result(reader, request(), metadata=meta)
    reader.image = np.zeros((4, 8, 3), dtype=np.uint16)
    with pytest.raises(ValueError, match="image.dtype/channels"):
        read_physical_patch_result(reader, request(), metadata=meta)
    failure = OSError("backend read failed")

    def fail(*args, **kwargs):
        raise failure

    reader.read_region = fail
    with pytest.raises(OSError) as captured:
        read_physical_patch_result(reader, request(), metadata=meta)
    assert captured.value is failure and not reader.closed


@pytest.mark.parametrize("mode", ["nearest", "bilinear"])
def test_real_png_exact_result(tmp_path, mode):
    path = tmp_path / "gradient.png"
    Image.fromarray(gradient()).save(path)
    patch = PhysicalPatchRequest(
        path,
        origin_um=(0.125, -0.1),
        output_size=(5, 4),
        target_mpp=(0.4, 0.6),
        source_mpp=(0.25, 0.5),
        interpolation=mode,
    )
    with ImageReader() as reader:
        result = read_physical_patch_result(reader, patch)
    expected = sample_rgb(
        gradient(),
        origin=patch.origin_um,
        target=patch.target_mpp,
        size=patch.output_size,
        native=patch.source_mpp,
        mode=mode,
    )
    np.testing.assert_array_equal(result.image, expected[0])
    np.testing.assert_array_equal(result.valid_mask, expected[1])


@pytest.mark.parametrize("base,reduced", [(1000, 333), (800, 267)])
@pytest.mark.parametrize("mode", ["nearest", "bilinear"])
def test_real_noninteger_tiff_level_sampling(tmp_path, base, reduced, mode):
    _, path, pixels = write_backend_samples(tmp_path, base, reduced)
    with TiffReader() as reader:
        meta = reader.metadata(path)
        native = meta.levels[1].mpp
        for index in (1, 17, reduced - 2):
            patch = PhysicalPatchRequest(
                path,
                origin_um=((index + 0.25) * native[0], (index - 0.25) * native[1]),
                output_size=(3, 2),
                target_mpp=(0.7 * native[0], 1.3 * native[1]),
                level=1,
                interpolation=mode,
            )
            result = read_physical_patch_result(reader, patch, metadata=meta)
            expected = sample_rgb(
                pixels,
                origin=patch.origin_um,
                target=patch.target_mpp,
                size=patch.output_size,
                native=native,
                extent=(base * meta.mpp[0], base * meta.mpp[1]),
                mode=mode,
            )
            assert result.source_level == 1
            np.testing.assert_array_equal(result.image, expected[0])
            np.testing.assert_array_equal(result.valid_mask, expected[1])


@pytest.mark.parametrize("mode", ["nearest", "bilinear"])
def test_strip_boundaries_keep_global_phase_and_partial_padding(mode):
    array = np.random.default_rng(51).integers(0, 256, (140, 8, 3), dtype=np.uint8)
    patch = request(
        origin_um=(-0.5, -0.75),
        target_mpp=(0.8, 1),
        output_size=(12, 150),
        interpolation=mode,
    )
    result = read_physical_patch_result(Reader(array), patch)
    expected = sample_rgb(
        array,
        origin=patch.origin_um,
        target=patch.target_mpp,
        size=patch.output_size,
        mode=mode,
    )
    np.testing.assert_array_equal(result.image, expected[0])
    np.testing.assert_array_equal(result.valid_mask, expected[1])


def test_uint64_native_labels_above_float_precision_remain_exact():
    labels = np.array([[2**63 + 9, 2**64 - 1]], dtype=np.uint64)
    result = read_physical_patch_result(
        Reader(labels),
        request(
            color_mode="native",
            interpolation="nearest",
            output_size=(2, 1),
            fill_value=0,
        ),
    )
    np.testing.assert_array_equal(result.image[..., 0], labels)


def test_selected_level_and_policy_reach_native_reader():
    class PyramidReader(Reader):
        def metadata(self, path, *, source_mpp=None):
            return SlideMetadata(
                path,
                (
                    LevelInfo(0, (8, 4), (1, 1), (1, 1)),
                    LevelInfo(1, (4, 2), (2, 2), (2, 2)),
                ),
                mpp=(1, 1),
            )

        def read_region(self, path, location, level, size):
            self.calls.append(level)
            return np.full((size[1], size[0], 3), 100 * level, dtype=np.uint8)

    reader = PyramidReader()
    for policy, level, expected in [
        ("nearest", None, 1),
        ("finer", None, 0),
        ("finer", 1, 1),
    ]:
        result = read_physical_patch_result(
            reader, request(target_mpp=1.5, level_policy=policy, level=level)
        )
        assert result.source_level == expected
        assert reader.calls[-1] == expected
        assert np.all(result.image[result.valid_mask] == 100 * expected)
