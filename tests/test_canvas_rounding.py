"""A08/A16: share rounding from SlideSpec through every request sampler."""

import numpy as np
import pytest

from wsi_patchkit import (
    GridPatchRequestSampler,
    IndexedPatchRequestSampler,
    LevelInfo,
    PatchRequest,
    PatchStream,
    RandomPatchRequestSampler,
    SlideMetadata,
    SlideSpec,
    TissueMask,
    TissueRandomPatchRequestSampler,
    plan_aligned_read,
    read_aligned_patch_result,
    virtual_canvas_size,
)


class Reader:
    def __init__(self):
        self.calls = []
        self.info = SlideMetadata(
            "fixture", (LevelInfo(0, (5, 3), (1, 1), (1, 1)),), mpp=(1, 1)
        )

    def metadata(self, slide, *, source_mpp=None):
        return self.info

    def read_region(self, slide, location, level, size):
        self.calls.append((location, level, size))
        return np.full((size[1], size[0], 3), 10, dtype=np.uint8)


@pytest.mark.parametrize("rounding,canvas", [("round", (2, 2)), ("ceil", (3, 2))])
def test_rounding_is_shared_by_spec_grid_and_read(rounding, canvas):
    reader = Reader()
    slide = SlideSpec.from_metadata(reader.info, target_mpp=2, canvas_rounding=rounding)
    assert slide.canvas_size == canvas
    assert virtual_canvas_size(reader.info, (2, 2), canvas_rounding=rounding) == canvas
    requests = list(GridPatchRequestSampler((2, 2), stride=(2, 2)).sample([slide]))
    assert [p.x for p in requests] == ([0] if rounding == "round" else [0, 1])
    for patch in requests:
        assert patch.canvas_rounding == rounding
        result = read_aligned_patch_result(reader, patch, interpolation="nearest")
        assert result.valid_mask.all()
        assert result.plan.destination_box == (0, 0, 2, 2)
    stream = list(PatchStream(reader, requests, interpolation="nearest"))
    assert len(stream) == len(requests)
    assert np.all(stream[-1].image == 10)


def test_helper_override_is_explicit_and_default_uses_request():
    reader = Reader()
    patch = PatchRequest("fixture", 1, 0, 2, 2, 2, canvas_rounding="ceil")
    assert plan_aligned_read(reader.info, patch).destination_box == (0, 0, 2, 2)
    assert plan_aligned_read(
        reader.info, patch, canvas_rounding="round"
    ).destination_box == (0, 0, 1, 2)
    result = read_aligned_patch_result(
        reader, patch, interpolation="nearest", canvas_rounding="round"
    )
    assert result.valid_mask[:, 0].all() and not result.valid_mask[:, 1].any()
    assert np.all(result.image[:, 1] == 255)


def test_drop_and_patch_larger_than_canvas_keep_existing_meaning():
    slide = SlideSpec.from_metadata(Reader().info, target_mpp=2, canvas_rounding="ceil")
    assert len(list(GridPatchRequestSampler((2, 2), edge="drop").sample([slide]))) == 1
    assert list(GridPatchRequestSampler((8, 8), edge="drop").sample([slide])) == []
    patches = list(GridPatchRequestSampler((8, 8), edge="align").sample([slide]))
    assert len(patches) == 1 and patches[0].canvas_rounding == "ceil"
    result = read_aligned_patch_result(Reader(), patches[0], interpolation="nearest")
    assert result.image.shape == (8, 8, 3)
    assert result.valid_mask.sum() == 6


def test_random_tissue_and_indexed_preserve_rounding():
    slide = SlideSpec.from_metadata(Reader().info, target_mpp=2, canvas_rounding="ceil")
    samplers = [
        RandomPatchRequestSampler(num_samples=10, patch_size=(2, 2)),
        TissueRandomPatchRequestSampler(
            {slide.path: TissueMask(np.ones((2, 3)), canvas_size=slide.canvas_size)},
            num_samples=10,
            patch_size=(2, 2),
            minimum_fraction=1,
        ),
    ]
    for sampler in samplers:
        patches = list(sampler.sample([slide]))
        assert all(p.canvas_rounding == "ceil" for p in patches)
        assert list(IndexedPatchRequestSampler(patches).sample()) == patches


@pytest.mark.parametrize("value", ["floor", None, True])
def test_illegal_canvas_option_fails_in_all_entry_points(value):
    reader = Reader()
    with pytest.raises(ValueError, match="canvas_rounding"):
        SlideSpec.from_metadata(reader.info, target_mpp=2, canvas_rounding=value)
    with pytest.raises(ValueError, match="canvas_rounding"):
        PatchRequest("fixture", 0, 0, 1, 1, 1, canvas_rounding=value)
    with pytest.raises(ValueError, match="canvas_rounding"):
        virtual_canvas_size(reader.info, (2, 2), canvas_rounding=value)


@pytest.mark.parametrize("value", [-1, True, 0.5, 1.0, 10])
def test_invalid_legacy_explicit_level_fails_before_region_io(value):
    reader = Reader()
    with pytest.raises(ValueError, match="level"):
        read_aligned_patch_result(
            reader, PatchRequest("fixture", 0, 0, 1, 1, 1), level=value
        )
    assert reader.calls == []
