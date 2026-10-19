"""Database-backed user authentication and persistent, revocable sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth_store import AuthStore, username_key

_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    """Generate a salted PBKDF2-SHA256 hash; never store the plaintext password."""
    if not 12 <= len(password) <= 1024:
        raise ValueError("password must contain 12-1024 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


@dataclass(frozen=True)
class AdminAuth:
    username: str
    password_hash: str = ""
    secure_cookie: bool = False
    session_seconds: int = 8 * 3600
    database_path: str | Path | None = None

    def __post_init__(self) -> None:
        if not self.username or not 1 <= self.session_seconds <= 7 * 86400:
            raise ValueError("invalid admin username or session lifetime")
        username_key(self.username)
        if not self.password_hash and self.database_path is not None:
            return
        try:
            algorithm, iterations, salt, digest = self.password_hash.split("$")
            valid = (
                algorithm == "pbkdf2_sha256"
                and int(iterations) == _ITERATIONS
                and len(bytes.fromhex(salt)) == 16
                and len(bytes.fromhex(digest)) == 32
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("invalid password hash; use patchkit-password-hash")

    @classmethod
    def from_env(cls) -> AdminAuth:
        digest = os.environ.get("PATCHKIT_ADMIN_PASSWORD_HASH", "")
        filename = os.environ.get("PATCHKIT_ADMIN_PASSWORD_HASH_FILE")
        if filename:
            digest = Path(filename).read_text(encoding="utf-8").strip()
        database = Path(
            os.environ.get("PATCHKIT_AUTH_DB", "data/auth.sqlite3")
        ).expanduser()
        if not digest and not database.is_file():
            raise ValueError(
                "Set PATCHKIT_ADMIN_PASSWORD_HASH_FILE or "
                "PATCHKIT_ADMIN_PASSWORD_HASH; "
                "use --no-auth only for local development"
            )
        secure = os.environ.get("PATCHKIT_COOKIE_SECURE", "false").lower()
        if secure not in {"true", "false"}:
            raise ValueError("PATCHKIT_COOKIE_SECURE must be true or false")
        return cls(
            username=os.environ.get("PATCHKIT_ADMIN_USER", "admin"),
            password_hash=digest,
            secure_cookie=secure == "true",
            session_seconds=int(os.environ.get("PATCHKIT_SESSION_SECONDS", "28800")),
            database_path=database,
        )


class SessionGuard:
    """Pure ASGI middleware keeps upload/response streaming intact."""

    def __init__(self, app: ASGIApp, auth: AdminAuth, store: AuthStore):
        self.app, self.auth, self.store = app, auth, store
        self.cookie = (
            "__Host-patchkit_session" if auth.secure_cookie else "patchkit_session"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if (
            self.auth.secure_cookie
            and request.url.scheme != "https"
            and path != "/healthz"
        ):
            response = JSONResponse(
                {"detail": "https_required"},
                status_code=426,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        public = path in {
            "/login",
            "/register",
            "/api/auth/register",
            "/static/register.js",
            "/api/auth/login",
            "/healthz",
            "/static/login.js",
            "/static/login.css",
        }
        key = hashlib.sha256(request.cookies.get(self.cookie, "").encode()).hexdigest()
        session = self.store.session(key)
        response = None
        if not public and session is None:
            if path in {"/", "/crops", "/admin/users"}:
                response = RedirectResponse("/login", status_code=303)
            else:
                response = JSONResponse({"detail": "authentication required"}, 401)
        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Origin is required even for login, preventing login CSRF.
            origin = request.headers.get("origin")
            if origin != str(request.base_url).rstrip("/"):
                response = JSONResponse({"detail": "invalid request origin"}, 403)
            elif not public and not hmac.compare_digest(
                request.headers.get("x-csrf-token", "").encode(),
                session["csrf_token"].encode(),
            ):
                response = JSONResponse({"detail": "invalid CSRF token"}, 403)
        state = scope.setdefault("state", {})
        state["session_key"] = key
        state["auth_session"] = session
        state["user"] = session["user"] if session else None
        policy = getattr(request.app.state, "file_access", None)
        if (
            response is None
            and session
            and policy
            and not policy.allowed_request(request, session["user"])
        ):
            response = JSONResponse({"detail": "unknown file"}, 404)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Authenticated image data must not survive logout in HTTP caches.
                headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"same-origin"),
                        (b"x-frame-options", b"DENY"),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        if response is not None:
            await response(scope, receive, secure_send)
        else:
            await self.app(scope, receive, secure_send)


def install_auth(app: FastAPI, auth: AdminAuth | None) -> None:
    @app.get("/healthz", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    if auth is None:

        @app.get("/api/auth/session", include_in_schema=False)
        async def disabled_session():
            return {"enabled": False}

        return

    store = AuthStore(auth.database_path)
    try:
        store.bootstrap(auth.username, auth.password_hash)
    except BaseException:
        store.close()
        raise
    app.state.auth_store = store
    hashing_slots = asyncio.Semaphore(2)
    cookie = "__Host-patchkit_session" if auth.secure_cookie else "patchkit_session"
    app.add_middleware(SessionGuard, auth=auth, store=store)

    async def read_payload(request: Request) -> dict:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413, "request too large")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, TypeError) as error:
            raise HTTPException(400, "invalid request body") from error

    def text(payload: dict, name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or len(value) > 1024:
            raise HTTPException(400, f"invalid {name}")
        try:
            value.encode("utf-8")
        except UnicodeError as error:
            raise HTTPException(400, f"invalid {name}") from error
        return value

    async def verify(password: str, digest: str) -> bool:
        _, iterations, salt, expected = digest.split("$")
        async with hashing_slots:
            actual = await asyncio.to_thread(
                hashlib.pbkdf2_hmac,
                "sha256",
                password.encode(),
                bytes.fromhex(salt),
                int(iterations),
            )
        return hmac.compare_digest(actual, bytes.fromhex(expected))

    async def new_hash(password: str) -> str:
        async with hashing_slots:
            try:
                return await asyncio.to_thread(hash_password, password)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error

    def require_admin(request: Request) -> None:
        if not request.state.user["is_admin"]:
            raise HTTPException(403, "administrator access required")

    def clear_cookie(response):
        response.delete_cookie(
            cookie,
            path="/",
            secure=auth.secure_cookie,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/login", include_in_schema=False)
    async def login_page():
        return HTMLResponse(
            Path(__file__).with_name("static").joinpath("login.html").read_text("utf-8")
        )

    @app.get("/register", include_in_schema=False)
    async def registration_page():
        return HTMLResponse(
            Path(__file__)
            .with_name("static")
            .joinpath("register.html")
            .read_text("utf-8")
        )

    @app.post("/api/auth/register", status_code=202, include_in_schema=False)
    async def register(request: Request):
        payload = await read_payload(request)
        if set(payload) != {"username", "password", "password_confirm"}:
            raise HTTPException(
                400, "provide username, password and password_confirm only"
            )
        username, password = text(payload, "username"), text(payload, "password")
        if password != text(payload, "password_confirm"):
            raise HTTPException(400, "passwords_do_not_match")
        try:
            username_key(username)
        except ValueError as error:
            raise HTTPException(400, "invalid_username") from error
        client_ip = request.client.host if request.client else "unknown"
        if not store.consume_registration_attempt(client_ip):
            raise HTTPException(
                429, "too_many_registration_attempts", headers={"Retry-After": "60"}
            )
        digest = await new_hash(password)
        try:
            store.create_user(username, digest, pending=True)
        except ValueError as error:
            raise HTTPException(409, "username_exists") from error
        return {"status": "pending"}

    @app.get("/admin/users", include_in_schema=False)
    async def user_admin_page(request: Request):
        require_admin(request)
        return HTMLResponse(
            Path(__file__).with_name("static").joinpath("users.html").read_text("utf-8")
        )

    @app.get("/api/groups", include_in_schema=False)
    async def list_groups(request: Request):
        policy = getattr(app.state, "file_access", None)
        groups = policy.refresh_groups() if policy else []
        if not request.state.user["is_admin"]:
            allowed = set(store.user_groups(request.state.user["id"]))
            groups = [group for group in groups if group["id"] in allowed]
        return {"groups": groups}

    @app.post("/api/auth/login", include_in_schema=False)
    async def login(request: Request):
        payload = await read_payload(request)
        username, password = text(payload, "username"), text(payload, "password")
        if len(username) > 256:
            raise HTTPException(400, "invalid username")
        client_ip = request.client.host if request.client else "unknown"
        if not store.consume_login_attempt(username, client_ip):
            return JSONResponse(
                {"detail": "too many login attempts"},
                429,
                headers={"Retry-After": "60"},
            )
        user = store.find_user(username)
        # Unknown and disabled users still perform the same password hashing work.
        dummy = f"pbkdf2_sha256${_ITERATIONS}${'00' * 16}${'00' * 32}"
        expected = user["password_hash"] if user else dummy
        valid = await verify(password, expected)
        if not valid or user is None:
            raise HTTPException(401, "invalid credentials")
        if user["status"] in {"pending", "rejected"}:
            raise HTTPException(403, "account_" + user["status"])
        if not user["is_active"]:
            raise HTTPException(401, "invalid credentials")
        session = store.issue_session(
            user["id"],
            expected,
            request.state.session_key,
            auth.session_seconds,
        )
        if session is None:
            raise HTTPException(401, "invalid credentials")
        response = JSONResponse(
            {
                "username": session["user"]["username"],
                "user": session["user"],
                "csrf_token": session["csrf_token"],
                "expires_at": session["expires_at"],
            }
        )
        response.set_cookie(
            cookie,
            session["token"],
            max_age=auth.session_seconds,
            httponly=True,
            secure=auth.secure_cookie,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/auth/session", include_in_schema=False)
    async def current_session(request: Request):
        session = request.state.auth_session
        return {"enabled": True, "username": session["user"]["username"], **session}

    @app.post("/api/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        store.revoke(request.state.session_key)
        return clear_cookie(JSONResponse({"ok": True}))

    @app.post("/api/auth/logout-all", include_in_schema=False)
    async def logout_all(request: Request):
        store.revoke_all(request.state.user["id"])
        return clear_cookie(JSONResponse({"ok": True}))

    @app.post("/api/auth/password", include_in_schema=False)
    async def change_password(request: Request):
        payload = await read_payload(request)
        current = text(payload, "current_password")
        replacement = text(payload, "new_password")
        user = store.find_user(request.state.user["username"])
        client_ip = request.client.host if request.client else "unknown"
        if not store.consume_login_attempt(user["username"], client_ip):
            raise HTTPException(
                429, "too many password attempts", headers={"Retry-After": "60"}
            )
        if not await verify(current, user["password_hash"]):
            raise HTTPException(400, "incorrect current password")
        digest = await new_hash(replacement)
        if not store.change_password(user["id"], user["password_hash"], digest):
            raise HTTPException(409, "account changed; log in again")
        return clear_cookie(JSONResponse({"ok": True}))

    @app.get("/api/users", include_in_schema=False)
    async def list_users(request: Request):
        require_admin(request)
        return {
            "users": [
                {**user, "group_ids": store.user_groups(user["id"])}
                for user in store.list_users()
            ]
        }

    @app.post("/api/users", status_code=201, include_in_schema=False)
    async def create_user(request: Request):
        require_admin(request)
        payload = await read_payload(request)
        if set(payload) != {"username", "password"}:
            raise HTTPException(400, "provide username and password only")
        username, password = text(payload, "username"), text(payload, "password")
        try:
            username_key(username)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        digest = await new_hash(password)
        try:
            return store.create_user(username, digest)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.patch("/api/users/{user_id}", include_in_schema=False)
    async def set_active(user_id: str, request: Request):
        require_admin(request)
        payload = await read_payload(request)
        if not payload or set(payload) - {"is_active", "status", "group_ids"}:
            raise HTTPException(400, "invalid account update")
        if "is_active" in payload and (
            type(payload["is_active"]) is not bool or "status" in payload
        ):
            raise HTTPException(400, "provide a boolean is_active or a status")
        status = payload.get("status")
        if "is_active" in payload:
            status = "active" if payload["is_active"] else "disabled"
        if "status" in payload and (
            not isinstance(status, str)
            or status not in {"pending", "active", "disabled", "rejected"}
        ):
            raise HTTPException(400, "invalid account status")
        group_ids = payload.get("group_ids")
        if "group_ids" in payload:
            if not isinstance(group_ids, list) or any(
                not isinstance(g, str) for g in group_ids
            ):
                raise HTTPException(400, "group_ids must be a list of strings")
            if len(group_ids) > 1000:
                raise HTTPException(400, "too many groups")
            policy = getattr(app.state, "file_access", None)
            known = {g["id"] for g in policy.refresh_groups()} if policy else set()
            if set(group_ids) - known:
                raise HTTPException(400, "unknown file group")
        try:
            result = store.review_user(user_id, status, group_ids)
            return {**result, "group_ids": store.user_groups(user_id)}
        except KeyError as error:
            raise HTTPException(404, "unknown user") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error


def password_hash_main() -> None:
    """Interactive entry point avoids passwords in shell history/process args."""
    from getpass import getpass

    password = getpass("Admin password (12+ characters): ")
    if password != getpass("Confirm password: "):
        raise SystemExit("Passwords do not match")
    try:
        print(hash_password(password))
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    password_hash_main()
