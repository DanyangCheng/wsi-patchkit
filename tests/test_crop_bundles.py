"""Aligned class masks remain reusable after the source slide is removed."""

import asyncio
import json
from io import BytesIO
from zipfile import ZipFile

import numpy as np
import pytest
import tifffile
from PIL import Image

httpx = pytest.importorskip("httpx2")

from wsi_patchkit.web import (  # noqa: E402
    BinaryRasterSource,
    ExporterRegistry,
    IndexedOverlay,
    OverlayClass,
    OverlayFragment,
    OverlayGeometry,
    TiffRasterSource,
    create_app,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def sources(tmp_path):
    slide = tmp_path / "slide.tif"
    tifffile.imwrite(
        slide,
        np.full((8, 8, 3), 128, dtype="uint8"),
        photometric="rgb",
        resolution=(40000, 40000),
        resolutionunit="CENTIMETER",
        metadata=None,
    )
    mask = np.array([[0, 300], [300, 999]], dtype="uint16")
    coverage = np.array([[1, 1], [1, 0]], dtype="uint8")
    tifffile.imwrite(tmp_path / "mask.tif", mask, metadata=None)
    tifffile.imwrite(tmp_path / "coverage.tif", coverage, metadata=None)
    overlay = IndexedOverlay(
        slide_id="case",
        overlay_id="prediction",
        classes=(
            OverlayClass(0, "normal", (0, 0, 0, 0)),
            OverlayClass(300, "tumor", (255, 0, 0, 255)),
        ),
        fragments=(
            OverlayFragment(
                source=TiffRasterSource(tmp_path / "mask.tif"),
                coverage=BinaryRasterSource(tmp_path / "coverage.tif"),
                geometry=OverlayGeometry(
                    origin_um=(0.5, 0.5), mpp=(0.5, 0.5), dimensions=(2, 2)
                ),
                revision="v1",
            ),
        ),
    )
    return slide, overlay


async def wait_job(client, job):
    for _ in range(200):
        status = (await client.get(f"/api/slides/case/crops/{job['job_id']}")).json()
        if status["status"] not in {"queued", "running"}:
            return status
        await asyncio.sleep(0.01)
    pytest.fail("crop did not finish")


@pytest.mark.anyio
@pytest.mark.parametrize("target_mpp,size", [(0, 8), (0.5, 4)])
async def test_bundle_masks_metadata_export_and_delete(tmp_path, target_mpp, size):
    slide, overlay = sources(tmp_path)
    output = tmp_path / "crops"
    app = create_app(
        {"case": slide},
        overlays={"case": {"prediction": overlay}},
        crop_output_dir=output,
        upload_dir=tmp_path / "uploads",
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.post(
            "/api/slides/case/crops",
            json={
                "x": 0,
                "y": 0,
                "width": 8,
                "height": 8,
                "target_mpp": target_mpp,
                "filename": "sample.png",
            },
        )
        assert response.status_code == 202, response.text
        assert (await wait_job(client, response.json()))["status"] == "completed"
        bundle = output / "sample.png.labels"
        metadata = json.loads((bundle / "metadata.json").read_text())
        assert metadata["output_dimensions"] == [size, size]
        assert metadata["overlays"][0]["classes"][1]["id"] == 300
        with Image.open(output / "sample.png") as image:
            assert image.size == (size, size)
        with Image.open(bundle / "layer-0/mask.png") as image:
            mask = np.array(image)
        with Image.open(bundle / "layer-0/coverage.png") as image:
            coverage = np.array(image)
        # Nearest-neighbor physical mapping, including uncovered class zero.
        centers = (np.arange(size) + 0.5) * 2 / size
        expected_mask = np.zeros((size, size), dtype="uint16")
        expected_coverage = np.zeros((size, size), dtype="uint8")
        for y, cy in enumerate(centers):
            for x, cx in enumerate(centers):
                if 0.5 <= cx < 1.5 and 0.5 <= cy < 1.5:
                    sx, sy = int((cx - 0.5) / 0.5), int((cy - 0.5) / 0.5)
                    if (sx, sy) != (1, 1):
                        expected_coverage[y, x] = 1
                        expected_mask[y, x] = 0 if (sx, sy) == (0, 0) else 300
        np.testing.assert_array_equal(mask, expected_mask)
        np.testing.assert_array_equal(coverage, expected_coverage)
        assert len((await client.get("/api/crops")).json()) == 1
        assert (await client.get("/api/crops/exporters")).json()[0][
            "id"
        ] == "indexed-png"
        slide.unlink()
        response = await client.post(
            "/api/crops/batch-download", json={"filenames": ["sample.png"]}
        )
        assert response.status_code == 200
        with ZipFile(BytesIO(response.content)) as archive:
            assert "sample.png.labels/layer-0/mask.png" in archive.namelist()
            assert "sample.png.labels/metadata.json" in archive.namelist()

        def custom(bundle, archive):
            archive.writestr(
                bundle.labels.name + "/custom.txt",
                str(bundle.metadata["output_dimensions"]),
            )

        app.state.crop_exporters.register("custom", custom, name="Custom")
        response = await client.get("/api/crops/sample.png/export?label_format=custom")
        with ZipFile(BytesIO(response.content)) as archive:
            assert (
                archive.read("sample.png.labels/custom.txt")
                == str([size, size]).encode()
            )
        assert (
            await client.get("/api/crops/sample.png/export?label_format=missing")
        ).status_code == 400
        assert (await client.delete("/api/crops/sample.png")).status_code == 200
        assert not bundle.exists()


@pytest.mark.anyio
async def test_selection_validation_and_failed_crop_cleanup(tmp_path):
    slide, overlay = sources(tmp_path)
    app = create_app(
        {"case": slide},
        overlays={"case": {"prediction": overlay}},
        crop_output_dir=tmp_path / "crops",
        upload_dir=tmp_path / "uploads",
    )
    payload = {"x": 0, "y": 0, "width": 8, "height": 8}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        for options in (
            {"overlay_ids": ["unknown"]},
            {"overlay_ids": "prediction"},
            {"format": "jpg"},
        ):
            assert (
                await client.post("/api/slides/case/crops", json={**payload, **options})
            ).status_code == 400
        job = (
            await client.post(
                "/api/slides/case/crops",
                json={**payload, "overlay_ids": [], "format": "jpg"},
            )
        ).json()
        assert (await wait_job(client, job))["status"] == "completed"
        # Invalid covered class IDs fail without publishing any partial outputs.
        tifffile.imwrite(
            tmp_path / "mask.tif", np.full((2, 2), 999, dtype="uint16"), metadata=None
        )
        job = (
            await client.post(
                "/api/slides/case/crops", json={**payload, "filename": "failed.png"}
            )
        ).json()
        assert (await wait_job(client, job))["status"] == "failed"
        assert not (tmp_path / "crops/failed.png").exists()
        assert not (tmp_path / "crops/failed.png.labels").exists()


def test_registry_rejects_duplicate_and_invalid_formats():
    registry = ExporterRegistry()
    with pytest.raises(ValueError):
        registry.register("indexed-png", lambda *_: None, name="duplicate")
    with pytest.raises(ValueError):
        registry.register("../escape", lambda *_: None, name="invalid")
