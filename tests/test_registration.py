"""Registration review and access isolation at actual HTTP entry points."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from io import BytesIO

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx2")
from PIL import Image  # noqa: E402

from wsi_patchkit.web import create_app  # noqa: E402
from wsi_patchkit.web.__main__ import SlideDirectoryScanner  # noqa: E402
from wsi_patchkit.web.auth import AdminAuth, hash_password  # noqa: E402
from wsi_patchkit.web.auth_store import AuthStore  # noqa: E402

PASSWORD = "correct horse battery staple"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def digest():
    return hash_password(PASSWORD)


@pytest.fixture
async def site(tmp_path, digest):
    root = tmp_path / "slides"
    for name, color in (("group-a", "red"), ("group-b", "blue")):
        directory = root / name
        directory.mkdir(parents=True)
        Image.new("RGB", (16, 16), color).save(directory / "case.png")
    Image.new("RGB", (8, 8)).save(root / "root-only.png")
    scanner = SlideDirectoryScanner([root], interval=0)
    app = create_app(
        scanner.refresh(),
        slide_scanner=scanner,
        auth=AdminAuth("admin", digest, database_path=tmp_path / "auth.sqlite3"),
        upload_dir=tmp_path / "uploads",
        crop_output_dir=tmp_path / "crops",
    )
    async with app.router.lifespan_context(app), client_for(app) as admin:
        assert (await login(admin, "admin")).status_code == 200
        yield app, admin, tmp_path


@asynccontextmanager
async def client_for(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


async def login(client, username, password=PASSWORD):
    return await client.post(
        "/api/auth/login",
        headers=ORIGIN,
        json={"username": username, "password": password},
    )


async def mutate(client, method, path, payload=None, content=None):
    state = (await client.get("/api/auth/session")).json()
    return await client.request(
        method,
        path,
        json=payload,
        content=content,
        headers={
            **ORIGIN,
            "X-CSRF-Token": state["csrf_token"],
        },
    )


async def register(client, username):
    return await client.post(
        "/api/auth/register",
        headers=ORIGIN,
        json={
            "username": username,
            "password": PASSWORD,
            "password_confirm": PASSWORD,
        },
    )


async def account(admin, username):
    return next(
        user
        for user in (await admin.get("/api/users")).json()["users"]
        if user["username"] == username
    )


async def approve(admin, username, groups):
    user = await account(admin, username)
    response = await mutate(
        admin,
        "PATCH",
        f"/api/users/{user['id']}",
        {
            "status": "active",
            "group_ids": groups,
        },
    )
    assert response.status_code == 200
    return user["id"]


async def crop(client, slide_id, filename):
    response = await mutate(
        client,
        "POST",
        f"/api/slides/{slide_id}/crops",
        {
            "x": 0,
            "y": 0,
            "width": 4,
            "height": 4,
            "format": "png",
            "filename": filename,
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    for _ in range(100):
        response = await client.get(f"/api/slides/{slide_id}/crops/{job_id}")
        assert response.status_code == 200
        if response.json()["status"] == "completed":
            return job_id
        assert response.json()["status"] != "failed", response.text
        await asyncio.sleep(0.01)
    pytest.fail("crop did not complete")


@pytest.mark.anyio
async def test_register_pending_approval_rejection_and_no_privilege_injection(site):
    app, admin, _ = site
    async with client_for(app) as alice:
        for path in ("/register", "/static/register.js", "/static/login.css"):
            assert (await alice.get(path)).status_code == 200
        assert (await alice.get("/admin/users")).status_code == 303
        assert (await admin.get("/admin/users")).status_code == 200
        assert (await alice.post("/api/auth/register", json={})).status_code == 403
        injection = await alice.post(
            "/api/auth/register",
            headers=ORIGIN,
            json={
                "username": "alice",
                "password": PASSWORD,
                "password_confirm": PASSWORD,
                "is_admin": True,
                "status": "active",
            },
        )
        assert injection.status_code == 400
        result = await register(alice, "alice")
        assert result.status_code == 202
        assert "set-cookie" not in result.headers
        assert result.json() == {"status": "pending"}
        user = await account(admin, "alice")
        assert (
            user["status"] == "pending"
            and not user["is_active"]
            and not user["is_admin"]
        )
        assert user["group_ids"] == []
        response = await login(alice, "alice")
        assert (
            response.status_code == 403
            and response.json()["detail"] == "account_pending"
        )
        assert (await login(alice, "alice", "wrong")).status_code == 401
        assert (await alice.get("/api/slides")).status_code == 401
        assert (await register(alice, "ALICE")).status_code == 409
        rejected = await mutate(
            admin, "PATCH", f"/api/users/{user['id']}", {"status": "rejected"}
        )
        assert rejected.status_code == 200
        assert (await login(alice, "alice")).json()["detail"] == "account_rejected"
        await approve(admin, "alice", [])
        assert (await login(alice, "alice")).status_code == 200
        assert (await alice.get("/api/slides")).json() == []
        assert (await alice.get("/api/groups")).json() == {"groups": []}
        assert (await alice.get("/admin/users")).status_code == 403
        assert (
            await mutate(
                alice,
                "PATCH",
                f"/api/users/{user['id']}",
                {
                    "status": "active",
                    "group_ids": [],
                },
            )
        ).status_code == 403


@pytest.mark.anyio
async def test_registration_validation_and_shared_ip_limit(site):
    app, _, _ = site
    async with client_for(app) as client:
        response = await client.post(
            "/api/auth/register",
            headers=ORIGIN,
            json={
                "username": "alice",
                "password": PASSWORD,
                "password_confirm": "different",
            },
        )
        assert response.status_code == 400
        for index in range(5):
            assert (await register(client, f"new-user-{index}")).status_code == 202
        assert (await register(client, "sixth-user")).status_code == 429


@pytest.mark.anyio
async def test_group_authorization_revocation_and_symlink_alias(site):
    app, admin, tmp_path = site
    groups = (await admin.get("/api/groups")).json()["groups"]
    group_a = next(group["id"] for group in groups if group["name"] == "group-a")
    async with client_for(app) as alice:
        await register(alice, "alice")
        user_id = await approve(admin, "alice", [group_a])
        await login(alice, "alice")
        records = (await alice.get("/api/slides")).json()
        assert [record["id"] for record in records] == ["group-a_case.png"]
        assert (await alice.get("/api/slides/group-a_case.png")).status_code == 200
        assert (
            await alice.get("/iiif/3/group-a_case.png/full/8,/0/default.jpg")
        ).status_code == 200
        for path in (
            "/api/slides/group-b_case.png",
            "/api/slides/root-only.png",
            "/iiif/3/group-b_case.png/info.json",
            "/iiif/3/group-b_case.png/full/8,/0/default.jpg",
            "/api/slides/group-b_case.png/overlays",
            "/iiif/3/group-b_case.png/overlays/secret/info.json",
            "/api/slides/group-b_case.png/detections/secret/points",
        ):
            assert (await alice.get(path)).status_code == 404, path
        assert (
            await mutate(alice, "POST", "/api/slides/group-b_case.png/crops", {})
        ).status_code == 404
        if hasattr((tmp_path / "slides"), "symlink_to"):
            alias = tmp_path / "slides/group-a/alias.png"
            alias.symlink_to(tmp_path / "slides/group-b/case.png")
            await alice.get("/api/slides")
            await alice.get("/api/slides")
            assert (await alice.get("/api/slides/group-a_alias.png")).status_code == 404
        await crop(alice, "group-a_case.png", "own.png")
        assert (
            await mutate(admin, "PATCH", f"/api/users/{user_id}", {"group_ids": []})
        ).status_code == 200
        assert (await alice.get("/api/auth/session")).status_code == 200
        assert (
            await alice.get("/iiif/3/group-a_case.png/info.json")
        ).status_code == 404
        assert (await alice.get("/api/crops/own.png/image")).status_code == 200
        assert (await alice.get("/api/slides")).json() == []


@pytest.mark.anyio
async def test_private_uploads_crops_jobs_and_batch_operations(site):
    app, admin, tmp_path = site
    groups = (await admin.get("/api/groups")).json()["groups"]
    group_a = next(group["id"] for group in groups if group["name"] == "group-a")
    async with client_for(app) as alice, client_for(app) as bob:
        for name, client in (("alice", alice), ("bobby", bob)):
            await register(client, name)
            await approve(admin, name, [group_a])
            await login(client, name)
        image = BytesIO()
        Image.new("RGB", (8, 8), "green").save(image, "PNG")
        uploads = []
        for client in (alice, bob):
            response = await mutate(
                client,
                "POST",
                "/api/slides/upload?filename=same.png",
                content=image.getvalue(),
            )
            assert response.status_code == 201, response.text
            uploads.append(response.json()["id"])
        assert uploads[0] != uploads[1]
        assert (await alice.get(f"/api/slides/{uploads[1]}")).status_code == 404
        assert (await bob.get(f"/iiif/3/{uploads[0]}/info.json")).status_code == 404
        assert (await admin.get(f"/api/slides/{uploads[0]}")).status_code == 404
        assert {r["id"] for r in (await alice.get("/api/slides")).json()} == {
            "group-a_case.png",
            uploads[0],
        }
        job = await crop(alice, "group-a_case.png", "alice-only.png")
        assert (
            await bob.get(f"/api/slides/group-a_case.png/crops/{job}")
        ).status_code == 404
        for path in (
            "/api/crops/alice-only.png/image",
            "/api/crops/alice-only.png/download",
        ):
            assert (await bob.get(path)).status_code == 404
        assert (
            await mutate(bob, "DELETE", "/api/crops/alice-only.png")
        ).status_code == 404
        await crop(alice, "group-a_case.png", "same.png")
        await crop(bob, "group-a_case.png", "same.png")
        assert [r["filename"] for r in (await bob.get("/api/crops")).json()] == [
            "same.png"
        ]
        for endpoint in ("batch-delete", "batch-download"):
            response = await mutate(
                bob,
                "POST",
                f"/api/crops/{endpoint}",
                {
                    "filenames": ["same.png", "alice-only.png"],
                },
            )
            assert response.status_code == 404
        assert (await bob.get("/api/crops/same.png/image")).status_code == 200
        response = await mutate(
            bob, "POST", "/api/crops/batch-download", {"filenames": ["same.png"]}
        )
        assert (
            response.status_code == 200
            and response.headers["content-type"] == "application/zip"
        )
        assert (
            await mutate(
                bob, "POST", "/api/crops/batch-delete", {"filenames": ["same.png"]}
            )
        ).status_code == 200
        assert (await alice.get("/api/crops/same.png/image")).status_code == 200
        alice_id = (await account(admin, "alice"))["id"]
        bob_id = (await account(admin, "bobby"))["id"]
        assert (tmp_path / "crops" / alice_id / "same.png").is_file()
        assert not (tmp_path / "crops" / bob_id / "same.png").exists()
        assert len(list((tmp_path / "uploads" / alice_id).glob("*.png"))) == 1


@pytest.mark.anyio
async def test_invalid_grants_do_not_approve_account(site):
    app, admin, _ = site
    async with client_for(app) as alice:
        await register(alice, "alice")
        user_id = (await account(admin, "alice"))["id"]
        for payload in (
            {"status": "active", "group_ids": ["not-a-group"]},
            {"status": []},
            {"group_ids": "all"},
            {"is_admin": True},
            {"is_active": True, "status": "active"},
        ):
            result = await mutate(admin, "PATCH", f"/api/users/{user_id}", payload)
            assert result.status_code == 400
        assert (await account(admin, "alice"))["status"] == "pending"


def test_v1_database_migration_preserves_users_and_sessions(tmp_path, digest):
    path = tmp_path / "auth.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY, username TEXT NOT NULL,
              username_key TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
              is_admin INTEGER NOT NULL, is_active INTEGER NOT NULL,
              created_at REAL NOT NULL);
            CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
              csrf_token TEXT NOT NULL, created_at REAL NOT NULL,
              expires_at REAL NOT NULL);
            PRAGMA user_version=1;
        """)
        db.execute(
            "INSERT INTO users VALUES ('existing', 'admin', 'admin', ?, 1, 1, ?)",
            (digest, time.time()),
        )
        db.execute(
            "INSERT INTO users VALUES ('disabled', 'alice', 'alice', ?, 0, 0, ?)",
            (digest, time.time()),
        )
        db.execute(
            "INSERT INTO sessions VALUES ('token-hash', 'existing', 'csrf', ?, ?)",
            (time.time(), time.time() + 3600),
        )
    store = AuthStore(path)
    try:
        assert store.find_user("admin")["status"] == "active"
        assert store.find_user("alice")["status"] == "disabled"
        assert store.session("token-hash")["user"]["id"] == "existing"
        store.bootstrap("newadmin", digest)
        assert store.find_user("newadmin") is None
    finally:
        store.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


