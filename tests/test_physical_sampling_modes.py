"""Phase 3 acceptance: extended kernels, full-axis padding, and IO budgets."""

import math

import numpy as np
import pytest
from phase0_reference import gradient, impulse, reflect_index, sample_rgb
from test_physical_reading import Reader, request

from wsi_patchkit import (
    SamplingLimits,
    plan_physical_read,
    read_physical_patch_result,
)

MODES = ("nearest", "bilinear", "bicubic", "area")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("padding", ["constant", "reflect"])
@pytest.mark.parametrize("array", [gradient(), impulse()], ids=["gradient", "impulse"])
def test_all_kernels_against_independent_scalar_oracle(mode, padding, array):
    rng = np.random.default_rng(113)
    reader = Reader(array.copy())
    for _ in range(20):
        origin = tuple(rng.uniform(-12, 10, 2))
        target = tuple(rng.uniform(1 if mode == "area" else 0.4, 3, 2))
        size = tuple(int(i) for i in rng.integers(1, 5, 2))
        result = read_physical_patch_result(
            reader,
            request(
                origin_um=origin,
                target_mpp=target,
                output_size=size,
                interpolation=mode,
                padding_mode=padding,
                fill_value=(1, 2, 3),
            ),
        )
        expected = sample_rgb(
            array,
            origin=origin,
            target=target,
            size=size,
            mode=mode,
            padding=padding,
            fill=(1, 2, 3),
        )
        np.testing.assert_array_equal(result.image, expected[0])
        np.testing.assert_array_equal(result.valid_mask, expected[1])


def test_fixed_keys_impulse_gradient_and_saturation():
    result = read_physical_patch_result(
        Reader(impulse()),
        request(origin_um=(3.5, 3), output_size=(1, 1), interpolation="bicubic"),
    )
    np.testing.assert_array_equal(result.image, [[[143] * 3]])
    result = read_physical_patch_result(
        Reader(),
        request(origin_um=(0.5, 1), output_size=(3, 1), interpolation="bicubic"),
    )
    np.testing.assert_array_equal(result.image[0, :, 0], [4, 15, 25])
    step = np.zeros((1, 8, 3), dtype=np.uint8)
    step[:, 4:] = 255
    result = read_physical_patch_result(
        Reader(step),
        request(origin_um=(2.5, 0), output_size=(3, 1), interpolation="bicubic"),
    )
    np.testing.assert_array_equal(result.image[0, :, 0], [0, 128, 255])


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("shape", [(1, 1), (1, 4), (4, 1)])
def test_reflect_single_pixel_axes_and_far_coordinates(mode, shape):
    array = np.random.default_rng(12).integers(0, 256, (*shape, 3), dtype=np.uint8)
    origin, target, size = (-100.5, -999.25), (1.5, 2), (3, 2)
    result = read_physical_patch_result(
        Reader(array),
        request(
            origin_um=origin,
            target_mpp=target,
            output_size=size,
            interpolation=mode,
            padding_mode="reflect",
        ),
    )
    expected = sample_rgb(
        array, origin=origin, target=target, size=size, mode=mode, padding="reflect"
    )
    np.testing.assert_array_equal(result.image, expected[0])
    assert not result.valid_mask.any()
    assert result.plan.read_size[0] <= shape[1] and result.plan.read_size[1] <= shape[0]


@pytest.mark.parametrize("mode", ["nearest", "bilinear", "bicubic"])
@pytest.mark.parametrize("origin", [-1e200, 1e200])
def test_far_point_reflection_uses_integer_periods_without_overflow(mode, origin):
    array = gradient()
    reader = Reader(array)
    result = read_physical_patch_result(
        reader,
        request(
            origin_um=(origin, origin),
            output_size=(2, 2),
            interpolation=mode,
            padding_mode="reflect",
        ),
    )
    index = math.floor(origin)  # Float spacing is > 1, so the center phase is integral.
    expected = array[reflect_index(index, 4), reflect_index(index, 8)]
    np.testing.assert_array_equal(result.image, np.broadcast_to(expected, (2, 2, 3)))
    assert not result.valid_mask.any()
    assert len(reader.calls) == 1
    assert result.plan.read_pixels <= 32


