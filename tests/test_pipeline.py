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
from graph.pipeline import extract, materialize, resolve, summarize, triage

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

    # the summary stage runs right after extraction on the same document
    # (build spec v5 §3); scoped to this event, the database is shared
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "summary": "Nvidia supplies boards to AMD.", "key_points": ["One."]})
    summarize.request(con, event_id)       # the page asked; served first
    monkeypatch.setattr(config, "SUMMARY_MIN_CHARS", 10)   # the note is short
    out = summarize.run(con, limit=1, requested_only=True)
    assert out["summarized"] == 1
    row = con.execute("select * from document_summary where event_id=%s",
                      (event_id,)).fetchone()
    assert row["status"] == "done"
    assert row["summary"] == "Nvidia supplies boards to AMD."
    assert row["key_points"] == ["One."]
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **k: copy.deepcopy(EXTRACTION))

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
    # scoped to this claim: earlier files leave asserted edges of their own
    edges = con.execute("select * from edge where origin='asserted' "
                        "and %s = any(claim_ids)", (claim["claim_id"],)).fetchall()
    assert len(edges) == 1
    assert edges[0]["predicate"] == "supplies"
    assert edges[0]["src"] == claim["subject_entity"]
    assert edges[0]["dst"] == claim["object_entity"]
    con.commit()

    # envelope dedup: same bytes again -> None, mirror event on the same lineage
    assert manual.ingest_file(con, str(doc)) is None
    dup = con.execute("select * from event where status='duplicate' "
                      "and content_hash = %s", (event["content_hash"],)).fetchone()
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
        "where connector='podcast' and added_by='seed'")}
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
    ev = con.execute("select * from event where connector='rss' "
                     "and meta->>'feed' = 'exampleblog'").fetchone()
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


def test_quota_pattern_latches_every_cli_phrasing():
    """The seat must latch on every quota message the CLI has used; a missed
    phrasing burns two attempts per item on a dead seat (2026-08-20: the
    weekly-limit message matched nothing and production events went to
    'failed' during a quota window)."""
    quota = [
        "You've hit your weekly limit · resets Aug 21, 3am (UTC)",
        "You have hit your usage limit. Your limit resets at 3am (UTC).",
        "Rate limit exceeded",
        "rate-limited, retry later",
        "Spend limit reached for this workspace",
        "5-hour limit reached · resets 3am",
        "You've hit your limit · resets September 1, 9am",
    ]
    for text in quota:
        assert llm._QUOTA_PATTERN.search(text), text
    # ordinary failures must not latch the seat
    not_quota = [
        "claude code call timed out after 1200s",
        "no valid JSON after 2 attempts",
        "The document discusses speed limits on highways",
        "connection reset by peer",
    ]
    for text in not_quota:
        assert not llm._QUOTA_PATTERN.search(text), text


def test_health_and_unknown_api_path():
    client = TestClient(webapp.app)
    assert client.get("/health").json() == {"ok": True}
    # unknown /api paths must 404, never fall through to the SPA catch-all
    assert client.get("/api/nope").status_code == 404


