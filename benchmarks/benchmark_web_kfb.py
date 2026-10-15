"""Compare legacy KFB opening/rendering with persistent bounded-memory indices."""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from wsi_patchkit.io.kfbslide import KfbSlideReader
from wsi_patchkit.tiles import TileRenderer


def working_set_mb() -> float | None:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]

    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.windll.kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    ):
        raise ctypes.WinError()
    return round(counters.WorkingSetSize / 1024**2, 2)


class LegacyReader(KfbSlideReader):
    def read_region(self, path, location, level, size):
        image = self._slide(self._path(path)).read_region(location, level, size)
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        alpha = rgba[..., 3:4].astype(np.uint32)
        return (
            (rgba[..., :3].astype(np.uint32) * alpha + 255 * (255 - alpha) + 127) // 255
        ).astype(np.uint8)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slide", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/kfb-index"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    Image.init()
    results = []
    expected = None
    for name in ("legacy", "sqlite_first", "sqlite_reopen"):
        gc.collect()
        before = working_set_mb()
        reader = (
            LegacyReader(cache_size=1)
            if name == "legacy"
            else KfbSlideReader(
                cache_size=1,
                index_cache_dir=args.cache_dir,
                tile_cache_size=128,
            )
        )
        renderer = TileRenderer(reader, cache_size=0)
        try:
            start = time.perf_counter()
            metadata = renderer.metadata(args.slide)
            open_seconds = time.perf_counter() - start
            opened_mb = working_set_mb()
            index = next(iter(reader._slides.values()))._index
            width, height = metadata.dimensions
            size = (380, max(1, round(height * 380 / width)))
            renders = []
            for _ in range(2):
                start = time.perf_counter()
                image = renderer.render_region(args.slide, (0, 0, width, height), size)
                renders.append(round(time.perf_counter() - start, 4))
                if expected is None:
                    expected = image.content
                if image.content != expected:
                    raise AssertionError("rendered overview differs from legacy JPEG")
            record = {
                "mode": name,
                "open_seconds": round(open_seconds, 4),
                "memory_before_mb": before,
                "memory_after_open_mb": opened_mb,
                "memory_after_render_mb": working_set_mb(),
                "overview_seconds": renders,
                "jpeg_identical": True,
                "index_records": len(index.entries),
                "index_built": getattr(index, "built", None),
            }
            if hasattr(index, "database_path"):
                record["database"] = str(index.database_path)
            print(json.dumps(record), flush=True)
            results.append(record)
        finally:
            renderer.close()
            index = None  # release the legacy index before the next measurement
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
