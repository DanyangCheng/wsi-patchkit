from __future__ import annotations

import os
import pickle
import struct
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from wsi_patchkit.io._color import image_to_rgb
from wsi_patchkit.io._kfb_index import SQLiteTileIndex


def _index_file(path: Path, count: int = 3, *, bad_size: bool = False):
    images = []
    for color in ((220, 30, 10), (10, 210, 40), (20, 30, 200)):
        output = BytesIO()
        Image.new("RGB", (256, 256), color).save(output, "JPEG")
        images.append(output.getvalue())
    prefix = bytearray(92)
    prefix[1:4] = b"\x01\xee\xee"
    data = bytes(prefix) + b"".join(images)
    index = bytearray(count * 64)
    for i in range(count):
        struct.pack_into("<IIII", index, i * 64 + 4, i * 256, 0, 256, 256)
        struct.pack_into("<f", index, i * 64 + 20, 40)
        size = len(images[i % 3])
        if bad_size:
            size += -10 if i == 0 else 10 if i == 1 else 0
        struct.pack_into("<I", index, i * 64 + 32, size)
    data = bytearray(data)
    struct.pack_into("<QQ", data, 60, len(data) + len(index), len(data))
    path.write_bytes(data + index)
    header = SimpleNamespace(
        tile_size=256,
        tile_count=count,
        tile_index_start=len(data),
        tile_index_end=len(data) + len(index),
        scan_scale=40,
        width=count * 256,
        height=256,
        mpp=0.25,
        version=1,
        spend_time=0,
        scan_time=0,
    )
    return SimpleNamespace(header=header, tile_data_offset=92, assoc_images=[])


def test_rgb_conversion_matches_integer_reference_for_all_alpha_values():
    rng = np.random.default_rng(17)
    rgba = rng.integers(0, 256, (32, 256, 4), dtype=np.uint8)
    rgba[..., 3] = np.arange(256, dtype=np.uint8)
    alpha = rgba[..., 3:4].astype(np.uint32)
    expected = (
        (rgba[..., :3].astype(np.uint32) * alpha + 255 * (255 - alpha) + 127) // 255
    ).astype(np.uint8)
    np.testing.assert_array_equal(image_to_rgb(Image.fromarray(rgba)), expected)


def test_index_reuse_batched_lookup_and_source_invalidation(tmp_path):
    path = tmp_path / "slide.kfb"
    info = _index_file(path)
    cache = tmp_path / "indices"
    first = SQLiteTileIndex(str(path), info, cache)
    assert first.built
    first.prefetch(40, (0, 0, 768, 256))
    assert first.lookup.get((40, 256, 0)) == 1
    assert first.lookup.get((40, 256, 256)) is None
    assert first.entries[1]["width"] == 256
    assert first.offsets[1] == 92 + first.entries[0]["size"]
    database = first.database_path
    first.close()
    second = SQLiteTileIndex(str(path), info, cache)
    assert not second.built
    assert not second._records  # reopening does not load the whole index
    second.close()
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    changed = SQLiteTileIndex(str(path), info, cache)
    assert changed.built
    assert changed.database_path != database
    changed.close()


def test_concurrent_builders_publish_one_complete_index(tmp_path):
    path = tmp_path / "slide.kfb"
    info = _index_file(path, count=1000)

    def open_index(_):
        index = SQLiteTileIndex(str(path), info, tmp_path / "cache")
        try:
            assert index.lookup.get((40, 999 * 256, 0)) == 999
            return index.built
        finally:
            index.close()

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert sum(workers.map(open_index, range(4))) == 1


def test_index_record_and_region_caches_are_bounded(tmp_path):
    path = tmp_path / "slide.kfb"
    info = _index_file(path, count=1024)
    index = SQLiteTileIndex(str(path), info, tmp_path / "cache")
    try:
        index.prefetch(40, (0, 0, 1024 * 256, 256))
        assert not index._region_lookup
        for i in range(1024):
            assert index.lookup.get((40, i * 256, 0)) == i
        assert len(index._records) == 512
    finally:
        index.close()


