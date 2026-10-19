"""Single-administrator authentication with process-local, revocable sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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
    password_hash: str
    secure_cookie: bool = False
    session_seconds: int = 8 * 3600

    def __post_init__(self) -> None:
        if not self.username or not 1 <= self.session_seconds <= 7 * 86400:
            raise ValueError("invalid admin username or session lifetime")
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
        if not digest:
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
        )


class SessionGuard:
    """Pure ASGI middleware keeps upload/response streaming intact."""

    def __init__(self, app: ASGIApp, auth: AdminAuth, sessions: dict):
        self.app, self.auth, self.sessions = app, auth, sessions
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
            "/api/auth/login",
            "/healthz",
            "/static/login.js",
            "/static/login.css",
        }
        key = hashlib.sha256(request.cookies.get(self.cookie, "").encode()).hexdigest()
        session = self.sessions.get(key)
        if session is not None and session[0] <= time.monotonic():
            self.sessions.pop(key, None)
            session = None
        response = None
        if not public and session is None:
            if path in {"/", "/crops"}:
                response = RedirectResponse("/login", status_code=303)
            else:
                response = JSONResponse({"detail": "authentication required"}, 401)
        elif request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Origin is required even for login, preventing login CSRF.
            origin = request.headers.get("origin")
            if origin != str(request.base_url).rstrip("/"):
                response = JSONResponse({"detail": "invalid request origin"}, 403)
            elif not public and not hmac.compare_digest(
                request.headers.get("x-csrf-token", "").encode(), session[1].encode()
            ):
                response = JSONResponse({"detail": "invalid CSRF token"}, 403)
        scope.setdefault("state", {})["session_key"] = key

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

    sessions: dict[str, tuple[float, str]] = {}
    attempts: deque[float] = deque()
    lock = asyncio.Lock()
    cookie = "__Host-patchkit_session" if auth.secure_cookie else "patchkit_session"
    app.add_middleware(SessionGuard, auth=auth, sessions=sessions)

    @app.get("/login", include_in_schema=False)
    async def login_page():
        return HTMLResponse(
            Path(__file__).with_name("static").joinpath("login.html").read_text("utf-8")
        )

    @app.post("/api/auth/login", include_in_schema=False)
    async def login(request: Request):
        # Bound streaming bodies before parsing; never trust Content-Length alone.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"detail": "request too large"}, 413)
        try:
            payload = json.loads(body)
            username, password = payload["username"], payload["password"]
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError
            if len(username) > 256 or len(password) > 1024:
                raise ValueError
            # Reject unpaired JSON surrogate escapes before password hashing.
            username.encode("utf-8")
            password.encode("utf-8")
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"detail": "invalid credentials"}, 400)
        async with lock:
            now = time.monotonic()
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            # Global budget is intentional for the single administrator account.
            if len(attempts) >= 10:
                return JSONResponse(
                    {"detail": "too many login attempts"},
                    429,
                    headers={"Retry-After": "60"},
                )
            attempts.append(now)
            _, iterations, salt, expected = auth.password_hash.split("$")
            actual = await asyncio.to_thread(
                hashlib.pbkdf2_hmac,
                "sha256",
                password.encode(),
                bytes.fromhex(salt),
                int(iterations),
            )
            valid_password = hmac.compare_digest(actual, bytes.fromhex(expected))
            valid_user = hmac.compare_digest(username.encode(), auth.username.encode())
            if not (valid_password and valid_user):
                return JSONResponse({"detail": "invalid credentials"}, 401)
            now = time.monotonic()
            for key, (expiry, _) in list(sessions.items()):
                if expiry <= now:
                    del sessions[key]
            # Rotate a previous session and bound memory for this single account.
            sessions.pop(request.state.session_key, None)
            if len(sessions) >= 32:
                sessions.pop(next(iter(sessions)))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            sessions[hashlib.sha256(token.encode()).hexdigest()] = (
                now + auth.session_seconds,
                csrf,
            )
        response = JSONResponse({"username": auth.username, "csrf_token": csrf})
        response.set_cookie(
            cookie,
            token,
            max_age=auth.session_seconds,
            httponly=True,
            secure=auth.secure_cookie,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/auth/session", include_in_schema=False)
    async def current_session(request: Request):
        return {
            "enabled": True,
            "username": auth.username,
            "csrf_token": sessions[request.state.session_key][1],
        }

    @app.post("/api/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        sessions.pop(request.state.session_key, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            cookie,
            path="/",
            secure=auth.secure_cookie,
            httponly=True,
            samesite="strict",
        )
        return response


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
