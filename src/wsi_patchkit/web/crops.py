"""Durable background crop records and native or physical-resolution outputs."""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..tiles import ImageFormat, TileRenderer
from ..types import SlideMetadata
from .personal_store import PersonalStore
from .registry import SlideSource

CropJobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
_LOGGER = logging.getLogger(__name__)


def crop_output(metadata: SlideMetadata, region, level: int, target_mpp):
    """Map a native-level rectangle to a physical output grid."""
    if target_mpp is None or (not isinstance(target_mpp, bool) and target_mpp == 0):
        return None, region[2:], None
    if (
        isinstance(target_mpp, bool)
        or not isinstance(target_mpp, (int, float))
        or not math.isfinite(target_mpp)
        or not 0.001 <= target_mpp <= 1000
    ):
        raise ValueError("target_mpp must be 0 or between 0.001 and 1000")
    if metadata.mpp is None:
        raise ValueError("切片 MPP 未知，无法按目标 MPP 裁剪，请选择原生层级输出")
    x, y, width, height = region
    dx, dy = metadata.levels[level].downsample
    x0, y0 = round(x * dx), round(y * dy)
    x1 = min(metadata.dimensions[0], round((x + width) * dx))
    y1 = min(metadata.dimensions[1], round((y + height) * dy))
    base_region = x0, y0, x1 - x0, y1 - y0
    size = (
        max(1, round((x1 - x0) * metadata.mpp[0] / target_mpp)),
        max(1, round((y1 - y0) * metadata.mpp[1] / target_mpp)),
    )
    return float(target_mpp), size, base_region


@dataclass(slots=True)
class _CropJob:
    job_id: str
    slide_id: str
    source: SlideSource
    region: tuple[int, int, int, int]
    level: int
    image_format: ImageFormat
    filename: str
    namespace: str | None = None
    status: CropJobStatus = "queued"
    error: str | None = None
    provenance: dict | None = None
    target_mpp: float | None = None

    def public(self) -> dict[str, object]:
        x, y, width, height = self.region
        result: dict[str, object] = {
            "job_id": self.job_id,
            "status": self.status,
            "slide_id": self.slide_id,
            "filename": self.filename,
            "format": self.image_format,
            "level": self.level,
            "region": {"x": x, "y": y, "width": width, "height": height},
        }
        if self.error is not None:
            result["error"] = self.error
        if self.target_mpp is not None:
            result["target_mpp"] = self.target_mpp
        return result


