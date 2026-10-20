"""Backend-neutral whole-slide image patch reading and sampling."""

from importlib.metadata import PackageNotFoundError, version

from ._canvas import CanvasRounding
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
    ImageReader,
    KfbSlideReader,
    OpenSlideReader,
    SlideReader,
    TiffReader,
)
from .reading import read_physical_patch_result
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
from .sampling_plan import plan_physical_read
from .stream import PatchStream, PhysicalPatchStream
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
    OutputGrid,
    Patch,
    PatchRequest,
    PhysicalPatchRequest,
    PhysicalPatchResult,
    PixelFormat,
    SamplingContext,
    SamplingLimits,
    SamplingPlan,
    Size,
    SlideMetadata,
    SlideSpec,
    as_mpp,
    as_size,
)

try:
    __version__ = version("wsi-patchkit")
except PackageNotFoundError:
    __version__ = "0.3.0"

__all__ = [
    "MPP",
    "CanvasRounding",
    "PhysicalPatchRequest",
    "PhysicalPatchResult",
    "PhysicalPatchStream",
    "read_physical_patch_result",
    "OutputGrid",
    "SamplingLimits",
    "SamplingPlan",
    "plan_physical_read",
    "Size",
    "AutoSlideReader",
    "EncodedImage",
    "GridPatchRequestSampler",
    "IndexedPatchRequestSampler",
    "ImageReader",
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
