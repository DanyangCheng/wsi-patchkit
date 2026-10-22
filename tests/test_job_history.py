"""Bounded task history and result metadata have independent lifetimes."""

import json
import sqlite3

import pytest

from wsi_patchkit.web.personal_store import PersonalStore


def record(key, *, slide="slide", status="completed", created=100, updated=None):
    return {
        "job_id": key,
        "slide_id": slide,
        "status": status,
        "created_at": created,
        "updated_at": created if updated is None else updated,
        "filename": key + ".png",
        "level": 0,
        "region": {"x": 1, "y": 2, "width": 3, "height": 4},
    }


def test_cursor_pagination_is_stable_and_scoped(tmp_path):
    store = PersonalStore(tmp_path / "history.sqlite3")
    try:
        for key in ("a", "b", "c", "d", "e"):
            store.put("alice", "job", key, record(key))
        store.put("bob", "job", "secret", record("secret"))
        store.put("alice", "job", "other", record("other", slide="other"))
        first = store.jobs_page("alice", slide_id="slide", limit=2)
        assert [j["job_id"] for j in first["items"]] == ["e", "d"]
        store.put("alice", "job", "new", record("new", created=200))
        seen = first["items"][:]
        cursor = first["next_cursor"]
        while cursor:
            page = store.jobs_page("alice", slide_id="slide", limit=2, cursor=cursor)
            seen.extend(page["items"])
            cursor = page["next_cursor"]
        assert [j["job_id"] for j in seen] == ["e", "d", "c", "b", "a"]
        assert first["counts"] == {"completed": 5}
        for cursor in ("not-json", "bnVsbA", "WzEsIHRydWUsICJrIl0"):
            with pytest.raises(ValueError, match="invalid job cursor"):
                store.jobs_page("alice", cursor=cursor)
    finally:
        store.close()


def test_job_filters_and_active_priority(tmp_path):
    store = PersonalStore(tmp_path / "history.sqlite3")
    try:
        for key, status, created in (
            ("old-active", "running", 1),
            ("new", "completed", 100),
            ("failed", "failed", 90),
            ("queued", "queued", 5),
        ):
            store.put("alice", "job", key, record(key, status=status, created=created))
        first = store.jobs_page("alice", limit=2, active_first=True)
        assert [j["job_id"] for j in first["items"]] == ["queued", "old-active"]
        second = store.jobs_page(
            "alice", limit=2, active_first=True, cursor=first["next_cursor"]
        )
        assert [j["job_id"] for j in second["items"]] == ["new", "failed"]
        page = store.jobs_page("alice", since=80, until=100, status="failed")
        assert [j["job_id"] for j in page["items"]] == ["failed"]
        assert page["counts"] == {"completed": 1, "failed": 1}
    finally:
        store.close()


def test_retention_limits_each_owner_and_preserves_active_and_sources(tmp_path):
    store = PersonalStore(tmp_path / "history.sqlite3")
    try:
        for owner in ("alice", "bob"):
            for i in range(8):
                store.put(owner, "job", str(i), record(str(i), created=i * 10))
            for status in ("queued", "running"):
                store.put(
                    owner, "job", status, record(status, status=status, created=0)
                )
            store.put(
                owner,
                "job",
                "expired",
                record("expired", created=0, status="cancelled"),
            )
            store.put(
                owner, "job", "failed", record("failed", created=0, status="failed")
            )
        assert store.prune_jobs(cutoff=50, maximum=2, batch_size=2) == 16
        for owner in ("alice", "bob"):
            assert {j["job_id"] for j in store.list(owner, "job")} == {
                "6",
                "7",
                "queued",
                "running",
            }
            assert store.get(owner, "crop", "0.png")["region"]["x"] == 1
        assert store.prune_jobs(cutoff=50, maximum=2) == 0
    finally:
        store.close()


def test_migration_copies_latest_source_once(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE personal_records (owner TEXT, kind TEXT, key TEXT, "
            "data TEXT, PRIMARY KEY(owner,kind,key))"
        )
        for key in ("old", "new"):
            job = {**record(key), "filename": "same.png"}
            db.execute(
                "INSERT INTO personal_records VALUES ('alice','job',?,?)",
                (key, json.dumps(job)),
            )
    store = PersonalStore(path)
    assert store.get("alice", "crop", "same.png")["job_id"] == "new"
    store.delete("alice", "crop", "same.png")
    store.close()
    store = PersonalStore(path)
    try:
        assert store.get("alice", "crop", "same.png") is None
    finally:
        store.close()


def test_startup_enforces_custom_policy_after_recovering_interrupted_jobs(tmp_path):
    import time

    from wsi_patchkit.web.crops import CropJobQueue

    store = PersonalStore(tmp_path / "history.sqlite3")
    now = time.time()
    for key, status, updated in (
        ("expired", "completed", now - 3 * 86400),
        ("a", "completed", now - 100),
        ("b", "failed", now - 50),
        ("c", "cancelled", now - 10),
        ("interrupted", "running", 1),
    ):
        store.put("local", "job", key, record(key, status=status, created=updated))
    queue = CropJobQueue(
        object(), tmp_path / "crops", store=store, history_days=1, history_limit=2
    )
    try:
        jobs = store.list("local", "job")
        assert {j["job_id"] for j in jobs} == {"interrupted", "c"}
        assert store.get("local", "job", "interrupted")["status"] == "failed"
        assert store.get("local", "crop", "expired.png")["region"]["width"] == 3
    finally:
        queue.close()
        store.close()
