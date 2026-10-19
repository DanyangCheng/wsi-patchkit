"""SQLite user identities, revocable sessions, and shared login throttling."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def username_key(username: str) -> str:
    key = unicodedata.normalize("NFKC", username).strip().casefold()
    if not 3 <= len(key) <= 64 or not all(c.isalnum() or c in "_.-" for c in key):
        raise ValueError("username must contain 3-64 letters, digits, dots, _ or -")
    return key


def public_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "status": row["status"],
    }


class AuthStore:
    """Short serialized transactions; one connection per application instance."""

    def __init__(self, path: str | Path | None = None) -> None:
        location = ":memory:"
        if path is not None:
            target = Path(path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            location = str(target)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(location, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._closed = False
        try:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("unsupported authentication database version")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL CHECK(is_admin IN (0, 1)),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS login_limits (
                    key TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    attempts INTEGER NOT NULL
                );

            """)
            with self.transaction() as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
                if "status" not in columns:
                    db.execute(
                        "ALTER TABLE users ADD COLUMN status TEXT "
                        "NOT NULL DEFAULT 'active'"
                    )
                    db.execute("UPDATE users SET status='disabled' WHERE is_active=0")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS file_groups ("
                    "id TEXT PRIMARY KEY, storage_key TEXT NOT NULL UNIQUE, "
                    "name TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS user_groups ("
                    "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                    "group_id TEXT NOT NULL REFERENCES file_groups(id) "
                    "ON DELETE CASCADE, "
                    "PRIMARY KEY(user_id, group_id))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS uploads ("
                    "id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), "
                    "filename TEXT NOT NULL, display_name TEXT NOT NULL)"
                )
                db.execute("PRAGMA user_version=2")
        except BaseException:
            self._db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def bootstrap(self, username: str, password_hash: str) -> None:
        """Seed exactly one administrator on an empty database, never reset it."""
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return
            if not password_hash:
                raise ValueError("empty user database requires an admin password hash")
            self._insert_user(db, username, password_hash, True)

    @staticmethod
    def _insert_user(db, username, password_hash, is_admin, status="active"):
        user_id = uuid.uuid4().hex
        try:
            db.execute(
                "INSERT INTO users (id, username, username_key, password_hash, "
                "is_admin, "
                "is_active, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    username.strip(),
                    username_key(username),
                    password_hash,
                    int(is_admin),
                    int(status == "active"),
                    time.time(),
                    status,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("username already exists") from error
        return public_user(
            db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        )

    def create_user(
        self, username: str, password_hash: str, *, pending: bool = False
    ) -> dict:
        with self.transaction() as db:
            return self._insert_user(
                db, username, password_hash, False, "pending" if pending else "active"
            )

    def find_user(self, username: str) -> sqlite3.Row | None:
        try:
            key = username_key(username)
        except ValueError:
            return None
        with self._lock:
            return self._db.execute(
                "SELECT * FROM users WHERE username_key=?",
                (key,),
            ).fetchone()

    def list_users(self) -> list[dict]:
        with self._lock:
            return [
                public_user(row)
                for row in self._db.execute(
                    "SELECT * FROM users ORDER BY created_at, id"
                )
            ]

    def set_active(self, user_id: str, active: bool) -> dict:
        return self.review_user(user_id, "active" if active else "disabled")

    def review_user(
        self,
        user_id: str,
        status: str | None = None,
        group_ids: list[str] | None = None,
    ) -> dict:
        if status is not None and status not in {
            "pending",
            "active",
            "disabled",
            "rejected",
        }:
            raise ValueError("invalid account status")
        with self.transaction() as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None:
                raise KeyError(user_id)
            if (
                status is not None
                and status != "active"
                and user["is_admin"]
                and user["is_active"]
            ):
                count = db.execute(
                    "SELECT COUNT(*) FROM users WHERE is_admin=1 AND is_active=1"
                ).fetchone()[0]
                if count <= 1:
                    raise ValueError("cannot disable the last active administrator")
            if status is not None:
                db.execute(
                    "UPDATE users SET status=?, is_active=? WHERE id=?",
                    (status, int(status == "active"), user_id),
                )
                if status != "active":
                    db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            if group_ids is not None:
                for group_id in group_ids:
                    if not db.execute(
                        "SELECT 1 FROM file_groups WHERE id=?", (group_id,)
                    ).fetchone():
                        raise ValueError("unknown file group")
                db.execute("DELETE FROM user_groups WHERE user_id=?", (user_id,))
                db.executemany(
                    "INSERT INTO user_groups VALUES (?, ?)",
                    [(user_id, group_id) for group_id in set(group_ids)],
                )
            return public_user(
                db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            )

    def sync_groups(self, entries: list[tuple[str, str]]) -> list[dict]:
        groups = []
        with self.transaction() as db:
            for key, name in entries:
                db.execute(
                    "INSERT INTO file_groups VALUES (?, ?, ?) "
                    "ON CONFLICT(storage_key) DO UPDATE SET name=excluded.name",
                    (uuid.uuid4().hex, key, name),
                )
                row = db.execute(
                    "SELECT id, name FROM file_groups WHERE storage_key=?", (key,)
                ).fetchone()
                groups.append(dict(row))
        return groups

    def group_id(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM file_groups WHERE storage_key=?", (key,)
            ).fetchone()
        return row[0] if row else None

    def user_groups(self, user_id: str) -> list[str]:
        with self._lock:
            return [
                row[0]
                for row in self._db.execute(
                    "SELECT group_id FROM user_groups WHERE user_id=? "
                    "ORDER BY group_id",
                    (user_id,),
                )
            ]

    def add_upload(
        self, slide_id: str, user_id: str, filename: str, display_name: str
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO uploads VALUES (?, ?, ?, ?)",
                (slide_id, user_id, filename, display_name),
            )

    def uploaded(self, slide_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM uploads WHERE id=?", (slide_id,)
            ).fetchone()
        return dict(row) if row else None

    def all_uploads(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM uploads")]

    def consume_registration_attempt(self, client_ip: str) -> bool:
        return self._consume_limits(
            [("registration:" + client_ip, 5), ("registration:all", 60)]
        )

    def consume_login_attempt(self, username: str, client_ip: str) -> bool:
        return self._consume_limits(
            [
                (
                    "user:"
                    + unicodedata.normalize("NFKC", username).strip().casefold(),
                    10,
                ),
                ("ip:" + client_ip, 60),
            ]
        )

    def _consume_limits(self, keys: list[tuple[str, int]]) -> bool:
        now = time.time()
        # Hash bucket identifiers so guesses and client addresses are not stored.
        keys = [(hashlib.sha256(k.encode()).hexdigest(), limit) for k, limit in keys]
        with self.transaction() as db:
            db.execute("DELETE FROM login_limits WHERE started_at<=?", (now - 60,))
            for key, limit in keys:
                row = db.execute(
                    "SELECT attempts FROM login_limits WHERE key=?", (key,)
                ).fetchone()
                if row and row[0] >= limit:
                    return False
            for key, _ in keys:
                db.execute(
                    "INSERT INTO login_limits VALUES (?, ?, 1) "
                    "ON CONFLICT(key) DO UPDATE SET attempts=attempts+1",
                    (key, now),
                )
        return True

    def issue_session(
        self, user_id: str, expected_hash: str, old_key: str, lifetime: int
    ) -> dict | None:
        now = time.time()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.transaction() as db:
            # Recheck after hashing, which ran outside this transaction.
            user = db.execute(
                "SELECT * FROM users WHERE id=? AND password_hash=? AND is_active=1",
                (user_id, expected_hash),
            ).fetchone()
            if user is None:
                return None
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            # A login only rotates a previous session belonging to this account.
            db.execute(
                "DELETE FROM sessions WHERE token_hash=? AND user_id=?",
                (old_key, user_id),
            )
            db.execute(
                "DELETE FROM sessions WHERE token_hash IN ("
                "SELECT token_hash FROM sessions WHERE user_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET 31)",
                (user_id,),
            )
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                (key, user_id, csrf, now, now + lifetime),
            )
        return {
            "token": token,
            "csrf_token": csrf,
            "expires_at": now + lifetime,
            "user": public_user(user),
        }

    def session(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT users.*, sessions.csrf_token, sessions.expires_at "
                "FROM sessions JOIN users ON users.id=sessions.user_id "
                "WHERE token_hash=? AND expires_at>? AND is_active=1",
                (key, time.time()),
            ).fetchone()
        if row is None:
            return None
        return {
            "user": public_user(row),
            "csrf_token": row["csrf_token"],
            "expires_at": row["expires_at"],
        }

    def revoke(self, key: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (key,))

    def revoke_all(self, user_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def change_password(self, user_id: str, expected_hash: str, new_hash: str) -> bool:
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE users SET password_hash=? WHERE id=? AND password_hash=? "
                "AND is_active=1",
                (new_hash, user_id, expected_hash),
            ).rowcount
            if changed:
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return bool(changed)
