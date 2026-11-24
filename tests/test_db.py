"""Concurrency and durability tests against the real SQLite file.

These deliberately bypass the API and call the db layer from several threads
to show that ingest and GC are serialised by SQLite transactions rather than
by the process-global GIL.
"""

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "mirror.db"
    db.init_db(path)
    return path


def test_parallel_ingest_keeps_all_receipts(db_path):
    events = [
        {"seq": seq, "path": f"p{seq}.tgz", "operation": "publish",
         "content_bytes": b"data", "media_type": "application/octet-stream"}
        for seq in range(1, 51)
    ]
    errors = []

    def worker(batch):
        try:
            db.ingest_events(db_path, batch)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(events[i:i + 5],))
        for i in range(0, len(events), 5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    conn = db.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM receipts").fetchone()["n"]
    finally:
        conn.close()
    assert count == 50


def test_concurrent_gc_never_passes_a_gap(db_path):
    db.ingest_events(
        db_path,
        [{"seq": seq, "path": f"p{seq}", "operation": "delete"}
         for seq in (1, 2, 3, 5, 6)],
    )
    results = []
    lock = threading.Lock()

    def worker():
        result = db.run_gc(db_path, 3)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    cleared = sorted(seq for result in results for seq in result["cleared"])
    # Each number is reclaimed exactly once, and seq 4 is never skipped.
    assert cleared == [1, 2, 3]
    status = db.gc_status(db_path)
    assert status["watermark"] == 3
    assert status["next_received_seq"] == 5


def test_data_survives_reopen(db_path):
    db.ingest_events(
        db_path,
        [{"seq": 1, "path": "a", "operation": "publish",
          "content_bytes": b"persisted", "media_type": "text/plain"}],
    )
    # A new connection is a new "process" view of the same file.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM artifacts WHERE path = 'a'").fetchone()
    finally:
        conn.close()
    assert row["operation"] == "publish"
    assert bytes(row["content"]) == b"persisted"


# ---------------------------------------------------------------------------
# Upgrading a database written by the previous schema (no receipt digest)
# ---------------------------------------------------------------------------

LEGACY_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE receipts (
    seq         INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    operation   TEXT NOT NULL CHECK (operation IN ('publish', 'delete')),
    received_at TEXT NOT NULL
);
CREATE TABLE artifacts (
    path        TEXT PRIMARY KEY,
    latest_seq  INTEGER NOT NULL,
    operation   TEXT NOT NULL CHECK (operation IN ('publish', 'delete')),
    content     BLOB,
    media_type  TEXT,
    sha256      TEXT,
    size        INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);
"""


def _make_legacy_db(path, receipts, artifacts):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('gc_watermark', '0')")
        conn.executemany(
            "INSERT INTO receipts(seq, path, operation, received_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00')",
            receipts,
        )
        conn.executemany(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, "
            "'2026-01-01T00:00:00+00:00')",
            artifacts,
        )
        conn.commit()
    finally:
        conn.close()


def _digest(body: bytes) -> str:
    import hashlib

    return hashlib.sha256(body).hexdigest()


def _ingest_one(db_path, seq, path, operation, body=b""):
    results, _ = db.ingest_events(
        db_path,
        [{
            "seq": seq,
            "path": path,
            "operation": operation,
            "content_bytes": body,
            "media_type": "application/octet-stream",
        }],
    )
    return results[0]


def test_upgrade_backfills_digest_for_still_live_package(tmp_path):
    path = tmp_path / "mirror.db"
    body = b"still-live"
    _make_legacy_db(
        path,
        receipts=[(1, "a.tgz", "publish")],
        artifacts=[("a.tgz", 1, "publish", body, "text/plain", _digest(body),
                    len(body))],
    )
    db.init_db(path)

    match = _ingest_one(path, 1, "a.tgz", "publish", body)
    assert match["conflict"] is False
    assert match["verification"] == "match"
    mismatch = _ingest_one(path, 1, "a.tgz", "publish", b"different")
    assert mismatch["conflict"] is True
    assert mismatch["verification"] == "mismatch"
    # The upgrade did not change the served bytes.
    assert bytes(db.get_artifact(path, "a.tgz")["content"]) == body


def test_upgrade_marks_lost_body_unverifiable_without_adopting_resend(tmp_path):
    path = tmp_path / "mirror.db"
    # seq 1 was a publish for a.tgz, superseded by the newer publish seq 2 whose
    # body is all that remains.  The seq 1 body is therefore lost.
    _make_legacy_db(
        path,
        receipts=[(1, "a.tgz", "publish"), (2, "a.tgz", "publish")],
        artifacts=[("a.tgz", 2, "publish", b"v2", "text/plain",
                    _digest(b"v2"), 2)],
    )
    db.init_db(path)

    result = _ingest_one(path, 1, "a.tgz", "publish", b"guessed-original")
    assert result["status"] == "duplicate"
    assert result["conflict"] is None
    assert result["content_verified"] is None
    assert result["verification"] == "unverifiable"
    assert result["original_sha256"] is None
    assert result["received_sha256"] == _digest(b"guessed-original")

    # The resend did not become the original: a different resend is still
    # unverifiable rather than matching the first resend.
    again = _ingest_one(path, 1, "a.tgz", "publish", b"something-else")
    assert again["verification"] == "unverifiable"
    # The live package and the newer receipt are unchanged.
    assert bytes(db.get_artifact(path, "a.tgz")["content"]) == b"v2"
    conn = db.connect(path)
    try:
        stored = conn.execute(
            "SELECT content_sha256 FROM receipts WHERE seq = 1"
        ).fetchone()["content_sha256"]
    finally:
        conn.close()
    assert stored is None


def test_upgrade_marks_lost_body_unverifiable_after_withdrawal(tmp_path):
    path = tmp_path / "mirror.db"
    _make_legacy_db(
        path,
        receipts=[(1, "a.tgz", "publish"), (2, "a.tgz", "delete")],
        artifacts=[("a.tgz", 2, "delete", None, None, None, 0)],
    )
    db.init_db(path)

    result = _ingest_one(path, 1, "a.tgz", "publish", b"whatever")
    assert result["verification"] == "unverifiable"
    assert result["conflict"] is None
    # Still withdrawn.
    assert db.get_artifact(path, "a.tgz")["operation"] == "delete"
    assert db.get_artifact(path, "other.tgz") is None


def test_upgrade_is_idempotent_across_restarts(tmp_path):
    path = tmp_path / "mirror.db"
    body = b"live"
    _make_legacy_db(
        path,
        receipts=[(1, "a.tgz", "publish")],
        artifacts=[("a.tgz", 1, "publish", body, "text/plain", _digest(body),
                    len(body))],
    )
    db.init_db(path)
    db.init_db(path)  # second startup must not fail or lose the digest

    conn = db.connect(path)
    try:
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(receipts)")]
        stored = conn.execute(
            "SELECT content_sha256 FROM receipts WHERE seq = 1"
        ).fetchone()["content_sha256"]
    finally:
        conn.close()
    assert "content_sha256" in columns
    assert columns.count("content_sha256") == 1
    assert stored == _digest(body)


def test_new_receipts_record_digest_and_gc_removes_it(tmp_path):
    path = tmp_path / "mirror.db"
    db.init_db(path)
    _ingest_one(path, 1, "a.tgz", "publish", b"abc")

    conn = db.connect(path)
    try:
        stored = conn.execute(
            "SELECT content_sha256 FROM receipts WHERE seq = 1"
        ).fetchone()["content_sha256"]
    finally:
        conn.close()
    assert stored == _digest(b"abc")

    db.run_gc(path, 10)
    conn = db.connect(path)
    try:
        rows = conn.execute("SELECT COUNT(*) AS n FROM receipts").fetchone()["n"]
    finally:
        conn.close()
    assert rows == 0


def test_upgraded_digest_still_compares_after_live_package_is_replaced(tmp_path):
    path = tmp_path / "mirror.db"
    original = b"original-v1"
    _make_legacy_db(
        path,
        receipts=[(1, "a.tgz", "publish")],
        artifacts=[("a.tgz", 1, "publish", original, "text/plain",
                    _digest(original), len(original))],
    )
    db.init_db(path)

    # A newer publish replaces the served package after the upgrade.
    assert _ingest_one(path, 2, "a.tgz", "publish", b"replacement")["applied"]

    # The receipt digest belongs to seq 1 and survives that replacement, so a
    # resend of seq 1 is still compared against the *first* body.
    assert _ingest_one(path, 1, "a.tgz", "publish", original)["conflict"] is False
    mismatch = _ingest_one(path, 1, "a.tgz", "publish", b"replacement")
    assert mismatch["conflict"] is True
    assert mismatch["verification"] == "mismatch"
    assert bytes(db.get_artifact(path, "a.tgz")["content"]) == b"replacement"


def test_restart_does_not_overwrite_a_recorded_digest(tmp_path):
    path = tmp_path / "mirror.db"
    db.init_db(path)
    _ingest_one(path, 1, "a.tgz", "publish", b"first")
    _ingest_one(path, 2, "a.tgz", "publish", b"second")

    db.init_db(path)  # simulate restart; migration must leave seq 1 alone

    conn = db.connect(path)
    try:
        first = conn.execute(
            "SELECT content_sha256 FROM receipts WHERE seq = 1"
        ).fetchone()["content_sha256"]
        second = conn.execute(
            "SELECT content_sha256 FROM receipts WHERE seq = 2"
        ).fetchone()["content_sha256"]
    finally:
        conn.close()
    assert first == _digest(b"first")
    assert second == _digest(b"second")
