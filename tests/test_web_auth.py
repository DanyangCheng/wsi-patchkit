"""Exercise authentication boundaries, not only the login response."""

from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx2")
from PIL import Image  # noqa: E402

from wsi_patchkit.web import create_app  # noqa: E402
from wsi_patchkit.web.auth import AdminAuth, hash_password  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def password_hash():
    return hash_password("correct horse battery staple")


@pytest.fixture
async def secured(tmp_path, password_hash):
    app = create_app(
        {},
        auth=AdminAuth("admin", password_hash, secure_cookie=True),
        upload_dir=tmp_path / "uploads",
        crop_output_dir=tmp_path / "crops",
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client,
    ):
        yield client


async def login(client, **kwargs):
    return await client.post(
        "/api/auth/login",
        headers={"Origin": "https://testserver"},
        json={"username": "admin", "password": "correct horse battery staple"},
        **kwargs,
    )


@pytest.mark.anyio
async def test_all_data_and_schema_routes_require_session(secured):
    for path in (
        "/api/slides",
        "/api/crops",
        "/api/crops/x.png/image",
        "/api/crops/x.png/thumbnail",
        "/api/crops/x.png/download",
        "/iiif/3/slide/info.json",
        "/iiif/3/slide/full/max/0/default.jpg",
        "/docs",
        "/openapi.json",
        "/static/app.js",
        "/api/auth/session",
    ):
        response = await secured.get(path)
        assert response.status_code == 401, path
        assert response.headers["cache-control"] == "no-store"
    for path in ("/", "/crops"):
        response = await secured.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    for path in ("/api/slides/upload", "/api/crops/batch-delete"):
        assert (await secured.post(path, content=b"private")).status_code == 401
    for path in ("/login", "/static/login.js", "/static/login.css", "/healthz"):
        assert (await secured.get(path)).status_code == 200


@pytest.mark.anyio
async def test_login_csrf_logout_and_replay(secured):
    response = await login(secured)
    assert response.status_code == 200
    cookie_header = response.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag in cookie_header
    assert cookie_header.startswith("__Host-patchkit_session=")
    old_cookie = secured.cookies.get("__Host-patchkit_session")
    assert "admin" not in old_cookie
    state = (await secured.get("/api/auth/session")).json()
    assert state["username"] == "admin"
    assert (await secured.get("/api/slides")).status_code == 200
    for headers in (
        {},
        {"Origin": "https://evil.example"},
        {"Origin": "https://testserver", "X-CSRF-Token": "wrong"},
    ):
        assert (
            await secured.post("/api/auth/logout", headers=headers)
        ).status_code == 403
    assert (await secured.get("/api/auth/session")).status_code == 200
    response = await secured.post(
        "/api/auth/logout",
        headers={
            "Origin": "https://testserver",
            "X-CSRF-Token": state["csrf_token"],
        },
    )
    assert response.status_code == 200
    secured.cookies.set("__Host-patchkit_session", old_cookie)
    assert (await secured.get("/api/slides")).status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize("secure", [None, True, False])
