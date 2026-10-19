"""Persistent identities and cross-instance session lifecycle guarantees."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx2")
from wsi_patchkit.web import create_app  # noqa: E402
from wsi_patchkit.web.auth import AdminAuth, hash_password  # noqa: E402
from wsi_patchkit.web.auth_store import AuthStore  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def digest():
    return hash_password("correct horse battery staple")


@asynccontextmanager
async def running(tmp_path, digest=""):
    app = create_app(
        {},
        auth=AdminAuth("admin", digest, database_path=tmp_path / "auth.sqlite3"),
        upload_dir=tmp_path / "uploads",
        crop_output_dir=tmp_path / "crops",
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        yield app, client


async def login(client, username="admin", password="correct horse battery staple"):
    return await client.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": username, "password": password},
    )


async def mutate(client, method, path, payload=None):
    state = (await client.get("/api/auth/session")).json()
    return await client.request(
        method,
        path,
        json=payload,
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": state["csrf_token"],
        },
    )


def test_store_bootstrap_uniqueness_private_file_and_hashed_tokens(tmp_path, digest):
    path = tmp_path / "db" / "auth.sqlite3"
    store = AuthStore(path)
    try:
        store.bootstrap("admin", digest)
        admin = store.find_user("ADMIN")
        store.bootstrap("other-admin", digest)
        assert store.find_user("other-admin") is None
        user = store.create_user("Alice", digest)
        assert user["id"] != admin["id"]
        assert not user["is_admin"]
        with pytest.raises(ValueError, match="already exists"):
            store.create_user("ＡLICE", digest)
        with pytest.raises(ValueError, match="last active"):
            store.set_active(admin["id"], False)
        session = store.issue_session(admin["id"], digest, "", 3600)
        with sqlite3.connect(path) as db:
            row = db.execute("SELECT token_hash, user_id FROM sessions").fetchone()
            assert row == (
                hashlib.sha256(session["token"].encode()).hexdigest(),
                admin["id"],
            )
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert path.stat().st_mode & 0o777 == 0o600
        assert all("password_hash" not in u for u in store.list_users())
    finally:
        store.close()
        store.close()


def test_session_limit_expiry_and_disable_races(tmp_path, digest, monkeypatch):
    import wsi_patchkit.web.auth_store as module

    clock = type("Clock", (), {"time": staticmethod(lambda: 1000.0)})
    monkeypatch.setattr(module, "time", clock)
    store = AuthStore(tmp_path / "auth.sqlite3")
    try:
        store.bootstrap("admin", digest)
        admin = store.find_user("admin")
        alice = store.create_user("alice", digest)
        other = store.issue_session(alice["id"], digest, "", 30)
        sessions = [store.issue_session(admin["id"], digest, "", 30) for _ in range(33)]

        def key(session):
            return hashlib.sha256(session["token"].encode()).hexdigest()

        assert store.session(key(sessions[0])) is None
        assert store.session(key(sessions[1])) is not None
        assert store.session(key(other)) is not None
        store.set_active(alice["id"], False)
        assert store.session(key(other)) is None
        assert store.issue_session(alice["id"], digest, "", 30) is None
        store.set_active(alice["id"], True)
        assert store.session(key(other)) is None
        assert store.issue_session(admin["id"], "stale credential hash", "", 30) is None
        monkeypatch.setattr(clock, "time", staticmethod(lambda: 1030.0))
        assert store.session(key(sessions[-1])) is None
    finally:
        store.close()


def test_login_limits_shared_between_instances(tmp_path):
    path = tmp_path / "auth.sqlite3"
    first, second = AuthStore(path), AuthStore(path)
    try:
        for _ in range(10):
            assert first.consume_login_attempt("alice", "127.0.0.1")
        assert not second.consume_login_attempt("ALICE", "127.0.0.2")
        assert second.consume_login_attempt("bob", "127.0.0.1")
    finally:
        first.close()
        second.close()


@pytest.mark.anyio
async def test_persistent_session_restart_and_cross_instance_logout(tmp_path, digest):
    async with running(tmp_path, digest) as (_, first):
        response = await login(first)
        state = response.json()
        token = first.cookies.get("patchkit_session")
        assert state["user"]["is_admin"]
        async with running(tmp_path) as (_, second):
            second.cookies.set("patchkit_session", token)
            response = await second.get("/api/auth/session")
            assert response.json()["user"]["id"] == state["user"]["id"]
            assert response.json()["csrf_token"] == state["csrf_token"]
            assert (await mutate(second, "POST", "/api/auth/logout")).status_code == 200
            assert (await first.get("/api/auth/session")).status_code == 401
        await login(first)
        token = first.cookies.get("patchkit_session")
    # All connections have closed; SQLite still contains the session.
    async with running(tmp_path) as (_, restarted):
        restarted.cookies.set("patchkit_session", token)
        assert (await restarted.get("/api/auth/session")).status_code == 200
        assert (await login(restarted)).status_code == 200
    with sqlite3.connect(tmp_path / "auth.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


@pytest.mark.anyio
async def test_admin_creates_users_and_disabling_revokes_sessions(tmp_path, digest):
    async with (
        running(tmp_path, digest) as (app, admin),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as alice,
    ):
        await login(admin)
        result = await mutate(
            admin,
            "POST",
            "/api/users",
            {
                "username": "alice",
                "password": "correct horse battery staple",
            },
        )
        assert result.status_code == 201
        user = result.json()
        assert not user["is_admin"]
        assert "password_hash" not in user
        duplicate = await mutate(
            admin,
            "POST",
            "/api/users",
            {
                "username": "ALICE",
                "password": "correct horse battery staple",
            },
        )
        assert duplicate.status_code == 409
        assert (await login(alice, "Alice")).status_code == 200
        state = (await alice.get("/api/auth/session")).json()
        assert state["user"]["id"] == user["id"]
        assert (await alice.get("/api/users")).status_code == 403
        assert (
            await mutate(
                alice,
                "POST",
                "/api/users",
                {
                    "username": "mallory",
                    "password": "correct horse battery staple",
                },
            )
        ).status_code == 403
        assert (
            await mutate(
                alice,
                "PATCH",
                f"/api/users/{user['id']}",
                {
                    "is_active": False,
                },
            )
        ).status_code == 403
        assert (
            await mutate(
                admin,
                "PATCH",
                f"/api/users/{user['id']}",
                {
                    "is_active": False,
                },
            )
        ).status_code == 200
        assert (await alice.get("/api/auth/session")).status_code == 401
        assert (await login(alice, "alice")).status_code == 401
        assert (
            await mutate(
                admin,
                "PATCH",
                f"/api/users/{user['id']}",
                {
                    "is_active": True,
                },
            )
        ).status_code == 200
        assert (await alice.get("/api/auth/session")).status_code == 401
        assert (await login(alice, "alice")).status_code == 200
        users = (await admin.get("/api/users")).json()["users"]
        assert {u["username"] for u in users} == {"admin", "alice"}
        admin_id = next(u["id"] for u in users if u["is_admin"])
        assert (
            await mutate(
                admin,
                "PATCH",
                f"/api/users/{admin_id}",
                {
                    "is_active": False,
                },
            )
        ).status_code == 409


@pytest.mark.anyio
async def test_password_change_and_logout_all_revoke_other_sessions(tmp_path, digest):
    async with (
        running(tmp_path, digest) as (app, first),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as second,
    ):
        await login(first)
        await login(second)
        assert (
            await mutate(
                first,
                "POST",
                "/api/auth/password",
                {
                    "current_password": "wrong",
                    "new_password": "a different good password",
                },
            )
        ).status_code == 400
        assert (await second.get("/api/auth/session")).status_code == 200
        response = await mutate(
            first,
            "POST",
            "/api/auth/password",
            {
                "current_password": "correct horse battery staple",
                "new_password": "a different good password",
            },
        )
        assert response.status_code == 200
        for client in (first, second):
            assert (await client.get("/api/auth/session")).status_code == 401
        assert (await login(first)).status_code == 401
        assert (
            await login(first, password="a different good password")
        ).status_code == 200
        assert (
            await login(second, password="a different good password")
        ).status_code == 200
        assert (await mutate(first, "POST", "/api/auth/logout-all")).status_code == 200
        assert (await second.get("/api/auth/session")).status_code == 401
    # Changing the bootstrap environment cannot reset the database password.
    async with running(tmp_path, digest) as (_, client):
        assert (await login(client)).status_code == 401
        assert (
            await login(client, password="a different good password")
        ).status_code == 200


def test_configuration_accepts_existing_database_without_bootstrap_secret(
    tmp_path, digest, monkeypatch
):
    path = tmp_path / "auth.sqlite3"
    store = AuthStore(path)
    store.bootstrap("admin", digest)
    store.close()
    monkeypatch.setenv("PATCHKIT_AUTH_DB", str(path))
    monkeypatch.delenv("PATCHKIT_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("PATCHKIT_ADMIN_PASSWORD_HASH_FILE", raising=False)
    config = AdminAuth.from_env()
    assert config.database_path == path
    assert not config.password_hash


def test_empty_database_requires_bootstrap_and_unknown_schema_rejected(tmp_path):
    path = tmp_path / "auth.sqlite3"
    store = AuthStore(path)
    with pytest.raises(ValueError, match="requires an admin"):
        store.bootstrap("admin", "")
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="unsupported"):
        AuthStore(path)
