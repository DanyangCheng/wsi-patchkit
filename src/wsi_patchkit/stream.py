"""Lazy conversion of patch requests into image arrays."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from threading import local
from typing import Literal

from .geometry import Interpolation, read_aligned_patch
from .io.base import SlideReader
from .types import Patch, PatchRequest, SlideMetadata


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
    ) -> None:
        if interpolation not in ("nearest", "bilinear", "area"):
            raise ValueError("interpolation must be 'nearest', 'bilinear', or 'area'")
        if isinstance(num_workers, bool) or not isinstance(num_workers, int):
            raise ValueError("num_workers must be a positive integer")
        if num_workers < 1:
            raise ValueError("num_workers must be a positive integer")
        if max_pending is None:
            max_pending = 2 * num_workers
        if (
            isinstance(max_pending, bool)
            or not isinstance(max_pending, int)
            or max_pending < 1
        ):
            raise ValueError("max_pending must be a positive integer")
        if reader is not None and reader_factory is not None:
            raise ValueError("provide either reader or reader_factory")
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

    def _read(
        self,
        reader: SlideReader,
        request: PatchRequest,
        metadata_cache: dict[tuple[str, tuple[float, float] | None], SlideMetadata],
    ) -> Patch:
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
        if self.num_workers == 1:
            with ExitStack() as resources:
                reader = self.reader
                if self.reader_factory is not None:
                    reader = self.reader_factory()
                    resources.callback(reader.close)
                assert reader is not None
                metadata_cache: dict[
                    tuple[str, tuple[float, float] | None], SlideMetadata
                ] = {}
                for request in self.requests:
                    yield self._read(reader, request, metadata_cache)
            return

        state = local()
        readers: list[SlideReader] = []

        def read(request: PatchRequest) -> Patch:
            if not hasattr(state, "reader"):
                assert self.reader_factory is not None
                state.reader = self.reader_factory()
                readers.append(state.reader)
                state.metadata_cache = {}
            return self._read(state.reader, request, state.metadata_cache)

        pending: deque[Future[Patch]] = deque()
        executor = ThreadPoolExecutor(max_workers=self.num_workers)
        try:
            requests = iter(self.requests)
            for _ in range(self.max_pending):
                request = next(requests, None)
                if request is None:
                    break
                pending.append(executor.submit(read, request))
            while pending:
                yield pending.popleft().result()
                request = next(requests, None)
                if request is not None:
                    pending.append(executor.submit(read, request))
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            with ExitStack() as resources:
                for reader in readers:
                    resources.callback(reader.close)