@pytest.mark.parametrize("mode", MODES)
def test_reflect_matches_full_axis_not_cropped_read_block(mode):
    reader = Reader()
    target = (1.5, 1.5)
    origin = (10.25, -5.25)
    result = read_physical_patch_result(
        reader,
        request(
            origin_um=origin,
            target_mpp=target,
            output_size=(2, 2),
            interpolation=mode,
            padding_mode="reflect",
        ),
    )
    expected = sample_rgb(
        reader.image,
        origin=origin,
        target=target,
        size=(2, 2),
        mode=mode,
        padding="reflect",
    )
    np.testing.assert_array_equal(result.image, expected[0])
    assert result.plan.read_pixels < 32 or mode == "bicubic"
    assert not result.valid_mask.any()


@pytest.mark.parametrize("padding", ["constant", "reflect"])
@pytest.mark.parametrize(
    "origin,red", [((0.5, 0), 10), ((-0.5, 0), None), ((-2, 0), None)]
)
def test_area_fractional_footprint_and_boundary_policy(padding, origin, red):
    reader = Reader()
    result = read_physical_patch_result(
        reader,
        request(
            origin_um=origin,
            target_mpp=(2, 1),
            output_size=(1, 1),
            interpolation="area",
            padding_mode=padding,
        ),
    )
    if red is None:
        red = {
            ("constant", -0.5): 3,
            ("reflect", -0.5): 5,
            ("constant", -2): 255,
            ("reflect", -2): 15,
        }[padding, origin[0]]
    assert result.image[0, 0, 0] == red


@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_area_checkerboard_and_invalid_center_whole_fill(padding):
    array = np.repeat(
        ((np.indices((4, 4)).sum(0) % 2) * 255).astype(np.uint8)[..., None], 3, axis=2
    )
    result = read_physical_patch_result(
        Reader(array),
        request(
            output_size=(2, 2), target_mpp=2, interpolation="area", padding_mode=padding
        ),
    )
    np.testing.assert_array_equal(result.image, np.full((2, 2, 3), 128))
    result = read_physical_patch_result(
        Reader(gradient(3, 1)),
        request(
            origin_um=(2.5, 0),
            output_size=(1, 1),
            interpolation="area",
            fill_value=(253, 252, 251),
        ),
    )
    np.testing.assert_array_equal(result.image, [[[253, 252, 251]]])
    assert not result.valid_mask.any()


def test_area_huge_reflect_footprint_is_period_counted():
    array = np.repeat(np.array([[0, 100, 200]], dtype=np.uint8)[..., None], 3, axis=2)
    reader = Reader(array)
    result = read_physical_patch_result(
        reader,
        request(
            origin_um=(-1e12, 0),
            target_mpp=(2e12, 1),
            output_size=(1, 1),
            interpolation="area",
            padding_mode="reflect",
        ),
        limits=SamplingLimits(max_read_pixels=3),
    )
    np.testing.assert_array_equal(result.image, [[[100] * 3]])
    assert result.plan.read_size == (3, 1) and len(reader.calls) == 1


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_limits_precede_allocation_and_io_for_every_mode(mode, padding, monkeypatch):
    reader = Reader()
    meta = reader.metadata("fixture")
    patch = request(
        origin_um=(1.25, 0.25),
        target_mpp=2,
        output_size=(2, 2),
        interpolation=mode,
        padding_mode=padding,
    )
    plan = plan_physical_read(meta, patch)

    def forbidden(*args, **kwargs):
        raise AssertionError("allocated before limits")

    for name in ("empty", "zeros", "arange"):
        monkeypatch.setattr(np, name, forbidden)
    with pytest.raises(
        ValueError, match="max_output_pixels.*source_level=0.*output_size=.*limit=3"
    ):
        read_physical_patch_result(
            reader, patch, metadata=meta, limits=SamplingLimits(max_output_pixels=3)
        )
    with pytest.raises(ValueError, match="max_read_pixels.*read_size=.*limit="):
        read_physical_patch_result(
            reader,
            patch,
            metadata=meta,
            limits=SamplingLimits(max_read_pixels=plan.read_pixels - 1),
        )
    assert not reader.calls


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_equal_limits_and_independent_disable_are_allowed(mode, padding):
    reader = Reader()
    patch = request(
        target_mpp=2, output_size=(2, 2), interpolation=mode, padding_mode=padding
    )
    plan = plan_physical_read(reader.metadata("fixture"), patch)
    for limits in [
        SamplingLimits(max_read_pixels=plan.read_pixels, max_output_pixels=4),
        SamplingLimits(max_read_pixels=None, max_output_pixels=4),
        SamplingLimits(max_read_pixels=plan.read_pixels, max_output_pixels=None),
    ]:
        result = read_physical_patch_result(reader, patch, limits=limits)
        assert result.plan.read_pixels == plan.read_pixels


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_neighboring_requests_preserve_extended_phase(mode, padding):
    reader = Reader()
    a = read_physical_patch_result(
        reader,
        request(
            origin_um=(-1.25, -0.25),
            output_size=(3, 2),
            target_mpp=1.5,
            interpolation=mode,
            padding_mode=padding,
        ),
    )
    b = read_physical_patch_result(
        reader,
        request(
            origin_um=(1.75, -0.25),
            output_size=(3, 2),
            target_mpp=1.5,
            interpolation=mode,
            padding_mode=padding,
        ),
    )
    np.testing.assert_array_equal(a.image[:, 2], b.image[:, 0])
    np.testing.assert_array_equal(a.valid_mask[:, 2], b.valid_mask[:, 0])


