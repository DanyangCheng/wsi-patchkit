"""Durable account-scoped workbench records, separate from credentials."""

from __future__ import annotations

import base64
import json
import math
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
        priority = (
            "CASE WHEN json_extract(data, '$.status') IN ('queued','running') "
            "THEN 0 ELSE 1 END"
        )
        chronological = "json_extract(data, '$.created_at') DESC, key DESC"
        by_slide = "owner, kind, json_extract(data, '$.slide_id'), "
        for name, fields in {
            "jobs_time": f"owner, kind, {chronological}",
            "jobs_slide": by_slide + chronological,
            "jobs_status": (
                "owner, kind, json_extract(data, '$.status'), "
                "json_extract(data, '$.updated_at') DESC"
            ),
            "jobs_slide_priority": f"{by_slide}{priority}, {chronological}",
        }.items():
            self.db.execute(
                f"CREATE INDEX IF NOT EXISTS {name} ON personal_records "
                f"({fields}) WHERE kind='job'"
            )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS personal_migrations (name TEXT PRIMARY KEY)"
        )
        # Preserve result metadata before any history retention can run. A one-time
        # migration also prevents deleted results from being recreated on restart.
        if not self.db.execute(
            "SELECT 1 FROM personal_migrations WHERE name='crop_provenance'"
        ).fetchone():
            self.db.execute(
                "INSERT OR IGNORE INTO personal_records(owner, kind, key, data) "
                "SELECT owner, 'crop', json_extract(data, '$.filename'), data "
                "FROM personal_records WHERE kind='job' "
                "AND json_extract(data, '$.status')='completed' ORDER BY rowid DESC"
            )
            self.db.execute(
                "INSERT INTO personal_migrations VALUES ('crop_provenance')"
            )
        self.db.commit()

    def put(self, owner: str, kind: str, key: str, data: dict) -> dict:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO personal_records VALUES (?, ?, ?, ?)",
                (owner, kind, key, json.dumps(data, ensure_ascii=False)),
            )
            if kind == "job" and data.get("status") == "completed":
                self.db.execute(
                    "INSERT OR REPLACE INTO personal_records VALUES (?, 'crop', ?, ?)",
                    (owner, data["filename"], json.dumps(data, ensure_ascii=False)),
                )
        return data

    def jobs_page(
        self,
        owner,
        *,
        limit=20,
        cursor=None,
        slide_id=None,
        status=None,
        since=None,
        until=None,
        active_first=False,
    ):
        clauses, args = ["owner=?", "kind='job'"], [owner]
        for field, value, operator in (
            ("slide_id", slide_id, "="),
            ("created_at", since, ">="),
            ("created_at", until, "<="),
        ):
            if value is not None:
                clauses.append(f"json_extract(data, '$.{field}') {operator} ?")
                args.append(value)
        where = " AND ".join(clauses)
        with self.lock:
            counts = dict(
                self.db.execute(
                    "SELECT json_extract(data, '$.status'), COUNT(*) "
                    f"FROM personal_records WHERE {where} "
                    "GROUP BY json_extract(data, '$.status')",
                    args,
                ).fetchall()
            )
            if status:
                clauses.append("json_extract(data, '$.status')=?")
                args.append(status)
            rank = (
                "CASE WHEN json_extract(data, '$.status') IN ('queued','running') "
                "THEN 0 ELSE 1 END"
                if active_first
                else "1"
            )
            created = "json_extract(data, '$.created_at')"
            ordering = (
                f"priority, {created} DESC, key DESC"
                if active_first
                else f"{created} DESC, key DESC"
            )
            if cursor:
                try:
                    rank_value, timestamp, key = json.loads(
                        base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                    )
                    if (
                        type(rank_value) is not int
                        or rank_value not in (0, 1)
                        or type(timestamp) not in (int, float)
                        or not math.isfinite(timestamp)
                        or not isinstance(key, str)
                        or len(key) > 128
                    ):
                        raise ValueError
                except (ValueError, TypeError, OverflowError) as error:
                    raise ValueError("invalid job cursor") from error
                clauses.append(
                    f"({rank} > ? OR ({rank} = ? AND "
                    f"({created} < ? OR ({created} = ? AND key < ?))))"
                )
                args.extend([rank_value, rank_value, timestamp, timestamp, key])
            rows = self.db.execute(
                f"SELECT data, {rank} AS priority FROM personal_records "
                f"WHERE {' AND '.join(clauses)} ORDER BY {ordering} LIMIT ?",
                [*args, limit + 1],
            ).fetchall()
        items = [json.loads(row[0]) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = (
                base64.urlsafe_b64encode(
                    json.dumps(
                        [rows[limit - 1][1], last.get("created_at", 0), last["job_id"]]
                    ).encode()
                )
                .decode()
                .rstrip("=")
            )
        return {"items": items, "next_cursor": next_cursor, "counts": counts}

    def prune_jobs(self, *, cutoff, maximum, batch_size=250):
        """Remove ended jobs in small transactions; retain every active job."""
        total = 0
        while True:
            with self.lock, self.db:
                rows = self.db.execute(
                    "WITH ended AS (SELECT rowid AS id, "
                    "COALESCE(json_extract(data, '$.updated_at'), 0) AS ended_at, "
                    "ROW_NUMBER() OVER (PARTITION BY owner ORDER BY "
                    "COALESCE(json_extract(data, '$.updated_at'), 0) DESC, "
                    "key DESC) AS position "
                    "FROM personal_records WHERE kind='job' AND "
                    "json_extract(data, '$.status') "
                    "IN ('completed','failed','cancelled')) "
                    "SELECT id FROM ended WHERE ended_at < ? OR position > ? LIMIT ?",
                    (cutoff, maximum, batch_size),
                ).fetchall()
                self.db.executemany("DELETE FROM personal_records WHERE rowid=?", rows)
            total += len(rows)
            if len(rows) < batch_size:
                return total

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
