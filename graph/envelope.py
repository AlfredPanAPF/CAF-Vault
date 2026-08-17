"""Event creation with dedup + lineage (design §4.3/§4.4).

Exact-hash duplicates become mirror events attached to the original's lineage
and never re-enter processing. Near-duplicates — simhash within 3 bits of a
recent event — become mirrors the same way, but keep their own artifact and
content hash: syndicated copies differ by a few words, not zero.
"""
import hashlib

from . import artifacts

RECENT_WINDOW = 2000    # events compared for near-dup (§4.4)
NEAR_DUP_BITS = 3


def simhash64(text: str) -> int:
    """64-bit simhash over lowercase word 3-shingles, returned SIGNED so it
    fits a postgres bigint. Shingles are hashed with blake2b — Python's
    built-in hash() is salted per process and would never match across runs."""
    words = text.lower().split()
    shingles = ([" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
                or ([" ".join(words)] if words else []))
    counts = [0] * 64
    for s in shingles:
        h = int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(),
                           "big")
        for bit in range(64):
            counts[bit] += 1 if (h >> bit) & 1 else -1
    value = sum(1 << bit for bit in range(64) if counts[bit] > 0)
    return value - (1 << 64) if value >= (1 << 63) else value


def _hamming(a: int, b: int) -> int:
    # xor of signed ints, masked back to the unsigned 64-bit space
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()


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
            "artifact_uri, mime, status, lineage_id, simhash, meta) "
            "select %s, %s, %s, content_hash, artifact_uri, mime, 'duplicate', "
            "lineage_id, simhash, %s from event where event_id=%s returning event_id",
            (source_id, connector, published_at, psycopg_json(meta), existing["event_id"]),
        ).fetchone()
        return row["event_id"], False

    simhash = simhash64(content.decode("utf-8", errors="replace"))
    near = None
    for r in con.execute(
            "select event_id, lineage_id, simhash from event "
            "where simhash is not null and lineage_id is not null "
            "order by fetched_at desc limit %s", (RECENT_WINDOW,)):
        if _hamming(simhash, r["simhash"]) <= NEAR_DUP_BITS:
            near = r
            break

    slug = connector
    uri, _ = artifacts.put(slug, content, ext)
    if near:
        # mirror on the match's lineage, but with its own artifact/hash/simhash
        # — the content genuinely differs, and the row must dedup future copies
        meta = {**(meta or {}), "near_duplicate_of": str(near["event_id"])}
        row = con.execute(
            "insert into event (source_id, connector, published_at, content_hash, "
            "artifact_uri, mime, status, lineage_id, simhash, meta) "
            "values (%s,%s,%s,%s,%s,%s,'duplicate',%s,%s,%s) returning event_id",
            (source_id, connector, published_at, digest, uri, mime,
             near["lineage_id"], simhash, psycopg_json(meta))).fetchone()
        return row["event_id"], False

    row = con.execute(
        "insert into event (source_id, connector, published_at, content_hash, "
        "artifact_uri, mime, status, simhash, meta) "
        "values (%s,%s,%s,%s,%s,%s,'pending',%s,%s) returning event_id",
        (source_id, connector, published_at, digest, uri, mime, simhash,
         psycopg_json(meta))).fetchone()
    event_id = row["event_id"]
    lineage = con.execute(
        "insert into lineage (root_event_id) values (%s) returning lineage_id",
        (event_id,)).fetchone()["lineage_id"]
    con.execute("update event set lineage_id=%s where event_id=%s", (lineage, event_id))
    return event_id, True


def psycopg_json(obj):
    from psycopg.types.json import Jsonb
    return Jsonb(obj) if obj is not None else None
