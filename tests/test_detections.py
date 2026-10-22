from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx2")

from wsi_patchkit import LevelInfo, SlideMetadata, TiffReader  # noqa: E402
from wsi_patchkit.web import (  # noqa: E402
    DetectionOverlay,
    create_app,
    load_overlay_manifest,
)
from wsi_patchkit.web.__main__ import discover_overlays  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_frontend_rendering_and_request_lifecycle():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the viewer Canvas harness")
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [node, str(root / "tests/test_detection_frontend.cjs")],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )


def manifest(tmp_path: Path, records=None, **changes) -> Path:
    if records is None:
        records = [
            {"id": "a", "class_id": 0, "center_um": [1.25, 2.25], "score": 0.9},
            {"id": "b", "class_id": 1, "bbox_um": [2, 2, 4, 4]},
            {"id": "c", "class_id": 0, "center_um": [4, 4]},
        ]
    payload = {
        "schema": "wsi-patchkit-detection/v1",
        "slide_id": "case",
        "overlay_id": "nuclei",
        "encoding": "objects",
        "coordinate_system": "slide_um",
        "revision": "run-1",
        "classes": [
            {"id": 0, "name": "Nucleus", "rgba": [255, 0, 0, 220]},
            {"id": 1, "name": "Other", "rgba": [0, 255, 0, 255]},
        ],
        "objects": {"path": "objects.jsonl", "format": "jsonl", "count": len(records)},
        "coverage": {"type": "full_slide"},
        "provenance": {
            "model_name": "test",
            "model_revision": "v1",
            "source_mpp": [0.25, 0.25],
            "source_mpp_source": "slide_metadata",
            "center_definition": "heatmap peak",
            "score_definition": "uncalibrated confidence",
            "postprocessing": {
                "score_threshold": None,
                "deduplication": "non-overlapping ROI",
            },
        },
    }
    payload.update(changes)
    (tmp_path / "objects.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8"
    )
    path = tmp_path / "prediction.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def metadata(mpp=(0.25, 0.25)):
    return SlideMetadata(
        "unused.tif",
        (
            LevelInfo(0, (32, 32), (1, 1)),
            LevelInfo(1, (8, 8), (4, 4)),
            LevelInfo(2, (2, 2), (16, 16)),
        ),
        mpp=mpp,
    )


def test_local_query_is_half_open_paginated_and_independent_of_source(tmp_path):
    path = manifest(tmp_path)
    layer = load_overlay_manifest(path)
    try:
        layer.validate_slide(metadata())
        # A registered revision remains queryable without reopening JSONL.
        (tmp_path / "objects.jsonl").unlink()
        page = layer.query((0, 0, 4, 4), limit=1)
        assert page == {"points": [[0, 1.25, 2.25]], "next_cursor": 1}
        assert layer.query((0, 0, 4, 4), after=page["next_cursor"], limit=1) == {
            "points": [[1, 3, 3]],
            "next_cursor": None,
        }
        assert layer.query((4, 4, 5, 5))["points"] == [[0, 4, 4]]
        assert layer.query((6, 6, 7, 7))["points"] == []
    finally:
        layer.close()


@pytest.mark.parametrize(
    "change,match",
    [
        ({"class_id": 99}, "unknown class"),
        ({"center_um": [float("nan"), 1]}, "invalid JSON constant"),
        ({"center_um": [float("inf"), 1]}, "invalid JSON constant"),
        ({"center_um": [True, 1]}, "finite numbers"),
        ({"score": None}, "finite numbers"),
        ({"score": 1.1}, "score must"),
        ({"bbox_um": [2, 2, 1, 1]}, "positive area"),
        ({"center_um": [1, 1], "bbox_um": [2, 2, 3, 3]}, "inside bbox"),
        ({"properties": []}, "properties"),
    ],
)
def test_invalid_object_rejected(tmp_path, change, match):
    record = {"id": "a", "class_id": 0, "center_um": [1, 1]}
    record.update(change)
    with pytest.raises(ValueError, match=match):
        DetectionOverlay(manifest(tmp_path, [record]))


