from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx2")

from wsi_patchkit import LevelInfo, SlideMetadata, TiffReader  # noqa: E402
from wsi_patchkit.web import (  # noqa: E402
    BinaryRasterSource,
    IndexedOverlay,
    OverlayClass,
    OverlayFragment,
    OverlayGeometry,
    OverlayRegistry,
    OverlayRenderer,
    TiffRasterSource,
    create_app,
    load_indexed_overlay_manifest,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _write_gray(path: Path, array: np.ndarray) -> None:
    tifffile.imwrite(path, array, photometric="minisblack", metadata=None)


def _write_slide(path: Path) -> None:
    tifffile.imwrite(
        path,
        np.full((8, 8, 3), 128, dtype=np.uint8),
        photometric="rgb",
        resolution=(40_000, 40_000),
        resolutionunit="CENTIMETER",
        metadata=None,
    )


def _overlay(
    tmp_path: Path,
    *,
    prediction: np.ndarray | None = None,
    coverage: np.ndarray | None = None,
) -> IndexedOverlay:
    prediction_path = tmp_path / "prediction-index.tif"
    coverage_path = tmp_path / "prediction-coverage.tif"
    prediction = (
        np.array(
            [
                [1, 1, 2, 2],
                [1, 9, 2, 2],
                [1, 1, 2, 2],
            ],
            dtype=np.uint8,
        )
        if prediction is None
        else prediction
    )
    coverage = (
        np.array(
            [
                [1, 1, 1, 1],
                [1, 0, 1, 1],
                [1, 1, 1, 1],
            ],
            dtype=np.uint8,
        )
        if coverage is None
        else coverage
    )
    _write_gray(prediction_path, prediction)
    _write_gray(coverage_path, coverage)
    return IndexedOverlay(
        slide_id="case-001",
        overlay_id="prediction",
        display_name="Prediction",
        classes=(
            OverlayClass(0, "Normal", (0, 0, 0, 0)),
            OverlayClass(1, "G3", (255, 0, 0, 160)),
            OverlayClass(2, "G4", (0, 255, 0, 200)),
        ),
        fragments=(
            OverlayFragment(
                source=TiffRasterSource(prediction_path),
                coverage=BinaryRasterSource(coverage_path),
                geometry=OverlayGeometry(
                    origin_um=(1.0, 1.0),
                    mpp=(0.5, 0.5),
                    dimensions=(4, 3),
                ),
                revision="run-1",
            ),
        ),
    )


def test_overlay_registry_accepts_an_explicit_manifest_slide_id_alias(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path)

    registry = OverlayRegistry(
        {"case-001.svs": {"prediction": overlay}},
        slide_ids=("case-001.svs",),
        manifest_slide_id_aliases={"case-001": "case-001.svs"},
    )

    assert registry.get("case-001.svs", "prediction") is overlay


def test_manifest_loads_indexed_fragment_and_rejects_escaping_paths(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path)
    manifest = tmp_path / "prediction.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "wsi-patchkit-overlay/v1",
                "slide_id": "case-001",
                "overlay_id": "prediction",
                "display_name": "Prediction",
                "encoding": "indexed",
                "classes": [
                    {"id": 0, "name": "Normal", "rgba": [0, 0, 0, 0]},
                    {"id": 1, "name": "G3", "rgba": [255, 0, 0, 160]},
                    {"id": 2, "name": "G4", "rgba": [0, 255, 0, 200]},
                ],
                "fragments": [
                    {
                        "prediction": "prediction-index.tif",
                        "coverage": "prediction-coverage.tif",
                        "full_coverage": False,
                        "origin_um": [1.0, 1.0],
                        "mpp": [0.5, 0.5],
                        "dimensions": [4, 3],
                        "revision": "run-1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_indexed_overlay_manifest(manifest)

    assert loaded.slide_id == overlay.slide_id
    assert loaded.overlay_id == overlay.overlay_id
    assert loaded.fragments[0].geometry.bounds_um == (1.0, 1.0, 3.0, 2.5)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["fragments"][0]["prediction"] = "../outside.tif"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        load_indexed_overlay_manifest(manifest)


def test_overlay_cache_token_changes_when_prediction_is_replaced(tmp_path: Path) -> None:
    overlay = _overlay(tmp_path)
    before = overlay.public_metadata()["revision"]
    prediction = overlay.fragments[0].source.path
    stat = prediction.stat()
    os.utime(prediction, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert overlay.public_metadata()["revision"] != before


def test_manifest_requires_exactly_one_coverage_declaration(tmp_path: Path) -> None:
    _overlay(tmp_path)
    payload = {
        "schema": "wsi-patchkit-overlay/v1",
        "slide_id": "case-001",
        "overlay_id": "prediction",
        "encoding": "indexed",
        "classes": [{"id": 0, "name": "Normal", "rgba": [0, 0, 0, 0]}],
        "fragments": [
            {
                "prediction": "prediction-index.tif",
                "full_coverage": False,
                "origin_um": [0, 0],
                "mpp": [0.5, 0.5],
                "dimensions": [4, 3],
                "revision": "run-1",
            }
        ],
    }
    manifest = tmp_path / "prediction.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="provide coverage"):
        load_indexed_overlay_manifest(manifest)

    payload["fragments"][0]["full_coverage"] = True
    payload["fragments"][0]["coverage"] = "prediction-coverage.tif"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="provide coverage"):
        load_indexed_overlay_manifest(manifest)


def test_renderer_places_partial_prediction_and_applies_coverage(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path)
    metadata = SlideMetadata(
        tmp_path / "slide.tif",
        (LevelInfo(0, (8, 8), (1, 1), (0.5, 0.5)),),
        mpp=(0.5, 0.5),
    )
    renderer = OverlayRenderer(reader_pool_size=1)
    try:
        encoded = renderer.render_region(overlay, metadata, (0, 0, 8, 8), (8, 8))
    finally:
        renderer.close()

    rgba = np.asarray(Image.open(BytesIO(encoded.content)))
    assert rgba.shape == (8, 8, 4)
    assert not rgba[:2, :, 3].any()
    assert not rgba[:, :2, 3].any()
    np.testing.assert_array_equal(rgba[2, 2], np.array([255, 0, 0, 160]))
    np.testing.assert_array_equal(rgba[2, 4], np.array([0, 255, 0, 200]))
    assert rgba[3, 3, 3] == 0  # Prediction ID 9 is ignored outside coverage.
    assert not rgba[5:, :, 3].any()


def test_empty_coverage_skips_prediction_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _overlay(tmp_path, coverage=np.zeros((3, 4), dtype=np.uint8))
    metadata = SlideMetadata(
        tmp_path / "slide.tif",
        (LevelInfo(0, (8, 8), (1, 1), (0.5, 0.5)),),
        mpp=(0.5, 0.5),
    )
    renderer = OverlayRenderer(reader_pool_size=1)
    sampled_paths: list[Path] = []
    original = renderer._read_sampled

    def track_read(reader, path, source_x, source_y, requested_downsample):
        sampled_paths.append(Path(path))
        return original(reader, path, source_x, source_y, requested_downsample)

    monkeypatch.setattr(renderer, "_read_sampled", track_read)
    try:
        encoded = renderer.render_region(overlay, metadata, (0, 0, 8, 8), (8, 8))
    finally:
        renderer.close()

    assert sampled_paths == [overlay.fragments[0].coverage.path]
    assert not np.asarray(Image.open(BytesIO(encoded.content))).any()


def test_renderer_rejects_invalid_coverage_and_unknown_valid_ids(
    tmp_path: Path,
) -> None:
    metadata = SlideMetadata(
        tmp_path / "slide.tif",
        (LevelInfo(0, (8, 8), (1, 1), (0.5, 0.5)),),
        mpp=(0.5, 0.5),
    )
    invalid_coverage = np.ones((3, 4), dtype=np.uint8)
    invalid_coverage[0, 0] = 2
    invalid_directory = tmp_path / "invalid-coverage"
    invalid_directory.mkdir()
    unknown_directory = tmp_path / "unknown-class"
    unknown_directory.mkdir()
    renderer = OverlayRenderer(reader_pool_size=1)
    try:
        with pytest.raises(ValueError, match="outside 0/1"):
            renderer.render_region(
                _overlay(invalid_directory, coverage=invalid_coverage),
                metadata,
                (0, 0, 8, 8),
                (8, 8),
            )
        valid_coverage = np.ones((3, 4), dtype=np.uint8)
        with pytest.raises(ValueError, match="undeclared class IDs"):
            renderer.render_region(
                _overlay(unknown_directory, coverage=valid_coverage),
                metadata,
                (0, 0, 8, 8),
                (8, 8),
            )
    finally:
        renderer.close()


def test_overlay_sampling_uses_a_suitable_native_pyramid_level() -> None:
    metadata = SlideMetadata(
        "mask.tif",
        (
            LevelInfo(0, (8, 8), (1, 1)),
            LevelInfo(1, (2, 2), (4, 4)),
        ),
    )

    class Reader:
        selected_level: int | None = None

        def metadata(self, path):
            return metadata

        def read_region(self, path, location, level, size):
            self.selected_level = level
            assert location == (0, 0)
            assert size == (2, 2)
            return np.array([[1, 2], [3, 4]], dtype=np.uint8)[..., None]

    reader = Reader()
    sampled = OverlayRenderer._read_sampled(
        reader,
        "mask.tif",
        np.array([0, 4], dtype=np.int64),
        np.array([0, 4], dtype=np.int64),
        (4.0, 4.0),
    )

    assert reader.selected_level == 1
    np.testing.assert_array_equal(sampled, np.array([[1, 2], [3, 4]]))


@pytest.mark.anyio
async def test_viewer_serves_registered_overlay_metadata_and_tiles(
    tmp_path: Path,
) -> None:
    slide = tmp_path / "slide.tif"
    _write_slide(slide)
    overlay = _overlay(tmp_path)
    app = create_app(
        {"case-001": slide},
        overlays={"case-001": {"prediction": overlay}},
        reader=TiffReader(),
        tile_size=4,
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        metadata = await client.get("/api/slides/case-001")
        overlays = await client.get("/api/slides/case-001/overlays")
        info = await client.get(
            "/iiif/3/case-001/overlays/prediction/info.json"
        )
        tile = await client.get(
            "/iiif/3/case-001/overlays/prediction/full/max/0/default.png"
        )
        cached = await client.get(
            "/iiif/3/case-001/overlays/prediction/full/max/0/default.png",
            headers={"If-None-Match": tile.headers["etag"]},
        )

    assert metadata.status_code == 200
    assert metadata.json()["overlays"][0]["id"] == "prediction"
    assert overlays.status_code == 200
    assert overlays.json()[0]["classes"][1]["name"] == "G3"
    revision = overlays.json()[0]["revision"]
    assert len(revision) == 16
    assert info.status_code == 200
    assert info.json()["width"] == 8
    assert info.json()["preferredFormats"] == ["png"]
    assert tile.status_code == 200
    assert tile.headers["content-type"] == "image/png"
    assert np.asarray(Image.open(BytesIO(tile.content))).shape == (
        8,
        8,
        4,
    )
    assert cached.status_code == 304


@pytest.mark.anyio
async def test_viewer_applies_class_palette_and_visibility_from_style_url(
    tmp_path: Path,
) -> None:
    slide = tmp_path / "slide.tif"
    _write_slide(slide)
    overlay = _overlay(tmp_path)
    app = create_app(
        {"case-001": slide},
        overlays={"case-001": {"prediction": overlay}},
        reader=TiffReader(),
        tile_size=4,
    )
    style = "00000000" "123456a0" "00ff0000"
    revision = overlay.cache_token
    prefix = (
        f"/iiif/3/case-001/overlays/prediction/revision/{revision}/style/{style}"
    )
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        info = await client.get(f"{prefix}/info.json")
        tile = await client.get(f"{prefix}/full/max/0/default.png")
        hidden = await client.get(
            "/iiif/3/case-001/overlays/prediction/style/"
            "000000001234560000ff0000/full/max/0/default.png"
        )
        invalid = await client.get(
            "/iiif/3/case-001/overlays/prediction/style/abc/info.json"
        )
        stale = await client.get(
            f"/iiif/3/case-001/overlays/prediction/revision/deadbeef/style/{style}/info.json"
        )

    assert info.status_code == 200
    assert info.json()["id"].endswith(prefix)
    assert tile.status_code == 200
    pixels = np.asarray(Image.open(BytesIO(tile.content)))
    np.testing.assert_array_equal(pixels[5, 5], [0x12, 0x34, 0x56, 0xA0])
    assert np.asarray(Image.open(BytesIO(hidden.content)))[5, 5, 3] == 0
    assert invalid.status_code == 400
    assert stale.status_code == 404
