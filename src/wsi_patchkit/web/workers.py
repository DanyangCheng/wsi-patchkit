"""A small async-compatible worker queue for blocking WSI rendering."""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class _WorkItem:
    future: Future[Any]
    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    loop: asyncio.AbstractEventLoop


class TileWorkerPool:
    """Run blocking tile work without blocking the ASGI event loop."""

    def __init__(self, size: int = 4, *, max_pending: int | None = None) -> None:
        if size < 1:
            raise ValueError("tile worker count must be positive")
        limit = size * 4 if max_pending is None else max_pending
        if limit < 1:
            raise ValueError("max_pending must be positive")
        self._slots = asyncio.Semaphore(limit)
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue()
        self._closed = False
        self._state_lock = threading.Lock()
        self._threads = tuple(
            threading.Thread(
                target=self._worker,
                name=f"wsi-tile-{index}",
                daemon=True,
            )
            for index in range(size)
        )
        for thread in self._threads:
            thread.start()

    @property
    def size(self) -> int:
        return len(self._threads)

    async def run(
        self,
        function: Callable[..., T],
        /,
        *args: object,
        **kwargs: object,
    ) -> T:
        # Backpressure before creating/queuing a work item. Disconnected clients
        # can cancel this wait without leaving any obsolete work in the queue.
        await self._slots.acquire()
        future: Future[T] = Future()
        with self._state_lock:
            if self._closed:
                self._slots.release()
                raise RuntimeError("tile worker pool is closed")
            self._queue.put(
                _WorkItem(
                    future,
                    function,
                    args,
                    kwargs,
                    asyncio.get_running_loop(),
                )
            )
        try:
            # Notify the event loop when work finishes instead of scheduling a
            # timer for every pending request every millisecond.
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            # If work is still queued, the worker will skip it via
            # set_running_or_notify_cancel(). A synchronous read that has
            # already started cannot be interrupted safely.
            future.cancel()
            raise

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                if not item.future.set_running_or_notify_cancel():
                    continue
                try:
                    result = item.function(*item.args, **item.kwargs)
                except BaseException as caught:
                    item.future.set_exception(caught)
                else:
                    item.future.set_result(result)
            finally:
                # A cancelled running read still occupies capacity until it
                # actually finishes; cancelled queue items release on dequeue.
                # Otherwise rapid cancellations could grow an unbounded queue.
                # The owning event loop may have shut down during close.
                with suppress(RuntimeError):
                    item.loop.call_soon_threadsafe(self._slots.release)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            for _ in self._threads:
                self._queue.put(None)
        for thread in self._threads:
            thread.join()
