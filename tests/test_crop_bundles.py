"""Aligned class masks remain reusable after the source slide is removed."""

import asyncio
import json
from io import BytesIO
from zipfile import ZipFile

import numpy as np
import pytest
import tifffile
from PIL import Image

pytest.importorskip("fastapi")
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
    registry = ExporterRegistry()
    app = create_app(
        {"case": slide},
        crop_exporters=registry,
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

        response = await client.post(
            "/api/crops/batch-download",
            json={
                "filenames": ["sample.png"],
                "label_format": "none",
                "include_manifest": True,
            },
        )
        assert response.status_code == 200
        with ZipFile(BytesIO(response.content)) as archive:
            assert set(archive.namelist()) == {"sample.png", "manifest.json"}
            assert archive.read("sample.png") == (output / "sample.png").read_bytes()
        # An image-only export keeps the saved labels for subsequent exports.
        assert (bundle / "layer-0/mask.png").is_file()

        formats = (await client.get("/api/crops/exporters")).json()
        assert {
            "id": "segmentation-mask",
            "name": "Segmentation Mask 1.1",
        } in formats
        for method, url, kwargs in [
            (
                "get",
                "/api/crops/sample.png/export?label_format=segmentation-mask&include_images=false",
                {},
            ),
            (
                "post",
                "/api/crops/batch-download",
                {
                    "json": {
                        "filenames": ["sample.png"],
                        "label_format": "segmentation-mask",
                        "include_images": False,
                    }
                },
            ),
        ]:
            response = await client.request(method, url, **kwargs)
            assert response.status_code == 200, response.text
            with ZipFile(BytesIO(response.content)) as archive:
                assert archive.read("ImageSets/Segmentation/default.txt") == b"sample\n"
                assert archive.read("labelmap.txt").decode().splitlines() == [
                    "background:0,0,0::",
                    "normal:128,0,0::",
                    "tumor:0,128,0::",
                ]
                with Image.open(
                    BytesIO(archive.read("SegmentationClass/sample.png"))
                ) as image:
                    exported = np.array(image)
                np.testing.assert_array_equal(
                    np.any(exported != 0, axis=2), expected_coverage.astype(bool)
                )
                assert np.all(exported[(mask == 0) & (coverage == 1)] == [128, 0, 0])
                assert np.all(exported[(mask == 300) & (coverage == 1)] == [0, 128, 0])
                assert "sample.png" not in archive.namelist()
                assert not any(".labels/" in name for name in archive.namelist())
                assert set(
                    name for name in archive.namelist() if name.endswith(".png")
                ) == {"SegmentationClass/sample.png"}

        response = await client.post(
            "/api/crops/batch-download",
            json={
                "filenames": ["sample.png"],
                "label_format": "none",
                "include_images": True,
            },
        )
        assert response.status_code == 200
        with ZipFile(BytesIO(response.content)) as archive:
            assert archive.namelist() == ["sample.png"]
        response = await client.post(
            "/api/crops/batch-download",
            json={"filenames": ["sample.png"], "include_images": "false"},
        )
        assert response.status_code == 400

        def custom(bundle, archive):
            archive.writestr(
                bundle.labels.name + "/custom.txt",
                str(bundle.metadata["output_dimensions"]),
            )

        assert app.state.crop_exporters is registry
        registry.register("custom", custom, name="Custom")
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


@pytest.mark.anyio
async def test_native_fractional_level_preserves_image_mask_alignment(tmp_path):
    # 8/3 is non-integral: round(2 * 8/3) selects column 1 instead of 2.
    slide = tmp_path / "pyramid.tif"
    native = np.broadcast_to(
        np.array([4, 12, 20], dtype="uint8")[None, :, None], (3, 3, 3)
    ).copy()
    with tifffile.TiffWriter(slide) as writer:
        writer.write(
            np.zeros((8, 8, 3), dtype="uint8"),
            photometric="rgb",
            subifds=1,
            resolution=(40000, 20000),
            resolutionunit="CENTIMETER",
            metadata=None,
        )
        writer.write(native, photometric="rgb", subfiletype=1, metadata=None)
    mask = np.broadcast_to(np.arange(24, dtype="uint8"), (24, 24)).copy()
    tifffile.imwrite(tmp_path / "indices.tif", mask, metadata=None)
    overlay = IndexedOverlay(
        slide_id="case",
        overlay_id="dense",
        classes=tuple(OverlayClass(i, str(i), (i, i, i, 255)) for i in range(24)),
        fragments=(
            OverlayFragment(
                source=TiffRasterSource(tmp_path / "indices.tif"),
                geometry=OverlayGeometry(
                    origin_um=(0, 0), mpp=(2 / 24, 4 / 24), dimensions=(24, 24)
                ),
                revision="v1",
            ),
        ),
    )
    output = tmp_path / "crops"
    app = create_app(
        {"case": slide},
        overlays={"case": {"dense": overlay}},
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
                "x": 2,
                "y": 1,
                "width": 1,
                "height": 1,
                "level": 1,
                "filename": "native.png",
            },
        )
        assert response.status_code == 202, response.text
        assert (await wait_job(client, response.json()))["status"] == "completed"
        with Image.open(output / "native.png") as image:
            assert image.getpixel((0, 0)) == (20, 20, 20)
        with Image.open(output / "native.png.labels/layer-0/mask.png") as image:
            assert image.getpixel((0, 0)) == 20
        with Image.open(output / "native.png.labels/layer-0/coverage.png") as image:
            assert image.getpixel((0, 0)) == 1
        metadata = json.loads((output / "native.png.labels/metadata.json").read_text())
        assert metadata["region_level0"] == pytest.approx([16 / 3, 8 / 3, 8 / 3, 8 / 3])
        assert metadata["output_mpp"] == pytest.approx([2 / 3, 4 / 3])


