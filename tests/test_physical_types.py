"""A01/A13/A16: strict values, center affine, and explicit legacy migration."""

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from wsi_patchkit import (
    OutputGrid,
    PatchRequest,
    PhysicalPatchRequest,
    SamplingLimits,
    as_mpp,
    as_size,
)


def request(**overrides):
    fields = dict(origin_um=(-0.25, 1.25), output_size=(3, 2), target_mpp=(0.5, 1.5))
    fields.update(overrides)
    return PhysicalPatchRequest("fixture", **fields)


def test_continuous_grid_affine_and_immutable_values():
    result = request()
    assert result.origin_um == (-0.25, 1.25)
    assert result.output_size == (3, 2)
    matrix = np.asarray(result.grid.output_to_slide_um)
    np.testing.assert_allclose(matrix @ [2, 1, 1], [1, 3.5, 1], rtol=0, atol=1e-12)
    with pytest.raises(FrozenInstanceError):
        result.origin_um = (0, 0)
    assert hash(result.grid) == hash(
        OutputGrid(origin_um=(-0.25, 1.25), target_mpp=(0.5, 1.5), output_size=(3, 2))
    )


def test_numpy_numeric_values_and_rgb_fill_normalization():
    result = request(
        output_size=(np.int64(3), np.uint32(2)),
        level=np.int64(0),
        target_mpp=np.float64(0.5),
        fill_value=np.array([1, 2, 3], dtype=np.uint8),
    )
    assert result.target_mpp == (0.5, 0.5)
    assert result.fill_value == (1, 2, 3)
    assert result.level == 0
    assert type(result.output_size[0]) is int


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin_um", (True, 0)),
        ("origin_um", (np.bool_(False), 0)),
        ("origin_um", (np.nan, 0)),
        ("origin_um", (0, np.inf)),
        ("origin_um", (0,)),
        ("origin_um", "12"),
        ("origin_um", None),
        ("target_mpp", 0),
        ("target_mpp", -1),
        ("target_mpp", True),
        ("target_mpp", np.bool_(True)),
        ("target_mpp", np.nan),
        ("target_mpp", (1, np.inf)),
        ("target_mpp", (1, False)),
        ("target_mpp", "1"),
        ("source_mpp", (0, 1)),
        ("source_mpp", True),
        ("output_size", (0, 2)),
        ("output_size", (-1, 2)),
        ("output_size", (True, 2)),
        ("output_size", (np.bool_(True), 2)),
        ("output_size", (1.0, 2)),
        ("output_size", (1.5, 2)),
        ("output_size", (1,)),
        ("output_size", 2),
        ("level", -1),
        ("level", True),
        ("level", np.bool_(True)),
        ("level", 1.0),
        ("level", 1.5),
        ("level", "0"),
        ("level_policy", "unknown"),
        ("interpolation", "unknown"),
        ("padding_mode", "clamp"),
        ("color_mode", "unknown"),
        ("fill_value", True),
        ("fill_value", np.bool_(True)),
        ("fill_value", 1.0),
        ("fill_value", -1),
        ("fill_value", 256),
        ("fill_value", (1, 2)),
        ("fill_value", (1, 2, 3, 4)),
        ("fill_value", (1, 2, True)),
        ("fill_value", (1, 2, 3.5)),
        ("fill_value", (1, 2, 300)),
    ],
)
def test_strict_fields_report_field(field, value):
    with pytest.raises(ValueError, match=field):
        request(**{field: value})


def test_policy_is_validated_even_with_explicit_level():
    with pytest.raises(ValueError, match="level_policy"):
        request(level=0, level_policy="invalid")


@pytest.mark.parametrize("mode", ["bilinear", "bicubic", "area"])
def test_native_requires_nearest(mode):
    with pytest.raises(ValueError, match="interpolation.*nearest"):
        request(color_mode="native", interpolation=mode)
    assert (
        request(
            color_mode="native", interpolation="nearest", fill_value=-100
        ).fill_value
        == -100
    )
    with pytest.raises(ValueError, match="fill_value"):
        request(color_mode="native", interpolation="nearest", fill_value=(1, 2, 3))


