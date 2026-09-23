"""Command-line entry point for the bundled WSI viewer."""

from __future__ import annotations

import argparse
import re
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

SLIDE_EXTENSIONS = frozenset(
    {
        ".bif",
        ".mrxs",
        ".ndpi",
        ".qptiff",
        ".scn",
        ".svs",
        ".tif",
        ".tiff",
        ".vms",
        ".vmu",
    }
)


def _slide(value: str) -> tuple[str, Path]:
    slide_id, separator, path = value.partition("=")
    if not separator or not slide_id or not path:
        raise argparse.ArgumentTypeError("slides must use ID=/path/to/slide.svs")
    return slide_id, Path(path)


def _unique_slide_id(relative_path: Path, existing: set[str]) -> str:
    """Create a stable, URL-safe ID from a path relative to its scan root."""
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", relative_path.as_posix())
    candidate = candidate.strip("._-") or "slide"
    candidate = candidate[:128]
    if candidate not in existing:
        return candidate

    sequence = 2
    while True:
        suffix = f"-{sequence}"
        unique = f"{candidate[: 128 - len(suffix)]}{suffix}"
        if unique not in existing:
            return unique
        sequence += 1


def discover_slides(
    directories: Iterable[str | Path],
    *,
    existing: Mapping[str, Path] | None = None,
) -> dict[str, Path]:
    """Recursively discover supported WSI files below one or more directories."""
    slides = dict(existing or {})
    ids = set(slides)
    for value in directories:
        directory = Path(value).expanduser().resolve()
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        paths = sorted(
            (
                path
                for path in directory.rglob("*")
                if path.is_file()
                and not path.name.startswith(".upload-")
                and path.suffix.lower() in SLIDE_EXTENSIONS
            ),
            key=lambda path: path.relative_to(directory).as_posix().casefold(),
        )
        for path in paths:
            slide_id = _unique_slide_id(path.relative_to(directory), ids)
            slides[slide_id] = path
            ids.add(slide_id)
    return slides


class SlideDirectoryScanner:
    """Refresh discovered slides while keeping public IDs stable."""

    def __init__(
        self,
        directories: Iterable[str | Path],
        explicit: Mapping[str, Path] | None = None,
        *,
        interval: float = 5.0,
    ) -> None:
        self.directories = tuple(
            Path(value).expanduser().resolve() for value in directories
        )
        self.explicit = dict(explicit or {})
        self.interval = interval
        self._lock = threading.Lock()
        self._last_scan = 0.0
        self._signatures: dict[Path, tuple[int, int]] = {}
        self._known_ids: dict[Path, str] = {}
        self._slides: dict[str, Path] = dict(self.explicit)
        self._relative_paths: dict[str, str] = {}
        self.refresh(force=True, initial=True)

    def relative_paths(self) -> dict[str, str]:
        """Return public IDs mapped to paths relative to their scan roots."""
        with self._lock:
            return dict(self._relative_paths)

    def refresh(self, *, force: bool = False, initial: bool = False) -> dict[str, Path]:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_scan < self.interval:
                return dict(self._slides)
            self._last_scan = now
            signatures: dict[Path, tuple[int, int]] = {}
            paths: list[tuple[Path, Path]] = []
            for directory in self.directories:
                if not directory.is_dir():
                    raise NotADirectoryError(directory)
                for path in sorted(
                    directory.rglob("*"), key=lambda p: p.as_posix().casefold()
                ):
                    if path.name.startswith(".upload-"):
                        continue
                    if path.suffix.lower() not in SLIDE_EXTENSIONS:
                        continue
                    try:
                        stat = path.stat()
                        if not path.is_file():
                            continue
                    except OSError:
                        continue
                    signatures[path] = (stat.st_size, stat.st_mtime_ns)
                    paths.append((directory, path))

            slides = dict(self.explicit)
            relative_paths: dict[str, str] = {}
            used = set(slides) | set(self._known_ids.values())
            for directory, path in paths:
                if path in self.explicit.values():
                    continue
                if not initial and signatures[path] != self._signatures.get(path):
                    continue
                slide_id = self._known_ids.get(path)
                if slide_id is None:
                    slide_id = _unique_slide_id(path.relative_to(directory), used)
                    self._known_ids[path] = slide_id
                    used.add(slide_id)
                slides[slide_id] = path
                relative_paths[slide_id] = path.relative_to(directory).as_posix()
            self._signatures = signatures
            self._slides = slides
            self._relative_paths = relative_paths
            return dict(slides)


