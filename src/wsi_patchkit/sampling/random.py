"""Reproducible random sampling."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import numpy as np

from ..types import PatchRequest, SamplingContext, SlideSpec, as_size


@dataclass(frozen=True, slots=True)
class RandomPatchRequestSampler:
    """Draw globally deterministic random patch requests across weighted slides."""

    num_samples: int
    patch_size: int | tuple[int, int]
    seed: int = 0
    fill_value: int = 255

    def __post_init__(self) -> None:
        if self.num_samples < 1:
            raise ValueError("num_samples must be positive")
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if not 0 <= self.fill_value <= 255:
            raise ValueError("fill_value must be in [0, 255]")
        object.__setattr__(
            self, "patch_size", as_size(self.patch_size, name="patch_size")
        )

    def sample(
        self,
        slides: Iterable[SlideSpec],
        *,
        context: SamplingContext | None = None,
    ) -> Iterator[PatchRequest]:
        context = context or SamplingContext()
        available = tuple(slides)
        if not available:
            raise ValueError("at least one slide is required")
        weights = np.asarray([slide.weight for slide in available], dtype=np.float64)
        if not np.any(weights > 0):
            raise ValueError("at least one slide must have positive weight")
        probabilities = weights / weights.sum()
        patch_width, patch_height = self.patch_size
        for virtual_index in context.indices(self.num_samples):
            global_index = context.source_index(virtual_index, self.num_samples)
            rng = np.random.default_rng(
                np.random.SeedSequence([self.seed, context.epoch, global_index])
            )
            slide = available[int(rng.choice(len(available), p=probabilities))]
            width, height = slide.canvas_size
            max_x = max(width - patch_width, 0)
            max_y = max(height - patch_height, 0)
            x = int(rng.integers(0, max_x + 1)) if max_x else 0
            y = int(rng.integers(0, max_y + 1)) if max_y else 0
            yield PatchRequest(
                slide.path,
                x,
                y,
                patch_width,
                patch_height,
                slide.target_mpp,
                source_mpp=slide.source_mpp,
                fill_value=self.fill_value,
                canvas_rounding=slide.canvas_rounding,
            )