@pytest.mark.anyio
async def test_group_grants_and_private_files_survive_restart(site):
    app, admin, tmp_path = site
    groups = (await admin.get("/api/groups")).json()["groups"]
    group_a = next(g["id"] for g in groups if g["name"] == "group-a")
    async with client_for(app) as alice:
        await register(alice, "alice")
        await approve(admin, "alice", [group_a])
        await login(alice, "alice")
        await crop(alice, "group-a_case.png", "persistent.png")
        image = BytesIO()
        Image.new("RGB", (8, 8)).save(image, "PNG")
        response = await mutate(
            alice,
            "POST",
            "/api/slides/upload?filename=persistent.png",
            content=image.getvalue(),
        )
        slide_id = response.json()["id"]
        token = alice.cookies.get("patchkit_session")
    scanner = SlideDirectoryScanner([tmp_path / "slides"], interval=0)
    restarted = create_app(
        scanner.refresh(),
        slide_scanner=scanner,
        auth=AdminAuth("admin", database_path=tmp_path / "auth.sqlite3"),
        upload_dir=tmp_path / "uploads",
        crop_output_dir=tmp_path / "crops",
    )
    async with (
        restarted.router.lifespan_context(restarted),
        client_for(restarted) as alice,
    ):
        alice.cookies.set("patchkit_session", token)
        assert (await alice.get("/api/auth/session")).status_code == 200
        assert (await alice.get("/api/crops/persistent.png/image")).status_code == 200
        assert (await alice.get(f"/iiif/3/{slide_id}/info.json")).status_code == 200
        assert {r["id"] for r in (await alice.get("/api/slides")).json()} == {
            "group-a_case.png",
            slide_id,
        }