def test_seat_status(monkeypatch):
    monkeypatch.setattr(llm, "_SEATS", None)
    monkeypatch.setattr(llm, "_load_latches", lambda seats: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_VAULT_1", "tok-a")
    assert llm.seat_status() == [{"seat": 1, "has_token": True,
                                  "latched": False, "kind": None,
                                  "reason": "", "latched_at": None,
                                  "resets_at": None, "retry_at": None,
                                  "utilization": None}]
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


def test_api_watchlist_post(con, monkeypatch):
    """POST /api/watchlist resolves through graph.watchlist (build spec v4 §2);
    Yahoo is monkeypatched, so this stores the resolved columns offline."""
    from graph import watchlist

    client = TestClient(webapp.app)
    resolved = {"ticker": "NVDA", "name": "NVIDIA Corporation",
                "sector": "Technology", "industry": "Semiconductors",
                "exchange": "NasdaqGS", "country": "United States",
                "currency": "USD", "quote_type": "EQUITY",
                "website": "https://www.nvidia.com", "resolver": "yfinance",
                "cik": 1045810}
    monkeypatch.setattr(watchlist, "resolve",
                        lambda c, t: dict(resolved) if t == "NVDA" else None)

    r = client.post("/api/watchlist", json={"ticker": " nvda "})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "ticker": "NVDA",
                        "name": "NVIDIA Corporation", "sector": "Technology",
                        "exchange": "NasdaqGS"}
    row = con.execute("select * from watchlist where ticker='NVDA'").fetchone()
    assert row["active"] and row["added_by"] == "web"
    assert row["name"] == "NVIDIA Corporation"
    assert row["industry"] == "Semiconductors"
    assert row["exchange"] == "NasdaqGS"
    assert row["country"] == "United States"
    assert row["currency"] == "USD"
    assert row["quote_type"] == "EQUITY"
    assert row["cik"] == 1045810
    assert row["resolver"] == "yfinance"
    assert row["resolved_at"] is not None

    # an explicit sector wins over the resolved one
    r = client.post("/api/watchlist", json={"ticker": "NVDA", "sector": "semis"})
    assert r.json()["sector"] == "semis"

    # a ticker neither Yahoo nor the SEC lists is a 400, and nothing is written
    r = client.post("/api/watchlist", json={"ticker": "ZZZZ9"})
    assert r.status_code == 400
    assert r.json()["detail"] == ("Unknown ticker ZZZZ9. Yahoo Finance and the "
                                 "SEC registry do not list it.")
    assert con.execute("select 1 from watchlist where ticker='ZZZZ9'"
                       ).fetchone() is None

    # toggle flips active
    r = client.post("/api/watchlist/NVDA/toggle")
    assert r.json() == {"ok": True, "active": False}
    r = client.post("/api/watchlist/NVDA/toggle")
    assert r.json() == {"ok": True, "active": True}
    assert client.post("/api/watchlist/ZZZZ9/toggle").status_code == 404


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


def _pending_event(con, tmp_path, text, name):
    """Ingest a file and mark it triaged so extract can see it. The triage
    score is set directly: a global triage.run would un-gate other files'
    committed pending events (shared-DB rule) and widen the batch."""
    doc = tmp_path / name
    doc.write_text(text)
    event_id = manual.ingest_file(con, str(doc))
    assert event_id is not None
    con.execute("update event set triage = '{\"score\": 0.5}'::jsonb "
                "where event_id=%s", (event_id,))
    return event_id


def _row(con, event_id):
    return con.execute("select * from event where event_id=%s",
                       (event_id,)).fetchone()


def test_extract_empty_result_guard(con, tmp_path, monkeypatch):
    """A conforming {"mentions": [], "claims": []} must never be recorded as
    success (spec v6 §3) — the 2026-08-20 incident was 8 documents marked
    extracted with zero rows written."""
    event_id = _pending_event(con, tmp_path, "An empty-ish note xkq1.",
                              "empty-guard.txt")
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **k: {"mentions": [], "claims": []})
    out = extract.run(con, limit=50)
    assert out["extracted"] == 0 and out["failed"] >= 1
    row = _row(con, event_id)
    assert row["status"] == "pending" and row["attempts"] == 1
    assert "empty extraction" in row["last_error"]

    # zero claims with nonzero mentions stays legal
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "mentions": [{"surface": "Nvidia", "type": "company"}], "claims": []})
    extract.run(con, limit=50)
    row = _row(con, event_id)
    assert row["status"] == "extracted"
    assert con.execute("select count(*) n from mention where event_id=%s",
                       (event_id,)).fetchone()["n"] == 1


def test_extract_all_claims_dropped_guard(con, tmp_path, monkeypatch):
    event_id = _pending_event(con, tmp_path, "A note about nothing xkq2.",
                              "dropped-guard.txt")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {
        "mentions": [{"surface": "Nvidia", "type": "company"}],
        "claims": [
            {"subject": {"surface": ""}, "predicate": "supplies",
             "object": {"surface": "AMD"}},
            {"subject": {"surface": "Nvidia"}, "predicate": "supplies",
             "object": {}},
        ]})
    out = extract.run(con, limit=50)
    assert out["failed"] >= 1
    row = _row(con, event_id)
    assert row["status"] == "pending" and row["attempts"] == 1
    assert "all 2 claims dropped by validation" in row["last_error"]
    # leave nothing readable behind (shared-DB rule)
    con.execute("update event set status='failed', attempts=2 "
                "where event_id=%s", (event_id,))


