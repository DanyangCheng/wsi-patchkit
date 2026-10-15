"""Low-resolution tissue-mask mapping and request filtering."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import tifffile
from numpy.typing import NDArray

from ..types import PatchRequest, SamplingContext, SlideSpec, as_size


@dataclass(slots=True)
class TissueMask:
    """A low-resolution binary mask mapped over a virtual WSI canvas."""

    mask: NDArray[np.generic]
    canvas_size: tuple[int, int]
    _integral: NDArray[np.int64] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        mask = np.asarray(self.mask, dtype=bool)
        if mask.ndim != 2 or min(mask.shape) < 1:
            raise ValueError("mask must be a non-empty 2D array")
        self.mask = np.ascontiguousarray(mask)
        self.canvas_size = as_size(self.canvas_size, name="canvas_size")
        self._integral = np.pad(
            self.mask.astype(np.int64).cumsum(0).cumsum(1),
            ((1, 0), (1, 0)),
        )

    @classmethod
    def from_tiff(
        cls,
        path: str | Path,
        *,
        canvas_size: tuple[int, int],
    ) -> TissueMask:
        """Load a one-channel MINISBLACK or MINISWHITE TIFF mask."""
        with tifffile.TiffFile(path) as tif:
            page = tif.pages[0]
            array = np.asarray(page.asarray())
            while array.ndim > 2 and array.shape[-1] == 1:
                array = array[..., 0]
            if array.ndim != 2:
                raise ValueError("tissue mask TIFF must be single-channel")
            foreground = array == 0 if int(page.photometric) == 0 else array > 0
        return cls(foreground, canvas_size)

    def fraction(self, request: PatchRequest) -> float:
        """Approximate mask coverage within a request rectangle."""
        canvas_width, canvas_height = self.canvas_size
        x0, y0 = max(0, request.x), max(0, request.y)
        x1 = min(canvas_width, request.x + request.width)
        y1 = min(canvas_height, request.y + request.height)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        mask_height, mask_width = self.mask.shape
        mx0 = min(mask_width, x0 * mask_width // canvas_width)
        my0 = min(mask_height, y0 * mask_height // canvas_height)
        mx1 = min(mask_width, -(-x1 * mask_width // canvas_width))
        my1 = min(mask_height, -(-y1 * mask_height // canvas_height))
        if mx1 <= mx0 or my1 <= my0:
            return 0.0
        integral = self._integral
        count = (
            integral[my1, mx1]
            - integral[my0, mx1]
            - integral[my1, mx0]
            + integral[my0, mx0]
        )
        return float(count / ((mx1 - mx0) * (my1 - my0)))


class TissueFilter:
    """Filter requests using one tissue mask per slide."""

    def __init__(
        self,
        masks: Mapping[str | Path, TissueMask],
        *,
        minimum_fraction: float = 0.01,
        missing: str = "error",
    ) -> None:
        if not 0 <= minimum_fraction <= 1:
            raise ValueError("minimum_fraction must be in [0, 1]")
        if missing not in ("error", "keep", "drop"):
            raise ValueError("missing must be 'error', 'keep', or 'drop'")
        self.masks = {str(Path(path)): mask for path, mask in masks.items()}
        self.minimum_fraction = float(minimum_fraction)
        self.missing = missing

    def filter(self, requests: Iterable[PatchRequest]) -> Iterator[PatchRequest]:
        for request in requests:
            mask = self.masks.get(request.slide)
            if mask is None:
                if self.missing == "error":
                    raise KeyError(f"no tissue mask for slide {request.slide!r}")
                if self.missing == "keep":
                    yield request
                continue
            if mask.fraction(request) >= self.minimum_fraction:
                yield request


@dataclass(frozen=True, slots=True)
class TissueRandomPatchRequestSampler:
    """Draw a fixed, deterministic number of random requests inside tissue.

    Unlike :class:`TissueFilter`, this sampler validates tissue membership
    before distributed sharding is observed by the consumer. Every virtual
    global sample therefore either resolves to a valid request or raises a
    clear error, allowing equal-length DDP shards.
    """

    masks: Mapping[str | Path, TissueMask]
    num_samples: int
    patch_size: int | tuple[int, int]
    minimum_fraction: float = 0.01
    max_attempts: int = 100
    seed: int = 0
    fill_value: int = 255

    def __post_init__(self) -> None:
        if self.num_samples < 1:
            raise ValueError("num_samples must be positive")
        if not 0 <= self.minimum_fraction <= 1:
            raise ValueError("minimum_fraction must be in [0, 1]")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if not 0 <= self.fill_value <= 255:
            raise ValueError("fill_value must be in [0, 255]")
        object.__setattr__(
            self,
            "patch_size",
            as_size(self.patch_size, name="patch_size"),
        )
        object.__setattr__(
            self,
            "masks",
            {str(Path(path)): mask for path, mask in self.masks.items()},
        )

    def sample(
        self,
        slides: Iterable[SlideSpec],
        *,
        context: SamplingContext | None = None,
    ) -> Iterator[PatchRequest]:
        """Yield valid virtual-canvas requests assigned to ``context``.

        Every positive-weight slide must provide a canvas-aligned tissue mask.
        Candidate attempts are seeded from the global sample index, so changing
        the worker count only changes which worker materializes each request.
        """
        context = context or SamplingContext()
        available = tuple(slides)
        if not available:
            raise ValueError("at least one slide is required")
        weights = np.asarray([slide.weight for slide in available], dtype=np.float64)
        if not np.any(weights > 0):
            raise ValueError("at least one slide must have positive weight")
        for slide in available:
            if slide.weight <= 0:
                continue
            try:
                mask = self.masks[slide.path]
            except KeyError as error:
                raise KeyError(f"no tissue mask for slide {slide.path!r}") from error
            if mask.canvas_size != slide.canvas_size:
                raise ValueError(
                    f"tissue mask canvas for {slide.path!r} is {mask.canvas_size}, "
                    f"expected {slide.canvas_size}"
                )
        probabilities = weights / weights.sum()
        patch_width, patch_height = self.patch_size
        for virtual_index in context.indices(self.num_samples):
            global_index = context.source_index(virtual_index, self.num_samples)
            for attempt in range(self.max_attempts):
                rng = np.random.default_rng(
                    np.random.SeedSequence(
                        [self.seed, context.epoch, global_index, attempt]
                    )
                )
                slide = available[int(rng.choice(len(available), p=probabilities))]
                width, height = slide.canvas_size
                max_x = max(width - patch_width, 0)
                max_y = max(height - patch_height, 0)
                request = PatchRequest(
                    slide.path,
                    int(rng.integers(0, max_x + 1)) if max_x else 0,
                    int(rng.integers(0, max_y + 1)) if max_y else 0,
                    patch_width,
                    patch_height,
                    slide.target_mpp,
                    source_mpp=slide.source_mpp,
                    fill_value=self.fill_value,
                    canvas_rounding=slide.canvas_rounding,
                )
                if self.masks[slide.path].fraction(request) >= self.minimum_fraction:
                    yield request
                    break
            else:
                raise RuntimeError(
                    "unable to draw a tissue-valid patch after "
                    f"{self.max_attempts} attempts for global sample {global_index}"
                )