@pytest.mark.parametrize(
    "values",
    [
        dict(origin_um=(1e308, 0), target_mpp=1e308),
        dict(output_size=(10**400, 2)),
    ],
)
def test_derived_extent_overflow_is_localized(values):
    with pytest.raises(ValueError, match="output_size.*(overflow|finite)"):
        request(**values)


@pytest.mark.parametrize("field", ["max_output_pixels", "max_read_pixels"])
@pytest.mark.parametrize("value", [0, -1, True, np.bool_(True), 1.0, 1.5, np.nan, "1"])
def test_limits_strict(field, value):
    with pytest.raises(ValueError, match=field):
        SamplingLimits(**{field: value})


def test_default_and_independent_disabled_limits():
    assert SamplingLimits().max_output_pixels == 4096**2
    assert SamplingLimits().max_read_pixels == 8192**2
    assert SamplingLimits(max_output_pixels=None).max_read_pixels == 8192**2
    assert SamplingLimits(max_read_pixels=None).max_output_pixels == 4096**2
    assert SamplingLimits(max_output_pixels=np.int64(1)).max_output_pixels == 1


def test_legacy_conversion_uses_mpp_not_native_indices():
    old = PatchRequest(
        "fixture", -2, 3, 4, 5, (0.5, 1.5), source_mpp=(0.25, 0.5), fill_value=10
    )
    result = PhysicalPatchRequest.from_legacy(old)
    assert result.origin_um == (-1, 4.5)
    assert result.output_size == (4, 5)
    assert result.source_mpp == (0.25, 0.5)
    assert result.fill_value == 10


@pytest.mark.parametrize(
    "field,value",
    [
        ("x", 0.5),
        ("y", True),
        ("width", 2.5),
        ("height", 2.0),
        ("fill_value", False),
        ("target_mpp", True),
        ("source_mpp", (True, 0.5)),
    ],
)
def test_legacy_deprecated_inputs_are_rejected(field, value):
    fields = dict(x=0, y=0, width=2, height=2, target_mpp=1)
    fields[field] = value
    with pytest.raises(ValueError, match=field):
        PatchRequest("fixture", **fields)


@pytest.mark.parametrize("value", [True, np.bool_(True), 2.0, 2.5, np.float64(2)])
@pytest.mark.parametrize("field", ["x", "y", "width", "height", "fill_value"])
def test_legacy_integer_fields_reject_non_integer_types(field, value):
    fields = dict(x=0, y=0, width=2, height=2, target_mpp=1)
    fields[field] = value
    with pytest.raises(ValueError, match=field):
        PatchRequest("fixture", **fields)


@pytest.mark.parametrize("value", [True, np.bool_(True), 2.0, 2.5, np.float64(2)])
@pytest.mark.parametrize("as_pair", [False, True])
def test_shared_size_rejects_deprecated_inputs(value, as_pair):
    with pytest.raises(ValueError, match="patch_size"):
        as_size((value, 3) if as_pair else value, name="patch_size")


@pytest.mark.parametrize("value", [True, False, np.bool_(True), np.bool_(False)])
@pytest.mark.parametrize("as_pair", [False, True])
def test_shared_mpp_rejects_bool(value, as_pair):
    with pytest.raises(ValueError, match="mpp"):
        as_mpp((0.5, value) if as_pair else value)


def test_shared_helpers_preserve_valid_numeric_inputs():
    assert as_size(np.int64(2)) == (2, 2)
    assert as_size((np.int32(2), 3)) == (2, 3)
    # Fractional MPP is a valid physical resolution, not an integer field.
    assert as_mpp((0.25, 0.5)) == (0.25, 0.5)