def test_native_reflect_and_unknown_outside_format():
    labels = np.array([[2**63 + 9, 2**64 - 1]], dtype=np.uint64)
    reader = Reader(labels)
    patch = request(
        origin_um=(-2, 0),
        output_size=(2, 1),
        interpolation="nearest",
        color_mode="native",
        fill_value=0,
        padding_mode="reflect",
    )
    result = read_physical_patch_result(reader, patch)
    np.testing.assert_array_equal(result.image[..., 0], labels)
    assert result.image.dtype == np.uint64 and not result.valid_mask.any()
    reader = Reader(labels, describe=False)
    with pytest.raises(ValueError, match="pixel_format.*outside native"):
        read_physical_patch_result(reader, patch)
    assert not reader.calls


@pytest.mark.parametrize("mode", ["bilinear", "bicubic", "area"])
def test_native_continuous_kernels_are_rejected(mode):
    with pytest.raises(ValueError, match="native requires nearest"):
        request(color_mode="native", interpolation=mode)


@pytest.mark.parametrize("padding", ["constant", "reflect"])
@pytest.mark.parametrize("target", [(0.5, 2), (2, 0.5)])
def test_area_enlargement_rejected_before_pixel_io(padding, target):
    reader = Reader()
    with pytest.raises(ValueError, match="area.*enlarge"):
        read_physical_patch_result(
            reader,
            request(target_mpp=target, interpolation="area", padding_mode=padding),
        )
    assert not reader.calls


@pytest.mark.parametrize("mode", ["bicubic", "area"])
@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_extended_results_keep_ownership_after_reader_close(mode, padding):
    reader = Reader()
    patch = request(
        origin_um=(-0.5, -0.5), target_mpp=1.5, interpolation=mode, padding_mode=padding
    )
    result = read_physical_patch_result(reader, patch)
    saved = result.image.copy(), result.valid_mask.copy()
    reader.close()
    np.testing.assert_array_equal(result.image, saved[0])
    np.testing.assert_array_equal(result.valid_mask, saved[1])
    assert result.image.flags.owndata and result.valid_mask.flags.owndata


@pytest.mark.parametrize("dtype", [np.float32, np.complex64, object])
@pytest.mark.parametrize("describe", [False, True])
def test_native_unsupported_dtype_and_known_fill_range(dtype, describe):
    reader = Reader(np.zeros((2, 3, 1), dtype=dtype), describe=describe)
    with pytest.raises(ValueError, match="dtype.*native requires"):
        read_physical_patch_result(
            reader, request(color_mode="native", interpolation="nearest", fill_value=0)
        )
    assert len(reader.calls) == int(not describe)


