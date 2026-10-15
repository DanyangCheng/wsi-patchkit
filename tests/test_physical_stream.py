"""A09–A12/A15: bounded ordered full results and deterministic cleanup."""

from contextlib import closing
from threading import Barrier, Event, Thread, get_ident

import numpy as np
import pytest
from test_physical_reading import Reader, request

from wsi_patchkit import (
    PatchRequest,
    PatchStream,
    PhysicalPatchStream,
    SamplingLimits,
    read_physical_patch_result,
)


class OwnedReader(Reader):
    def __init__(self, *, stage=None, cleanup=None, barrier=None, first_wait=None):
        super().__init__()
        self.owner = get_ident()
        self.stage = stage
        self.cleanup = cleanup
        self.barrier = barrier
        self.first_wait = first_wait
        self.close_count = 0

    def metadata(self, *args, **kwargs):
        assert get_ident() == self.owner
        if self.stage == "metadata":
            raise OSError("metadata failed")
        return super().metadata(*args, **kwargs)

    def read_region(self, path, location, level, size):
        assert get_ident() == self.owner
        if self.barrier:
            self.barrier.wait(timeout=5)
        if self.first_wait:
            if location[0] == 0:
                assert self.first_wait.wait(timeout=5)
            else:
                self.first_wait.set()
        if self.stage == "read":
            raise OSError("read failed")
        return super().read_region(path, location, level, size)

    def close(self):
        assert get_ident() == self.owner
        self.close_count += 1
        super().close()
        if self.cleanup:
            raise self.cleanup


class Source:
    def __init__(self, count=8, *, fail_at=None, cleanup=None):
        self.count, self.fail_at, self.cleanup = count, fail_at, cleanup
        self.consumed = 0
        self.close_count = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.fail_at is not None and self.consumed == self.fail_at:
            raise OSError("generation failed")
        if self.consumed >= self.count:
            raise StopIteration
        value = request(origin_um=(self.consumed % 7, 0), output_size=(1, 1))
        self.consumed += 1
        return value

    def close(self):
        self.close_count += 1
        if self.cleanup:
            raise self.cleanup


def factory(readers, **kwargs):
    def make():
        reader = OwnedReader(**kwargs)
        readers.append(reader)
        return reader

    return make


@pytest.mark.parametrize("mode", ["nearest", "bilinear", "bicubic", "area"])
@pytest.mark.parametrize("padding", ["constant", "reflect"])
def test_complete_results_match_direct_and_serial_parallel(mode, padding):
    samples = [
        request(
            origin_um=(i - 1.25, -0.25),
            target_mpp=1.5,
            interpolation=mode,
            padding_mode=padding,
        )
        for i in range(5)
    ]
    expected = [read_physical_patch_result(Reader(), sample) for sample in samples]
    for workers in (1, 3):
        readers = []
        actual = list(
            PhysicalPatchStream(
                None,
                samples,
                reader_factory=factory(readers),
                num_workers=workers,
                max_pending=2,
            )
        )
        assert [r.request for r in actual] == samples
        for a, b in zip(actual, expected, strict=True):
            np.testing.assert_array_equal(a.image, b.image)
            np.testing.assert_array_equal(a.valid_mask, b.valid_mask)
            assert a.grid == b.grid and a.source_level == b.source_level
        assert all(r.closed and r.close_count == 1 for r in readers)
        assert all(
            result.image.flags.owndata and result.valid_mask.flags.owndata
            for result in actual
        )


def test_out_of_order_completion_still_yields_input_order():
    readers = []
    samples = [
        request(origin_um=(i, 0), output_size=(1, 1), interpolation="nearest")
        for i in (0, 1)
    ]
    results = list(
        PhysicalPatchStream(
            None,
            samples,
            num_workers=2,
            reader_factory=factory(readers, barrier=Barrier(2), first_wait=Event()),
        )
    )
    assert [int(r.image[0, 0, 0]) for r in results] == [0, 10]
    assert len({r.owner for r in readers}) == 2


