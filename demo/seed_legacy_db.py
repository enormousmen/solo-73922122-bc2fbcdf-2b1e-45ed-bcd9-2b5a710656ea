#!/usr/bin/env python3
"""Create a database written by the previous (pre-digest) schema.

Used by ``demo/legacy_upgrade.sh``.  The database contains five receipts that
exercise the two upgrade paths:

* ``seq 1`` publish of ``releases/live.tgz`` -- still the live artifact, so
  its body is available and the upgrade can backfill a comparison digest.
* ``seq 2`` publish of ``releases/lost.tgz`` -- superseded by ``seq 3``, so
  its original body is gone.
* ``seq 4`` publish of ``releases/gone.tgz`` -- withdrawn by ``seq 5``, so its
  original body is gone too.

Run with the Python standard library only:

    python3 demo/seed_legacy_db.py /path/to/mirror.db
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import datetime, timezone

LEGACY_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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

NOW = datetime.now(timezone.utc).isoformat(timespec="microseconds")


def seed(db_path: str) -> None:
    receipts: list[tuple] = [
        (1, "releases/live.tgz", "publish", NOW),
        (2, "releases/lost.tgz", "publish", NOW),
        (3, "releases/lost.tgz", "publish", NOW),
        (4, "releases/gone.tgz", "publish", NOW),
        (5, "releases/gone.tgz", "delete", NOW),
    ]
    live = b"live-package-v1"
    lost_latest = b"lost-package-v3"
    artifacts = [
        (
            "releases/live.tgz", 1, "publish", live, "application/octet-stream",
            hashlib.sha256(live).hexdigest(), len(live), NOW,
        ),
        (
            "releases/lost.tgz", 3, "publish", lost_latest,
            "application/octet-stream", hashlib.sha256(lost_latest).hexdigest(),
            len(lost_latest), NOW,
        ),
        (
            "releases/gone.tgz", 5, "delete", None, None, None, 0, NOW,
        ),
    ]

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('gc_watermark', '0')")
        conn.executemany("INSERT INTO receipts VALUES (?, ?, ?, ?)", receipts)
        conn.executemany("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         artifacts)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} DB_PATH")
    seed(sys.argv[1])
    print(f"seeded legacy database at {sys.argv[1]}")