def test_repairs_invalidate_other_connections_and_persist_on_error(tmp_path):
    path = tmp_path / "slide.kfb"
    info = _index_file(path)
    a = SQLiteTileIndex(str(path), info, tmp_path / "cache")
    b = SQLiteTileIndex(str(path), info, tmp_path / "cache")
    try:
        b.prefetch(40, (0, 0, 768, 256))
        old = b.offsets[1]
        third = b.offsets[2]
        with (
            pytest.raises(ValueError, match="broken JPEG"),
            a.repair_window(0) as window,
        ):
            window.entries[0]["size"] += 10
            window.entries[1]["size"] -= 10
            window.offsets[1] += 10
            raise ValueError("broken JPEG")
        b.prefetch(40, (0, 0, 768, 256))
        assert b.offsets[1] == old + 10
        assert b.offsets[2] == third
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("bad_size", [False, True])
def test_sqlite_backend_matches_native_pixels_and_repairs(
    tmp_path,
    monkeypatch,
    bad_size,
):
    pytest.importorskip("kfbslide")
    import kfbslide._slide as backend

    from wsi_patchkit.io.kfbslide import KfbSlideReader

    path = tmp_path / "slide.kfb"
    info = _index_file(path, bad_size=bad_size)
    monkeypatch.setattr(backend, "parse_kfb_file", lambda _: info)
    native = KfbSlideReader(cache_size=1)
    cached = KfbSlideReader(cache_size=1, index_cache_dir=tmp_path / "cache")
    try:
        assert cached.metadata(path).dimensions == native.metadata(path).dimensions
        for location, size in [
            ((0, 0), (768, 256)),
            ((-30, -20), (100, 90)),
            ((700, 200), (120, 90)),
        ]:
            np.testing.assert_array_equal(
                cached.read_region(path, location, 0, size),
                native.read_region(path, location, 0, size),
            )
        db_index = next(iter(cached._slides.values()))._index
        native_index = next(iter(native._slides.values()))._index
        for i in range(3):
            assert db_index.offsets[i] == native_index.offsets[i]
            assert db_index.entries[i]["size"] == native_index.entries[i]["size"]
        restored = pickle.loads(pickle.dumps(cached))
        assert restored.index_cache_dir == cached.index_cache_dir
        assert not restored._slides
        restored.close()
    finally:
        native.close()
        cached.close()


@pytest.mark.parametrize("persistent", [False, True])
def test_reader_evicts_old_slides_and_reopens_replaced_source(
    tmp_path, monkeypatch, persistent
):
    pytest.importorskip("kfbslide")
    import kfbslide._slide as backend

    from wsi_patchkit.io.kfbslide import KfbSlideReader

    paths = [tmp_path / f"{i}.kfb" for i in range(2)]
    infos = {str(path): _index_file(path) for path in paths}
    monkeypatch.setattr(backend, "parse_kfb_file", lambda path: infos[path])
    reader = KfbSlideReader(
        cache_size=1,
        index_cache_dir=tmp_path / "cache" if persistent else None,
    )
    try:
        reader.metadata(paths[0])
        previous = next(iter(reader._slides.values()))
        handle = previous._ensure_open_handle()
        reader.metadata(paths[1])
        assert previous._closed
        assert handle.closed
        if persistent:
            assert previous._index._closed
        assert len(reader._slides) == 1
        original = next(iter(reader._slides.values()))
        stat = paths[1].stat()
        os.utime(paths[1], ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        reader.metadata(paths[1])
        assert original._closed
        assert next(iter(reader._slides.values())) is not original
    finally:
        reader.close()


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("tile_cache_size", [0, 2])
def test_reader_respects_tile_cache_budget(
    tmp_path, monkeypatch, persistent, tile_cache_size
):
    pytest.importorskip("kfbslide")
    import kfbslide._slide as backend

    from wsi_patchkit.io.kfbslide import KfbSlideReader

    path = tmp_path / "slide.kfb"
    info = _index_file(path)
    monkeypatch.setattr(backend, "parse_kfb_file", lambda _: info)
    with KfbSlideReader(
        index_cache_dir=tmp_path / "cache" if persistent else None,
        tile_cache_size=tile_cache_size,
    ) as reader:
        reader.read_region(path, (0, 0), 0, (768, 256))
        cache = next(iter(reader._slides.values()))._tile_cache
        assert len(cache._cache) == tile_cache_size


def test_persistent_reader_preserves_initialization_error(tmp_path, monkeypatch):
    pytest.importorskip("kfbslide")
    import kfbslide._slide as backend

    from wsi_patchkit.io.kfbslide import KfbSlideReader

    path = tmp_path / "slide.kfb"
    path.touch()

    def fail_parse(_):
        raise ValueError("invalid KFB header")

    monkeypatch.setattr(backend, "parse_kfb_file", fail_parse)
    with (
        KfbSlideReader(index_cache_dir=tmp_path / "cache") as reader,
        pytest.raises(ValueError, match="invalid KFB header"),
    ):
        reader.metadata(path)