def test_duplicate_and_missing_geometry_rejected(tmp_path):
    record = {"id": "a", "class_id": 0, "center_um": [1, 1]}
    with pytest.raises(ValueError, match="UNIQUE"):
        DetectionOverlay(manifest(tmp_path, [record, record]))
    with pytest.raises(ValueError, match="needs center"):
        DetectionOverlay(manifest(tmp_path, [{"id": "a", "class_id": 0}]))


def test_count_and_escaping_paths_rejected(tmp_path):
    with pytest.raises(ValueError, match="count does not match"):
        DetectionOverlay(
            manifest(
                tmp_path,
                objects={"path": "objects.jsonl", "format": "jsonl", "count": 4},
            )
        )
    with pytest.raises(ValueError, match="escapes"):
        DetectionOverlay(
            manifest(
                tmp_path,
                objects={"path": "../objects.jsonl", "format": "jsonl", "count": 3},
            )
        )


def test_rectangle_coverage_and_zero_detection(tmp_path):
    with pytest.raises(ValueError, match="outside coverage"):
        DetectionOverlay(
            manifest(
                tmp_path, coverage={"type": "rectangles", "bounds_um": [[0, 0, 4, 4]]}
            )
        )
    layer = DetectionOverlay(
        manifest(tmp_path, [], coverage={"type": "rectangles", "bounds_um": []})
    )
    try:
        layer.validate_slide(metadata())
        assert layer.query((0, 0, 8, 8))["points"] == []
    finally:
        layer.close()


def test_raster_coverage_holes_and_invalid_values(tmp_path):
    coverage = {
        "type": "raster",
        "path": "coverage.tif",
        "origin_um": [0, 0],
        "mpp": [1, 1],
        "dimensions": [8, 8],
    }
    mask = np.ones((8, 8), dtype=np.uint8)
    tifffile.imwrite(tmp_path / "coverage.tif", mask, metadata=None)
    path = manifest(tmp_path, coverage=coverage)
    layer = DetectionOverlay(path)
    layer.validate_slide(metadata())
    layer.close()
    mask[2, 1] = 0
    tifffile.imwrite(tmp_path / "coverage.tif", mask, metadata=None)
    with pytest.raises(ValueError, match="outside coverage"):
        DetectionOverlay(path)
    mask[2, 1] = 2
    tifffile.imwrite(tmp_path / "coverage.tif", mask, metadata=None)
    with pytest.raises(ValueError, match="only 0/1"):
        DetectionOverlay(path)


def test_slide_mpp_bounds_and_revision(tmp_path):
    path = manifest(tmp_path)
    layer = DetectionOverlay(path)
    try:
        token = layer.cache_token
        with pytest.raises(ValueError, match="differs"):
            layer.validate_slide(metadata((0.5, 0.5)))
    finally:
        layer.close()
    layer = DetectionOverlay(manifest(tmp_path, revision="run-2"))
    assert layer.cache_token != token
    layer.close()

    layer = DetectionOverlay(
        manifest(tmp_path, [{"id": "x", "class_id": 0, "center_um": [8, 8]}])
    )
    try:
        with pytest.raises(ValueError, match="outside WSI"):
            layer.validate_slide(metadata())
    finally:
        layer.close()


