"""A01/A04/A13: IO-free budgets, exact-grid plans, and native origins."""

import math

import numpy as np
import pytest
from phase0_fixtures import write_backend_samples
from phase0_reference import area_weights, point_weights, reflect_index

from wsi_patchkit import (
    LevelInfo,
    PhysicalPatchRequest,
    PixelFormat,
    SamplingLimits,
    SlideMetadata,
    TiffReader,
    plan_physical_read,
)


def metadata(dimensions=(8, 4), mpp=(1, 1), pixel_format=None):
    return SlideMetadata(
        "fixture",
        (LevelInfo(0, dimensions, (1, 1), mpp, pixel_format=pixel_format),),
        mpp=mpp,
    )


def request(**overrides):
    values = dict(origin_um=(0, 0), output_size=(3, 2), target_mpp=(1, 1))
    values.update(overrides)
    return PhysicalPatchRequest("fixture", **values)


def test_plan_preserves_phase_and_separates_native_and_output_grids():
    result = plan_physical_read(
        metadata(), request(origin_um=(1.5, 0), target_mpp=(1.5, 1), output_size=(3, 1))
    )
    assert result.native_origin == (1, 0)
    assert result.read_size == (5, 2)
    assert result.read_location == (1, 0)
    assert result.grid.origin_um == (1.5, 0)
    assert result.grid.target_mpp == (1.5, 1)
    assert result.source_level == 0
    assert result.output_pixels == 3 and result.read_pixels == 10


