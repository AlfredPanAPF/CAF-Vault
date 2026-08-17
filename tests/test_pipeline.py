"""End-to-end pipeline test: ingest -> extract -> resolve -> materialize.
No network, no real LLM — graph.llm.complete_json is monkeypatched.
Database fixtures (database, con) live in conftest.py."""
import copy
import json
import os

import requests
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from graph import cli, config, db, llm, webapp
from graph.connectors import edgar, manual, rss
from graph.pipeline import extract, materialize, resolve, triage

# Canned extraction output, shaped per graph/prompts/extraction.md.
EXTRACTION = {
    "doc_id": "test",
    "defined_terms": {},
    "mentions": [
        {"surface": "Nvidia", "type": "company", "count": 2},
        {"surface": "AMD", "type": "company", "count": 1},
    ],
    "claims": [
        {
            "subject": {"surface": "Nvidia"},
            "predicate": "supplies",
            "object": {"surface": "AMD"},
            "qualifiers": {"stance": "stated"},
            "valid_from": None,
            "valid_to": None,
            "evidence_quote": "Nvidia supplies accelerator boards to AMD.",
            "confidence": 0.9,
        }
    ],
}

# "Advanced Micro Devices" must appear in the doc text: the resolver's ticker
# tier requires the registrant's name in the document before trusting "AMD".
DOC_TEXT = ("Nvidia supplies accelerator boards to AMD. Advanced Micro Devices\n"
            "confirmed the arrangement in a statement.\n")


def test_pipeline(con, tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **k: copy.deepcopy(EXTRACTION))

    doc = tmp_path / "note.txt"
    doc.write_text(DOC_TEXT)

    event_id = manual.ingest_file(con, str(doc))
    assert event_id is not None
    con.commit()

    # extract only consumes triaged events (worker order: triage first)
    assert extract.run(con, limit=10)["extracted"] == 0
    triage.run(con)
    out = extract.run(con, limit=10)
    assert out["extracted"] == 1
    assert out["claims"] == 1
    event = con.execute("select * from event where event_id=%s",
                        (event_id,)).fetchone()
    assert event["status"] == "extracted"

    resolve.run(con)
    mentions = {r["surface"]: r for r in con.execute(
        "select m.surface, m.resolver, e.registry_refs from mention m "
        "join entity e on e.entity_id = m.resolved_entity "
        "where m.event_id=%s", (event_id,))}
    assert set(mentions) == {"Nvidia", "AMD"}
    assert mentions["Nvidia"]["registry_refs"]["cik"] == "1045810"
    assert mentions["AMD"]["registry_refs"]["cik"] == "2488"

    claim = con.execute("select * from claim where event_id=%s",
                        (event_id,)).fetchone()
    assert claim["subject_entity"] is not None
    assert claim["object_entity"] is not None

    materialize.run(con)
    edges = con.execute("select * from edge where origin='asserted'").fetchall()
    assert len(edges) == 1
    assert edges[0]["predicate"] == "supplies"
    assert edges[0]["src"] == claim["subject_entity"]
    assert edges[0]["dst"] == claim["object_entity"]
    con.commit()

    # envelope dedup: same bytes again -> None, mirror event on the same lineage
    assert manual.ingest_file(con, str(doc)) is None
    dup = con.execute("select * from event where status='duplicate'").fetchone()
    assert dup is not None
    assert dup["lineage_id"] == event["lineage_id"]


# ---------------------------------------------------------------- v1 sources


def test_seed_watchlist_and_podcast_sources(con):
    cli.seed(con, sources=True)
    wl = json.loads(config.WATCHLIST.read_text())
    tickers = {t for ts in wl.values() for t in ts}
    rows = con.execute(
        "select ticker, active, added_by from watchlist").fetchall()
    assert {r["ticker"] for r in rows} == tickers
    assert all(r["active"] and r["added_by"] == "seed" for r in rows)

    feeds = {r["name"]: r for r in con.execute(
        "select name, url, status, added_by from source "
        "where connector='podcast'")}
    assert set(feeds) == {"podcast:unhedged", "podcast:aidailybrief"}
    assert all(f["url"] and f["status"] == "active" and f["added_by"] == "seed"
               for f in feeds.values())

    # re-seeding never overwrites an existing row's active flag
    paused = sorted(tickers)[0]
    con.execute("update watchlist set active=false where ticker=%s", (paused,))
    cli.seed(con, sources=True)
    assert con.execute("select active from watchlist where ticker=%s",
                       (paused,)).fetchone()["active"] is False


