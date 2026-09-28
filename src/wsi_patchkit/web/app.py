"""Optional FastAPI application serving IIIF tiles and the WSI viewer."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from .__main__ import SlideDirectoryScanner

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, Response
except ImportError as error:  # pragma: no cover - exercised without the web extra
    raise ImportError(
        "The WSI viewer requires the 'web' extra: uv sync --extra web"
    ) from error

from ..io import AutoSlideReader, SlideReader
from ..tiles import TileRenderer, iiif_scale_factors
from ..types import SlideMetadata
from .crops import CropJobQueue
from .overlays import RGBA, IndexedOverlay, OverlayRegistry, OverlayRenderer
from .registry import SlideRegistry, SlideSource, valid_public_id
from .workers import TileWorkerPool

_STATIC_DIR = Path(__file__).with_name("static")
_LOGGER = logging.getLogger(__name__)
_STATIC_ASSETS = {
    "app.js": (_STATIC_DIR / "app.js").read_bytes(),
    "styles.css": (_STATIC_DIR / "styles.css").read_bytes(),
}
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
_CROP_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_UPLOAD_EXTENSIONS = frozenset(
    {".bif", ".btf", ".btiff", ".ndpi", ".qptiff", ".scn", ".svs", ".tif", ".tiff"}
)


def _upload_name(filename: str) -> str:
    if not filename or filename != Path(filename).name or "\\" in filename:
        raise ValueError("filename must be a file name without directories")
    suffix = Path(filename).suffix.lower()
    if suffix not in _UPLOAD_EXTENSIONS:
        raise ValueError("unsupported slide file extension")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem)
    stem = stem.strip("._-")[: 128 - len(suffix) - len("upload_")]
    return f"{stem or 'slide'}{suffix}"


def _overlay_palette(
    overlay: IndexedOverlay, style: str | None
) -> dict[int, RGBA] | None:
    if style is None:
        return None
    if len(style) != 8 * len(overlay.classes) or re.fullmatch(
        r"[0-9a-fA-F]+", style
    ) is None:
        raise HTTPException(status_code=400, detail="invalid overlay palette")
    palette: dict[int, RGBA] = {}
    for index, item in enumerate(overlay.classes):
        rgba = bytes.fromhex(style[index * 8 : index * 8 + 8])
        palette[item.id] = (rgba[0], rgba[1], rgba[2], rgba[3])
    return palette


T = TypeVar("T")


async def _run_while_connected(
    workers: TileWorkerPool,
    request: Request,
    function: Callable[..., T],
    /,
    *args: object,
    **kwargs: object,
) -> T:
    """Cancel queued blocking work after the HTTP client disconnects."""
    work = asyncio.create_task(workers.run(function, *args, **kwargs))
    try:
        while not work.done():
            if await request.is_disconnected():
                work.cancel()
                with suppress(asyncio.CancelledError):
                    await work
                raise asyncio.CancelledError
            await asyncio.sleep(0.01)
        return work.result()
    finally:
        if not work.done():
            work.cancel()


def _public_metadata(
    slide_id: str,
    metadata: SlideMetadata,
    *,
    overlays: Mapping[str, IndexedOverlay] | None = None,
) -> dict[str, object]:
    return {
        "id": slide_id,
        "width": metadata.dimensions[0],
        "height": metadata.dimensions[1],
        "mpp": metadata.mpp,
        "vendor": metadata.vendor,
        "levels": [
            {
                "level": level.level,
                "width": level.dimensions[0],
                "height": level.dimensions[1],
                "downsample": level.downsample,
                "mpp": level.mpp,
            }
            for level in metadata.levels
        ],
        "overlays": [
            overlay.public_metadata() for overlay in (overlays or {}).values()
        ],
    }


def _parse_region(value: str, dimensions: tuple[int, int]) -> tuple[int, int, int, int]:
    if value == "full":
        return 0, 0, dimensions[0], dimensions[1]
    try:
        parts = tuple(int(part) for part in value.split(","))
    except ValueError as error:
        raise ValueError("region must be 'full' or x,y,width,height") from error
    if len(parts) != 4:
        raise ValueError("region must be 'full' or x,y,width,height")
    x, y, width, height = parts
    if x < 0 or y < 0 or width < 1 or height < 1:
        raise ValueError("region coordinates must be non-negative and sized")
    return x, y, width, height


def _parse_size(value: str, region: tuple[int, int, int, int]) -> tuple[int, int]:
    region_width, region_height = region[2:]
    if value == "max":
        return region_width, region_height
    parts = value.split(",")
    if len(parts) != 2 or (not parts[0] and not parts[1]):
        raise ValueError("size must be 'max', width, ,height, or width,height")
    try:
        width = int(parts[0]) if parts[0] else None
        height = int(parts[1]) if parts[1] else None
    except ValueError as error:
        raise ValueError("size values must be integers") from error
    if width is not None and width < 1 or height is not None and height < 1:
        raise ValueError("size values must be positive")
    if width is None:
        assert height is not None
        width = max(1, round(height * region_width / region_height))
    if height is None:
        height = max(1, round(width * region_height / region_width))
    return width, height


def _crop_integer(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _crop_level(payload: Mapping[str, object], metadata: SlideMetadata) -> int:
    level = payload.get("level", 0)
    if isinstance(level, bool) or not isinstance(level, int):
        raise ValueError("level must be an integer")
    if level < 0 or level >= len(metadata.levels):
        raise ValueError("level does not exist for this slide")
    return level


def _crop_filename(
    payload: Mapping[str, object],
    *,
    slide_id: str,
    region: tuple[int, int, int, int],
    image_format: str,
) -> str:
    extension = "jpg" if image_format == "jpg" else "png"
    requested = payload.get("filename")
    if requested is None or requested == "":
        x, y, width, height = region
        token = uuid.uuid4().hex[:10]
        return f"{slide_id}_x{x}_y{y}_w{width}_h{height}_{token}.{extension}"
    if not isinstance(requested, str) or not _CROP_FILENAME.fullmatch(requested):
        raise ValueError(
            "filename must use 1-200 letters, numbers, dots, dashes, or underscores"
        )
    path = Path(requested)
    if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        requested = f"{requested}.{extension}"
    elif path.suffix.lower() not in (
        {".jpg", ".jpeg"} if extension == "jpg" else {".png"}
    ):
        raise ValueError("filename extension must match format")
    return requested


def create_app(
    slides: Mapping[str, SlideSource | str | Path],
    *,
    overlays: Mapping[
        str,
        Mapping[str, IndexedOverlay | str | Path],
    ]
    | None = None,
    manifest_slide_id_aliases: Mapping[str, str] | None = None,
    reader: SlideReader | None = None,
    reader_factory: Callable[[], SlideReader] | None = None,
    reader_pool_size: int = 4,
    tile_workers: int | None = None,
    tile_size: int = 256,
    cache_size: int = 512,
    jpeg_quality: int = 85,
    max_output_pixels: int = 16_777_216,
    cache_control: str = "private, max-age=3600",
    crop_output_dir: str | Path = "crops",
    crop_workers: int = 1,
    slide_scanner: SlideDirectoryScanner | None = None,
    upload_dir: str | Path = "uploads",
    max_upload_bytes: int = 32 * 1024**3,
) -> FastAPI:
    """Create a self-contained WSI viewer for an explicit slide registry."""
    if tile_size < 1:
        raise ValueError("tile_size must be positive")
    if reader is not None and reader_factory is not None:
        raise ValueError("provide reader or reader_factory, not both")
    if crop_workers < 1:
        raise ValueError("crop_workers must be positive")
    if max_upload_bytes < 1:
        raise ValueError("max_upload_bytes must be positive")
    upload_directory = Path(upload_dir).expanduser().resolve()
    if upload_directory.exists() and not upload_directory.is_dir():
        raise NotADirectoryError(upload_directory)
    initial_slides = dict(slides)
    uploaded_sources: dict[str, SlideSource] = {}
    for path in sorted(upload_directory.iterdir()) if upload_directory.is_dir() else ():
        if (
            path.name.startswith(".upload-")
            or not path.is_file()
            or path.suffix.lower() not in _UPLOAD_EXTENSIONS
        ):
            continue
        slide_id = f"upload_{path.name}"
        if slide_id in initial_slides:
            continue
        try:
            valid_public_id(slide_id, name="uploaded slide ID")
            uploaded_sources[slide_id] = SlideSource(path)
        except (OSError, ValueError):
            _LOGGER.warning("Skipping invalid uploaded slide %s", path)
    registry = SlideRegistry(
        {**initial_slides, **uploaded_sources}, allow_empty=True
    )
    upload_lock = asyncio.Lock()
    overlay_registry = OverlayRegistry(
        overlays,
        slide_ids=tuple(registry),
        manifest_slide_id_aliases=manifest_slide_id_aliases,
    )
    crop_directory = Path(crop_output_dir).expanduser().resolve()
    if reader is not None:
        renderer = TileRenderer(
            reader,
            cache_size=cache_size,
            jpeg_quality=jpeg_quality,
            max_output_pixels=max_output_pixels,
        )
    else:
        renderer = TileRenderer(
            reader_factory=reader_factory or AutoSlideReader,
            reader_pool_size=reader_pool_size,
            cache_size=cache_size,
            jpeg_quality=jpeg_quality,
            max_output_pixels=max_output_pixels,
        )
    worker_count = reader_pool_size if tile_workers is None else tile_workers
    workers = TileWorkerPool(worker_count)
    overlay_workers = TileWorkerPool(max(1, worker_count // 2))
    crop_jobs = CropJobQueue(
        renderer,
        crop_directory,
        worker_count=crop_workers,
    )
    overlay_renderer = OverlayRenderer(
        reader_pool_size=reader_pool_size,
        cache_size=cache_size,
        max_output_pixels=max_output_pixels,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        crop_jobs.close()
        overlay_workers.close()
        overlay_renderer.close()
        workers.close()
        renderer.close()

    app = FastAPI(title="wsi-patchkit viewer", lifespan=lifespan)
    app.state.slide_registry = registry
    app.state.overlay_registry = overlay_registry
    app.state.overlay_renderer = overlay_renderer
    app.state.tile_renderer = renderer
    app.state.tile_workers = workers
    app.state.overlay_workers = overlay_workers
    app.state.crop_output_dir = crop_directory
    app.state.crop_jobs = crop_jobs
    app.state.upload_dir = upload_directory

    def publish_slides(scanned: Mapping[str, SlideSource | str | Path]) -> None:
        uploaded_paths = {source.path for source in uploaded_sources.values()}
        merged = {}
        for slide_id, source in scanned.items():
            path = source.path if isinstance(source, SlideSource) else source
            if Path(path).resolve() not in uploaded_paths:
                merged[slide_id] = source
        merged.update(uploaded_sources)
        registry.replace(merged)

    def source_for(slide_id: str) -> SlideSource:
        try:
            return registry[slide_id]
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown slide") from error

    def metadata_for(slide_id: str) -> tuple[SlideSource, SlideMetadata]:
        source = source_for(slide_id)
        try:
            metadata = renderer.metadata(source.path, source_mpp=source.source_mpp)
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            _LOGGER.exception("Unable to read registered slide %s", slide_id)
            raise HTTPException(
                status_code=422,
                detail="registered slide could not be opened",
            ) from error
        for overlay in overlay_registry.for_slide(slide_id).values():
            try:
                overlay.validate_slide(metadata)
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail="registered overlay is incompatible with its slide",
                ) from error
        return source, metadata

    def validate_uploaded_slide(path: Path) -> None:
        metadata = renderer.metadata(path)
        width = min(16, metadata.dimensions[0])
        height = min(16, metadata.dimensions[1])
        renderer.render_region(path, (0, 0, width, height), (width, height))

    def overlay_for(slide_id: str, overlay_id: str) -> IndexedOverlay:
        source_for(slide_id)
        try:
            return overlay_registry.get(slide_id, overlay_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown overlay") from error

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

    @app.get("/static/{asset_name}", include_in_schema=False)
    async def static_asset(asset_name: str) -> Response:
        try:
            content = _STATIC_ASSETS[asset_name]
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown asset") from error
        media_type = "text/javascript" if asset_name.endswith(".js") else "text/css"
        return Response(
            content,
            media_type=media_type,
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/slides", name="list_slides")
    async def list_slides() -> list[dict[str, str]]:
        relative_paths: dict[str, str] = {}
        if slide_scanner is not None:
            try:
                publish_slides(await workers.run(slide_scanner.refresh))
                relative_paths = slide_scanner.relative_paths()
            except (OSError, ValueError):
                _LOGGER.exception("Unable to refresh slide directories")
        # Keep directory browsing responsive even for large collections. Reading
        # WSI metadata can be expensive, so defer it until a slide is selected.
        records = []
        for slide_id in registry:
            record = {"id": slide_id}
            if slide_id in uploaded_sources:
                record["path"] = f"上传切片/{uploaded_sources[slide_id].path.name}"
            elif slide_id in relative_paths:
                record["path"] = relative_paths[slide_id]
            records.append(record)
        return records

    @app.post("/api/slides/upload", name="upload_slide", status_code=201)
    async def upload_slide(request: Request, filename: str) -> dict[str, str]:
        try:
            safe_name = _upload_name(filename)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        content_length = request.headers.get("content-length")
        if (
            content_length
            and content_length.isdecimal()
            and int(content_length) > max_upload_bytes
        ):
            raise HTTPException(status_code=413, detail="slide upload is too large")
        upload_directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".upload-", suffix=Path(safe_name).suffix,
            dir=upload_directory,
        )
        temporary_path = Path(temporary_name)
        try:
            size = 0
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_upload_bytes:
                        raise HTTPException(
                            status_code=413, detail="slide upload is too large"
                        )
                    output.write(chunk)
            if not size:
                raise HTTPException(status_code=400, detail="slide upload is empty")
            try:
                await workers.run(validate_uploaded_slide, temporary_path)
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                raise HTTPException(
                    status_code=422, detail="uploaded slide could not be opened"
                ) from error

            async with upload_lock:
                stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
                candidate = safe_name
                number = 2
                while (
                    (upload_directory / candidate).exists()
                    or f"upload_{candidate}" in registry
                ):
                    tail = f"-{number}"
                    max_stem = 128 - len(suffix) - len("upload_") - len(tail)
                    candidate = f"{stem[:max_stem]}{tail}{suffix}"
                    number += 1
                destination = upload_directory / candidate
                os.replace(temporary_path, destination)
                slide_id = f"upload_{candidate}"
                uploaded_sources[slide_id] = SlideSource(destination)
                registry.replace(
                    {**dict(registry), slide_id: uploaded_sources[slide_id]}
                )
            return {"id": slide_id, "path": f"上传切片/{candidate}"}
        finally:
            temporary_path.unlink(missing_ok=True)

    @app.get("/api/slides/{slide_id}", name="slide_metadata")
    async def slide_metadata(slide_id: str, request: Request) -> dict[str, object]:
        _, metadata = await _run_while_connected(
            workers, request, metadata_for, slide_id
        )
        return _public_metadata(
            slide_id,
            metadata,
            overlays=overlay_registry.for_slide(slide_id),
        )

    @app.get("/api/slides/{slide_id}/overlays", name="list_overlays")
    async def list_overlays(slide_id: str) -> list[dict[str, object]]:
        source_for(slide_id)
        return [
            overlay.public_metadata()
            for overlay in overlay_registry.for_slide(slide_id).values()
        ]

    @app.post("/api/slides/{slide_id}/crops", name="save_crop")
    async def save_slide_crop(
        slide_id: str,
        payload: dict[str, object],
    ) -> JSONResponse:
        source, metadata = await workers.run(metadata_for, slide_id)
        try:
            x = _crop_integer(payload, "x")
            y = _crop_integer(payload, "y")
            width = _crop_integer(payload, "width")
            height = _crop_integer(payload, "height")
            level = _crop_level(payload, metadata)
            region = (x, y, width, height)
            if x < 0 or y < 0 or width < 1 or height < 1:
                raise ValueError("crop coordinates must be non-negative and sized")
            level_dimensions = metadata.levels[level].dimensions
            if x + width > level_dimensions[0] or y + height > level_dimensions[1]:
                raise ValueError("crop must be fully inside the selected level")
            image_format_value = payload.get("format", "png")
            if not isinstance(image_format_value, str):
                raise ValueError("format must be png or jpg")
            image_format = image_format_value.lower()
            if image_format == "jpeg":
                image_format = "jpg"
            if image_format not in ("png", "jpg"):
                raise ValueError("format must be png or jpg")
            filename = _crop_filename(
                payload,
                slide_id=slide_id,
                region=region,
                image_format=image_format,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            job = crop_jobs.submit(
                slide_id=slide_id,
                source=source,
                region=region,
                level=level,
                image_format=image_format,  # type: ignore[arg-type]
                filename=filename,
            )
        except FileExistsError as error:
            raise HTTPException(
                status_code=409,
                detail="a crop with this filename already exists",
            ) from error
        except RuntimeError as error:
            raise HTTPException(
                status_code=503,
                detail="crop queue is unavailable",
            ) from error
        return JSONResponse(status_code=202, content=job)

    @app.get("/api/slides/{slide_id}/crops/{job_id}", name="crop_status")
    async def crop_status(slide_id: str, job_id: str) -> dict[str, object]:
        source_for(slide_id)
        try:
            return crop_jobs.get(job_id, slide_id=slide_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404,
                detail="unknown crop job",
            ) from error

    @app.get("/iiif/3/{slide_id}/info.json", name="iiif_info")
    async def iiif_info(slide_id: str, request: Request) -> JSONResponse:
        _, metadata = await _run_while_connected(
            workers, request, metadata_for, slide_id
        )
        width, height = metadata.dimensions
        info_url = str(request.url_for("iiif_info", slide_id=slide_id))
        service_id = info_url.removesuffix("/info.json")
        return JSONResponse(
            {
                "@context": "http://iiif.io/api/image/3/context.json",
                "id": service_id,
                "type": "ImageService3",
                "protocol": "http://iiif.io/api/image",
                "profile": "level0",
                "width": width,
                "height": height,
                "tiles": [
                    {
                        "type": "Tile",
                        "width": tile_size,
                        "height": tile_size,
                        "scaleFactors": list(
                            iiif_scale_factors(metadata.dimensions, tile_size)
                        ),
                    }
                ],
                "preferredFormats": ["jpg"],
                "extraFormats": ["jpg", "png"],
                "extraQualities": ["default"],
                "extraFeatures": ["regionByPx", "sizeByW", "sizeByWh"],
            },
            headers={"Cache-Control": cache_control},
        )

    @app.get(
        "/iiif/3/{slide_id}/overlays/{overlay_id}/revision/{revision}/style/{style}/info.json",
        name="versioned_styled_overlay_iiif_info",
    )
    @app.get(
        "/iiif/3/{slide_id}/overlays/{overlay_id}/style/{style}/info.json",
        name="styled_overlay_iiif_info",
    )
    @app.get(
        "/iiif/3/{slide_id}/overlays/{overlay_id}/info.json",
        name="overlay_iiif_info",
    )
    async def overlay_iiif_info(
        slide_id: str,
        overlay_id: str,
        request: Request,
        style: str | None = None,
        revision: str | None = None,
    ) -> JSONResponse:
        overlay = overlay_for(slide_id, overlay_id)
        if revision is not None and revision != overlay.cache_token:
            raise HTTPException(status_code=404, detail="unknown overlay revision")
        _overlay_palette(overlay, style)
        _, metadata = await _run_while_connected(
            workers, request, metadata_for, slide_id
        )
        width, height = metadata.dimensions
        info_url = str(request.url)
        service_id = info_url.removesuffix("/info.json")
        return JSONResponse(
            {
                "@context": "http://iiif.io/api/image/3/context.json",
                "id": service_id,
                "type": "ImageService3",
                "protocol": "http://iiif.io/api/image",
                "profile": "level0",
                "width": width,
                "height": height,
                "tiles": [
                    {
                        "type": "Tile",
                        "width": tile_size,
                        "height": tile_size,
                        "scaleFactors": list(
                            iiif_scale_factors(metadata.dimensions, tile_size)
                        ),
                    }
                ],
                "preferredFormats": ["png"],
                "extraFormats": ["png"],
                "extraQualities": ["default"],
                "extraFeatures": ["regionByPx", "sizeByW", "sizeByWh"],
            },
            headers={"Cache-Control": cache_control},
        )

    @app.get(
        "/iiif/3/{slide_id}/overlays/{overlay_id}/revision/{revision}/style/{style}/{region}/{size}/"
        "{rotation}/{quality}.{image_format}",
        name="versioned_styled_overlay_iiif_image",
    )
    @app.get(
        "/iiif/3/{slide_id}/overlays/{overlay_id}/style/{style}/{region}/{size}/"
        "{rotation}/{quality}.{image_format}",
        name="styled_overlay_iiif_image",
    )
    @app.get(
        "/iiif/3/{slide_id}/overlays/{overlay_id}/{region}/{size}/"
        "{rotation}/{quality}.{image_format}",
        name="overlay_iiif_image",
    )
    async def overlay_iiif_image(
        slide_id: str,
        overlay_id: str,
        region: str,
        size: str,
        rotation: str,
        quality: str,
        image_format: str,
        request: Request,
        style: str | None = None,
        revision: str | None = None,
    ) -> Response:
        if rotation != "0" or quality != "default" or image_format != "png":
            raise HTTPException(
                status_code=400,
                detail="overlays support only rotation 0, default quality, and PNG",
            )
        overlay = overlay_for(slide_id, overlay_id)
        if revision is not None and revision != overlay.cache_token:
            raise HTTPException(status_code=404, detail="unknown overlay revision")
        palette = _overlay_palette(overlay, style)
        _, metadata = await _run_while_connected(
            workers, request, metadata_for, slide_id
        )
        try:
            parsed_region = _parse_region(region, metadata.dimensions)
            output_size = _parse_size(size, parsed_region)
            if output_size[0] * output_size[1] > max_output_pixels:
                raise ValueError("requested output exceeds max_output_pixels")
            if (
                parsed_region[0] >= metadata.dimensions[0]
                or parsed_region[1] >= metadata.dimensions[1]
            ):
                raise ValueError("region starts outside the image")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            encoded = await _run_while_connected(
                overlay_workers,
                request,
                overlay_renderer.render_region,
                overlay,
                metadata,
                parsed_region,
                output_size,
                palette,
            )
        except (OSError, RuntimeError, ValueError) as error:
            _LOGGER.exception(
                "Unable to render overlay %s for slide %s", overlay_id, slide_id
            )
            raise HTTPException(
                status_code=422,
                detail="registered overlay tile could not be rendered",
            ) from error
        headers = {"Cache-Control": cache_control, "ETag": encoded.etag}
        if request.headers.get("if-none-match") == encoded.etag:
            return Response(status_code=304, headers=headers)
        return Response(encoded.content, media_type=encoded.media_type, headers=headers)

    @app.get(
        "/iiif/3/{slide_id}/{region}/{size}/{rotation}/{quality}.{image_format}",
        name="iiif_image",
    )
    async def iiif_image(
        slide_id: str,
        region: str,
        size: str,
        rotation: str,
        quality: str,
        image_format: str,
        request: Request,
    ) -> Response:
        if rotation != "0" or quality != "default":
            raise HTTPException(
                status_code=400,
                detail="only rotation 0 and default quality are supported",
            )
        normalized_format = "jpg" if image_format == "jpeg" else image_format
        if normalized_format not in ("jpg", "png"):
            raise HTTPException(status_code=400, detail="format must be jpg or png")
        source, metadata = await _run_while_connected(
            workers, request, metadata_for, slide_id
        )
        try:
            parsed_region = _parse_region(region, metadata.dimensions)
            output_size = _parse_size(size, parsed_region)
            if not all(math.isfinite(value) for value in output_size):
                raise ValueError("invalid output size")
            if output_size[0] * output_size[1] > max_output_pixels:
                raise ValueError("requested output exceeds max_output_pixels")
            if (
                parsed_region[0] >= metadata.dimensions[0]
                or parsed_region[1] >= metadata.dimensions[1]
            ):
                raise ValueError("region starts outside the image")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            encoded = await _run_while_connected(
                workers,
                request,
                renderer.render_region,
                source.path,
                parsed_region,
                output_size,
                image_format=normalized_format,
                source_mpp=source.source_mpp,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            _LOGGER.exception("Unable to render tile for slide %s", slide_id)
            raise HTTPException(
                status_code=422,
                detail="registered slide tile could not be rendered",
            ) from error
        headers = {"Cache-Control": cache_control, "ETag": encoded.etag}
        if request.headers.get("if-none-match") == encoded.etag:
            return Response(status_code=304, headers=headers)
        return Response(encoded.content, media_type=encoded.media_type, headers=headers)

    return app
