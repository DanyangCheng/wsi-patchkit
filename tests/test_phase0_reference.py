"""Self-check the independent oracle against analytic and fixed examples."""

import numpy as np
import pytest
from phase0_reference import (
    area_weights,
    gradient,
    impulse,
    keys,
    point_weights,
    quantize,
    reflect_index,
    sample_rgb,
)


@pytest.mark.parametrize(
    "origin,target,size,mode,red",
    [
        ((1, 1), (1, 1), (3, 2), "nearest", [10, 20, 30]),
        ((0, 0), (3, 1), (2, 1), "nearest", [10, 40]),
        ((1.5, 0), (1.5, 1), (3, 1), "bilinear", [18, 33, 48]),
        ((0.5, 0), (1, 1), (3, 1), "bilinear", [5, 15, 25]),
        ((0.5, 1), (1, 1), (3, 1), "bicubic", [4, 15, 25]),
    ],
)
def test_requirement_examples(origin, target, size, mode, red):
    image, _, _, _ = sample_rgb(
        gradient(), origin=origin, target=target, size=size, mode=mode
    )
    np.testing.assert_array_equal(image[0, :, 0], red)


@pytest.mark.parametrize("position,index", [(-1.5, -1), (-0.5, 0), (0.5, 1), (1.5, 2)])
def test_nearest_ties(position, index):
    assert point_weights(position, "nearest") == [(index, 1)]


def test_non_square_grid_and_adjacent_phase():
    kwargs = dict(target=(0.5, 1.5), size=(3, 2))
    left = sample_rgb(gradient(), origin=(1.25, 0.25), **kwargs)
    right = sample_rgb(gradient(), origin=(2.25, 0.25), **kwargs)
    np.testing.assert_allclose(left[2][0, 0], [1.5, 1], rtol=0, atol=1e-12)
    np.testing.assert_array_equal(left[0][:, 2], right[0][:, 0])
    np.testing.assert_array_equal(left[2][:, 2], right[2][:, 0])


def test_half_open_mask_and_full_constant():
    image, mask, _, _ = sample_rgb(
        gradient(3, 1), origin=(0, 0), target=(2, 1), size=(2, 1), mode="nearest"
    )
    np.testing.assert_array_equal(image[0, :, 0], [10, 255])
    np.testing.assert_array_equal(mask, [[True, False]])
    image, mask, _, _ = sample_rgb(
        gradient(), origin=(-100, -100), target=(1, 1), size=(2, 2), fill=(1, 2, 3)
    )
    np.testing.assert_array_equal(image, np.broadcast_to([1, 2, 3], (2, 2, 3)))
    assert not mask.any()


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
@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_exact_four_physical_boundaries(center, valid, padding):
    _, mask, _, _ = sample_rgb(
        gradient(3, 3),
        origin=(center[0] - 0.5, center[1] - 0.5),
        target=(1, 1),
        size=(1, 1),
        padding=padding,
    )
    assert bool(mask[0, 0]) is valid


@pytest.mark.parametrize(
    "origin", [(-1, 0), (7, 0), (0, -1), (0, 3), (-1, -1), (7, -1), (-1, 3), (7, 3)]
)
def test_constant_four_edges_and_corners(origin):
    _, mask, centers, raw = sample_rgb(
        gradient(), origin=origin, target=(1, 1), size=(2, 2), fill=(1, 2, 3)
    )
    for row in range(2):
        for col in range(2):
            x, y = centers[row, col]
            assert mask[row, col] == (0 <= x < 8 and 0 <= y < 4)
            if not mask[row, col]:
                np.testing.assert_array_equal(raw[row, col], [1, 2, 3])


def test_keys_impulse_and_saturation():
    assert keys(0) == 1
    assert keys(0.5) == 0.5625
    assert keys(1.5) == -0.0625
    assert keys(2) == 0
    _, _, _, raw = sample_rgb(
        impulse(), origin=(3.5, 3), target=(1, 1), size=(1, 1), mode="bicubic"
    )
    np.testing.assert_allclose(raw, [[[143.4375] * 3]], rtol=0, atol=1e-12)
    step = np.zeros((1, 8, 3), dtype=np.uint8)
    step[:, 4:] = 255
    image, _, _, raw = sample_rgb(
        step, origin=(2.5, 0), target=(1, 1), size=(3, 1), mode="bicubic"
    )
    np.testing.assert_allclose(raw[0, :, 0], [-15.9375, 127.5, 270.9375])
    np.testing.assert_array_equal(image[0, :, 0], [0, 128, 255])
    np.testing.assert_array_equal(
        quantize([-1, 0.49, 0.5, 254.5, 300]), [0, 0, 1, 255, 255]
    )


def test_reflection_matches_numpy_and_far_single_axis():
    expected = np.pad(np.arange(4), (12, 12), mode="reflect")
    assert [reflect_index(i, 4) for i in range(-12, 16)] == expected.tolist()
    image, mask, _, _ = sample_rgb(
        gradient(4, 1),
        origin=(-100.5, -999),
        target=(1, 1),
        size=(1, 1),
        padding="reflect",
        mode="bilinear",
    )
    np.testing.assert_array_equal(image, [[[15, 0, 0]]])
    assert not mask.any()


def test_area_phase_boundary_and_reflection():
    assert area_weights(0.5, 2.5) == [(0, 0.25), (1, 0.5), (2, 0.25)]
    for origin, padding, expected in [
        ((0.5, 0), "constant", 10),
        ((-0.5, 0), "constant", 3),
        ((-0.5, 0), "reflect", 5),
        ((-2, 0), "constant", 255),
    ]:
        image, _, _, _ = sample_rgb(
            gradient(),
            origin=origin,
            target=(2, 1),
            size=(1, 1),
            mode="area",
            padding=padding,
        )
        assert image[0, 0, 0] == expected
    with pytest.raises(ValueError, match="enlarge"):
        sample_rgb(gradient(), origin=(0, 0), target=(0.5, 2), size=(1, 1), mode="area")


def test_area_exact_rational_half_value():
    # A 3x2 footprint has sum=765, mean=127.5, so uint8 must be 128.
    image = np.repeat(
        np.array([[128, 127, 128], [127, 128, 127]], dtype=np.uint8)[..., None],
        3,
        axis=2,
    )
    result, _, _, raw = sample_rgb(
        image, origin=(0, 0), target=(3, 2), size=(1, 1), mode="area"
    )
    np.testing.assert_array_equal(raw, [[[127.5] * 3]])
    np.testing.assert_array_equal(result, [[[128] * 3]])