def test_edgar_poll_reads_db_watchlist(con, monkeypatch):
    con.execute(
        "insert into watchlist (ticker, sector, active) values "
        "('NVDA', 'tech_ai', true), ('AMD', 'tech_ai', false) "
        "on conflict (ticker) do update set active=excluded.active")
    fetched = []

    def fake_get(url, as_json=False):
        fetched.append(url)
        return {"filings": {"recent": {"form": [], "accessionNumber": [],
                                       "filingDate": [], "primaryDocument": []}}}

    monkeypatch.setattr(edgar, "get", fake_get)
    out = edgar.poll(con)
    assert out["errors"] == 0
    # only the active watchlist ticker gets its submissions fetched
    assert len(fetched) == 1
    assert "1045810" in fetched[0]


RSS_FEED_XML = """<?xml version="1.0"?>
<rss><channel><title>Example Blog</title>
<item>
<title>Nvidia supplies AMD</title>
<link>https://example.com/posts/nvidia-amd</link>
<pubDate>Mon, 04 Aug 2025 12:00:00 GMT</pubDate>
</item>
</channel></rss>
"""

RSS_ARTICLE_HTML = (
    "<html><head><title>Nvidia supplies AMD</title></head><body><article>"
    + "".join(f"<p>Paragraph {i} of the article body, long enough to clear "
              f"the forty-character paragraph floor comfortably.</p>"
              for i in range(8))
    + "</article></body></html>")


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.content = text.encode("utf-8")
        self.encoding = "utf-8"
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=65536):
        yield self.content

    def close(self):
        pass


def test_rss_poll_ingests_and_dedups(con, monkeypatch):
    con.execute(
        "insert into source (name, connector, url, status) values "
        "('exampleblog', 'rss', 'https://example.com/feed.xml', 'active')")

    def fake_get(url, *a, **k):
        return FakeResponse(RSS_FEED_XML if "feed.xml" in url else RSS_ARTICLE_HTML)

    monkeypatch.setattr(requests, "get", fake_get)
    out = rss.poll(con)
    assert out["new"] == 1
    assert out["thin"] == 0
    ev = con.execute("select * from event where connector='rss'").fetchone()
    assert ev["meta"]["feed"] == "exampleblog"
    assert ev["meta"]["item_url"] == "https://example.com/posts/nvidia-amd"
    assert con.execute("select last_polled from source where name='exampleblog'"
                       ).fetchone()["last_polled"] is not None

    # second run dedups on meta item_url
    out2 = rss.poll(con)
    assert out2["new"] == 0
    assert out2["duplicate"] == 1


# webapp API: the endpoints open their own connections and commit; the
# session-scoped test DB is dropped afterwards, so committed rows are fine.
# Tests that need fixture rows commit them on the test connection first.


def _mk_event(con, connector="rss", source_name="apitest-feed",
              status="extracted", meta=None, last_error=None):
    """Insert a committed-ready event + lineage pair directly (no artifact)."""
    source_id = db.get_or_create_source(con, source_name, connector)
    event_id = con.execute(
        "insert into event (source_id, connector, content_hash, artifact_uri, "
        "mime, status, meta, last_error) values "
        "(%s, %s, %s, 'file:///dev/null', 'text/plain', %s, %s, %s) "
        "returning event_id",
        (source_id, connector, os.urandom(32), status,
         Jsonb(meta) if meta is not None else None, last_error)).fetchone()["event_id"]
    lineage_id = con.execute(
        "insert into lineage (root_event_id) values (%s) returning lineage_id",
        (event_id,)).fetchone()["lineage_id"]
    con.execute("update event set lineage_id=%s where event_id=%s",
                (lineage_id, event_id))
    return event_id, lineage_id


