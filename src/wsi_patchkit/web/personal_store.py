"""Durable account-scoped workbench records, separate from credentials."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path


class PersonalStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS personal_records ("
            "owner TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, "
            "data TEXT NOT NULL, PRIMARY KEY(owner, kind, key))"
        )
        self.db.commit()

    def put(self, owner: str, kind: str, key: str, data: dict) -> dict:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO personal_records VALUES (?, ?, ?, ?)",
                (owner, kind, key, json.dumps(data, ensure_ascii=False)),
            )
        return data

    def get(self, owner: str, kind: str, key: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT data FROM personal_records WHERE owner=? AND kind=? AND key=?",
                (owner, kind, key),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, owner: str, kind: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM personal_records WHERE owner=? AND kind=? "
                "ORDER BY rowid DESC",
                (owner, kind),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, owner: str, kind: str, key: str):
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM personal_records WHERE owner=? AND kind=? AND key=?",
                (owner, kind, key),
            )

    def close(self):
        with self.lock:
            self.db.close()