@pytest.mark.anyio
async def test_bundle_publication_failure_removes_owned_artifacts(
    tmp_path, monkeypatch
):
    slide, overlay = sources(tmp_path)
    output = tmp_path / "crops"
    app = create_app(
        {"case": slide},
        overlays={"case": {"prediction": overlay}},
        crop_output_dir=output,
        upload_dir=tmp_path / "uploads",
    )

    def fail_publication(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("wsi_patchkit.web.crops.os.link", fail_publication)
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
                "filename": "failed.png",
            },
        )
        assert (await wait_job(client, response.json()))["status"] == "failed"
        assert list(output.iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize("selected,expected_count", [([300], 8), ([0], 4), ([], 0)])
async def test_crop_class_filter_preserves_ids_and_effective_coverage(
    tmp_path, selected, expected_count
):
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
                "filename": "selected.png",
                "overlay_ids": ["prediction"],
                "overlay_class_ids": {"prediction": selected},
            },
        )
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["overlay_class_ids"] == {"prediction": selected}
        assert (await wait_job(client, job))["status"] == "completed"
        bundle = output / "selected.png.labels"
        with Image.open(bundle / "layer-0/mask.png") as image:
            indices = np.array(image)
        with Image.open(bundle / "layer-0/coverage.png") as image:
            coverage = np.array(image)
        assert int(coverage.sum()) == expected_count
        assert np.all(indices[coverage == 0] == 0)
        if expected_count:
            assert set(np.unique(indices[coverage == 1])) == set(selected)
        metadata = json.loads((bundle / "metadata.json").read_text())
        assert metadata["overlays"][0]["selected_class_ids"] == selected
        assert (
            metadata["overlays"][0]["coverage_values"]["0"]
            == "unpredicted_or_unselected"
        )
        # Retry reconstructs the saved selection without consulting the browser.
        record = app.state.crop_jobs.list()[0]
        record["status"] = "failed"
        app.state.crop_jobs._store.put("local", "job", record["job_id"], record)
        retry = await client.post(f"/api/personal/jobs/{record['job_id']}/retry")
        assert retry.status_code == 202, retry.text
        assert retry.json()["overlay_class_ids"] == {"prediction": selected}
        assert (await wait_job(client, retry.json()))["status"] == "completed"