@pytest.mark.anyio
@pytest.mark.parametrize("embedded_mpp", [False, True])
@pytest.mark.parametrize("source_mpp,status", [(None, 422), (0.25, 200), (0.5, 422)])
async def test_detection_overlay_uses_embedded_or_fallback_mpp(
    tmp_path, source_mpp, status, embedded_mpp,
):
    if embedded_mpp:
        image = tmp_path / "case.tif"
        tifffile.imwrite(
            image, np.zeros((32, 32, 3), dtype=np.uint8), photometric="rgb",
            resolution=(40000, 40000), resolutionunit="CENTIMETER", metadata=None,
        )
        status = 200
    else:
        image = tmp_path / "case.png"
        Image.new("RGB", (32, 32)).save(image)
    app = create_app(
        {"case": image}, overlays={"case": {"nuclei": manifest(tmp_path)}},
        source_mpp=source_mpp, upload_dir=tmp_path / "uploads",
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        result = await client.get("/api/slides/case")
        assert result.status_code == status
        if status == 422 and source_mpp is None:
            assert "--source-mpp" in result.json()["detail"]
        elif status == 422:
            assert "differs" in result.json()["detail"]
        else:
            assert result.json()["mpp"] == [0.25, 0.25]
            assert (await client.get("/iiif/3/case/info.json")).status_code == 200


@pytest.mark.anyio
async def test_api_gate_pagination_registration_and_cleanup(tmp_path):
    slide = tmp_path / "slide.tif"
    with tifffile.TiffWriter(slide) as writer:
        writer.write(
            np.full((32, 32, 3), 128, dtype=np.uint8),
            photometric="rgb",
            resolution=(40000, 40000),
            resolutionunit="CENTIMETER",
            subifds=2,
            metadata=None,
        )
        writer.write(
            np.full((8, 8, 3), 128, dtype=np.uint8),
            photometric="rgb",
            subfiletype=1,
            metadata=None,
        )
        writer.write(
            np.full((2, 2, 3), 128, dtype=np.uint8),
            photometric="rgb",
            subfiletype=1,
            metadata=None,
        )
    path = manifest(tmp_path)
    discovered = discover_overlays([tmp_path], slide_ids=["case"])
    assert discovered == {"case": {"nuclei": path}}
    app = create_app(
        {"case": slide},
        overlays=discovered,
        reader=TiffReader(),
        upload_dir=tmp_path / "uploads",
        crop_output_dir=tmp_path / "crops",
    )
    layer = app.state.overlay_registry.get("case", "nuclei")
    index_path = Path(layer._directory.name)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        result = await client.get("/api/slides/case")
        assert result.status_code == 200
        record = result.json()["overlays"][0]
        assert record["encoding"] == "objects"
        params = {
            "bounds_um": "0,0,8,8",
            "downsample": 4,
            "revision": record["revision"],
            "limit": 1,
        }
        endpoint = "/api/slides/case/detections/nuclei/points"
        page = (await client.get(endpoint, params=params)).json()
        assert page == {"points": [[0, 1.25, 2.25]], "next_cursor": 1}
        page = (await client.get(endpoint, params={**params, "after": 1})).json()
        assert page["points"] == [[1, 3, 3]]
        for downsample in (4.01, 16):
            page = (
                await client.get(endpoint, params={**params, "downsample": downsample})
            ).json()
            assert page["suppressed"] and page["points"] == []
        assert (await client.get(endpoint, params={**params, "downsample": 1})).json()[
            "points"
        ]
        for extra in (
            {"downsample": "nan"},
            {"bounds_um": "0,0,0,8"},
            {"limit": 20001},
            {"after": -1},
        ):
            assert (
                await client.get(endpoint, params={**params, **extra})
            ).status_code == 400
        assert (
            await client.get(endpoint, params={**params, "revision": "stale"})
        ).status_code == 404
        assert (
            await client.get("/iiif/3/case/overlays/nuclei/info.json")
        ).status_code == 400
    assert not index_path.exists()


@pytest.mark.parametrize("stem", ["Z2213809 -13-24PILC-HE", "测序病例 (2)"])
def test_detection_source_stem_alias(tmp_path: Path, stem: str) -> None:
    from wsi_patchkit.web import OverlayRegistry

    path = manifest(tmp_path, slide_id=stem)
    with pytest.raises(ValueError, match="slide ID"):
        load_overlay_manifest(path)
    aliases = {stem: "case.svs"}
    discovered = discover_overlays(
        [tmp_path], slide_ids=["case.svs"], slide_id_aliases=aliases
    )
    registry = OverlayRegistry(
        discovered, slide_ids=["case.svs"], manifest_slide_id_aliases=aliases
    )
    overlay = registry.get("case.svs", "nuclei")
    try:
        assert overlay.slide_id == "case.svs"
    finally:
        overlay.close()
