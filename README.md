# Artifact Mirror

A small, read-only artifact mirror fed by numbered `publish`/`delete`
operations from a primary site. Built with FastAPI and SQLite, started with
Docker Compose.

The mirror must satisfy two properties that drive the design:

1. **A withdrawn package never comes back because of a late sync.** Downloads
   always reflect the highest sequence number received for a path. A late
   duplicate of an older operation is acknowledged but cannot change state —
   even after a restart and after the older receipt was cleaned up.
2. **Ingest tolerates reordering and re-sending.** Operations may arrive in any
   order and any number of times; the mirror converges on the same state.

## Quick start

```bash
docker compose up --build -d
curl -s localhost:8000/status | jq .
./demo/demo.sh                 # out-of-order ingest, GC, resend
./demo/legacy_upgrade.sh       # upgrade a pre-digest database, then resend
```

`demo/legacy_upgrade.sh` uses its own Compose file and volume (port 18000), so
it does not disturb the main demo. It seeds a database written by the previous
schema, starts the current image (which migrates it on startup), and shows a
verbatim resend, a content conflict, and two unverifiable legacy receipts.

Without Docker:

```bash
python3 -m pip install -r requirements.txt
MIRROR_DB_PATH=./data/mirror.db uvicorn app.main:app --port 8000
```

Tests (no Docker needed):

```bash
python3 -m pip install -r requirements.txt pytest httpx
python3 -m pytest -q
```

## Data model

| table       | meaning |
|-------------|---------|
| `receipts`  | one row per sequence number actually received, including ones superseded by a newer operation. Carries the SHA-256 of the body as first received. Cleared by GC. |
| `artifacts` | latest operation per path. `operation='publish'` serves bytes; `operation='delete'` is a tombstone that makes downloads 404. |
| `meta`      | `gc_watermark` — every number ≤ the watermark has been reclaimed. |

Sequence numbers form one global space across all paths, starting at 1, sent
by the primary along with the path. The mirror never renumbers and never
fills gaps itself.

## API

### `POST /events` — ingest

`multipart/form-data` with a `manifest` field (JSON array) and one binary field
per publish.

```json
[{"seq": 1, "path": "team/a.tgz", "operation": "publish",  "file": "f0"},
 {"seq": 2, "path": "team/a.tgz", "operation": "delete"}]
```

```bash
curl -X POST localhost:8000/events \
  -F 'manifest=[{"seq":1,"path":"team/a.tgz","operation":"publish","file":"f0"}]' \
  -F 'f0=@a.tgz'
```

Per-event result:

| `status`          | meaning |
|-------------------|---------|
| `accepted`        | new receipt was stored. `applied` says whether it changed the download state; if not, `superseded_by` names the newer sequence in effect. |
| `duplicate`       | that sequence number was already received; acknowledged only. See conflict fields below. |
| `already_cleared` | the sequence number is at or below the GC watermark. Acknowledged only — never applied, so a late resend cannot resurrect a withdrawn package after a restart. Its comparison basis was reclaimed too, so `verification` is `not_available`. |

#### Distinguishing a verbatim resend from the wrong package

When a `duplicate` is a `publish` with the same path and operation, the mirror
compares the submitted bytes against the SHA-256 captured when that sequence
number was **first** received. This works whether the original package is
still live, was superseded by a newer number, or was later withdrawn — the
receipt keeps its own basis. The comparison is a fixed-size digest, so its cost
does not grow with package size.

| field               | meaning |
|---------------------|---------|
| `conflict`          | `true` = body differs from the original; `false` = identical body; `null` = cannot be verified. |
| `content_verified`  | `true`/`false`/`null` mirroring the verification outcome. |
| `verification`      | `match`, `mismatch`, `unverifiable`, `not_applicable` (delete), or `not_comparable` (path/operation differs). |
| `original_sha256`   | digest recorded at first receipt (`null` when lost). |
| `received_sha256`   | digest of the body in this resend. |

The first write always wins: a mismatched or unverifiable resend is
acknowledged but never replaces the original receipt or the served artifact.
If an older database has a publish receipt whose original body was already lost
(its package was superseded, replaced or withdrawn before the upgrade), the
digest is absent. The receipt is reported as `verification="unverifiable"`, and
the resend is **not** adopted as the new original, so the situation cannot be
silently normalised. Receipts whose package is still the live artifact are
backfilled automatically on startup.

The digest is part of the receipt row: it is committed or rolled back with the
same batch and deleted by GC along with the receipt. After a restart the same
basis is used again.

A batch is one SQLite transaction: either every receipt in it is stored, or
none is.

### `GET /artifacts/{path}` — download

`200` with the current bytes (`ETag`, `X-Sequence`), or `404` when the path was
never published or its highest-numbered operation is a delete.

### `GET /artifacts` — catalog

Lists live artifacts (`?include_deleted=true` also shows tombstones).

### `GET /events` — received operations

Paginated receipt feed (`?after=<seq>&limit=<n>`), useful for auditing.

### `POST /gc` — reclaim receipts

```json
{"limit": 5}
```

Starts after the saved watermark, reclaims at most `limit` **contiguous**
receipts, stops at the first gap, and deletes deletion tombstones whose latest
sequence falls inside the reclaimed range. Returns
`cleared` (the numbers reclaimed), `watermark_before/after`,
`stopped_reason` (`limit_reached` or `gap`) and `tombstones_reclaimed`.

Receipt deletion, tombstone deletion and the watermark update are one
transaction. On any failure the whole run rolls back: no receipt is reclaimed
and progress does not move, so a retry starts from the same point.

### `GET /status`, `GET /healthz`

Watermark, next expected number, receipt counts and catalog counts.

## Behaviour walkthrough

The sequence below is what `demo/demo.sh` shows.

```text
seq 4 delete team/a.tgz   arrives first        -> applied (tombstone)
seq 2 publish team/a.tgz  arrives next         -> receipt stored, superseded by 4
seq 1 publish team/b.tgz                       -> applied
GET team/a.tgz -> 404, GET team/b.tgz -> 200

POST /gc {"limit": 2}  -> cleared [1, 2], stopped at gap (3 missing)
seq 3 publish team/c.tgz                       -> applied
POST /gc {"limit": 10} -> cleared [3, 4], tombstone for team/a.tgz reclaimed

resend seq 2 (publish team/a.tgz) -> already_cleared, acknowledged only
GET team/a.tgz -> 404 (still deleted)

resend seq 5 (publish pkg/d.tgz, identical bytes) -> duplicate, conflict=false
resend seq 5 (publish pkg/d.tgz, different bytes) -> duplicate, conflict=true
withdraw pkg/d.tgz (seq 6), resend seq 5 with other bytes -> conflict=true, 404
```

That last line is the key property: the tombstone row was reclaimed, yet the
path stays withdrawn because numbers at or below the watermark can never be
applied again. The fact that the watermark advanced is what keeps the deletion
effective across restarts.

## Limits and configuration

| variable | default | purpose |
|----------|---------|---------|
| `MIRROR_DB_PATH` | `data/mirror.db` | SQLite file (persisted in the Compose volume) |
| `MIRROR_MAX_ARTIFACT_BYTES` | 64 MiB | per-artifact upload cap (413) |
| `MIRROR_MAX_BATCH_BYTES` | 256 MiB | per-request upload cap (413) |

At most 500 events per request, GC `limit` ≤ 1000.

## Known limitations

See the delivery notes in the task response: no authentication, artifact bytes
are stored in SQLite (fine for small packages, not for large ones), and no
background retry — ingest is driven by the primary.
