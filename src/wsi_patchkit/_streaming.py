"""Ordered bounded reading with worker-local resource lifetimes."""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from queue import Queue
from threading import Event, Thread


class ReaderState:
    def __init__(self, reader, factory):
        self.reader = reader
        self.factory = factory
        self.metadata_cache = {}

    def get(self):
        if self.reader is None:
            # A factory owns partial construction until it returns a reader.
            self.reader = self.factory()
        return self.reader

    def close(self):
        if self.factory is not None and self.reader is not None:
            self.reader.close()


def report_cleanup(primary, errors):
    if not errors:
        return
    if primary is None:
        primary, errors = errors[0], errors[1:]
        attach_cleanup(primary, errors)
        raise primary
    attach_cleanup(primary, errors)


def attach_cleanup(error, errors):
    error.cleanup_errors = tuple(getattr(error, "cleanup_errors", ())) + tuple(errors)
    if hasattr(error, "add_note"):
        for cleanup in errors:
            error.add_note(
                f"stream cleanup failed: {type(cleanup).__name__}: {cleanup}"
            )


class ReadIterator:
    """Explicit close releases an owned iterator even before its first next()."""

    def __init__(self, stream):
        self.closed = True
        self.source = iter(stream.requests)
        self.own_requests = stream.own_requests
        self.inner = iterate_reads(stream, self.source)
        self.started = False
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        self.started = True
        try:
            return next(self.inner)
        except BaseException:
            self.closed = True
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.inner.close()
        if not self.started and self.own_requests:
            close = getattr(self.source, "close", None)
            if close is not None:
                close()

    def __del__(self):
        self.close()


def iterate_reads(stream, source):
    """One pending capacity includes the result currently suspended at yield."""
    primary = None
    serial = ReaderState(stream.reader, stream.reader_factory)
    pending = deque()
    tasks = Queue()
    stopping = Event()
    threads = []
    # There cannot be more active readers than workers or pending requests.
    worker_errors = [[] for _ in range(min(stream.num_workers, stream.max_pending))]

    def worker(index):
        state = ReaderState(None, stream.reader_factory)
        try:
            while True:
                task = tasks.get()
                if task is None:
                    break
                future, request = task
                if stopping.is_set():
                    future.cancel()
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(
                        stream._read(state.get(), request, state.metadata_cache)
                    )
                except BaseException as error:
                    future.set_exception(error)
        finally:
            try:
                state.close()
            except BaseException as error:
                worker_errors[index].append(error)

    def submit(request):
        if not threads:
            for index in range(len(worker_errors)):
                thread = Thread(target=worker, args=(index,), name=f"patchkit-{index}")
                thread.start()
                threads.append(thread)
        future = Future()
        pending.append(future)
        tasks.put((future, request))

    try:
        if stream.num_workers == 1:
            for request in source:
                yield stream._read(serial.get(), request, serial.metadata_cache)
        else:
            exhausted = False
            for _ in range(stream.max_pending):
                try:
                    request = next(source)
                except StopIteration:
                    exhausted = True
                    break
                submit(request)
            while pending:
                # No replacement is submitted until the consumer resumes.
                yield pending.popleft().result()
                if not exhausted:
                    try:
                        request = next(source)
                    except StopIteration:
                        exhausted = True
                    else:
                        submit(request)
    except GeneratorExit:
        # Explicit close is a cleanup path, so cleanup failures propagate.
        raise
    except BaseException as error:
        primary = error
        raise
    finally:
        stopping.set()
        for future in pending:
            future.cancel()
        for _ in threads:
            tasks.put(None)
        for thread in threads:
            thread.join()  # Native IO already started is allowed to complete.
        errors = [error for group in worker_errors for error in group]
        if stream.num_workers == 1:
            try:
                serial.close()
            except BaseException as error:
                errors.append(error)
        if stream.own_requests and source is not None:
            try:
                close = getattr(source, "close", None)
                if close is not None:
                    close()
            except BaseException as error:
                errors.append(error)
        report_cleanup(primary, errors)