@pytest.mark.anyio
async def test_crop_rejects_invalid_class_filters(tmp_path):
    slide, overlay = sources(tmp_path)
    app = create_app(
        {"case": slide},
        overlays={"case": {"prediction": overlay}},
        crop_output_dir=tmp_path / "crops",
        upload_dir=tmp_path / "uploads",
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        for filters in (
            [],
            {"prediction": [True]},
            {"prediction": [300.0]},
            {"prediction": [999]},
            {"prediction": "300"},
            {"missing": [0]},
        ):
            response = await client.post(
                "/api/slides/case/crops",
                json={
                    "x": 0,
                    "y": 0,
                    "width": 8,
                    "height": 8,
                    "overlay_ids": ["prediction"],
                    "overlay_class_ids": filters,
                },
            )
            assert response.status_code == 400, response.text


def test_segmentation_mask_batch_layers_and_validation(tmp_path):
    from wsi_patchkit.web.exporters import CropBundle

    bundles = []
    for name, class_id in [("one", 300), ("two", 65535)]:
        image = tmp_path / f"{name}.png"
        Image.new("RGB", (2, 1)).save(image)
        labels = tmp_path / f"{name}.png.labels"
        labels.mkdir()
        Image.fromarray(np.array([[class_id, 0]], dtype="uint16")).save(
            labels / "mask.png"
        )
        Image.fromarray(np.array([[1, 0]], dtype="uint8")).save(labels / "coverage.png")
        (labels / "metadata.json").write_text(
            json.dumps(
                {
                    "overlays": [
                        {
                            "id": "prediction",
                            "classes": [{"id": class_id, "name": str(class_id)}],
                            "mask": "mask.png",
                            "coverage": "coverage.png",
                        }
                    ]
                }
            )
        )
        bundles.append(CropBundle(image, labels))

    def export():
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            ExporterRegistry().export_many("segmentation-mask", bundles, archive)
        return ZipFile(BytesIO(buffer.getvalue()))

    with export() as archive:
        assert archive.read("ImageSets/Segmentation/default.txt") == b"one\ntwo\n"
        assert len(archive.namelist()) == len(set(archive.namelist()))
        mapping = json.loads(archive.read("class-map.json"))["classes"]
        assert [item["source_id"] for item in mapping] == [300, 65535]
        with Image.open(BytesIO(archive.read("SegmentationClass/two.png"))) as image:
            assert image.getpixel((0, 0)) == (0, 128, 0)
            assert image.getpixel((1, 0)) == (0, 0, 0)
    metadata_path = bundles[1].labels / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["overlays"][0]["id"] = "other/../../layer"
    metadata_path.write_text(json.dumps(metadata))
    with export() as archive:
        datasets = json.loads(archive.read("segmentation-datasets.json"))
        assert len(datasets) == 2
        for dataset in datasets:
            with ZipFile(BytesIO(archive.read(dataset["archive"]))) as nested:
                assert "labelmap.txt" in nested.namelist()
                assert not any(".labels/" in name for name in nested.namelist())
                assert all(
                    name.startswith("SegmentationClass/")
                    for name in nested.namelist()
                    if name.endswith(".png")
                )
                assert (
                    len(nested.read("ImageSets/Segmentation/default.txt").splitlines())
                    == 1
                )
    metadata["overlays"][0]["mask"] = "../one.png.labels/mask.png"
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="invalid crop bundle"):
        export()


def test_segmentation_mask_requires_saved_labels(tmp_path):
    from wsi_patchkit.web.exporters import CropBundle

    with (
        ZipFile(BytesIO(), "w") as archive,
        pytest.raises(ValueError, match="没有类别掩码"),
    ):
        ExporterRegistry().export_many(
            "segmentation-mask",
            [CropBundle(tmp_path / "old.png", tmp_path / "old.png.labels")],
            archive,
        )