def discover_overlays(
    directories: Iterable[str | Path],
    *,
    slide_ids: Iterable[str],
    slide_id_aliases: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Path]]:
    """Discover manifests below overlay roots for the registered slides.

    Every ``prediction.json`` is loaded before it is registered, so the
    manifest's ``slide_id`` and ``overlay_id`` -- rather than a directory name
    -- determine its destination. ``slide_id_aliases`` permits a manifest to
    use a unique source-file stem when the viewer's public ID includes the file
    extension. Manifests for slides not served by this invocation are
    deliberately ignored, allowing one shared output root to contain
    predictions for more slides than the current viewer exposes.
    """
    from .overlays import load_indexed_overlay_manifest

    registered = set(slide_ids)
    aliases = dict(slide_id_aliases or {})
    overlays: dict[str, dict[str, Path]] = {}
    for value in directories:
        directory = Path(value).expanduser().resolve()
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        manifests = sorted(
            directory.rglob("prediction.json"),
            key=lambda path: path.relative_to(directory).as_posix().casefold(),
        )
        for manifest in manifests:
            overlay = load_indexed_overlay_manifest(manifest)
            slide_id = (
                overlay.slide_id
                if overlay.slide_id in registered
                else aliases.get(overlay.slide_id)
            )
            if slide_id not in registered:
                continue
            slide_overlays = overlays.setdefault(slide_id, {})
            if overlay.overlay_id in slide_overlays:
                previous = slide_overlays[overlay.overlay_id]
                raise ValueError(
                    f"duplicate overlay {overlay.overlay_id!r} for slide "
                    f"{slide_id!r}: {previous} and {manifest}"
                )
            slide_overlays[overlay.overlay_id] = manifest
    return overlays


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the wsi-patchkit viewer")
    parser.add_argument(
        "--slide",
        action="append",
        type=_slide,
        metavar="ID=PATH",
        help="register a public slide ID; repeat to expose multiple slides",
    )
    parser.add_argument(
        "--slide-dir",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="recursively register supported WSI files in a directory; repeatable",
    )
    parser.add_argument(
        "slides",
        nargs="*",
        type=Path,
        metavar="SLIDES_DIR",
        help="directories of WSI files (shorthand for --slide-dir)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--tile-size", default=256, type=int)
    parser.add_argument("--reader-pool-size", default=4, type=int)
    parser.add_argument(
        "--overlay",
        action="append",
        default=[],
        type=Path,
        metavar="MANIFEST.json",
        help="register a wsi-patchkit-overlay/v1 manifest; repeatable",
    )
    parser.add_argument(
        "--overlay-root",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help=(
            "recursively discover prediction.json manifests for registered slides; "
            "repeatable"
        ),
    )
    parser.add_argument(
        "--crop-output-dir",
        default=Path("crops"),
        type=Path,
        metavar="PATH",
        help="save level-0 rectangular crops in this server-side directory",
    )
    parser.add_argument(
        "--upload-dir",
        default=Path("uploads"),
        type=Path,
        metavar="PATH",
        help="store browser-uploaded slides in this server-side directory",
    )
    parser.add_argument(
        "--max-upload-gb",
        default=32,
        type=int,
        metavar="N",
        help="maximum browser upload size in GiB (default: 32)",
    )
    parser.add_argument(
        "--crop-workers",
        default=1,
        type=int,
        metavar="N",
        help="number of background crop workers (default: 1)",
    )
    args = parser.parse_args()
    if args.max_upload_gb < 1:
        parser.error("--max-upload-gb must be positive")

    try:
        import uvicorn
    except ImportError as error:
        raise SystemExit("Install the viewer with: uv sync --extra web") from error

    from .app import create_app
    from .overlays import load_indexed_overlay_manifest

    explicit_slides = dict(args.slide or [])
    directories = [*args.slide_dir, *args.slides]
    try:
        scanner = SlideDirectoryScanner(directories, explicit_slides)
        slides = scanner.refresh()
    except NotADirectoryError as error:
        parser.error(f"slide directory does not exist: {error}")
    overlays: dict[str, dict[str, object]] = {}
    try:
        for manifest in args.overlay:
            overlay = load_indexed_overlay_manifest(manifest)
            slide_overlays = overlays.setdefault(overlay.slide_id, {})
            if overlay.overlay_id in slide_overlays:
                parser.error(
                    f"duplicate overlay {overlay.overlay_id!r} "
                    f"for slide {overlay.slide_id!r}"
                )
            slide_overlays[overlay.overlay_id] = overlay
        slide_id: dict[str, str | None] = {}
        for id, path in slides.items():
            stem = path.stem
            if stem not in slide_id:
                slide_id[stem] = id
            elif slide_id[stem] != id:
                # An ambiguous source stem must not silently select a slide.
                slide_id[stem] = None
        manifest_slide_id_aliases = {
            stem: id
            for stem, id in slide_id.items()
            if id is not None
        }
        discovered_overlays = discover_overlays(
            args.overlay_root,
            slide_ids=slides,
            slide_id_aliases=manifest_slide_id_aliases,
        )
        for slide_id, slide_overlays in discovered_overlays.items():
            registered_overlays = overlays.setdefault(slide_id, {})
            for overlay_id, manifest in slide_overlays.items():
                if overlay_id in registered_overlays:
                    parser.error(
                        f"duplicate overlay {overlay_id!r} for slide {slide_id!r}"
                    )
                registered_overlays[overlay_id] = manifest
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        parser.error(str(error))

    app = create_app(
        slides,
        overlays=overlays,
        manifest_slide_id_aliases=manifest_slide_id_aliases,
        tile_size=args.tile_size,
        reader_pool_size=args.reader_pool_size,
        crop_output_dir=args.crop_output_dir,
        upload_dir=args.upload_dir,
        max_upload_bytes=args.max_upload_gb * 1024**3,
        crop_workers=args.crop_workers,
        slide_scanner=scanner if directories else None,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
