"""End-to-end pipeline test: ingest -> extract -> resolve -> materialize.
No network, no real LLM — graph.llm.complete_json is monkeypatched.

CAF_DB_URL must be set before graph.config is imported (it reads env at import).
"""
import copy
import json
import os
import subprocess

os.environ["CAF_DB_URL"] = "postgresql:///caf_graph_test"

import pytest
import requests
from fastapi.testclient import TestClient

from graph import cli, config, db, llm, webapp
from graph.connectors import edgar, manual, rss
from graph.pipeline import extract, materialize, resolve

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


@pytest.fixture(scope="session")
def database():
    subprocess.run(["dropdb", "caf_graph_test"], capture_output=True)
    subprocess.run(["createdb", "caf_graph_test"], capture_output=True)
    db.migrate()
    with db.connect() as con:
        con.execute("insert into registry_sec (cik, ticker, title) values "
                    "(1045810, 'NVDA', 'NVIDIA CORP'), "
                    "(2488, 'AMD', 'ADVANCED MICRO DEVICES INC')")
        con.commit()
    yield
    subprocess.run(["dropdb", "caf_graph_test"], capture_output=True)


@pytest.fixture
def con(database, tmp_path, monkeypatch):
    monkeypatch.setenv("CAF_ARTIFACTS", str(tmp_path))
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path)
    c = db.connect()
    yield c
    c.rollback()
    c.close()


def test_pipeline(con, tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **k: copy.deepcopy(EXTRACTION))

    doc = tmp_path / "note.txt"
    doc.write_text(DOC_TEXT)

    event_id = manual.ingest_file(con, str(doc))
    assert event_id is not None
    con.commit()

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
    cli.seed(con)
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
    cli.seed(con)
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


# webapp admin: the endpoints open their own connections and commit; the
# session-scoped test DB is dropped afterwards, so committed rows are fine.


def test_admin_page_renders(con):
    client = TestClient(webapp.app)
    r = client.get("/admin")
    assert r.status_code == 200
    for section in ("Watchlist", "Feeds", "Upload"):
        assert section in r.text


def test_admin_watchlist_post(con):
    client = TestClient(webapp.app)
    r = client.post("/admin/watchlist", data={"ticker": "nvda", "sector": "semis"})
    assert r.status_code == 200            # 303 followed back to /admin
    assert "NVIDIA CORP" in r.text         # confirmation line with registry title
    row = con.execute("select * from watchlist where ticker='NVDA'").fetchone()
    assert row is not None
    assert row["active"] and row["added_by"] == "web"

    # unknown tickers are rejected, not inserted
    r = client.post("/admin/watchlist", data={"ticker": "ZZZZ9", "sector": ""})
    assert r.status_code == 200
    assert "unknown ticker" in r.text
    assert con.execute("select 1 from watchlist where ticker='ZZZZ9'"
                       ).fetchone() is None


def test_admin_upload(con):
    client = TestClient(webapp.app)
    r = client.post("/admin/upload", files=[
        ("files", ("supply-note.txt",
                   b"Cocoa suppliers signed a new agreement with Hershey.",
                   "text/plain"))])
    assert r.status_code == 200
    assert "supply-note.txt: event " in r.text
    ev = con.execute("select * from event where connector='manual' "
                     "and meta->>'filename'='supply-note.txt'").fetchone()
    assert ev is not None


def test_admin_feeds_post(con):
    client = TestClient(webapp.app)
    r = client.post("/admin/feeds", data={
        "name": "chipsblog", "url": "https://chips.example.com/rss",
        "kind": "rss"})
    assert r.status_code == 200
    row = con.execute("select * from source where connector='rss' "
                      "and name='chipsblog'").fetchone()
    assert row is not None
    assert row["status"] == "active"
    assert row["added_by"] == "web"