def test_extract_truncation_flag_and_transient(con, tmp_path, monkeypatch):
    event_id = _pending_event(con, tmp_path,
                              "Nvidia ships boards. " * 40, "truncate-me.txt")
    monkeypatch.setattr(extract, "BODY_CAP", 100)
    prompts = []

    def fake(prompt, model, **kw):
        prompts.append(prompt)
        return {"mentions": [{"surface": "Nvidia", "type": "company"}],
                "claims": []}

    monkeypatch.setattr(llm, "complete_json", fake)
    extract.run(con, limit=50)
    row = _row(con, event_id)
    assert row["status"] == "extracted"
    assert row["meta"]["extract_truncated"] is True
    # scoped: the batch may contain other files' committed strays
    mine = [p for p in prompts if str(event_id) in p]
    assert len(mine) == 1 and "Nvidia ships boards." in mine[0]

    # a transient failure burns nothing: status and attempts as they were
    event2 = _pending_event(con, tmp_path, "Another note xkq3.",
                            "transient.txt")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (
        (_ for _ in ()).throw(llm.TransientError("claude code call timed "
                                                 "out after 1200s"))))
    out = extract.run(con, limit=50)
    assert out["transient"] >= 1 and out["paused"] is False
    row = _row(con, event2)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert "timed out" in row["last_error"]
    con.execute("update event set status='failed', attempts=2 "
                "where event_id=%s", (event2,))


def test_extract_drain_single_strike(con, tmp_path, monkeypatch):
    """One attempt per item per drain: an event that fails attempt 1 must not
    be re-selected in the same drain and reach 'failed' within one cycle,
    and the drain's failed counter must count events, not attempts."""
    event_id = _pending_event(con, tmp_path, "A doomed note xkq4.",
                              "doomed.txt")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (
        (_ for _ in ()).throw(ValueError("model exploded"))))

    def readable(sql="select count(*) n from event "
                     "where status='pending' and triage is not null"):
        # the suite shares one DB: other files' committed strays can be in
        # the batch, so counter assertions are scoped to what is readable
        return con.execute(sql).fetchone()["n"]

    before = readable()
    out = cli._extract_drain(con)
    # every readable event failed exactly once: events, not attempts
    assert out["failed"] == before and out["paused"] is False
    row = _row(con, event_id)
    assert row["status"] == "pending" and row["attempts"] == 1

    # the second strike lands a cycle later, which is the intent of two
    before = readable()
    out = cli._extract_drain(con)
    assert out["failed"] == before
    row = _row(con, event_id)
    assert row["status"] == "failed" and row["attempts"] == 2
    con.commit()   # the drain commits; leave the terminal state committed


def test_summarize_drain_attempts_a_transient_row_once(con, tmp_path,
                                                       monkeypatch):
    """A transient summarize row keeps its status and attempts, so without
    the exclude set the drain would re-select it every chunk at up to a
    20-minute timeout per attempt (v6 review finding)."""
    doc = tmp_path / "transient-summary.txt"
    doc.write_text("Nvidia supplies boards to a partner. " * 30)
    event_id = manual.ingest_file(con, str(doc))
    con.execute("update event set status='extracted', "
                "triage='{\"score\": 0.5}'::jsonb where event_id=%s",
                (event_id,))
    summarize.request(con, event_id)
    con.commit()
    monkeypatch.setattr(config, "SUMMARY_MIN_CHARS", 10)
    calls = {"n": 0}

    def transient(*a, **k):
        calls["n"] += 1
        raise llm.TransientError("claude code call timed out after 1200s")

    monkeypatch.setattr(llm, "complete_json", transient)
    out = cli._summarize_drain(con, limit=10, requested_only=True)
    assert calls["n"] == 1                     # once per drain, not per chunk
    assert out["transient"] == 1 and out["failed"] == 0
    row = con.execute("select status, attempts from document_summary "
                      "where event_id=%s", (event_id,)).fetchone()
    assert row["status"] == "requested" and row["attempts"] == 0
    # the drain committed the fixtures; leave nothing another file would eat
    con.execute("delete from document_summary where event_id=%s", (event_id,))
    con.execute("update event set status='failed', attempts=2 "
                "where event_id=%s", (event_id,))
    con.commit()