class CropJobQueue:
    """Render and save crop jobs outside their originating HTTP requests."""

    def __init__(
        self,
        renderer: TileRenderer,
        output_dir: str | Path,
        *,
        worker_count: int = 1,
        store: PersonalStore | None = None,
        history_days: int = 90,
        history_limit: int = 5000,
    ) -> None:
        if worker_count < 1:
            raise ValueError("crop worker count must be positive")
        if history_days < 1 or history_limit < 1:
            raise ValueError("crop history limits must be positive")
        self._renderer = renderer
        self._store = store
        self._history_days = history_days
        self._history_limit = history_limit
        self._cleanup_stop = threading.Event()
        self._output_dir = Path(output_dir)
        self._queue: queue.Queue[_CropJob | None] = queue.Queue()
        self._jobs: dict[str, _CropJob] = {}
        self._reserved_filenames: set[tuple[str | None, str]] = set()
        self._lock = threading.Lock()
        self._closed = False
        if store is not None:
            # Interrupted work stays visible and can be explicitly retried.
            with store.lock:
                rows = store.db.execute(
                    "SELECT owner, key, data FROM personal_records WHERE kind='job' "
                    "AND json_extract(data, '$.status') IN ('queued','running')"
                ).fetchall()
            for owner, key, encoded in rows:
                record = json.loads(encoded)
                if record["status"] in {"queued", "running"}:
                    record.update(
                        status="failed",
                        error="服务重启中断了任务，请重试",
                        updated_at=time.time(),
                    )
                    store.put(owner, "job", key, record)
            self.cleanup_history()
        self._threads = tuple(
            threading.Thread(
                target=self._worker,
                name=f"wsi-crop-{index}",
                daemon=True,
            )
            for index in range(worker_count)
        )
        for thread in self._threads:
            thread.start()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, name="wsi-crop-history", daemon=True
        )
        self._cleanup_thread.start()

    def cleanup_history(self) -> int:
        if not self._store:
            return 0
        return self._store.prune_jobs(
            cutoff=time.time() - self._history_days * 86400,
            maximum=self._history_limit,
        )

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(86400):
            try:
                self.cleanup_history()
            except Exception:
                _LOGGER.exception("Unable to clean crop history")

    @property
    def worker_count(self) -> int:
        return len(self._threads)

    def submit(
        self,
        *,
        slide_id: str,
        source: SlideSource,
        region: tuple[int, int, int, int],
        level: int,
        image_format: ImageFormat,
        filename: str,
        namespace: str | None = None,
        provenance: dict | None = None,
        target_mpp: float | None = None,
    ) -> dict[str, object]:
        job = _CropJob(
            uuid.uuid4().hex,
            slide_id,
            source,
            region,
            level,
            image_format,
            filename,
            namespace=namespace,
            provenance=provenance,
            target_mpp=target_mpp,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("crop queue is closed")
            if (namespace, filename) in self._reserved_filenames or (
                self._directory(namespace) / filename
            ).exists():
                raise FileExistsError(filename)
            self._jobs[job.job_id] = job
            self._reserved_filenames.add((namespace, filename))
            self._record(job)
            self._queue.put(job)
            return job.public()

    def get(
        self, job_id: str, *, slide_id: str, namespace: str | None = None
    ) -> dict[str, object]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.slide_id != slide_id or job.namespace != namespace:
                if self._store:
                    record = self._store.get(namespace or "local", "job", job_id)
                    if record and record["slide_id"] == slide_id:
                        return record
                raise KeyError(job_id)
            return job.public()

    def _record(self, job: _CropJob) -> None:
        if self._store:
            owner = job.namespace or "local"
            previous = self._store.get(owner, "job", job.job_id) or {}
            self._store.put(
                owner,
                "job",
                job.job_id,
                {
                    **job.public(),
                    **(job.provenance or {}),
                    "created_at": previous.get("created_at", time.time()),
                    "updated_at": time.time(),
                },
            )

    def list(self, namespace: str | None = None) -> list[dict]:
        if self._store:
            return self._store.list(namespace or "local", "job")
        with self._lock:
            return [j.public() for j in self._jobs.values() if j.namespace == namespace]

    def provenance_for(self, filenames: list[str], namespace: str | None = None):
        if self._store:
            return [
                self._store.get(namespace or "local", "crop", name)
                or {"filename": name, "provenance": "unknown_legacy_file"}
                for name in filenames
            ]
        latest = {}
        for job in self.list(namespace):
            if job["status"] == "completed":
                latest.setdefault(job["filename"], job)
        return [
            latest.get(name) or {"filename": name, "provenance": "unknown_legacy_file"}
            for name in filenames
        ]

    def cancel(self, job_id: str, namespace: str | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.namespace != namespace:
                if self._store and self._store.get(namespace or "local", "job", job_id):
                    raise RuntimeError("only queued jobs can be cancelled")
                raise KeyError(job_id)
            if job.status != "queued":
                raise RuntimeError("only queued jobs can be cancelled")
            job.status = "cancelled"
            self._reserved_filenames.discard((namespace, job.filename))
            self._record(job)
            if self._store:
                self._jobs.pop(job.job_id, None)

    def delete_saved_crops(
        self, filenames: list[str], *, namespace: str | None = None
    ) -> None:
        """Delete saved crops unless a queued job currently owns a filename."""
        with self._lock:
            if any(
                (namespace, filename) in self._reserved_filenames
                for filename in filenames
            ):
                raise RuntimeError("crop is still being rendered")
            paths = [self._directory(namespace) / filename for filename in filenames]
            if any(path.is_symlink() or not path.is_file() for path in paths):
                raise FileNotFoundError("one or more crops do not exist")
            for path in paths:
                path.unlink()
                if self._store:
                    self._store.delete(namespace or "local", "crop", path.name)

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            with self._lock:
                if job.status == "cancelled":
                    continue
                job.status = "running"
                self._record(job)
            try:
                self._save(job)
            except FileExistsError:
                self._finish(job, "failed", "a crop with this filename already exists")
            except (ImportError, RuntimeError, ValueError):
                _LOGGER.exception("Unable to render crop for slide %s", job.slide_id)
                self._finish(job, "failed", "crop could not be rendered")
            except OSError:
                _LOGGER.exception("Unable to save crop for slide %s", job.slide_id)
                self._finish(job, "failed", "crop could not be saved")
            except Exception:
                _LOGGER.exception("Unexpected crop failure for slide %s", job.slide_id)
                self._finish(job, "failed", "crop failed unexpectedly")
            else:
                self._finish(job, "completed")

    def _directory(self, namespace: str | None) -> Path:
        if namespace is None:
            return self._output_dir
        if len(namespace) != 32 or any(c not in "0123456789abcdef" for c in namespace):
            raise ValueError("invalid crop namespace")
        directory = self._output_dir / namespace
        if directory.is_symlink() or not directory.resolve().is_relative_to(
            self._output_dir.resolve()
        ):
            raise ValueError("invalid crop directory")
        return directory

    def _save(self, job: _CropJob) -> None:
        if job.target_mpp is None:
            encoded = self._renderer.render_level_region(
                job.source.path,
                job.region,
                job.level,
                image_format=job.image_format,
                source_mpp=job.source.source_mpp,
            )
        else:
            metadata = self._renderer.metadata(
                job.source.path, source_mpp=job.source.source_mpp
            )
            _, size, base_region = crop_output(
                metadata, job.region, job.level, job.target_mpp
            )
            encoded = self._renderer.render_region(
                job.source.path,
                base_region,
                size,
                image_format=job.image_format,
                source_mpp=job.source.source_mpp,
            )
        directory = self._directory(job.namespace)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / job.filename
        with destination.open("xb") as output:
            output.write(encoded.content)

    def _finish(
        self,
        job: _CropJob,
        status: Literal["completed", "failed"],
        error: str | None = None,
    ) -> None:
        with self._lock:
            job.status = status
            job.error = error
            self._reserved_filenames.discard((job.namespace, job.filename))
            self._record(job)
            if self._store:
                self._jobs.pop(job.job_id, None)

    def close(self) -> None:
        self._cleanup_stop.set()
        self._cleanup_thread.join()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for _ in self._threads:
                self._queue.put(None)
        for thread in self._threads:
            thread.join()