@pytest.mark.parametrize("workers,pending", [(1, 1), (4, 1), (4, 2), (3, 5)])
def test_suspended_yield_counts_in_pending_limit_and_early_close(workers, pending):
    readers, source = [], Source(count=30)
    with closing(
        iter(
            PhysicalPatchStream(
                None,
                source,
                reader_factory=factory(readers),
                num_workers=workers,
                max_pending=pending,
                own_requests=True,
            )
        )
    ) as results:
        for index in range(3):
            next(results)
            assert source.consumed == (index + 1 if workers == 1 else index + pending)
    assert source.close_count == 1
    assert all(r.close_count == 1 for r in readers)


@pytest.mark.parametrize("workers", [1, 4])
@pytest.mark.parametrize("own", [False, True])
def test_empty_stream_is_lazy_and_closes_only_owned_iterator(workers, own):
    readers, source = [], Source(count=0)
    assert (
        list(
            PhysicalPatchStream(
                None,
                source,
                num_workers=workers,
                reader_factory=factory(readers),
                own_requests=own,
            )
        )
        == []
    )
    assert not readers and source.close_count == int(own)


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("stage", ["metadata", "read", "generation", "factory"])
def test_primary_errors_survive_reader_and_iterator_cleanup_failures(workers, stage):
    cleanup = RuntimeError("reader close failed")
    source_cleanup = RuntimeError("iterator close failed")
    source = Source(
        fail_at=1 if stage == "generation" else None, cleanup=source_cleanup
    )
    readers = []
    make = factory(readers, stage=stage, cleanup=cleanup)
    if stage == "factory":

        def make():
            raise OSError("factory failed")

    with pytest.raises(OSError, match=stage) as error:
        list(
            PhysicalPatchStream(
                None,
                source,
                reader_factory=make,
                num_workers=workers,
                max_pending=1,
                own_requests=True,
            )
        )
    assert source.close_count == 1
    assert source_cleanup in error.value.cleanup_errors
    assert all(r.close_count == 1 for r in readers)
    if readers:
        assert cleanup in error.value.cleanup_errors


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("early", [False, True])
def test_success_or_early_close_propagates_first_cleanup_error(workers, early):
    readers = []
    cleanup = RuntimeError("close failed")
    iterator = iter(
        PhysicalPatchStream(
            None,
            Source(count=1),
            num_workers=workers,
            reader_factory=factory(readers, cleanup=cleanup),
        )
    )
    if early:
        next(iterator)
        with pytest.raises(RuntimeError) as error:
            iterator.close()
    else:
        with pytest.raises(RuntimeError) as error:
            list(iterator)
    assert error.value is cleanup
    assert all(r.close_count == 1 for r in readers)


def test_owned_actual_iterator_is_closed_not_iterable_wrapper():
    source = Source(count=1)

    class Wrapper:
        def __iter__(self):
            return source

        def close(self):
            raise AssertionError("closed iterable instead of iterator")

    reader = OwnedReader()
    list(PhysicalPatchStream(reader, Wrapper(), own_requests=True))
    assert source.close_count == 1 and reader.close_count == 0


def test_borrowed_reader_and_iterator_survive_read_failure():
    reader, source = OwnedReader(stage="read"), Source()
    with pytest.raises(OSError):
        list(PhysicalPatchStream(reader, source))
    assert reader.close_count == 0 and source.close_count == 0


def test_cache_keys_include_slide_and_source_mpp():
    reader = Reader()
    samples = [request(source_mpp=mpp) for mpp in (1, 1, 0.5, 0.5)]
    samples.append(
        type(samples[0])("other", origin_um=(0, 0), output_size=(1, 1), target_mpp=1)
    )
    list(PhysicalPatchStream(reader, samples))
    assert len(reader.metadata_calls) == 3


def test_limits_reach_sync_and_none_request_is_not_end_sentinel():
    with pytest.raises(ValueError, match="max_output_pixels"):
        list(
            PhysicalPatchStream(
                Reader(), [request()], limits=SamplingLimits(max_output_pixels=1)
            )
        )
    with pytest.raises(ValueError, match="request"):
        list(PhysicalPatchStream(Reader(), [None]))