@pytest.mark.anyio
async def test_group_directory_identity_survives_rename_but_not_replacement(site):
    app, admin, tmp_path = site
    original = next(
        g
        for g in (await admin.get("/api/groups")).json()["groups"]
        if g["name"] == "group-a"
    )
    async with client_for(app) as alice:
        await register(alice, "alice")
        await approve(admin, "alice", [original["id"]])
        await login(alice, "alice")
        renamed = tmp_path / "slides/renamed"
        (tmp_path / "slides/group-a").rename(renamed)
        renamed_group = next(
            g
            for g in (await admin.get("/api/groups")).json()["groups"]
            if g["name"] == "renamed"
        )
        assert renamed_group["id"] == original["id"]
        await alice.get("/api/slides")
        records = (await alice.get("/api/slides")).json()
        assert [r["id"] for r in records] == ["renamed_case.png"]
        renamed.rename(tmp_path / "retired-group")
        renamed.mkdir()
        Image.new("RGB", (16, 16)).save(renamed / "case.png")
        replacement = next(
            g
            for g in (await admin.get("/api/groups")).json()["groups"]
            if g["name"] == "renamed"
        )
        assert replacement["id"] != original["id"]
        await alice.get("/api/slides")
        assert (await alice.get("/api/slides")).json() == []
        assert (await alice.get("/api/slides/renamed_case.png")).status_code == 404
