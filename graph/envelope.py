"""Event creation with dedup + lineage (design §4.3/§4.4).

Exact-hash duplicates become mirror events attached to the original's lineage
and never re-enter processing. Near-duplicate detection comes later.
"""
import hashlib

from . import artifacts


def ingest(con, source_id, connector: str, content: bytes, mime: str, ext: str,
           published_at=None, meta: dict | None = None):
    """Returns (event_id, is_new). Mirrors get is_new=False."""
    digest = hashlib.sha256(content).digest()
    existing = con.execute(
        "select event_id, lineage_id from event where content_hash=%s "
        "and lineage_id is not null order by fetched_at limit 1", (digest,)).fetchone()
    if existing:
        row = con.execute(
            "insert into event (source_id, connector, published_at, content_hash, "
            "artifact_uri, mime, status, lineage_id, meta) "
            "select %s, %s, %s, content_hash, artifact_uri, mime, 'duplicate', "
            "lineage_id, %s from event where event_id=%s returning event_id",
            (source_id, connector, published_at, psycopg_json(meta), existing["event_id"]),
        ).fetchone()
        return row["event_id"], False

    slug = connector
    uri, _ = artifacts.put(slug, content, ext)
    row = con.execute(
        "insert into event (source_id, connector, published_at, content_hash, "
        "artifact_uri, mime, status, meta) values (%s,%s,%s,%s,%s,%s,'pending',%s) "
        "returning event_id",
        (source_id, connector, published_at, digest, uri, mime, psycopg_json(meta)),
    ).fetchone()
    event_id = row["event_id"]
    lineage = con.execute(
        "insert into lineage (root_event_id) values (%s) returning lineage_id",
        (event_id,)).fetchone()["lineage_id"]
    con.execute("update event set lineage_id=%s where event_id=%s", (lineage, event_id))
    return event_id, True


def psycopg_json(obj):
    from psycopg.types.json import Jsonb
    return Jsonb(obj) if obj is not None else None
