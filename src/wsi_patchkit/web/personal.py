"""Personal workbench APIs. Every object reference is checked against its owner."""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from io import BytesIO
from pathlib import Path

import numpy as np
from fastapi import HTTPException, Request
from PIL import Image

from .access import namespace, private_directory
from .crops import crop_output


def polygon_points(value, dimensions):
    if value is None or value == []:
        return []
    if not isinstance(value, list) or not 3 <= len(value) <= 128:
        raise ValueError("polygon must contain 3-128 points")
    points = []
    for point in value:
        if (
            not isinstance(point, list)
            or len(point) != 2
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in point
            )
        ):
            raise ValueError("invalid polygon point")
        x, y = point
        if not 0 <= x <= dimensions[0] or not 0 <= y <= dimensions[1]:
            raise ValueError("polygon must be inside the selected level")
        points.append((x, y))
    area = sum(
        a[0] * b[1] - b[0] * a[1]
        for a, b in zip(points, points[1:] + points[:1], strict=True)
    )
    if abs(area) < 1:
        raise ValueError("polygon must have a non-zero area")
    return points


def inside_polygon(x, y, points):
    inside = False
    for a, b in zip(points, points[1:] + points[:1], strict=True):
        if (a[1] > y) != (b[1] > y) and x < (
            (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]
        ):
            inside = not inside
    return inside


