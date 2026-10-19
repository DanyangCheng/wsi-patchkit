"""Personal workbench lifecycle, ownership, resumable uploads and exports."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from io import BytesIO
from zipfile import ZipFile

import pytest
from PIL import Image

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx2")
from wsi_patchkit.web import create_app  # noqa: E402
from wsi_patchkit.web.auth import AdminAuth, hash_password  # noqa: E402

PASSWORD = "workbench test password"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def digest():
    return hash_password(PASSWORD)


@asynccontextmanager
async def running(tmp_path, digest):
    app = create_app(
        {},
        source_mpp=0.25,
        auth=AdminAuth("admin", digest, database_path=tmp_path / "auth.sqlite3"),
        upload_dir=tmp_path / "uploads",
        crop_output_dir=tmp_path / "crops",
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield app, client


async def login(client, username="admin", password=PASSWORD):
    response = await client.post(
        "/api/auth/login",
        headers=ORIGIN,
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def mutate(client, method, path, payload=None, content=None):
    state = (await client.get("/api/auth/session")).json()
    return await client.request(
        method,
        path,
        json=payload,
        content=content,
        headers={**ORIGIN, "X-CSRF-Token": state["csrf_token"]},
    )


def image_bytes():
    buffer = BytesIO()
    Image.new("RGB", (64, 48), (160, 70, 110)).save(buffer, "PNG")
    return buffer.getvalue()


async def upload(client):
    response = await mutate(
        client, "POST", "/api/slides/upload?filename=sample.png", content=image_bytes()
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.anyio
async def test_personal_data_isolation_and_password_revokes_every_session(
    tmp_path, digest
):
    async with running(tmp_path, digest) as (app, client):
        assert (await client.get("/personal")).status_code == 303
        app.state.auth_store.create_user("alice", digest)
        app.state.auth_store.create_user("bob", digest)
        await login(client, "alice")
        slide_id = await upload(client)
        path = f"/api/personal/slides/{slide_id}"
        assert (
            await mutate(
                client,
                "PUT",
                path,
                {"project": "项目一", "tags": ["组织", "组织"], "notes": "private"},
            )
        ).status_code == 200
        await mutate(client, "PUT", "/api/personal/account", {"display_name": "Alice"})
        region = {"name": "ROI", "level": 0, "x": 1, "y": 2, "width": 20, "height": 20}
        assert (
            await mutate(client, "PUT", path + "/regions/first", region)
        ).status_code == 200
        alice_cookie = client.cookies.get("patchkit_session")
        await login(client, "bob")
        assert (await client.get("/api/personal/slides")).json() == []
        assert (await client.get(path + "/regions")).status_code == 404
        assert (
            await mutate(client, "PUT", path, {"notes": "hijack"})
        ).status_code == 404
        assert (await mutate(client, "POST", path + "/trash")).status_code == 404
        assert (await client.get("/api/personal/account")).json()["profile"] == {}
        await login(client, "alice")
        state = (await client.get("/api/personal/slides")).json()[0]
        assert state["project"] == "项目一" and state["tags"] == ["组织"]
        assert (await client.get("/api/personal/account")).json()["profile"][
            "display_name"
        ] == "Alice"
        bad = await mutate(
            client,
            "POST",
            "/api/auth/password",
            {"current_password": "bad", "new_password": "replacement password"},
        )
        assert bad.status_code == 400
        changed = await mutate(
            client,
            "POST",
            "/api/auth/password",
            {"current_password": PASSWORD, "new_password": "replacement password"},
        )
        assert changed.status_code == 200
        client.cookies.set("patchkit_session", alice_cookie)
        assert (await client.get("/api/auth/session")).status_code == 401
        client.cookies.clear()
        await login(client, "alice", "replacement password")
        assert (await client.get("/personal")).status_code == 200
        assert (await client.get("/api/users")).status_code == 403


@pytest.mark.anyio
async def test_resumable_upload_survives_restart_and_rejects_invalid_offsets(
    tmp_path, digest
):
    data = image_bytes()
    async with running(tmp_path, digest) as (app, client):
        app.state.auth_store.create_user("alice", digest)
        await login(client, "alice")
        result = await mutate(
            client,
            "POST",
            "/api/personal/uploads",
            {"filename": "组织切片.PNG", "size": len(data)},
        )
        assert result.status_code == 201
        upload_id = result.json()["id"]
        base = f"/api/personal/uploads/{upload_id}"
        assert (
            await mutate(client, "PUT", base + "?offset=0", content=data[:25])
        ).json()["offset"] == 25
        assert (
            await mutate(client, "PUT", base + "?offset=0", content=data)
        ).status_code == 409
        assert (await mutate(client, "POST", base + "/complete")).status_code == 409
    async with running(tmp_path, digest) as (_, client):
        await login(client)
        assert (await client.get("/api/personal/uploads")).json() == []
        assert (
            await mutate(client, "PUT", base + "?offset=25", content=data[25:])
        ).status_code == 404
        await login(client, "alice")
        assert (await client.get("/api/personal/uploads")).json()[0]["offset"] == 25
        assert (
            await mutate(client, "PUT", base + "?offset=25", content=data[25:])
        ).status_code == 200
        completed = await mutate(client, "POST", base + "/complete")
        assert completed.status_code == 201
        assert "组织切片.PNG" in completed.json()["path"]
        assert (await client.get("/api/personal/uploads")).json() == []
        slide_id = completed.json()["id"]
        assert "组织切片.PNG" in (await client.get("/api/slides")).json()[0]["path"]
        assert (await client.get(f"/api/slides/{slide_id}")).json()["width"] == 64
        pending = await mutate(
            client,
            "POST",
            "/api/personal/uploads",
            {"filename": "oversize.png", "size": 4},
        )
        other = "/api/personal/uploads/" + pending.json()["id"]
        assert (
            await mutate(client, "PUT", other + "?offset=0", content=b"12345")
        ).status_code == 413
        assert (await mutate(client, "DELETE", other)).status_code == 200
        assert not list((tmp_path / "uploads").rglob(".upload-*"))


@pytest.mark.anyio
async def test_grid_exports_provenance_and_recycle_restore_across_restart(
    tmp_path, digest
):
    async with running(tmp_path, digest) as (_, client):
        await login(client)
        slide_id = await upload(client)
        base = f"/api/personal/slides/{slide_id}"
        payload = {
            "x": 0,
            "y": 0,
            "width": 64,
            "height": 48,
            "level": 0,
            "tile_width": 32,
            "tile_height": 24,
            "stride": 24,
            "minimum_tissue": 0,
            "edge": "skip",
            "format": "png",
        }
        preview = await mutate(client, "POST", base + "/grid-preview", payload)
        assert preview.status_code == 200 and preview.json()["count"] == 4
        invalid = await mutate(
            client, "POST", base + "/grid-preview", {**payload, "stride": 1}
        )
        assert invalid.status_code == 400
        submitted = await mutate(client, "POST", base + "/grid", payload)
        assert submitted.status_code == 202 and submitted.json()["count"] == 4
        for _ in range(200):
            jobs = (await client.get("/api/personal/jobs")).json()
            if all(j["status"] == "completed" for j in jobs):
                break
            await asyncio.sleep(0.01)
        assert len(jobs) == 4 and all(j["status"] == "completed" for j in jobs)
        assert all(j["source_dimensions"] == [64, 48] for j in jobs)
        filenames = [j["filename"] for j in jobs]
        archive = await mutate(
            client,
            "POST",
            "/api/crops/batch-download",
            {"filenames": filenames, "include_manifest": True},
        )
        assert archive.status_code == 200
        with ZipFile(BytesIO(archive.content)) as z:
            assert set(z.namelist()) == {*filenames, "manifest.json"}
            records = json.loads(z.read("manifest.json"))
            assert records["coordinate_system"] == "selected_native_level_pixels"
            assert len(records["crops"]) == 4
        assert (await mutate(client, "POST", base + "/trash")).status_code == 200
        assert (await client.get("/api/slides")).json() == []
        assert (await client.get(f"/api/slides/{slide_id}")).status_code == 404
        assert len((await client.get("/api/crops")).json()) == 4
    async with running(tmp_path, digest) as (_, client):
        await login(client)
        assert (await client.get("/api/slides")).json() == []
        assert len((await client.get("/api/personal/jobs")).json()) == 4
        assert (await mutate(client, "POST", base + "/restore")).status_code == 200
        assert (await client.get(f"/api/slides/{slide_id}")).status_code == 200


@pytest.mark.anyio
async def test_csrf_required_for_personal_mutations_and_upload_paths(tmp_path, digest):
    async with running(tmp_path, digest) as (_, client):
        if os.name == "posix":
            assert (tmp_path / "auth.sqlite3").stat().st_mode & 0o777 == 0o600
        await login(client)
        response = await client.put(
            "/api/personal/account", headers=ORIGIN, json={"display_name": "bypass"}
        )
        assert response.status_code == 403
        bad = await mutate(
            client,
            "POST",
            "/api/personal/uploads",
            {"filename": "../../secret.png", "size": 20},
        )
        assert bad.status_code == 400
        for asset in ("personal.js", "personal.css"):
            assert (await client.get("/static/" + asset)).status_code == 200


@pytest.mark.anyio
async def test_polygon_tissue_filter_and_physical_output(tmp_path, digest):
    async with running(tmp_path, digest) as (_, client):
        await login(client)
        slide_id = await upload(client)
        base = f"/api/personal/slides/{slide_id}"
        params = {
            "x": 0,
            "y": 0,
            "width": 64,
            "height": 48,
            "level": 0,
            "tile_width": 16,
            "tile_height": 16,
            "stride": 16,
            "minimum_tissue": 0.9,
            "target_mpp": 0.5,
            "polygon": [[0, 0], [31, 0], [31, 48], [0, 48]],
        }
        preview = await mutate(client, "POST", base + "/grid-preview", params)
        assert preview.status_code == 200
        assert preview.json()["count"] == 6
        assert preview.json()["estimated_raw_bytes"] == 6 * 8 * 8 * 3
        saved = await mutate(
            client, "PUT", base + "/regions/left", {**params, "name": "left"}
        )
        assert saved.status_code == 200
        assert saved.json()["polygon"] == params["polygon"]
        invalid = await mutate(
            client,
            "POST",
            base + "/grid-preview",
            {
                **params,
                "polygon": [[0, 0], [100, 0], [0, 100]],
            },
        )
        assert invalid.status_code == 400
        submitted = await mutate(
            client,
            "POST",
            f"/api/slides/{slide_id}/crops",
            {
                "x": 0,
                "y": 0,
                "width": 64,
                "height": 48,
                "target_mpp": 0.5,
            },
        )
        assert submitted.status_code == 202
        filename = submitted.json()["filename"]
        for _ in range(200):
            response = await client.get(f"/api/crops/{filename}/image")
            if response.status_code == 200:
                break
            await asyncio.sleep(0.01)
        assert Image.open(BytesIO(response.content)).size == (32, 24)
        manifest = (await client.get("/api/personal/manifest")).json()
        assert manifest["crops"][0]["output_mpp"] == [0.5, 0.5]
        assert manifest["crops"][0]["output_dimensions"] == [32, 24]


@pytest.mark.anyio
async def test_cancel_and_retry_preserve_account_scoped_queue(tmp_path, digest):
    async with running(tmp_path, digest) as (app, client):
        await login(client)
        slide_id = await upload(client)
        started, release = threading.Event(), threading.Event()
        renderer = app.state.tile_renderer
        original = renderer.render_level_region

        def blocked(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        renderer.render_level_region = blocked
        params = {"x": 0, "y": 0, "width": 16, "height": 16}
        path = f"/api/slides/{slide_id}/crops"
        try:
            first = await mutate(client, "POST", path, params)
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            second = await mutate(client, "POST", path, params)
            job_id = second.json()["job_id"]
            job_path = f"/api/personal/jobs/{job_id}"
            assert (
                await mutate(client, "POST", job_path + "/cancel")
            ).status_code == 200
            assert (
                await mutate(client, "POST", job_path + "/retry")
            ).status_code == 202
            active_id = first.json()["job_id"]
            assert (
                await mutate(client, "POST", f"/api/personal/jobs/{active_id}/cancel")
            ).status_code == 409
        finally:
            release.set()
        for _ in range(200):
            jobs = (await client.get("/api/personal/jobs")).json()
            if all(j["status"] not in {"queued", "running"} for j in jobs):
                break
            await asyncio.sleep(0.01)
        assert sorted(j["status"] for j in jobs) == [
            "cancelled",
            "completed",
            "completed",
        ]
        assert len((await client.get("/api/crops")).json()) == 2