def test_close_waits_for_started_io_and_cancels_queued_requests():
    started, release, close_done = Event(), Event(), Event()
    readers, failures = [], []

    class BlockReader(OwnedReader):
        def read_region(self, *args):
            if len(self.calls) == 1:
                started.set()
                assert release.wait(timeout=5)
            return super().read_region(*args)

    def make():
        r = BlockReader()
        readers.append(r)
        return r

    iterator = iter(
        PhysicalPatchStream(
            None, Source(20), num_workers=2, max_pending=6, reader_factory=make
        )
    )
    next(iterator)

    # Worker scheduling can assign two readers; block every second local read.
    def close():
        try:
            iterator.close()
        except BaseException as error:
            failures.append(error)
        close_done.set()

    assert started.wait(timeout=5)
    thread = Thread(target=close)
    thread.start()
    assert not close_done.wait(timeout=0.02)
    release.set()
    thread.join(timeout=5)
    assert close_done.is_set() and not failures
    assert sum(len(r.calls) for r in readers) <= 4
    assert all(r.closed for r in readers)


def test_legacy_stream_shares_cleanup_and_same_thread_close():
    readers = []
    samples = [PatchRequest("fixture", i, 0, 1, 1, 1) for i in range(4)]
    results = list(
        PatchStream(None, samples, num_workers=2, reader_factory=factory(readers))
    )
    assert [int(r.image[0, 0, 0]) for r in results] == [0, 10, 20, 30]
    assert all(r.close_count == 1 for r in readers)


@pytest.mark.parametrize(
    "options",
    [
        {"num_workers": True},
        {"max_pending": 1.5},
        {"own_requests": 1},
        {"limits": None},
        {"num_workers": 2},
    ],
)
def test_invalid_stream_options(options):
    with pytest.raises(ValueError):
        PhysicalPatchStream(Reader(), [], **options)


@pytest.mark.parametrize("own", [False, True])
def test_close_before_first_next_is_lazy_but_honors_iterator_ownership(own):
    readers, source = [], Source()
    iterator = iter(
        PhysicalPatchStream(
            None, source, reader_factory=factory(readers), own_requests=own
        )
    )
    iterator.close()
    iterator.close()
    assert not readers and source.consumed == 0
    assert source.close_count == int(own)
    with pytest.raises(StopIteration):
        next(iterator)


def test_before_start_cleanup_failure_is_reported_once():
    failure = RuntimeError("unstarted source close failed")
    source = Source(cleanup=failure)
    iterator = iter(PhysicalPatchStream(Reader(), source, own_requests=True))
    with pytest.raises(RuntimeError) as captured:
        iterator.close()
    assert captured.value is failure
    iterator.close()
    assert source.close_count == 1


def test_cleanup_error_order_is_worker_number_then_owned_source():
    from threading import current_thread

    readers = []
    source_error = RuntimeError("source cleanup")
    barrier = Barrier(2)

    def make():
        reader = OwnedReader(
            barrier=barrier, cleanup=RuntimeError(current_thread().name)
        )
        readers.append(reader)
        return reader

    with pytest.raises(RuntimeError, match="patchkit-0") as captured:
        list(
            PhysicalPatchStream(
                None,
                Source(count=2, cleanup=source_error),
                num_workers=2,
                max_pending=2,
                reader_factory=make,
                own_requests=True,
            )
        )
    assert [str(e) for e in captured.value.cleanup_errors] == [
        "patchkit-1",
        "source cleanup",
    ]
    assert all(r.close_count == 1 for r in readers)


def test_factory_cleans_partial_construction_before_raising():
    readers = []
    failure = OSError("partial factory failed")

    def make():
        reader = OwnedReader()
        readers.append(reader)
        try:
            raise failure
        finally:
            reader.close()

    with pytest.raises(OSError) as captured:
        list(PhysicalPatchStream(None, Source(), num_workers=2, reader_factory=make))
    assert captured.value is failure
    assert readers and all(r.close_count == 1 for r in readers)


def test_request_iter_creation_failure_never_constructs_a_reader():
    readers = []

    class BadIterable:
        def __iter__(self):
            raise OSError("iter creation failed")

    with pytest.raises(OSError, match="iter creation"):
        iter(
            PhysicalPatchStream(
                None, BadIterable(), reader_factory=factory(readers), own_requests=True
            )
        )
    assert not readers
