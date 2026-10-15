from __future__ import annotations

from contextlib import closing
from pathlib import Path
from threading import Barrier, Event, get_ident

import numpy as np
import pytest
import tifffile

from wsi_patchkit import LevelInfo, PatchRequest, PatchStream, SlideMetadata, TiffReader


class RecordingReader:
    def __init__(self, *, barrier=None, second_done=None, fail=False):
        self.owner = get_ident()
        self.barrier = barrier
        self.second_done = second_done
        self.fail = fail
        self.closed = False
        self.metadata_calls = 0

    def metadata(self, path, *, source_mpp=None):
        assert get_ident() == self.owner
        self.metadata_calls += 1
        return SlideMetadata(
            path, (LevelInfo(0, (100, 100), (1, 1), (1, 1)),), mpp=(1, 1)
        )

    def read_region(self, path, location, level, size):
        assert get_ident() == self.owner
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        if self.second_done is not None:
            if location[0] == 0:
                assert self.second_done.wait(timeout=5)
            else:
                self.second_done.set()
        if self.fail:
            raise OSError("read failed")
        return np.full((size[1], size[0], 3), location[0], dtype=np.uint8)

    def close(self):
        self.closed = True


def requests(count=6):
    return [PatchRequest("slide.tif", x, 0, 1, 1, 1) for x in range(count)]


def test_parallel_readers_overlap_and_preserve_order():
    readers = []
    barrier = Barrier(2)
    second_done = Event()

    def factory():
        reader = RecordingReader(barrier=barrier, second_done=second_done)
        readers.append(reader)
        return reader

    patches = list(
        PatchStream(None, requests(2), reader_factory=factory, num_workers=2)
    )

    assert [int(p.image[0, 0, 0]) for p in patches] == [0, 1]
    assert len(readers) == 2
    assert len({reader.owner for reader in readers}) == 2
    assert all(reader.closed for reader in readers)


def test_bounded_prefetch_and_early_close():
    consumed = []
    readers = []

    def source():
        for request in requests(20):
            consumed.append(request)
            yield request

    def factory():
        reader = RecordingReader()
        readers.append(reader)
        return reader

    with closing(
        iter(
            PatchStream(
                None, source(), reader_factory=factory, num_workers=2, max_pending=3
            )
        )
    ) as patches:
        assert next(patches).request == consumed[0]
        assert len(consumed) == 3

    assert len(consumed) == 3
    assert readers and all(reader.closed for reader in readers)


@pytest.mark.parametrize("num_workers", [1, 2])
def test_read_failure_closes_owned_readers(num_workers):
    readers = []

    def factory():
        reader = RecordingReader(fail=True)
        readers.append(reader)
        return reader

    with pytest.raises(OSError, match="read failed"):
        list(
            PatchStream(
                None, requests(), reader_factory=factory, num_workers=num_workers
            )
        )

    assert readers and all(reader.closed for reader in readers)


def test_serial_reader_ownership_and_metadata_cache():
    reader = RecordingReader()
    assert len(list(PatchStream(reader, requests()))) == 6
    assert reader.metadata_calls == 1
    assert not reader.closed


def test_parallel_metadata_cache_is_per_reader():
    readers = []
    barrier = Barrier(2)

    def factory():
        reader = RecordingReader(barrier=barrier)
        readers.append(reader)
        return reader

    assert (
        len(
            list(
                PatchStream(
                    None,
                    requests(),
                    reader_factory=factory,
                    num_workers=2,
                    max_pending=2,
                )
            )
        )
        == 6
    )
    assert len(readers) == 2
    assert all(reader.metadata_calls == 1 for reader in readers)


def test_parallel_tiff_matches_serial(tmp_path: Path):
    path = tmp_path / "slide.tif"
    image = np.arange(64 * 64 * 3, dtype=np.uint8).reshape(64, 64, 3)
    tifffile.imwrite(path, image, photometric="rgb", tile=(16, 16))
    samples = [
        PatchRequest(path, x, y, 9, 7, 1, source_mpp=1)
        for y in range(0, 64, 8)
        for x in range(0, 64, 8)
    ]
    with TiffReader() as reader:
        expected = list(PatchStream(reader, samples))
    actual = list(
        PatchStream(
            None, samples, reader_factory=TiffReader, num_workers=4, max_pending=7
        )
    )
    assert [p.request for p in actual] == samples
    for serial, parallel in zip(expected, actual, strict=True):
        np.testing.assert_array_equal(serial.image, parallel.image)


@pytest.mark.parametrize(
    "options",
    [
        {"num_workers": 0},
        {"num_workers": True},
        {"num_workers": 1.5},
        {"max_pending": 0},
        {"max_pending": True},
        {"max_pending": 1.5},
        {"num_workers": 2},
        {"reader_factory": RecordingReader},
    ],
)
def test_invalid_configuration(options):
    with pytest.raises(ValueError):
        PatchStream(RecordingReader(), requests(), **options)


def test_missing_reader():
    with pytest.raises(ValueError, match="required"):
        PatchStream(None, requests())