async def test_http_cookie_configuration(tmp_path, password_hash, secure):
    app = create_app(
        {},
        auth=(
            AdminAuth("admin", password_hash)
            if secure is None
            else AdminAuth("admin", password_hash, secure_cookie=secure)
        ),
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
        response = await client.post(
            "/api/auth/login",
            headers={"Origin": "http://testserver"},
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        if secure:
            assert response.status_code == 426
            assert response.json()["detail"] == "https_required"
            assert "set-cookie" not in response.headers
            for path in ("/", "/login", "/api/slides", "/static/login.js"):
                assert (await client.get(path)).status_code == 426
            assert (await client.get("/healthz")).status_code == 200
        else:
            assert response.status_code == 200
            assert "Secure" not in response.headers["set-cookie"]
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/api/auth/session")).status_code == 200


@pytest.mark.anyio
async def test_login_origin_body_limit_and_rate_limit(secured):
    assert (await secured.post("/api/auth/login", json={})).status_code == 403
    response = await secured.post(
        "/api/auth/login",
        headers={"Origin": "https://testserver"},
        content=b"x" * 8193,
    )
    assert response.status_code == 413
    for _ in range(10):
        response = await secured.post(
            "/api/auth/login",
            headers={"Origin": "https://testserver"},
            json={"username": "admin", "password": "wrong"},
        )
        assert response.status_code == 401
    response = await login(secured)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


@pytest.mark.anyio
async def test_authenticated_upload_iiif_and_tile(secured):
    response = await login(secured)
    headers = {
        "Origin": "https://testserver",
        "X-CSRF-Token": response.json()["csrf_token"],
    }
    image = BytesIO()
    Image.new("RGB", (8, 8), "red").save(image, format="PNG")
    response = await secured.post(
        "/api/slides/upload?filename=private.png",
        content=image.getvalue(),
        headers={"Origin": "https://testserver"},
    )
    assert response.status_code == 403
    response = await secured.post(
        "/api/slides/upload?filename=private.png",
        content=image.getvalue(),
        headers=headers,
    )
    assert response.status_code == 201
    slide_id = response.json()["id"]
    response = await secured.get(f"/iiif/3/{slide_id}/info.json")
    assert response.status_code == 200
    assert response.json()["id"].startswith("https://testserver/")
    response = await secured.get(f"/iiif/3/{slide_id}/full/8,/0/default.jpg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert Image.open(BytesIO(response.content)).size == (8, 8)


@pytest.mark.anyio
async def test_session_rotation_expiry_and_restart(
    tmp_path, password_hash, monkeypatch
):
    import wsi_patchkit.web.auth_store as module

    now = [1000.0]

    # Replace only this module's clock, not asyncio's shared time module.
    class Clock:
        @staticmethod
        def time():
            return now[0]

    monkeypatch.setattr(module, "time", Clock)
    config = AdminAuth("admin", password_hash, secure_cookie=True, session_seconds=5)
    old = ""
    for _ in range(2):
        app = create_app({}, auth=config, upload_dir=tmp_path / "uploads")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://testserver",
            ) as client,
        ):
            if _:
                response = await client.get(
                    "/api/auth/session",
                    headers={
                        "Cookie": f"__Host-patchkit_session={old}",
                    },
                )
                assert response.status_code == 401
            await login(client)
            old = client.cookies.get("__Host-patchkit_session")
            await login(client)
            current = client.cookies.get("__Host-patchkit_session")
            assert current != old
            assert (
                await client.get(
                    "/api/auth/session",
                    headers={
                        "Cookie": f"__Host-patchkit_session={old}",
                    },
                )
            ).status_code == 401
            now[0] += 6
            assert (await client.get("/api/auth/session")).status_code == 401
            await login(client)
            old = client.cookies.get("__Host-patchkit_session")


def test_password_hash_and_configuration(password_hash, monkeypatch, tmp_path):
    algorithm, iterations, salt, digest = password_hash.split("$")
    assert algorithm == "pbkdf2_sha256"
    assert (
        hashlib.pbkdf2_hmac(
            "sha256",
            b"correct horse battery staple",
            bytes.fromhex(salt),
            int(iterations),
        ).hex()
        == digest
    )
    assert hash_password("correct horse battery staple") != password_hash
    with pytest.raises(ValueError):
        hash_password("short")
    with pytest.raises(ValueError):
        AdminAuth("admin", "plaintext")
    monkeypatch.delenv("PATCHKIT_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("PATCHKIT_ADMIN_PASSWORD_HASH_FILE", raising=False)
    monkeypatch.setenv("PATCHKIT_AUTH_DB", str(tmp_path / "missing.sqlite3"))
    with pytest.raises(ValueError, match="Set PATCHKIT"):
        AdminAuth.from_env()


def test_cli_passes_tls_files_to_uvicorn(tmp_path, monkeypatch):
    import uvicorn

    import wsi_patchkit.web.app as web_app
    from wsi_patchkit.web.__main__ import main

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.touch()
    key.touch()
    captured = {}
    sentinel = object()
    monkeypatch.setattr(web_app, "create_app", lambda *a, **kw: sentinel)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(
        "sys.argv",
        [
            "patchkit-web",
            "--no-auth",
            "--ssl-certfile",
            str(cert),
            "--ssl-keyfile",
            str(key),
            "--port",
            "8000",
        ],
    )
    main()
    assert captured["ssl_certfile"] == str(cert)
    assert captured["ssl_keyfile"] == str(key)
    assert captured["port"] == 8000


@pytest.mark.parametrize("flag", ["--ssl-certfile", "--ssl-keyfile"])
def test_cli_requires_certificate_and_key_together(monkeypatch, flag):
    from wsi_patchkit.web.__main__ import main

    monkeypatch.setattr("sys.argv", ["patchkit-web", flag, "missing.pem"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