def _mk_claim(con, event_id, lineage_id, subject, predicate, obj,
              stance="stated", quote=""):
    return con.execute(
        "insert into claim (subject_surface, predicate_raw, object_surface, "
        "qualifiers, event_id, lineage_id, observed_at, confidence, "
        "extractor, evidence_quote) "
        "values (%s, %s, %s, %s, %s, %s, now(), 0.9, 'test-v1', %s) "
        "returning claim_id",
        (subject, predicate, obj, Jsonb({"stance": stance}), event_id,
         lineage_id, quote)).fetchone()["claim_id"]


def test_extract_json_ignores_braces_inside_strings():
    # a brace inside a string value must not truncate the object (quoted
    # filing text does this constantly), and trailing prose is ignored
    assert llm._extract_json('noise {"a": "b } c", "n": 1} trailing'
                             ) == {"a": "b } c", "n": 1}
    assert llm._extract_json('{"reasoning": "quote says \'... }\' refutes"}'
                             ) == {"reasoning": "quote says '... }' refutes"}
    assert llm._extract_json('```json\n{"a": "{ x"}\n```') == {"a": "{ x"}
    import pytest as _pytest
    with _pytest.raises(ValueError):
        llm._extract_json("no object here")
    with _pytest.raises(ValueError):
        llm._extract_json('{"a": 1')   # genuinely truncated


def test_health_and_unknown_api_path():
    client = TestClient(webapp.app)
    assert client.get("/health").json() == {"ok": True}
    # unknown /api paths must 404, never fall through to the SPA catch-all
    assert client.get("/api/nope").status_code == 404