@pytest.mark.parametrize(
    "dtype,fill", [(np.bool_, 2), (np.uint8, 256), (np.int8, 128), (np.int16, -32769)]
)
def test_native_fill_is_validated_even_when_reflect_ignores_fill(dtype, fill):
    reader = Reader(np.zeros((2, 3, 1), dtype=dtype))
    with pytest.raises(ValueError, match="fill_value.*native"):
        read_physical_patch_result(
            reader,
            request(
                color_mode="native",
                interpolation="nearest",
                fill_value=fill,
                padding_mode="reflect",
            ),
        )
    assert not reader.calls


def test_disabled_read_limit_still_rejects_unaddressable_read_size():
    from wsi_patchkit import LevelInfo, SlideMetadata

    reader = Reader()
    meta = SlideMetadata(
        "fixture", (LevelInfo(0, (10**20, 2), (1, 1), (1, 1)),), mpp=(1, 1)
    )
    patch = request(interpolation="area", output_size=(1, 1), target_mpp=(1e20, 1))
    with pytest.raises(ValueError, match="read_size cannot be addressed"):
        read_physical_patch_result(
            reader, patch, metadata=meta, limits=SamplingLimits(max_read_pixels=None)
        )
    assert not reader.calls


@pytest.mark.parametrize("padding", ["constant", "reflect"])
@pytest.mark.parametrize("target", [(2, 2), (3, 2), (3, 3), (4, 3), (5, 3)])
def test_area_half_value_lattice_against_exact_rational_reference(padding, target):
    array = np.random.default_rng(19).integers(0, 256, (7, 8, 3), dtype=np.uint8)
    reader = Reader(array)
    for offset in (-5, -1, 0, 1, 5):
        for phase in (0, 0.25, 0.5, 0.75):
            origin = (offset + phase, offset - phase)
            result = read_physical_patch_result(
                reader,
                request(
                    origin_um=origin,
                    target_mpp=target,
                    output_size=(3, 3),
                    interpolation="area",
                    padding_mode=padding,
                ),
            )
            expected = sample_rgb(
                array,
                origin=origin,
                target=target,
                size=(3, 3),
                mode="area",
                padding=padding,
            )
            np.testing.assert_array_equal(result.image, expected[0])


def test_area_extremely_large_periods_keep_float64_products_finite():
    reader = Reader(np.full((3, 4, 3), 123, dtype=np.uint8))
    with np.errstate(over="raise", invalid="raise"):
        result = read_physical_patch_result(
            reader,
            request(
                origin_um=(-1e200, -1e200),
                target_mpp=2e200,
                output_size=(1, 1),
                interpolation="area",
                padding_mode="reflect",
            ),
        )
    np.testing.assert_array_equal(result.image, [[[123] * 3]])


@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_area_interior_precision_collapse_fails_before_io(padding):
    from wsi_patchkit import LevelInfo, SlideMetadata

    meta = SlideMetadata(
        "fixture", (LevelInfo(0, (2**54, 1), (1, 1), (1, 1)),), mpp=(1, 1)
    )
    reader = Reader()
    patch = request(
        origin_um=(2**53 + 2, 0),
        output_size=(4, 1),
        interpolation="area",
        padding_mode=padding,
    )
    with pytest.raises(ValueError, match="area footprint.*collapses"):
        read_physical_patch_result(reader, patch, metadata=meta)
    assert not reader.calls


@pytest.mark.parametrize("mode", ["bicubic", "area"])
@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_extended_kernels_preserve_phase_across_many_output_rows(mode, padding):
    array = gradient()
    patch = request(
        origin_um=(-0.25, -0.75),
        target_mpp=1.5,
        output_size=(3, 140),
        interpolation=mode,
        padding_mode=padding,
    )
    result = read_physical_patch_result(Reader(array), patch)
    expected = sample_rgb(
        array,
        origin=patch.origin_um,
        target=patch.target_mpp,
        size=patch.output_size,
        mode=mode,
        padding=padding,
    )
    np.testing.assert_array_equal(result.image, expected[0])
    np.testing.assert_array_equal(result.valid_mask, expected[1])
