"""Diagnose KFB opening and IIIF region rendering without the web server."""

from __future__ import annotations

import argparse
import faulthandler
import logging
import math
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def log(message: str) -> None:
    print(message, flush=True)


def integers(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(item) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slide", type=Path)
    parser.add_argument("--region", type=integers, required=True, metavar="X,Y,W,H")
    parser.add_argument("--size", type=integers, required=True, metavar="W,H")
    parser.add_argument("--stack-interval", type=float, default=15)
    parser.add_argument(
        "--worker-thread",
        action="store_true",
        help="open and render in a background thread as the web server does",
    )
    parser.add_argument(
        "--trace-tiles",
        action="store_true",
        help="log KFB tile decoding and index repairs",
    )
    parser.add_argument(
        "--preload-pillow",
        action="store_true",
        help="initialize image plugins on the main thread as the viewer does",
    )
    args = parser.parse_args()
    if len(args.region) != 4 or any(item < 0 for item in args.region[:2]):
        parser.error("--region needs four integers with non-negative X,Y")
    if any(item < 1 for item in args.region[2:]):
        parser.error("region W,H must be positive")
    if len(args.size) != 2 or any(item < 1 for item in args.size):
        parser.error("--size needs positive W,H")
    if not math.isfinite(args.stack_interval) or args.stack_interval <= 0:
        parser.error("--stack-interval must be finite and positive")
    if not args.slide.is_file():
        parser.error(f"slide does not exist: {args.slide}")

    log(f"Python {platform.python_version()}; {platform.platform()}")
    log(f"Executable: {sys.executable}; base prefix: {sys.base_prefix}")
    log(f"Execution: {'worker thread' if args.worker_thread else 'main thread'}")
    for package in ("wsi-patchkit", "kfbslide", "pillow", "numpy"):
        try:
            log(f"{package}: {version(package)}")
        except PackageNotFoundError:
            log(f"{package}: not installed")
    log(f"File size: {args.slide.stat().st_size:,} bytes")
    log(f"Region: {args.region}; output: {args.size}")
    faulthandler.enable(file=sys.stderr)
    faulthandler.dump_traceback_later(args.stack_interval, repeat=True, file=sys.stderr)
    try:
        if args.trace_tiles:
            logger = logging.getLogger("wsi_patchkit.io.kfbslide")
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
        if args.preload_pillow:
            from PIL import Image

            log("Pillow plugin initialization BEGIN")
            start = time.perf_counter()
            Image.init()
            log(f"Pillow plugin initialization END: {time.perf_counter() - start:.3f}s")
        if args.worker_thread:
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(diagnose, args.slide, args.region, args.size).result()
        else:
            diagnose(args.slide, args.region, args.size)
    finally:
        faulthandler.cancel_dump_traceback_later()


def diagnose(path: Path, region: tuple[int, ...], output_size: tuple[int, ...]) -> None:
    # Import after enabling the watchdog so import stalls also produce a stack.
    log("Importing readers...")
    from wsi_patchkit.io.kfbslide import KfbSlideReader
    from wsi_patchkit.tiles import TileRenderer

    class TracedReader(KfbSlideReader):
        def _slide(self, path):
            cold = path not in self._slides
            start = time.perf_counter()
            if cold:
                log("KFB open BEGIN")
            slide = super()._slide(path)
            if cold:
                log(f"KFB open END: {time.perf_counter() - start:.3f}s")
            return slide

        def read_region(self, path, location, level, size):
            log(f"Read BEGIN: level={level}, location={location}, size={size}")
            start = time.perf_counter()
            array = super().read_region(path, location, level, size)
            log(f"Read END: {time.perf_counter() - start:.3f}s")
            return array

    reader = TracedReader(cache_size=1)
    try:
        # Disable encoded response caching so both passes actually read pixels.
        renderer = TileRenderer(reader, cache_size=0)
        try:
            start = time.perf_counter()
            log("Metadata BEGIN")
            metadata = renderer.metadata(path)
            log(f"Metadata END: {time.perf_counter() - start:.3f}s")
            for level in metadata.levels:
                log(
                    f"Level {level.level}: dimensions={level.dimensions}, "
                    f"downsample={level.downsample}"
                )
            for attempt in (1, 2):
                log(f"Render {attempt} BEGIN")
                start = time.perf_counter()
                result = renderer.render_region(path, region, output_size)
                log(
                    f"Render {attempt} END: {time.perf_counter() - start:.3f}s; "
                    f"{len(result.content):,} encoded bytes"
                )
        finally:
            renderer.close()
    finally:
        reader.close()


if __name__ == "__main__":
    main()
