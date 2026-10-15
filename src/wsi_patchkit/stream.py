"""Lazy conversion of patch requests into image arrays."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Literal

from ._streaming import ReadIterator
from ._validation import integer
from .geometry import Interpolation, read_aligned_patch
from .io.base import SlideReader
from .reading import read_physical_patch_result
from .sampling_plan import DEFAULT_LIMITS
from .types import (
    Patch,
    PatchRequest,
    PhysicalPatchRequest,
    PhysicalPatchResult,
    SamplingLimits,
    SlideMetadata,
)


class PatchStream(Iterable[Patch]):
    """Read requests in order, optionally using independent readers per thread.

    Parallel reads require ``reader_factory`` because reader handles and caches
    must not be shared between threads. Factory-created readers are owned and
    closed by the iterator; a supplied ``reader`` remains caller-owned.
    Close the iterator explicitly when stopping before exhaustion.
    """

    def __init__(
        self,
        reader: SlideReader | None,
        requests: Iterable[PatchRequest],
        *,
        interpolation: Interpolation = "bilinear",
        color_mode: Literal["rgb", "native"] = "rgb",
        num_workers: int = 1,
        max_pending: int | None = None,
        reader_factory: Callable[[], SlideReader] | None = None,
        own_requests: bool = False,
    ) -> None:
        if interpolation not in ("nearest", "bilinear", "area"):
            raise ValueError("interpolation must be 'nearest', 'bilinear', or 'area'")
        num_workers = integer(num_workers, "num_workers", minimum=1)
        if max_pending is None:
            max_pending = 2 * num_workers
        max_pending = integer(max_pending, "max_pending", minimum=1)
        if not isinstance(own_requests, bool):
            raise ValueError("own_requests must be bool")
        if reader is not None and reader_factory is not None:
            raise ValueError("provide either reader or reader_factory")
        if reader_factory is not None and not callable(reader_factory):
            raise ValueError("reader_factory must be callable")
        if reader is None and reader_factory is None:
            raise ValueError("reader or reader_factory is required")
        if num_workers > 1 and reader_factory is None:
            raise ValueError("parallel reads require reader_factory")
        self.reader = reader
        self.requests = requests
        self.interpolation = interpolation
        self.color_mode = color_mode
        self.num_workers = num_workers
        self.max_pending = max_pending
        self.reader_factory = reader_factory
        self.own_requests = own_requests

    def _read(
        self,
        reader: SlideReader,
        request: PatchRequest,
        metadata_cache: dict[tuple[str, tuple[float, float] | None], SlideMetadata],
    ) -> Patch:
        if not isinstance(request, PatchRequest):
            raise ValueError(
                "request must be PatchRequest; "
                "use PhysicalPatchStream for physical requests"
            )
        key = request.slide, request.source_mpp
        metadata = metadata_cache.get(key)
        if metadata is None:
            metadata = reader.metadata(request.slide, source_mpp=request.source_mpp)
            metadata_cache[key] = metadata
        return Patch(
            request,
            read_aligned_patch(
                reader,
                request,
                metadata=metadata,
                interpolation=self.interpolation,
                color_mode=self.color_mode,
            ),
        )

    def __iter__(self) -> Iterator[Patch]:
        return ReadIterator(self)


class PhysicalPatchStream(PatchStream):
    """Ordered full physical results; per-request options reach the sync API.

    Factory-owned readers are created/read/closed on their owning worker.
    Request iterators are borrowed unless own_requests=True. Use
    contextlib.closing(iter(stream)) for early exit. Ordered waiting can delay
    later failures; close waits for started native IO and cancels queued work.
    """

    def __init__(
        self,
        reader: SlideReader | None,
        requests: Iterable[PhysicalPatchRequest],
        *,
        reader_factory: Callable[[], SlideReader] | None = None,
        num_workers: int = 1,
        max_pending: int | None = None,
        limits: SamplingLimits = DEFAULT_LIMITS,
        own_requests: bool = False,
    ) -> None:
        if not isinstance(limits, SamplingLimits):
            raise ValueError("limits must be SamplingLimits")
        super().__init__(
            reader,
            requests,
            reader_factory=reader_factory,
            num_workers=num_workers,
            max_pending=max_pending,
            own_requests=own_requests,
        )
        self.limits = limits

    def _read(self, reader, request, metadata_cache) -> PhysicalPatchResult:
        if not isinstance(request, PhysicalPatchRequest):
            raise ValueError("request must be a PhysicalPatchRequest")
        key = request.slide, request.source_mpp
        metadata = metadata_cache.get(key)
        if metadata is None:
            metadata = reader.metadata(request.slide, source_mpp=request.source_mpp)
            metadata_cache[key] = metadata
        return read_physical_patch_result(
            reader, request, metadata=metadata, limits=self.limits
        )

    def __iter__(self) -> Iterator[PhysicalPatchResult]:
        return ReadIterator(self)