def install_personal(
    app,
    *,
    store,
    registry,
    uploaded_sources,
    upload_directory,
    crop_directory,
    max_upload_bytes,
    list_slides,
    list_crops,
    metadata_for,
    metadata_workers,
    finish_upload,
    crop_jobs,
    save_crop,
    file_access,
    load_uploads,
):
    # Credentials and CSRF remain owned by the existing authentication layer.
    def owner(request):
        return namespace(request) or "local"

    def allowed(request, slide_id, *, include_trash=False):
        load_uploads()
        if (
            slide_id not in registry
            or (not include_trash and slide_id in app.state.trashed_slides)
            or (
                file_access
                and not file_access.allowed_slide(request.state.user, slide_id)
            )
        ):
            raise HTTPException(404, "unknown slide")
        return registry[slide_id]

    def text(value, limit=200):
        if not isinstance(value, str) or len(value) > limit:
            raise HTTPException(400, "invalid text field")
        return value.strip()

    def annotations(request, slide_id):
        return store.get(owner(request), "slide", slide_id) or {
            "id": slide_id,
            "project": "",
            "tags": [],
            "notes": "",
        }

    def owned_upload(request, slide_id):
        source = allowed(request, slide_id, include_trash=True)
        directory = private_directory(upload_directory, namespace(request))
        if source.path.parent != directory or source.path.is_symlink():
            raise HTTPException(403, "only your uploaded slides can be recycled")
        return source

    @app.get("/api/personal/overview")
    async def overview(request: Request):
        slides = await list_slides(request)
        crops = list_crops(request)
        jobs = crop_jobs.list(namespace(request))
        uploads = []
        for slide_id in registry:
            source = registry[slide_id]
            directory = private_directory(upload_directory, namespace(request))
            if source.path.parent == directory:
                uploads.append(source.path)
        return {
            "slides": len(slides),
            "uploads": len(uploads),
            "crops": len(crops),
            "upload_bytes": sum(p.stat().st_size for p in uploads if p.is_file()),
            "crop_bytes": sum(c["size_bytes"] for c in crops),
            "jobs": {
                s: sum(j["status"] == s for j in jobs)
                for s in ("queued", "running", "completed", "failed", "cancelled")
            },
            "max_upload_bytes": max_upload_bytes,
            "pending_bytes": sum(
                path.stat().st_size
                for record in store.list(owner(request), "upload")
                for _, path in [upload_record(request, record["id"])]
                if path.is_file()
            ),
            "recent_views": [
                record
                for record in store.list(owner(request), "view")
                if record["id"] in {s["id"] for s in slides}
            ][:5],
        }

    @app.get("/api/personal/slides")
    async def slides(request: Request):
        records = await list_slides(request)
        directory = private_directory(upload_directory, namespace(request))
        for record in records:
            source = registry[record["id"]]
            try:
                stat = source.path.stat()
                record.update(size_bytes=stat.st_size, modified_at=stat.st_mtime)
            except OSError:
                record.update(size_bytes=0, modified_at=0)
            record.update(annotations(request, record["id"]))
            record["uploaded"] = source.path.parent == directory
            upload_info = store.get(owner(request), "upload-info", record["id"])
            if upload_info:
                record["path"] = "上传切片/" + upload_info["display_name"]
        return records

    @app.post("/api/personal/slides/{slide_id}/view")
    async def viewed(slide_id: str, request: Request):
        allowed(request, slide_id)
        return store.put(
            owner(request), "view", slide_id, {"id": slide_id, "viewed_at": time.time()}
        )

    @app.put("/api/personal/slides/{slide_id}")
    async def update_slide(slide_id: str, payload: dict, request: Request):
        allowed(request, slide_id)
        tags = payload.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 30:
            raise HTTPException(400, "invalid tags")
        record = {
            "id": slide_id,
            "project": text(payload.get("project", "")),
            "tags": list(dict.fromkeys(text(t, 64) for t in tags if t)),
            "notes": text(payload.get("notes", ""), 4000),
        }
        return store.put(owner(request), "slide", slide_id, record)

    @app.get("/api/personal/account")
    async def account(request: Request):
        user = getattr(request.state, "user", None)
        return {
            "enabled": user is not None,
            "user": user,
            "profile": store.get(owner(request), "profile", "self") or {},
        }

    @app.put("/api/personal/account")
    async def update_account(payload: dict, request: Request):
        if not getattr(request.state, "user", None):
            raise HTTPException(409, "authentication is disabled")
        record = {
            "display_name": text(payload.get("display_name", ""), 80),
            "notes": text(payload.get("notes", ""), 1000),
        }
        return store.put(owner(request), "profile", "self", record)

    @app.get("/api/personal/trash")
    async def trash(request: Request):
        return store.list(owner(request), "trash")

    @app.post("/api/personal/slides/{slide_id}/trash")
    async def recycle(slide_id: str, request: Request):
        owned_upload(request, slide_id)
        if any(
            j["slide_id"] == slide_id and j["status"] in {"queued", "running"}
            for j in crop_jobs.list(namespace(request))
        ):
            raise HTTPException(409, "请等待关联裁剪任务结束后再移入回收站")
        record = {
            "id": slide_id,
            "deleted_at": time.time(),
            "name": annotations(request, slide_id).get("project") or slide_id,
            "related_jobs": sum(
                j["slide_id"] == slide_id for j in crop_jobs.list(namespace(request))
            ),
        }
        store.put(owner(request), "trash", slide_id, record)
        app.state.trashed_slides.add(slide_id)
        return record

    @app.post("/api/personal/slides/{slide_id}/restore")
    async def restore(slide_id: str, request: Request):
        if not store.get(owner(request), "trash", slide_id):
            raise HTTPException(404, "unknown recycled slide")
        owned_upload(request, slide_id)
        store.delete(owner(request), "trash", slide_id)
        app.state.trashed_slides.discard(slide_id)
        return {"ok": True}

    @app.get("/api/personal/jobs")
    async def jobs(request: Request):
        return crop_jobs.list(namespace(request))

    @app.post("/api/personal/jobs/{job_id}/cancel")
    async def cancel(job_id: str, request: Request):
        try:
            crop_jobs.cancel(job_id, namespace(request))
        except KeyError as error:
            raise HTTPException(404, "unknown job") from error
        except RuntimeError as error:
            raise HTTPException(409, "只能取消尚未开始的任务") from error
        return {"ok": True}

    @app.post("/api/personal/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str, request: Request):
        job = store.get(owner(request), "job", job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        if job["status"] not in {"failed", "cancelled"}:
            raise HTTPException(409, "only failed or cancelled jobs can be retried")
        allowed(request, job["slide_id"])
        return await save_crop(
            job["slide_id"],
            {
                **job["region"],
                "level": job["level"],
                "format": job["format"],
                "target_mpp": job.get("target_mpp", 0),
            },
            request,
        )

    @app.get("/api/personal/manifest")
    async def manifest(request: Request):
        available = [c["filename"] for c in list_crops(request)]
        return {
            "version": 1,
            "coordinate_system": "selected_native_level_pixels",
            "crops": crop_jobs.provenance_for(available, namespace(request)),
        }

    @app.get("/api/personal/templates")
    async def templates(request: Request):
        return store.list(owner(request), "template")

    @app.put("/api/personal/templates/{template_id}")
    async def template(template_id: str, payload: dict, request: Request):
        if len(template_id) > 64 or not template_id.isalnum():
            raise HTTPException(400, "invalid template id")
        # Store only known scalar parameters, never paths or executable values.
        result = {"id": template_id, "name": text(payload.get("name", ""), 80)}
        for key in (
            "level",
            "tile_width",
            "tile_height",
            "stride",
            "edge",
            "format",
            "minimum_tissue",
            "target_mpp",
        ):
            value = payload.get(key, 0) if key == "target_mpp" else payload.get(key)
            if not isinstance(value, (int, float, str)) or isinstance(value, bool):
                raise HTTPException(400, "invalid template parameter")
            if isinstance(value, str):
                text(value, 30)
            if isinstance(value, float) and not math.isfinite(value):
                raise HTTPException(400, "invalid template parameter")
            result[key] = value
        return store.put(owner(request), "template", template_id, result)

    @app.delete("/api/personal/templates/{template_id}")
    async def delete_template(template_id: str, request: Request):
        store.delete(owner(request), "template", template_id)
        return {"ok": True}

    @app.get("/api/personal/slides/{slide_id}/regions")
    async def regions(slide_id: str, request: Request):
        allowed(request, slide_id)
        return store.list(owner(request), "region:" + slide_id)

    @app.put("/api/personal/slides/{slide_id}/regions/{region_id}")
    async def save_region(
        slide_id: str, region_id: str, payload: dict, request: Request
    ):
        allowed(request, slide_id)
        if not region_id.isalnum() or len(region_id) > 64:
            raise HTTPException(400, "invalid region id")
        _, metadata = await metadata_workers.run(metadata_for, slide_id)
        from .app import _crop_integer, _crop_level

        try:
            level = _crop_level(payload, metadata)
            values = [_crop_integer(payload, k) for k in ("x", "y", "width", "height")]
            x, y, w, h = values
            size = metadata.levels[level].dimensions
            if min(x, y) < 0 or min(w, h) < 1 or x + w > size[0] or y + h > size[1]:
                raise ValueError("region must be inside the selected level")
            polygon = polygon_points(payload.get("polygon"), size)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        result = {
            "id": region_id,
            "name": text(payload.get("name", "选区"), 80),
            "notes": text(payload.get("notes", ""), 1000),
            "level": level,
            "polygon": polygon,
            **dict(zip(("x", "y", "width", "height"), values, strict=True)),
        }
        return store.put(owner(request), "region:" + slide_id, region_id, result)

    @app.delete("/api/personal/slides/{slide_id}/regions/{region_id}")
    async def delete_region(slide_id: str, region_id: str, request: Request):
        allowed(request, slide_id)
        store.delete(owner(request), "region:" + slide_id, region_id)
        return {"ok": True}

    def plan(slide_id, payload):
        from .app import _crop_integer, _crop_level

        source, metadata = metadata_for(slide_id)
        try:
            level = _crop_level(payload, metadata)
            dimensions = metadata.levels[level].dimensions
            polygon = polygon_points(payload.get("polygon"), dimensions)
            x, y = (_crop_integer(payload, k) for k in ("x", "y"))
            w, h = (_crop_integer(payload, k) for k in ("width", "height"))
            tw, th = (_crop_integer(payload, k) for k in ("tile_width", "tile_height"))
            stride = _crop_integer(payload, "stride")
            fraction = payload.get("minimum_tissue", 0)
            if (
                isinstance(fraction, bool)
                or not isinstance(fraction, (int, float))
                or not 0 <= fraction <= 1
            ):
                raise ValueError("minimum_tissue must be between 0 and 1")
            if min(x, y) < 0 or min(w, h, tw, th, stride) < 1:
                raise ValueError("invalid grid dimensions")
            if x + w > dimensions[0] or y + h > dimensions[1]:
                raise ValueError("grid region must be inside the selected level")
            if payload.get("edge", "skip") not in ("skip", "clip"):
                raise ValueError("edge must be skip or clip")
            if payload.get("format", "png") not in ("png", "jpg"):
                raise ValueError("format must be png or jpg")
            if math.ceil(w / stride) * math.ceil(h / stride) > 512:
                raise ValueError("单次最多预览或提交 512 个图块，请缩小区域或增大步长")
            result = []
            estimated_bytes = 0
            mask = None
            if fraction:
                scale = min(1, 1024 / max(metadata.dimensions))
                encoded = app.state.tile_renderer.render_region(
                    source.path,
                    (0, 0, *metadata.dimensions),
                    (
                        max(1, round(metadata.dimensions[0] * scale)),
                        max(1, round(metadata.dimensions[1] * scale)),
                    ),
                    source_mpp=source.source_mpp,
                )
                array = np.asarray(Image.open(BytesIO(encoded.content)).convert("RGB"))
                rgb = array.astype(np.int16)
                mask = (
                    (rgb.max(2) - rgb.min(2) > 15)
                    & (rgb.min(2) < 235)
                    & (rgb.max(2) > 20)
                )
            for yy in range(y, y + h, stride):
                for xx in range(x, x + w, stride):
                    width, height = min(tw, x + w - xx), min(th, y + h - yy)
                    if payload.get("edge", "skip") == "skip" and (
                        width < tw or height < th
                    ):
                        continue
                    if polygon and not inside_polygon(
                        xx + width / 2, yy + height / 2, polygon
                    ):
                        continue
                    _, output_size, _ = crop_output(
                        metadata,
                        (xx, yy, width, height),
                        level,
                        payload.get("target_mpp"),
                    )
                    if (
                        output_size[0] * output_size[1]
                        > app.state.tile_renderer.max_output_pixels
                    ):
                        raise ValueError("图块尺寸超过服务输出上限")
                    if mask is not None:
                        mh, mw = mask.shape
                        mx0, my0 = (
                            int(xx * mw / dimensions[0]),
                            int(yy * mh / dimensions[1]),
                        )
                        mx1 = max(mx0 + 1, math.ceil((xx + width) * mw / dimensions[0]))
                        my1 = max(
                            my0 + 1, math.ceil((yy + height) * mh / dimensions[1])
                        )
                        if float(mask[my0:my1, mx0:mx1].mean()) < fraction:
                            continue
                    result.append({"x": xx, "y": yy, "width": width, "height": height})
                    estimated_bytes += output_size[0] * output_size[1] * 3
            return {
                "regions": result,
                "count": len(result),
                "level": level,
                "mpp": metadata.levels[level].mpp,
                "estimated_raw_bytes": estimated_bytes,
                "coordinate_system": "selected_native_level_pixels",
            }
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/api/personal/slides/{slide_id}/grid-preview")
    async def preview(slide_id: str, payload: dict, request: Request):
        allowed(request, slide_id)
        return await metadata_workers.run(plan, slide_id, payload)

    @app.post("/api/personal/slides/{slide_id}/grid", status_code=202)
    async def grid(slide_id: str, payload: dict, request: Request):
        allowed(request, slide_id)
        planned = await metadata_workers.run(plan, slide_id, payload)
        # All geometry is validated before the first job is queued.
        submitted = []
        for region in planned["regions"]:
            response = await save_crop(
                slide_id,
                {
                    **region,
                    "level": planned["level"],
                    "format": payload.get("format", "png"),
                    "target_mpp": payload.get("target_mpp", 0),
                },
                request,
            )
            submitted.append(json.loads(response.body))
        return {"jobs": submitted, "count": len(submitted)}

    # Upload sessions use server-generated paths and an exact append offset.
    # Chunk writes and finalization share a lock to reject concurrent appends.
    upload_locks = {}

    def upload_record(request, upload_id):
        record = store.get(owner(request), "upload", upload_id)
        if not record:
            raise HTTPException(404, "unknown upload session")
        directory = private_directory(upload_directory, namespace(request))
        suffix = Path(record.get("safe_name", record["filename"])).suffix
        path = directory / (".upload-" + record["id"] + suffix)
        if path.is_symlink():
            raise HTTPException(404, "unknown upload session")
        return record, path

    @app.get("/api/personal/uploads")
    async def pending_uploads(request: Request):
        records = store.list(owner(request), "upload")
        for record in records:
            _, path = upload_record(request, record["id"])
            record["offset"] = path.stat().st_size if path.exists() else 0
        return records

    @app.post("/api/personal/uploads", status_code=201)
    async def begin_upload(payload: dict, request: Request):
        from .app import _upload_name

        try:
            original_name = text(payload.get("filename", ""), 255)
            filename = _upload_name(original_name)
        except (ValueError, TypeError) as error:
            raise HTTPException(400, "invalid slide filename") from error
        size = payload.get("size")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= max_upload_bytes
        ):
            raise HTTPException(413, "invalid upload size")
        if len(store.list(owner(request), "upload")) >= 20:
            raise HTTPException(409, "请先完成或移除已有的上传任务（最多 20 个）")
        record = {
            "id": uuid.uuid4().hex,
            "filename": original_name,
            "safe_name": filename,
            "size": size,
            "offset": 0,
            "created_at": time.time(),
        }
        _, path = (
            record,
            private_directory(upload_directory, namespace(request))
            / (".upload-" + record["id"] + Path(filename).suffix),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=False)
        return store.put(owner(request), "upload", record["id"], record)

    @app.put("/api/personal/uploads/{upload_id}")
    async def append_upload(upload_id: str, request: Request, offset: int):
        async with upload_locks.setdefault(upload_id, asyncio.Lock()):
            record, path = upload_record(request, upload_id)
            current = path.stat().st_size if path.exists() else 0
            if offset != current:
                raise HTTPException(
                    409, "upload offset changed; refresh before resuming"
                )
            chunk = bytearray()
            async for part in request.stream():
                chunk.extend(part)
                if len(chunk) > 8 * 1024**2 or current + len(chunk) > record["size"]:
                    raise HTTPException(413, "chunk exceeds size limit")
            if not chunk:
                raise HTTPException(400, "empty upload chunk")
            with path.open("ab") as output:
                output.write(chunk)
            return {"offset": current + len(chunk)}

    @app.post("/api/personal/uploads/{upload_id}/complete", status_code=201)
    async def complete_upload(upload_id: str, request: Request):
        async with upload_locks.setdefault(upload_id, asyncio.Lock()):
            record, path = upload_record(request, upload_id)
            if not path.is_file() or path.stat().st_size != record["size"]:
                raise HTTPException(409, "upload is incomplete")
            result = await finish_upload(
                request,
                path,
                record.get("safe_name", record["filename"]),
                record["filename"],
            )
            store.put(
                owner(request),
                "upload-info",
                result["id"],
                {"display_name": record["filename"]},
            )
            store.delete(owner(request), "upload", upload_id)
            return result

    @app.delete("/api/personal/uploads/{upload_id}")
    async def discard_upload(upload_id: str, request: Request):
        async with upload_locks.setdefault(upload_id, asyncio.Lock()):
            _, path = upload_record(request, upload_id)
            path.unlink(missing_ok=True)
            store.delete(owner(request), "upload", upload_id)
        return {"ok": True}