def test_seat_status(monkeypatch):
    monkeypatch.setattr(llm, "_SEATS", None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_VAULT_1", "tok-a")
    assert llm.seat_status() == [{"seat": 1, "has_token": True,
                                  "latched": False, "kind": None,
                                  "reason": "", "latched_at": None}]
    monkeypatch.setattr(llm, "_SEATS", None)   # drop the fake seat pool


def test_api_status_shape(con):
    client = TestClient(webapp.app)
    con.execute("delete from app_kv")
    con.execute("delete from stage_run")
    con.commit()

    # without a heartbeat: null, but the rest of the shape is intact
    body = client.get("/api/status").json()
    assert body["heartbeat"] is None
    assert body["stages"] == []
    counts = body["counts"]
    for key in ("pending", "extracting", "extracted", "failed", "duplicate"):
        assert isinstance(counts["events"][key], int)
    assert isinstance(counts["claims"], int)
    assert isinstance(counts["claims_7d"], int)
    for key in ("resolved", "queued", "unresolved", "skipped"):
        assert isinstance(counts["mentions"][key], int)
    assert isinstance(counts["entities"], int)
    assert set(counts["edges"]) >= {"asserted", "inferred"}
    assert set(counts["er_queue"]) >= {"pending", "decided", "failed"}
    assert "generated" in counts["hypotheses"]
    assert isinstance(body["sources"], list)
    assert isinstance(body["failed_events"], list)

    # with a heartbeat + stage runs: latest run per stage, run_requested flag
    hb = {"last_cycle_at": "2026-08-17T00:00:00+00:00", "interval_s": 900,
          "seats": [{"seat": 1, "has_token": False, "latched": False,
                     "kind": None, "reason": "", "latched_at": None}]}
    con.execute("insert into app_kv (key, value) values ('worker_heartbeat', %s)",
                (Jsonb(hb),))
    con.execute(
        "insert into stage_run (stage, started_at, finished_at, summary) values "
        "('edgar', now() - interval '20 minutes', now() - interval '19 minutes', %s)",
        (Jsonb({"new": 1}),))
    con.execute(
        "insert into stage_run (stage, started_at, finished_at, summary) values "
        "('edgar', now() - interval '2 minutes', now() - interval '1 minute', %s)",
        (Jsonb({"new": 3}),))
    con.execute(
        "insert into stage_run (stage, started_at, finished_at, error) values "
        "('rss', now() - interval '1 minute', now(), 'boom')")
    con.commit()

    body = client.get("/api/status").json()
    assert body["heartbeat"]["last_cycle_at"] == hb["last_cycle_at"]
    assert body["heartbeat"]["interval_s"] == 900
    assert body["heartbeat"]["seats"][0]["seat"] == 1
    assert body["heartbeat"]["run_requested"] is False

    stages = {s["stage"]: s for s in body["stages"]}
    assert set(stages) == {"edgar", "rss"}          # one row per stage
    assert stages["edgar"]["summary"] == {"new": 3}  # the latest edgar run
    assert stages["edgar"]["finished_at"] is not None
    assert stages["rss"]["error"] == "boom"

    # failed events surface with title from meta
    event_id, _ = _mk_event(con, connector="rss", status="failed",
                            meta={"title": "Broken doc"}, last_error="parse error")
    con.commit()
    body = client.get("/api/status").json()
    failed = {f["event_id"]: f for f in body["failed_events"]}
    assert str(event_id) in failed
    assert failed[str(event_id)]["title"] == "Broken doc"
    assert failed[str(event_id)]["last_error"] == "parse error"

    con.execute("delete from app_kv")
    con.execute("delete from stage_run")
    con.commit()


def test_api_claims_filters(con):
    client = TestClient(webapp.app)
    event_id, lineage_id = _mk_event(
        con, connector="rss", source_name="apitest-claims",
        meta={"title": "Chips daily", "sector": "tech_ai"})
    _mk_claim(con, event_id, lineage_id, "Nvidia", "supplies", "AMD",
              stance="stated", quote="Nvidia supplies xziq boards to AMD.")
    _mk_claim(con, event_id, lineage_id, "Hershey", "acquires", "CocoaCo",
              stance="speculative",
              quote="Hershey may acquire the xziq cocoa processor.")
    con.commit()

    # q isolates our fixture claims from anything other tests committed
    body = client.get("/api/claims", params={"q": "xziq"}).json()
    assert body["total"] == 2
    assert len(body["claims"]) == 2

    # stance + q combine
    body = client.get("/api/claims",
                      params={"q": "xziq", "stance": "speculative"}).json()
    assert body["total"] == 1
    claim = body["claims"][0]
    assert claim["subject"]["surface"] == "Hershey"
    assert claim["predicate"] == "acquires"
    assert claim["object"]["surface"] == "CocoaCo"
    assert claim["stance"] == "speculative"
    assert "cocoa" in claim["evidence_quote"]
    assert claim["connector"] == "rss"
    assert claim["source_name"] == "apitest-claims"
    assert claim["doc_title"] == "Chips daily"
    assert claim["event_id"] == str(event_id)

    # no matches -> empty page, zero total
    body = client.get("/api/claims", params={"q": "zzznomatchzzz"}).json()
    assert body == {"total": 0, "claims": []}

    # invalid source_type is rejected
    assert client.get("/api/claims",
                      params={"source_type": "carrier-pigeon"}).status_code == 400


def test_api_watchlist_post(con):
    client = TestClient(webapp.app)
    r = client.post("/api/watchlist", json={"ticker": "nvda", "sector": "semis"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "ticker": "NVDA", "company": "NVIDIA CORP"}
    row = con.execute("select * from watchlist where ticker='NVDA'").fetchone()
    assert row is not None
    assert row["active"] and row["added_by"] == "web"

    # unknown tickers are rejected with a 400, not inserted
    r = client.post("/api/watchlist", json={"ticker": "ZZZZ9"})
    assert r.status_code == 400
    assert "Unknown ticker" in r.json()["detail"]
    assert con.execute("select 1 from watchlist where ticker='ZZZZ9'"
                       ).fetchone() is None

    # toggle flips active
    r = client.post("/api/watchlist/NVDA/toggle")
    assert r.json() == {"ok": True, "active": False}
    r = client.post("/api/watchlist/NVDA/toggle")
    assert r.json() == {"ok": True, "active": True}
    assert client.post("/api/watchlist/ZZZZ9/toggle").status_code == 404


def test_api_feeds_post(con):
    client = TestClient(webapp.app)
    r = client.post("/api/feeds", json={
        "name": "chipsblog2", "url": "https://chips2.example.com/rss",
        "kind": "rss"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    row = con.execute("select * from source where source_id=%s",
                      (body["source_id"],)).fetchone()
    assert row["name"] == "chipsblog2"
    assert row["status"] == "active"
    assert row["added_by"] == "web"

    # duplicate name+kind -> 409
    r = client.post("/api/feeds", json={
        "name": "chipsblog2", "url": "https://chips2.example.com/rss",
        "kind": "rss"})
    assert r.status_code == 409

    # podcast names get the podcast: prefix; bad kinds are rejected
    r = client.post("/api/feeds", json={
        "name": "chipcast", "url": "https://chips2.example.com/pod.xml",
        "kind": "podcast"})
    assert r.status_code == 200
    assert con.execute("select 1 from source where name='podcast:chipcast' "
                       "and connector='podcast'").fetchone() is not None
    assert client.post("/api/feeds", json={
        "name": "x", "url": "https://x.example.com", "kind": "atom"
    }).status_code == 400


def test_api_upload(con):
    client = TestClient(webapp.app)
    payload = b"Vendor xziq signed a supply agreement with Hershey."
    r = client.post("/api/upload", files=[
        ("files", ("supply-note-2.txt", payload, "text/plain"))])
    assert r.status_code == 200
    result = r.json()["events"][0]
    assert result["filename"] == "supply-note-2.txt"
    assert result["event_id"] is not None
    assert result["duplicate"] is False
    ev = con.execute("select * from event where event_id=%s",
                     (result["event_id"],)).fetchone()
    assert ev["connector"] == "manual"
    assert ev["meta"]["filename"] == "supply-note-2.txt"

    # same bytes again -> duplicate, no new event id
    r = client.post("/api/upload", files=[
        ("files", ("supply-note-2.txt", payload, "text/plain"))])
    result = r.json()["events"][0]
    assert result["event_id"] is None
    assert result["duplicate"] is True


def test_api_retry_failed_event(con):
    client = TestClient(webapp.app)
    event_id, _ = _mk_event(con, status="failed", last_error="llm timeout",
                            meta={"title": "Retry me"})
    con.execute("update event set attempts=3 where event_id=%s", (event_id,))
    con.commit()

    r = client.post(f"/api/events/{event_id}/retry")
    assert r.json() == {"ok": True}
    row = con.execute("select status, attempts from event where event_id=%s",
                      (event_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0

    # no longer failed -> 404; unknown id -> 404
    assert client.post(f"/api/events/{event_id}/retry").status_code == 404
    import uuid as uuid_mod
    assert client.post(f"/api/events/{uuid_mod.uuid4()}/retry").status_code == 404


def test_api_run_now(con):
    client = TestClient(webapp.app)
    con.execute("delete from app_kv where key='run_requested'")
    con.commit()

    assert client.post("/api/run-now").json() == {"ok": True}
    row = con.execute("select value from app_kv where key='run_requested'"
                      ).fetchone()
    assert row["value"] == {"v": True}

    # idempotent upsert
    assert client.post("/api/run-now").json() == {"ok": True}
    row = con.execute("select value from app_kv where key='run_requested'"
                      ).fetchone()
    assert row["value"] == {"v": True}

    # the worker's nap-loop helper reads and resets the flag
    assert cli._run_requested() is True
    row = con.execute("select value from app_kv where key='run_requested'"
                      ).fetchone()
    assert row["value"] == {"v": False}
    assert cli._run_requested() is False

    con.execute("delete from app_kv where key='run_requested'")
    con.commit()