def test_planner_does_not_allocate_arrays(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("planner attempted an array allocation")

    for name in (
        "empty",
        "zeros",
        "ones",
        "full",
        "arange",
        "meshgrid",
        "indices",
        "array",
    ):
        monkeypatch.setattr(np, name, forbidden)
    plan = plan_physical_read(
        metadata(),
        request(output_size=(10**12, 2)),
        limits=SamplingLimits(max_output_pixels=None),
    )
    assert plan.output_pixels == 2 * 10**12
    assert plan.read_pixels == 24
    with pytest.raises(ValueError, match="max_output_pixels"):
        plan_physical_read(metadata(), request(output_size=(10**12, 2)))


@pytest.mark.parametrize(
    "mode,size",
    [
        ("nearest", (1, 1)),
        ("bilinear", (2, 2)),
        ("bicubic", (4, 4)),
        ("area", (3, 3)),
    ],
)
def test_kernel_neighbors_count_against_read_limit(mode, size):
    patch = request(
        origin_um=(2.5, 1.5),
        output_size=(1, 1),
        target_mpp=2 if mode == "area" else 1,
        interpolation=mode,
    )
    plan = plan_physical_read(metadata(dimensions=(10, 10)), patch)
    assert plan.read_size == size
    limit = plan.read_pixels
    assert (
        plan_physical_read(
            metadata(dimensions=(10, 10)),
            patch,
            limits=SamplingLimits(max_read_pixels=limit),
        ).read_pixels
        == limit
    )
    if limit > 1:
        with pytest.raises(
            ValueError, match="max_read_pixels.*source_level=0.*read_size=.*limit="
        ):
            plan_physical_read(
                metadata(dimensions=(10, 10)),
                patch,
                limits=SamplingLimits(max_read_pixels=limit - 1),
            )


def test_output_limit_boundary_and_none_are_independent():
    for limit in (6, 7, None):
        assert (
            plan_physical_read(
                metadata(),
                request(),
                limits=SamplingLimits(max_output_pixels=limit, max_read_pixels=None),
            ).output_pixels
            == 6
        )
    with pytest.raises(
        ValueError,
        match="max_output_pixels.*fixture.*source_level=0.*output_size=.*limit=5",
    ):
        plan_physical_read(
            metadata(), request(), limits=SamplingLimits(max_output_pixels=5)
        )


def test_constant_boundary_uses_centers_not_footprint_or_canvas():
    plan = plan_physical_read(
        metadata(dimensions=(3, 1)),
        request(output_size=(2, 1), target_mpp=(2, 1), interpolation="nearest"),
    )
    assert plan.native_origin == (1, 0) and plan.read_size == (1, 1)
    plan = plan_physical_read(
        metadata(dimensions=(3, 1)),
        request(origin_um=(2.5, 0), output_size=(1, 1), interpolation="nearest"),
    )
    assert plan.read_size == (0, 0)
    assert plan.native_origin is None and plan.read_location is None
    assert plan.source_level == 0 and plan.grid.origin_um == (2.5, 0)


@pytest.mark.parametrize("mode", ["nearest", "bilinear", "bicubic", "area"])
def test_completely_outside_constant_has_zero_read_budget(mode):
    plan = plan_physical_read(
        metadata(),
        request(origin_um=(-100, -100), interpolation=mode),
        limits=SamplingLimits(max_read_pixels=1),
    )
    assert plan.read_pixels == 0
    assert plan.read_location is None


def test_far_reflect_maps_before_budget_and_handles_single_axis():
    plan = plan_physical_read(
        metadata(dimensions=(4, 1)),
        request(origin_um=(-100.5, -999), output_size=(1, 1), padding_mode="reflect"),
        limits=SamplingLimits(max_read_pixels=2),
    )
    assert plan.native_origin == (1, 0) and plan.read_size == (2, 1)
    assert plan.read_location == (1, 0)


@pytest.mark.parametrize("mode", ["nearest", "bilinear", "bicubic", "area"])
@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_small_plans_contain_independent_oracle_neighbors(mode, padding):
    rng = np.random.default_rng(701)
    for _ in range(30):
        origin = tuple(rng.uniform(-20, 20, 2))
        target = tuple(rng.uniform(1, 4, 2))
        size = tuple(int(v) for v in rng.integers(1, 6, 2))
        patch = request(
            origin_um=origin,
            target_mpp=target,
            output_size=size,
            interpolation=mode,
            padding_mode=padding,
        )
        plan = plan_physical_read(metadata(), patch)
        needed = [set(), set()]
        for row in range(size[1]):
            for col in range(size[0]):
                centers = [
                    origin[a] + ([col, row][a] + 0.5) * target[a] for a in (0, 1)
                ]
                if padding == "constant" and not (
                    0 <= centers[0] < 8 and 0 <= centers[1] < 4
                ):
                    continue
                for axis, (index, length) in enumerate(((col, 8), (row, 4))):
                    if mode == "area":
                        weights = area_weights(
                            origin[axis] + index * target[axis],
                            origin[axis] + (index + 1) * target[axis],
                        )
                    else:
                        weights = point_weights(centers[axis] - 0.5, mode)
                    for native, weight in weights:
                        if weight != 0:
                            mapped = (
                                reflect_index(native, length)
                                if padding == "reflect"
                                else min(max(native, 0), length - 1)
                            )
                            needed[axis].add(mapped)
        if not needed[0]:
            assert plan.read_pixels == 0
        else:
            for axis in (0, 1):
                assert plan.native_origin[axis] <= min(needed[axis])
                assert plan.native_origin[axis] + plan.read_size[axis] > max(
                    needed[axis]
                )
                assert plan.read_size[axis] <= (8, 4)[axis]


def test_reflect_area_large_footprint_does_not_enumerate_native_cells():
    plan = plan_physical_read(
        metadata(),
        request(
            origin_um=(-1e12, -1e12),
            target_mpp=2e12,
            output_size=(1, 1),
            interpolation="area",
            padding_mode="reflect",
        ),
    )
    assert plan.native_origin == (0, 0) and plan.read_size == (8, 4)


def test_reflect_sparse_stride_does_not_read_whole_unmapped_envelope():
    plan = plan_physical_read(
        metadata(dimensions=(10, 1)),
        request(
            output_size=(4, 1),
            target_mpp=(18, 1),
            interpolation="nearest",
            padding_mode="reflect",
        ),
    )
    assert plan.native_origin == (9, 0) and plan.read_size == (1, 1)


@pytest.mark.parametrize("index", [0, 1, 2, 17, 29, 270])
def test_anisotropic_noninteger_level_start(index):
    downsample = (1000 / 333, 800 / 271)
    source = (0.25, 0.5)
    native = tuple(source[a] * downsample[a] for a in (0, 1))
    meta = SlideMetadata(
        "fixture",
        (
            LevelInfo(0, (1000, 800), (1, 1), source),
            LevelInfo(1, (333, 271), downsample, native),
        ),
        mpp=source,
    )
    plan = plan_physical_read(
        meta,
        request(
            origin_um=(index * native[0], index * native[1]),
            output_size=(1, 1),
            target_mpp=native,
            level=1,
            interpolation="nearest",
            source_mpp=source,
        ),
    )
    assert plan.native_origin == (index, index)
    assert plan.source_level == 1
    for axis in (0, 1):
        assert math.floor(plan.read_location[axis] / downsample[axis]) == index
    assert plan.grid.origin_um == (index * native[0], index * native[1])


def test_level_selection_ties_finer_and_explicit_priority():
    meta = SlideMetadata(
        "fixture",
        (
            LevelInfo(0, (16, 16), (1, 1), (1, 1)),
            LevelInfo(1, (4, 4), (4, 4), (4, 4)),
            LevelInfo(2, (2, 2), (8, 8), (8, 8)),
        ),
        mpp=(1, 1),
    )
    for target, policy, level, expected in [
        (2, "nearest", None, 0),
        (3, "nearest", None, 1),
        ((4, 3), "finer", None, 0),
        (6, "finer", None, 1),
        (0.5, "finer", None, 0),
        (6, "finer", 2, 2),
    ]:
        assert (
            plan_physical_read(
                meta, request(target_mpp=target, level_policy=policy, level=level)
            ).source_level
            == expected
        )
    with pytest.raises(ValueError, match="level=3.*fixture"):
        plan_physical_read(meta, request(level=3))


def test_metadata_path_mpp_and_missing_level_mpp():
    with pytest.raises(ValueError, match="metadata.path"):
        plan_physical_read(
            metadata(),
            PhysicalPatchRequest(
                "other", origin_um=(0, 0), output_size=(1, 1), target_mpp=1
            ),
        )
    with pytest.raises(ValueError, match="source_mpp"):
        plan_physical_read(metadata(), request(source_mpp=0.5))
    meta = SlideMetadata("fixture", (LevelInfo(0, (8, 4), (1, 1)),))
    with pytest.raises(ValueError, match="metadata.mpp"):
        plan_physical_read(meta, request())
    meta = SlideMetadata("fixture", (LevelInfo(0, (8, 4), (1, 1)),), mpp=(1, 1))
    with pytest.raises(ValueError, match="level.mpp"):
        plan_physical_read(meta, request(level=0))
    with pytest.raises(ValueError, match="limits"):
        plan_physical_read(metadata(), request(), limits=None)


def test_derived_native_index_overflow_and_area_enlargement():
    with pytest.raises(ValueError, match="native center index.*finite"):
        plan_physical_read(metadata(mpp=(1e-300, 1)), request(origin_um=(1e100, 0)))
    with pytest.raises(ValueError, match="area.*enlarge"):
        plan_physical_read(
            metadata(), request(target_mpp=(0.5, 2), interpolation="area")
        )


def test_native_unknown_outside_and_dtype_fill_prechecks():
    with pytest.raises(ValueError, match="pixel_format"):
        plan_physical_read(
            metadata(),
            request(
                origin_um=(-100, -100), color_mode="native", interpolation="nearest"
            ),
        )
    meta = metadata(pixel_format=PixelFormat("uint16", 1))
    assert (
        plan_physical_read(
            meta,
            request(color_mode="native", interpolation="nearest", fill_value=65535),
        ).read_pixels
        > 0
    )
    with pytest.raises(ValueError, match="fill_value.*uint16"):
        plan_physical_read(
            meta,
            request(color_mode="native", interpolation="nearest", fill_value=65536),
        )
    with pytest.raises(ValueError, match="RGB.*uint8"):
        plan_physical_read(meta, request())


def test_metadata_extent_overflow_reports_a_field():
    meta = metadata(dimensions=(10**400, 1))
    with pytest.raises(ValueError, match="metadata physical extent.*overflow"):
        plan_physical_read(meta, request())


@pytest.mark.parametrize("base,reduced", [(1000, 333), (800, 267)])
def test_new_plan_reads_real_tiff_native_origin(tmp_path, base, reduced):
    _, path, expected = write_backend_samples(tmp_path, base, reduced)
    with TiffReader() as reader:
        meta = reader.metadata(path)
        native = meta.levels[1].mpp
        for index in (0, 1, 17, reduced - 3):
            patch = PhysicalPatchRequest(
                path,
                origin_um=(index * native[0], index * native[1]),
                output_size=(2, 2),
                target_mpp=native,
                source_mpp=meta.mpp,
                interpolation="nearest",
                level=1,
            )
            plan = plan_physical_read(meta, patch)
            assert plan.native_origin == (index, index)
            actual = reader.read_region(
                path, plan.read_location, plan.source_level, plan.read_size
            )
            np.testing.assert_array_equal(
                actual, expected[index : index + 2, index : index + 2]
            )


def test_source_override_checks_selected_level_mapping():
    meta = SlideMetadata(
        "fixture",
        (
            LevelInfo(0, (8, 8), (1, 1), (1, 1)),
            LevelInfo(1, (4, 4), (2, 2), (3, 3)),
        ),
        mpp=(1, 1),
    )
    with pytest.raises(ValueError, match="level.mpp.*source_mpp"):
        plan_physical_read(meta, request(level=1, source_mpp=1))
