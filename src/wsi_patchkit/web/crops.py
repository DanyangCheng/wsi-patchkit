"""Queued background jobs for server-side native-level crops."""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..tiles import ImageFormat, TileRenderer
from .registry import SlideSource

CropJobStatus = Literal["queued", "running", "completed", "failed"]
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _CropJob:
    job_id: str
    slide_id: str
    source: SlideSource
    region: tuple[int, int, int, int]
    level: int
    image_format: ImageFormat
    filename: str
    status: CropJobStatus = "queued"
    error: str | None = None

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
        return result


class CropJobQueue:
    """Render and save crop jobs outside their originating HTTP requests."""

    def __init__(
        self,
        renderer: TileRenderer,
        output_dir: str | Path,
        *,
        worker_count: int = 1,
    ) -> None:
        if worker_count < 1:
            raise ValueError("crop worker count must be positive")
        self._renderer = renderer
        self._output_dir = Path(output_dir)
        self._queue: queue.Queue[_CropJob | None] = queue.Queue()
        self._jobs: dict[str, _CropJob] = {}
        self._reserved_filenames: set[str] = set()
        self._lock = threading.Lock()
        self._closed = False
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
    ) -> dict[str, object]:
        job = _CropJob(
            uuid.uuid4().hex,
            slide_id,
            source,
            region,
            level,
            image_format,
            filename,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("crop queue is closed")
            if (
                filename in self._reserved_filenames
                or (self._output_dir / filename).exists()
            ):
                raise FileExistsError(filename)
            self._jobs[job.job_id] = job
            self._reserved_filenames.add(filename)
            self._queue.put(job)
            return job.public()

    def get(self, job_id: str, *, slide_id: str) -> dict[str, object]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.slide_id != slide_id:
                raise KeyError(job_id)
            return job.public()

    def delete_saved_crops(self, filenames: list[str]) -> None:
        """Delete saved crops unless a queued job currently owns a filename."""
        with self._lock:
            if any(filename in self._reserved_filenames for filename in filenames):
                raise RuntimeError("crop is still being rendered")
            paths = [self._output_dir / filename for filename in filenames]
            if any(path.is_symlink() or not path.is_file() for path in paths):
                raise FileNotFoundError("one or more crops do not exist")
            for path in paths:
                path.unlink()

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            with self._lock:
                job.status = "running"
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

    def _save(self, job: _CropJob) -> None:
        encoded = self._renderer.render_level_region(
            job.source.path,
            job.region,
            job.level,
            image_format=job.image_format,
            source_mpp=job.source.source_mpp,
        )
        self._output_dir.mkdir(parents=True, exist_ok=True)
        destination = self._output_dir / job.filename
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
            self._reserved_filenames.discard(job.filename)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for _ in self._threads:
                self._queue.put(None)
        for thread in self._threads:
            thread.join()
