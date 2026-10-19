"""Validated detection records with a disk-backed, local point index."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from ..io import TiffReader
from ..types import SlideMetadata
from .overlays import BinaryRasterSource, OverlayClass, OverlayGeometry, _manifest_path
from .registry import manifest_slide_id, valid_public_id


def numbers(value: object, length: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{name} must contain {length} finite numbers")
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for v in value
    ):
        raise ValueError(f"{name} must contain {length} finite numbers")
    return tuple(float(v) for v in value)


def bounds(value: object) -> tuple[float, ...]:
    box = numbers(value, 4, "bounds")
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError("bounds must have positive area")
    return box


def text_field(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON numbers must be finite")
    return result


def _invalid_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _load_json(value):
    return json.loads(
        value, parse_float=_finite_float, parse_constant=_invalid_constant
    )


class DetectionOverlay:
    """Immutable imported revision; JSONL is read once, queries use SQLite R-tree."""

    def __init__(
        self, path: str | Path, *, slide_id_aliases: Mapping[str, str] | None = None
    ) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="patchkit-detections-")
        self._db = sqlite3.connect(
            str(Path(self._directory.name) / "points.sqlite"), check_same_thread=False
        )
        self._lock = threading.Lock()
        self._validated: set[tuple] = set()
        try:
            self._load(Path(path).resolve(), slide_id_aliases)
        except Exception:
            self.close()
            raise

    def _load(self, path: Path, slide_id_aliases: Mapping[str, str] | None) -> None:
        payload = _load_json(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("detection manifest must be an object")
        if payload.get("schema") != "wsi-patchkit-detection/v1":
            raise ValueError("unsupported detection schema")
        if payload.get("encoding") != "objects":
            raise ValueError("detection encoding must be objects")
        if payload.get("coordinate_system") != "slide_um":
            raise ValueError("detection coordinates must be slide_um")
        self.slide_id = manifest_slide_id(payload.get("slide_id"), slide_id_aliases)
        self.overlay_id = valid_public_id(payload.get("overlay_id"), name="overlay ID")
        self.revision = text_field(payload.get("revision"), "revision")
        self.display_name = text_field(
            payload.get("display_name", self.overlay_id), "display_name"
        )
        self.default_opacity = payload.get("default_opacity", 0.8)
        numbers([self.default_opacity], 1, "default_opacity")
        if not 0 <= self.default_opacity <= 1:
            raise ValueError("default_opacity must be in [0,1]")
        self.initially_visible = payload.get("initially_visible", False)
        if not isinstance(self.initially_visible, bool):
            raise ValueError("initially_visible must be boolean")
        raw_classes = payload.get("classes")
        if not isinstance(raw_classes, list) or not raw_classes:
            raise ValueError("classes must be a non-empty list")
        self.classes = tuple(
            OverlayClass(c.get("id"), c.get("name"), c.get("rgba", ()))
            for c in raw_classes
            if isinstance(c, dict)
        )
        if len(self.classes) != len(raw_classes):
            raise ValueError("classes must contain objects")
        class_ids = {c.id for c in self.classes}
        if len(class_ids) != len(self.classes):
            raise ValueError("class IDs must be unique")
        provenance = payload.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError("provenance is required")
        for name in ("model_name", "model_revision", "source_mpp_source"):
            text_field(provenance.get(name), name)
        self.source_mpp = numbers(provenance.get("source_mpp"), 2, "source_mpp")
        if min(self.source_mpp) <= 0:
            raise ValueError("source_mpp must be positive")
        post = provenance.get("postprocessing")
        if not isinstance(post, dict) or "score_threshold" not in post:
            raise ValueError("postprocessing must declare score_threshold")
        text_field(post.get("deduplication"), "deduplication")
        if post["score_threshold"] is not None:
            threshold = numbers([post["score_threshold"]], 1, "score_threshold")[0]
            if not 0 <= threshold <= 1:
                raise ValueError("score_threshold must be in [0,1]")
        coverage = payload.get("coverage")
        if not isinstance(coverage, dict):
            raise ValueError("coverage is required")
        self.coverage_type = coverage.get("type")
        self.coverage_boxes = ()
        self.coverage_geometry = None
        coverage_path = None
        if self.coverage_type == "rectangles":
            if not isinstance(coverage.get("bounds_um"), list):
                raise ValueError("coverage bounds_um must be a list")
            self.coverage_boxes = tuple(bounds(b) for b in coverage["bounds_um"])
        elif self.coverage_type == "raster":
            coverage_path = _manifest_path(
                path.parent, coverage.get("path"), name="coverage"
            )
            source = BinaryRasterSource(coverage_path)
            dimensions = coverage.get("dimensions")
            if (
                not isinstance(dimensions, list)
                or len(dimensions) != 2
                or any(
                    isinstance(v, bool) or not isinstance(v, int) for v in dimensions
                )
            ):
                raise ValueError("coverage dimensions must contain two integers")
            self.coverage_geometry = OverlayGeometry(
                numbers(coverage.get("origin_um"), 2, "origin_um"),
                numbers(coverage.get("mpp"), 2, "mpp"),
                tuple(dimensions),
            )
            if source.dimensions != self.coverage_geometry.dimensions:
                raise ValueError("coverage dimensions do not match TIFF")
        elif self.coverage_type != "full_slide":
            raise ValueError("unsupported coverage type")
        objects = payload.get("objects")
        if not isinstance(objects, dict) or objects.get("format") != "jsonl":
            raise ValueError("objects must declare jsonl format")
        self.count = objects.get("count")
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 0
        ):
            raise ValueError("objects count must be a non-negative integer")
        objects_path = _manifest_path(path.parent, objects.get("path"), name="objects")
        self._db.executescript("""
            CREATE TABLE points (seq INTEGER PRIMARY KEY, id TEXT UNIQUE,
                class_id INTEGER, x REAL, y REAL, score REAL);
            CREATE VIRTUAL TABLE spatial USING rtree(seq, x0, x1, y0, y1);
            CREATE TABLE coverage_bounds (seq INTEGER PRIMARY KEY,
                x0 REAL, x1 REAL, y0 REAL, y1 REAL);
            CREATE VIRTUAL TABLE coverage_index USING rtree(seq, x0, x1, y0, y1);
        """)
        for seq, (x0, y0, x1, y1) in enumerate(self.coverage_boxes):
            values = (seq, x0, x1, y0, y1)
            self._db.execute("INSERT INTO coverage_bounds VALUES (?,?,?,?,?)", values)
            self._db.execute("INSERT INTO coverage_index VALUES (?,?,?,?,?)", values)
        self.extents = [math.inf, math.inf, -math.inf, -math.inf]
        digest = hashlib.sha256(path.read_bytes())
        reader = TiffReader() if coverage_path else None
        coverage_blocks = OrderedDict()
        try:
            if reader:
                # Validate the complete coverage without allocating a whole-slide array.
                w, h = self.coverage_geometry.dimensions
                for y in range(0, h, 512):
                    for x in range(0, w, 512):
                        block = reader.read_region(
                            coverage_path, (x, y), 0, (min(512, w - x), min(512, h - y))
                        )
                        if not np.all((block == 0) | (block == 1)):
                            raise ValueError("coverage TIFF must contain only 0/1")
                with coverage_path.open("rb") as coverage_file:
                    while chunk := coverage_file.read(1024 * 1024):
                        digest.update(chunk)
            count = 0
            with objects_path.open("rb") as records:
                for line in records:
                    digest.update(line)
                    if not line.strip():
                        continue
                    count += 1
                    try:
                        record = _load_json(line)
                        if not isinstance(record, dict):
                            raise ValueError("record must be an object")
                        object_id = text_field(record.get("id"), "object id")
                        class_id = record.get("class_id")
                        if (
                            isinstance(class_id, bool)
                            or not isinstance(class_id, int)
                            or class_id not in class_ids
                        ):
                            raise ValueError("unknown class_id")
                        center = (
                            numbers(record["center_um"], 2, "center_um")
                            if "center_um" in record
                            else None
                        )
                        box = bounds(record["bbox_um"]) if "bbox_um" in record else None
                        if center is None and box is None:
                            raise ValueError("record needs center_um or bbox_um")
                        if center:
                            text_field(
                                provenance.get("center_definition"), "center_definition"
                            )
                        if (
                            center
                            and box
                            and not (
                                box[0] <= center[0] < box[2]
                                and box[1] <= center[1] < box[3]
                            )
                        ):
                            raise ValueError("center must be inside bbox")
                        if "outline_um" in record:
                            self._validate_outline(record["outline_um"], box)
                        if "properties" in record and not isinstance(
                            record["properties"], dict
                        ):
                            raise ValueError("properties must be an object")
                        score = None
                        if "score" in record:
                            score = numbers([record["score"]], 1, "score")[0]
                            if not 0 <= score <= 1:
                                raise ValueError("score must be in [0,1]")
                            text_field(
                                provenance.get("score_definition"), "score_definition"
                            )
                        x, y = center or ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                        if (
                            self.coverage_type == "rectangles"
                            and not self._db.execute(
                                """SELECT 1 FROM coverage_index i
                            JOIN coverage_bounds b ON b.seq=i.seq
                            WHERE i.x0<=? AND i.x1>=? AND i.y0<=? AND i.y1>=?
                              AND b.x0<=? AND b.x1>? AND b.y0<=? AND b.y1>?
                            LIMIT 1""",
                                (x, x, y, y, x, x, y, y),
                            ).fetchone()
                        ):
                            raise ValueError("point outside coverage")
                        if reader:
                            g = self.coverage_geometry
                            col = math.floor((x - g.origin_um[0]) / g.mpp[0])
                            row = math.floor((y - g.origin_um[1]) / g.mpp[1])
                            if not (
                                0 <= col < g.dimensions[0]
                                and 0 <= row < g.dimensions[1]
                            ):
                                raise ValueError("point outside coverage")
                            tile = (col // 512 * 512, row // 512 * 512)
                            block = coverage_blocks.get(tile)
                            if block is None:
                                block = reader.read_region(
                                    coverage_path,
                                    tile,
                                    0,
                                    (
                                        min(512, g.dimensions[0] - tile[0]),
                                        min(512, g.dimensions[1] - tile[1]),
                                    ),
                                )[..., 0]
                                coverage_blocks[tile] = block
                                if len(coverage_blocks) > 64:
                                    coverage_blocks.popitem(last=False)
                            coverage_blocks.move_to_end(tile)
                            if block[row - tile[1], col - tile[0]] != 1:
                                raise ValueError("point outside coverage")
                        extent = box or (x, y, x, y)
                        self.extents = [
                            min(self.extents[0], extent[0]),
                            min(self.extents[1], extent[1]),
                            max(self.extents[2], extent[2]),
                            max(self.extents[3], extent[3]),
                        ]
                        self._db.execute(
                            "INSERT INTO points VALUES (?,?,?,?,?,?)",
                            (count, object_id, class_id, x, y, score),
                        )
                        self._db.execute(
                            "INSERT INTO spatial VALUES (?,?,?,?,?)",
                            (count, x, x, y, y),
                        )
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        sqlite3.IntegrityError,
                    ) as error:
                        raise ValueError(
                            f"invalid detection record {count}: {error}"
                        ) from error
            if count != self.count:
                raise ValueError("objects count does not match JSONL")
            self._db.commit()
        finally:
            if reader:
                reader.close()
        self.cache_token = digest.hexdigest()[:16]

    @staticmethod
    def _validate_outline(outline, box) -> None:
        if (
            box is None
            or not isinstance(outline, dict)
            or outline.get("type") != "MultiPolygon"
        ):
            raise ValueError("outline requires bbox and MultiPolygon type")
        polygons = outline.get("coordinates")
        if not isinstance(polygons, list) or not polygons:
            raise ValueError("outline coordinates must be non-empty")
        for polygon in polygons:
            if not isinstance(polygon, list) or not polygon:
                raise ValueError("polygon must contain rings")
            for ring in polygon:
                if not isinstance(ring, list) or len(ring) < 4:
                    raise ValueError("ring must be closed and have three vertices")
                points = [numbers(p, 2, "outline vertex") for p in ring]
                if points[0] != points[-1] or len(set(points)) < 3:
                    raise ValueError("invalid closed ring")
                area = sum(
                    a[0] * b[1] - b[0] * a[1]
                    for a, b in zip(points, points[1:], strict=False)
                )
                if area == 0 or any(
                    not (box[0] <= x <= box[2] and box[1] <= y <= box[3])
                    for x, y in points
                ):
                    raise ValueError("invalid ring area or bbox containment")

    def public_metadata(self) -> dict[str, object]:
        return {
            "id": self.overlay_id,
            "display_name": self.display_name,
            "encoding": "objects",
            "revision": self.cache_token,
            "default_opacity": self.default_opacity,
            "initially_visible": self.initially_visible,
            "count": self.count,
            "classes": [
                {"id": c.id, "name": c.name, "rgba": list(c.rgba)} for c in self.classes
            ],
        }

    def validate_slide(self, metadata: SlideMetadata) -> None:
        key = (metadata.dimensions, metadata.mpp)
        if key in self._validated:
            return
        if metadata.mpp is None:
            raise ValueError(
                "slide has no MPP; provide source_mpp or --source-mpp "
                "to align physical-coordinate detections"
            )
        if any(
            not math.isclose(a, b, rel_tol=1e-6)
            for a, b in zip(metadata.mpp, self.source_mpp, strict=True)
        ):
            raise ValueError("detection source_mpp differs from slide MPP")
        w, h = (metadata.dimensions[i] * metadata.mpp[i] for i in range(2))
        if self.count:
            a, b, c, d = self.extents
            if a < 0 or b < 0 or c > w or d > h:
                raise ValueError("detection geometry outside WSI")
            with self._lock:
                if self._db.execute(
                    "SELECT 1 FROM points WHERE x >= ? OR y >= ? LIMIT 1", (w, h)
                ).fetchone():
                    raise ValueError("detection point outside WSI")
        for box in self.coverage_boxes:
            if box[0] < 0 or box[1] < 0 or box[2] > w or box[3] > h:
                raise ValueError("coverage outside WSI")
        self._validated.add(key)

    def query(self, box: tuple[float, ...], after: int = 0, limit: int = 10000) -> dict:
        x0, y0, x1, y1 = box
        with self._lock:
            rows = self._db.execute(
                """
                SELECT p.seq, p.class_id, p.x, p.y FROM spatial s
                JOIN points p ON p.seq=s.seq
                WHERE s.x1>=? AND s.x0<=? AND s.y1>=? AND s.y0<=?
                  AND p.x>=? AND p.x<? AND p.y>=? AND p.y<? AND p.seq>?
                ORDER BY p.seq LIMIT ?
            """,
                (x0, x1, y0, y1, x0, x1, y0, y1, after, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {
            "points": [[r[1], r[2], r[3]] for r in rows],
            "next_cursor": rows[-1][0] if more else None,
        }

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self._db.close()
        self._directory.cleanup()


def load_detection_overlay_manifest(
    path: str | Path, *, slide_id_aliases: Mapping[str, str] | None = None
) -> DetectionOverlay:
    return DetectionOverlay(path, slide_id_aliases=slide_id_aliases)