def test_adjudicate_object_valued_scalars_are_coerced(con, monkeypatch):
    """§7 regression: the model once emitted objects where scalars were
    expected and the whole adjudicate cycle died on 'cannot adapt type
    dict'. Scalar decision fields are coerced before any execute."""
    from graph.pipeline import adjudicate as adj

    event_id, _ = _mk_event(con, connector="rss", source_name="coerce-feed")
    mention_id = con.execute(
        "insert into mention (event_id, surface, resolver) values "
        "(%s, 'Qux Materials', 'queued') returning mention_id",
        (event_id,)).fetchone()["mention_id"]
    con.execute("insert into er_queue (mention_id, candidates) "
                "values (%s, '[]'::jsonb)", (mention_id,))
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"decisions": [{
        "mention_id": str(mention_id), "decision": "new_entity",
        "entity_hint": {"name": "an object, not a string"},
        "confidence": {"score": 0.9},
    }]})
    out = adj.run(con, limit=200)
    assert out.get("new_entity", 0) >= 1
    row = con.execute(
        "select m.resolver, m.confidence, e.canonical_name from mention m "
        "join entity e on e.entity_id = m.resolved_entity "
        "where m.mention_id=%s", (mention_id,)).fetchone()
    assert row["resolver"] == "adjudicated-v1:new_entity"
    assert float(row["confidence"]) == 0.8     # object confidence -> fallback
    assert row["canonical_name"] == "Qux Materials"   # object hint discarded


def test_adjudicate_transient_leaves_the_queue_untouched(con, monkeypatch):
    from graph.pipeline import adjudicate as adj

    event_id, _ = _mk_event(con, connector="rss", source_name="transient-feed")
    mention_id = con.execute(
        "insert into mention (event_id, surface, resolver) values "
        "(%s, 'Transient Co xkq5', 'queued') returning mention_id",
        (event_id,)).fetchone()["mention_id"]
    con.execute("insert into er_queue (mention_id, candidates) "
                "values (%s, '[]'::jsonb)", (mention_id,))
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (
        (_ for _ in ()).throw(llm.TransientError("process died"))))
    assert adj.run(con, limit=200) == {"paused": False, "transient": 1}
    row = con.execute("select status, decision from er_queue "
                      "where mention_id=%s", (mention_id,)).fetchone()
    assert row["status"] == "pending" and row["decision"] is None


def test_new_entity_dedup_and_created_entity_tier(con):
    """The same unregistered company across documents lands on ONE node:
    apply_decision reuses by name, and resolve gains a created_entity tier
    (design §8.8 — without this, attribute joins never see a shared via)."""
    from graph.pipeline import adjudicate as adj

    def queued_mention(surface, source_name):
        event_id, _ = _mk_event(con, connector="rss", source_name=source_name)
        mention_id = con.execute(
            "insert into mention (event_id, surface, resolver) values "
            "(%s, %s, 'queued') returning mention_id",
            (event_id, surface)).fetchone()["mention_id"]
        con.execute("insert into er_queue (mention_id, candidates) "
                    "values (%s, '[]'::jsonb)", (mention_id,))
        return mention_id

    m1 = queued_mention("Zeta Materials", "zeta-feed-1")
    decision, ent1 = adj.apply_decision(
        con, {"mention_id": m1, "surface": "Zeta Materials", "candidates": []},
        {"decision": "new_entity", "entity_hint": "Zeta Materials Inc"})
    assert decision == "new_entity"
    assert ent1 is not None

    # second adjudication of the same name reuses the node
    m2 = queued_mention("Zeta Materials Inc", "zeta-feed-2")
    decision, ent2 = adj.apply_decision(
        con, {"mention_id": m2, "surface": "Zeta Materials Inc", "candidates": []},
        {"decision": "new_entity", "entity_hint": None})
    assert decision == "new_entity"
    assert ent2 == ent1

    # a plain pending mention now resolves through the created_entity tier
    event_id, _ = _mk_event(con, connector="rss", source_name="zeta-feed-3")
    m3 = con.execute(
        "insert into mention (event_id, surface, resolver) values "
        "(%s, 'Zeta Materials', 'pending') returning mention_id",
        (event_id,)).fetchone()["mention_id"]
    resolve.run(con)
    row = con.execute("select resolved_entity, resolver from mention "
                      "where mention_id=%s", (m3,)).fetchone()
    assert row["resolved_entity"] == ent1
    assert row["resolver"] == "blocking-v1:created_entity"
