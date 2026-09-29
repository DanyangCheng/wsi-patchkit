"""Backend-neutral whole-slide image patch reading and sampling."""

from importlib.metadata import PackageNotFoundError, version

from .geometry import (
    Interpolation,
    LevelSelectionPolicy,
    PatchReadResult,
    ReadPlan,
    choose_level,
    plan_aligned_read,
    read_aligned_patch,
    read_aligned_patch_result,
    virtual_canvas_size,
)
from .io import (
    AutoSlideReader,
    KfbSlideReader,
    OpenSlideReader,
    SlideReader,
    TiffReader,
)
from .sampling import (
    GridPatchRequestSampler,
    IndexedPatchRequestSampler,
    PatchRequestSampler,
    PatchRequestSource,
    RandomPatchRequestSampler,
    TissueFilter,
    TissueMask,
    TissueRandomPatchRequestSampler,
    axis_positions,
)
from .stream import PatchStream
from .tiles import (
    EncodedImage,
    SlideReaderPool,
    TileRenderer,
    choose_level_for_downsample,
    iiif_scale_factors,
)
from .types import (
    MPP,
    LevelInfo,
    Patch,
    PatchRequest,
    PixelFormat,
    SamplingContext,
    Size,
    SlideMetadata,
    SlideSpec,
    as_mpp,
    as_size,
)

try:
    __version__ = version("wsi-patchkit")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = [
    "MPP",
    "Size",
    "AutoSlideReader",
    "EncodedImage",
    "GridPatchRequestSampler",
    "IndexedPatchRequestSampler",
    "Interpolation",
    "KfbSlideReader",
    "LevelSelectionPolicy",
    "LevelInfo",
    "OpenSlideReader",
    "Patch",
    "PatchReadResult",
    "PatchRequest",
    "PatchRequestSampler",
    "PatchRequestSource",
    "PatchStream",
    "PixelFormat",
    "RandomPatchRequestSampler",
    "ReadPlan",
    "SamplingContext",
    "SlideMetadata",
    "SlideReader",
    "SlideReaderPool",
    "SlideSpec",
    "TiffReader",
    "TileRenderer",
    "TissueFilter",
    "TissueMask",
    "TissueRandomPatchRequestSampler",
    "as_mpp",
    "as_size",
    "axis_positions",
    "choose_level",
    "choose_level_for_downsample",
    "iiif_scale_factors",
    "plan_aligned_read",
    "read_aligned_patch",
    "read_aligned_patch_result",
    "virtual_canvas_size",
]
