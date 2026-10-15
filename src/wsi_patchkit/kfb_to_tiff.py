"""Stream KFB slides into tiled BigTIFF, retaining native pyramid levels."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, TypeVar

import numpy as np
import tifffile
from PIL import Image, JpegImagePlugin

from .io._color import image_to_rgb
from .io.kfbslide import KfbSlideReader
from .types import LevelInfo

_T = TypeVar("_T")
_R = TypeVar("_R")
_SAMPLING = {0: (1, 1), 1: (2, 1), 2: (2, 2)}


def _bounded_map(
    executor: Executor, function: Callable[[_T], _R], items: Iterable[_T], limit: int
) -> Iterator[_R]:
    """Ordered map with backpressure (also on Python versions before 3.14)."""
    iterator = iter(items)
    pending = deque()
    try:
        for _ in range(limit):
            try:
                item = next(iterator)
            except StopIteration:
                break
            pending.append(executor.submit(function, item))
        while pending:
            yield pending.popleft().result()
            try:
                item = next(iterator)
            except StopIteration:
                continue
            pending.append(executor.submit(function, item))
    finally:
        for future in pending:
            future.cancel()


def _jpeg_sampling(data: bytes, tile: int, *, verify: bool = False) -> int | None:
    """Check JPEG headers without decoding; optionally validate all pixels."""
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            if (
                image.format != "JPEG"
                or image.mode != "RGB"
                or image.size != (tile, tile)
                or image.info.get("progressive")
                or image.info.get("adobe_transform") == 0
                or len(getattr(image, "layer", ())) != 3
                or image.layer[1][1:3] != (1, 1)
                or image.layer[2][1:3] != (1, 1)
            ):
                return None
            sampling = JpegImagePlugin.get_sampling(image)
            if verify:
                image.load()
            return sampling if sampling in _SAMPLING else None
    except (OSError, ValueError, SyntaxError):
        return None


def _native_jpeg(slide: Any, level: int, x: int, y: int, tile: int) -> bytes | None:
    # Private backend access is confined here. The project's pinned adapter
    # supplies a bounded SQLite index and independent worker file handles.
    index = slide._index
    if index.tile_size != tile:
        return None
    scale = index.level_scales[level]
    index.prefetch(scale, (x, y, x + tile, y + tile))
    entry_id = index.lookup.get((scale, x, y))
    if entry_id is None:
        return None
    entry = index.entries[entry_id]
    if (entry["width"], entry["height"]) != (tile, tile):
        return None
    size = entry["size"]
    offset = index.offsets[entry_id]
    if not 0 < size <= max(1024 * 1024, tile * tile * 6) or offset < 0:
        return None
    handle = slide._ensure_open_handle()
    handle.seek(offset)
    return handle.read(size)


@dataclass(frozen=True)
class ConversionResult:
    output: Path
    tiles: int
    copied_tiles: int
    seconds: float
    output_bytes: int


def convert_kfb_to_tiff(
    source: str | Path,
    output: str | Path,
    *,
    workers: int = 4,
    buffer_tiles: int | None = None,
    tile_size: int | None = None,
    compression: str = "jpeg",
    quality: int = 90,
    reencode: bool = False,
    verify_jpeg: bool = False,
    level0_only: bool = False,
    overwrite: bool = False,
    index_cache_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> ConversionResult:
    """Convert without materializing a full slide or sharing reader handles.

    JPEG mode copies compatible native JPEGs byte-for-byte; other tiles are
    encoded with Pillow. ``reencode`` applies ``quality`` to every tile.
    Deflate and none preserve pixels returned by KfbSlideReader. Associated
    label/macro images are not exported. Output is always tiled BigTIFF.
    """
    if workers < 1 or (buffer_tiles is not None and buffer_tiles < 1):
        raise ValueError("workers and buffer_tiles must be positive")
    if tile_size is not None and (tile_size < 16 or tile_size % 16):
        raise ValueError("tile_size must be a positive multiple of 16")
    if compression not in ("jpeg", "deflate", "none"):
        raise ValueError("compression must be jpeg, deflate, or none")
    if not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100")
    if compression == "jpeg":
        try:
            tifffile.TIFF.COMPRESSORS[7]
        except KeyError as error:
            raise ImportError(
                "JPEG TIFF requires imagecodecs; run uv sync --extra tiff-codecs "
                "or use --compression deflate"
            ) from error
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output:
        raise ValueError("source and output must be different files")
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() != ".kfb":
        raise ValueError("source must be a .kfb file")
    if output.exists() and not overwrite:
        raise FileExistsError(output)
    signature = source.stat().st_size, source.stat().st_mtime_ns
    output.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = (
        Path(index_cache_dir).resolve()
        if index_cache_dir is not None
        else output.parent / ".kfb-index"
    )
    started = time.perf_counter()
    Image.init()  # Initialize Pillow plugins before entering worker threads.
    completed = copied = 0
    temporary: Path | None = None
    worker_readers: list[KfbSlideReader] = []
    local = threading.local()
    readers_lock = threading.Lock()

    def make_reader() -> KfbSlideReader:
        return KfbSlideReader(
            cache_size=1, index_cache_dir=cache_dir, tile_cache_size=0
        )

    def initialize_worker() -> None:
        reader = make_reader()
        try:
            local.slide = reader._slide(str(source))
        except BaseException:
            reader.close()
            raise
        with readers_lock:
            worker_readers.append(reader)

    try:
        with ExitStack() as stack:
            reader = stack.enter_context(make_reader())
            metadata = reader.metadata(source)  # Build the shared index once.
            slide = reader._slide(str(source))
            levels = metadata.levels[:1] if level0_only else metadata.levels
            native_tile = int(slide._index.tile_size)
            tile = tile_size or (
                native_tile if native_tile >= 16 and native_tile % 16 == 0 else 512
            )
            limit = buffer_tiles if buffer_tiles is not None else workers * 2
            executor = None
            if workers > 1:
                executor = stack.enter_context(
                    ThreadPoolExecutor(
                        max_workers=workers, initializer=initialize_worker
                    )
                )
            else:
                local.slide = slide
            descriptor, filename = tempfile.mkstemp(
                prefix=f".{output.name}.", suffix=".partial", dir=output.parent
            )
            os.close(descriptor)
            temporary = Path(filename)
            writer = stack.enter_context(tifffile.TiffWriter(temporary, bigtiff=True))
            for position, level in enumerate(levels):
                width, height = level.dimensions
                columns, rows = math.ceil(width / tile), math.ceil(height / tile)
                total = columns * rows
                sampling = 2
                if compression == "jpeg" and not reencode:
                    sample = _native_jpeg(slide, level.level, 0, 0, tile)
                    if sample is not None:
                        native_sampling = _jpeg_sampling(sample, tile)
                        if native_sampling is not None:
                            sampling = native_sampling

                def encode_tile(
                    index: int,
                    *,
                    columns: int = columns,
                    level: LevelInfo = level,
                    width: int = width,
                    height: int = height,
                    sampling: int = sampling,
                ) -> tuple[bytes, bool]:
                    worker_slide = local.slide
                    x, y = index % columns * tile, index // columns * tile
                    if compression == "jpeg" and not reencode:
                        jpeg = _native_jpeg(worker_slide, level.level, x, y, tile)
                        if (
                            jpeg is not None
                            and _jpeg_sampling(jpeg, tile, verify=verify_jpeg)
                            == sampling
                        ):
                            return jpeg, True
                    # read_region uses level-0 coordinates; ceil avoids shifting
                    # a tile left/up at non-integral KFB downsample factors.
                    ds = float(worker_slide.level_downsamples[level.level])
                    image = worker_slide.read_region(
                        (math.ceil(x * ds), math.ceil(y * ds)),
                        level.level,
                        (min(tile, width - x), min(tile, height - y)),
                    )
                    pixels = image_to_rgb(image)
                    padded = np.full((tile, tile, 3), 255, dtype=np.uint8)
                    padded[: pixels.shape[0], : pixels.shape[1]] = pixels
                    if compression == "jpeg":
                        encoded = BytesIO()
                        Image.fromarray(padded).save(
                            encoded, "JPEG", quality=quality, subsampling=sampling
                        )
                        return encoded.getvalue(), False
                    raw = padded.tobytes()
                    return (
                        zlib.compress(raw, 1) if compression == "deflate" else raw,
                        False,
                    )

                def tile_stream(
                    *,
                    encode_tile: Callable[[int], tuple[bytes, bool]] = encode_tile,
                    total: int = total,
                    level: LevelInfo = level,
                    width: int = width,
                    height: int = height,
                ) -> Iterator[bytes]:
                    nonlocal completed, copied
                    results = (
                        map(encode_tile, range(total))
                        if executor is None
                        else _bounded_map(executor, encode_tile, range(total), limit)
                    )
                    last_report = 0.0
                    for count, (data, was_copied) in enumerate(results, 1):
                        completed += 1
                        copied += was_copied
                        now = time.perf_counter()
                        if progress and (now - last_report >= 2 or count == total):
                            progress(
                                f"{source.name}: level {level.level} "
                                f"{width}x{height}, {count}/{total} tiles"
                            )
                            last_report = now
                        yield data

                resolution = (
                    None
                    if level.mpp is None
                    else (10_000 / level.mpp[0], 10_000 / level.mpp[1])
                )
                description = (
                    json.dumps(
                        {
                            "source_format": "kfb",
                            "source_name": source.name,
                            "properties": dict(metadata.properties),
                        },
                        ensure_ascii=True,
                    )
                    if position == 0
                    else None
                )
                # Precompressed byte iterators let our workers parallelize both
                # decoding and encoding; tifffile performs only ordered writes.
                writer.write(
                    tile_stream(),
                    shape=(height, width, 3),
                    dtype=np.uint8,
                    tile=(tile, tile),
                    photometric="ycbcr" if compression == "jpeg" else "rgb",
                    compression=None if compression == "none" else compression,
                    subsampling=_SAMPLING[sampling] if compression == "jpeg" else None,
                    subifds=len(levels) - 1 if position == 0 else None,
                    subfiletype=0 if position == 0 else 1,
                    resolution=resolution,
                    resolutionunit="CENTIMETER" if resolution else None,
                    description=description,
                    metadata=None,
                    software="wsi-patchkit kfb-to-tiff",
                )
        stat = source.stat()
        if (stat.st_size, stat.st_mtime_ns) != signature:
            raise OSError("source KFB changed during conversion")
        if overwrite:
            os.replace(temporary, output)
        else:
            # Atomic no-clobber publication on NTFS and POSIX filesystems. A
            # competing converter cannot replace an already completed output.
            os.link(temporary, output)
        return ConversionResult(
            output,
            completed,
            copied,
            time.perf_counter() - started,
            output.stat().st_size,
        )
    finally:
        for worker_reader in worker_readers:
            worker_reader.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="KFB file or input directory")
    parser.add_argument("output", type=Path, help="TIFF file or output directory")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--jobs", type=int, default=1, help="concurrent files")
    parser.add_argument("--buffer-tiles", type=int, help="in-flight tiles per file")
    parser.add_argument("--tile-size", type=int, help="default: native KFB tile size")
    parser.add_argument(
        "--compression", choices=("jpeg", "deflate", "none"), default="jpeg"
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=90,
        help="JPEG quality for re-encoded tiles (1-100)",
    )
    parser.add_argument(
        "--reencode", action="store_true", help="decode and re-encode every JPEG tile"
    )
    parser.add_argument(
        "--verify-jpeg",
        action="store_true",
        help="fully decode copied JPEGs to detect damaged pixels",
    )
    parser.add_argument("--level0-only", action="store_true")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--index-cache-dir", type=Path)
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.workers < 1:
        parser.error("--jobs and --workers must be positive")
    if args.buffer_tiles is not None and args.buffer_tiles < 1:
        parser.error("--buffer-tiles must be positive")
    if args.tile_size is not None and (args.tile_size < 16 or args.tile_size % 16):
        parser.error("--tile-size must be a positive multiple of 16")
    if not 1 <= args.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    if args.source.is_dir():
        entries = args.source.rglob("*") if args.recursive else args.source.iterdir()
        sources = sorted(
            path for path in entries if path.is_file() and path.suffix.lower() == ".kfb"
        )
        tasks = [
            (path, args.output / path.relative_to(args.source).with_suffix(".tiff"))
            for path in sources
        ]
    elif args.source.is_file():
        tasks = [(args.source, args.output)]
    else:
        parser.error(f"source does not exist: {args.source}")
    if not tasks:
        parser.error("no KFB files found")
    destinations = [str(output.resolve()).casefold() for _, output in tasks]
    if len(set(destinations)) != len(destinations):
        parser.error("multiple inputs map to the same TIFF output")
    failures = 0

    def convert_task(task: tuple[Path, Path]) -> bool:
        source, output = task
        try:
            result = convert_kfb_to_tiff(
                source,
                output,
                workers=args.workers,
                buffer_tiles=args.buffer_tiles,
                tile_size=args.tile_size,
                compression=args.compression,
                quality=args.quality,
                reencode=args.reencode,
                verify_jpeg=args.verify_jpeg,
                level0_only=args.level0_only,
                overwrite=args.overwrite,
                index_cache_dir=args.index_cache_dir,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
            print(
                f"OK {result.output}: {result.seconds:.2f}s, "
                f"{result.output_bytes / 1024**2:.1f} MiB, "
                f"{result.copied_tiles}/{result.tiles} tiles copied "
                "without re-encoding",
                flush=True,
            )
            return True
        except Exception as error:
            print(f"FAILED {source}: {error}", file=sys.stderr, flush=True)
            return False

    if args.jobs == 1:
        failures = sum(not convert_task(task) for task in tasks)
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            failures = sum(
                not success
                for success in _bounded_map(executor, convert_task, tasks, args.jobs)
            )
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
