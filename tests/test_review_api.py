"""Review-surface API tests (build spec v3 §4.3, §7.1): alerts, digest, ER
queue decisions, hypothesis review, contradictions, merge/unmerge, retry-all,
claim_json staleness + predicate canon. No network, no real LLM.

Endpoints open their own connections and commit, so fixture rows are committed
on the test connection first (same idiom as test_pipeline.py). Run alone:

    CAF_DB_URL=postgresql:///caf_test_api uv run pytest tests/test_review_api.py -q
"""
import os
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from graph import db, webapp
from graph.pipeline import adjudicate, resolve


def _mk_event(con, connector="rss", source_name="reviewapi-feed",
              status="extracted", meta=None, artifact_uri="file:///dev/null",
              last_error=None):
    """Insert a committed-ready event + lineage pair directly."""
    source_id = db.get_or_create_source(con, source_name, connector)
    event_id = con.execute(
        "insert into event (source_id, connector, content_hash, artifact_uri, "
        "mime, status, meta, last_error) values "
        "(%s, %s, %s, %s, 'text/plain', %s, %s, %s) returning event_id",
        (source_id, connector, os.urandom(32), artifact_uri, status,
         Jsonb(meta) if meta is not None else None,
         last_error)).fetchone()["event_id"]
    lineage_id = con.execute(
        "insert into lineage (root_event_id) values (%s) returning lineage_id",
        (event_id,)).fetchone()["lineage_id"]
    con.execute("update event set lineage_id=%s where event_id=%s",
                (lineage_id, event_id))
    return event_id, lineage_id


def _mk_claim(con, event_id, lineage_id, subject, predicate, obj,
              subject_entity=None, object_entity=None, quote="", days_ago=0):
    return con.execute(
        "insert into claim (subject_surface, subject_entity, predicate_raw, "
        "object_surface, object_entity, qualifiers, event_id, lineage_id, "
        "observed_at, confidence, extractor, evidence_quote) values "
        "(%s, %s, %s, %s, %s, %s, %s, %s, now() - make_interval(days => %s), "
        "0.9, 'test-v1', %s) returning claim_id",
        (subject, subject_entity, predicate, obj, object_entity,
         Jsonb({"stance": "stated"}), event_id, lineage_id, days_ago,
         quote)).fetchone()["claim_id"]


def _mk_entity(con, name, refs=None):
    return con.execute(
        "insert into entity (kind, canonical_name, registry_refs) values "
        "('company', %s, %s) returning entity_id",
        (name, Jsonb(refs or {}))).fetchone()["entity_id"]


def _mk_hypothesis(con, subjects, state="generated", evidence=(), lineages=(),
                   score=None):
    return con.execute(
        "insert into hypothesis (type, subjects, statement, rationale, "
        "test_plan, origin, budget, state, evidence, lineages, score) values "
        "('shared_dependency', %s::uuid[], %s, "
        "'both depend on the same supplier', %s, %s, %s, %s, %s::uuid[], "
        "%s::uuid[], %s) returning hypothesis_id",
        (list(subjects),
         Jsonb({"template": "shared dependency",
                "text": "A and B share a supplier"}),
         Jsonb({"confirm": ["x"], "refute": ["y"]}),
         Jsonb({"strategy": "attribute_join_v0"}), Jsonb({"tokens": 0}),
         state, list(evidence), list(lineages),
         score)).fetchone()["hypothesis_id"]


def _mk_inferred_edge(con, src, dst, hypothesis_id, claim_ids=(), trail=None):
    con.execute(
        "insert into edge (src, dst, predicate, origin, claim_ids, confidence, "
        "last_evidence_at, evidence_trail) values "
        "(%s, %s, 'shared_dependency', 'inferred', %s::uuid[], 0.7, now(), %s)",
        (src, dst, list(claim_ids),
         Jsonb({"hypothesis_id": str(hypothesis_id), **(trail or {})})))


# ---------------------------------------------------------------- alerts


def test_api_alerts_list_read_and_read_all(con):
    client = TestClient(webapp.app)
    client.post("/api/alerts/read-all")  # zero out rows from earlier tests
    ent = _mk_entity(con, "Alerts test corp")
    a1 = con.execute(
        "insert into alert (kind, title, body, entity_ids) values "
        "('material_event', 'NVDA: 8-K', 'Item 2.01', %s) returning alert_id",
        ([ent],)).fetchone()["alert_id"]
    a2 = con.execute(
        "insert into alert (kind, title) values "
        "('contradiction', 'Conflicting claims on Alerts test corp') "
        "returning alert_id").fetchone()["alert_id"]
    a3 = con.execute(
        "insert into alert (kind, title, read_at) values "
        "('material_event', 'Old alert', now()) "
        "returning alert_id").fetchone()["alert_id"]
    con.commit()

    body = client.get("/api/alerts").json()
    assert body["unread"] == 2
    by_id = {a["alert_id"]: a for a in body["alerts"]}
    assert {str(a1), str(a2), str(a3)} <= set(by_id)
    row = by_id[str(a1)]
    assert row["kind"] == "material_event"
    assert row["title"] == "NVDA: 8-K"
    assert row["body"] == "Item 2.01"
    assert row["entities"] == [{"entity_id": str(ent),
                                "name": "Alerts test corp"}]
    assert row["read_at"] is None
    created = [a["created_at"] for a in body["alerts"]]
    assert created == sorted(created, reverse=True)  # newest first

    unread_only = client.get("/api/alerts", params={"unread": "true"}).json()
    assert {a["alert_id"] for a in unread_only["alerts"]} == {str(a1), str(a2)}

    assert client.post(f"/api/alerts/{a1}/read").json() == {"ok": True}
    first_read_at = con.execute("select read_at from alert where alert_id=%s",
                                (a1,)).fetchone()["read_at"]
    assert first_read_at is not None
    # idempotent re-read: the original read_at timestamp is preserved
    assert client.post(f"/api/alerts/{a1}/read").status_code == 200
    assert con.execute("select read_at from alert where alert_id=%s",
                       (a1,)).fetchone()["read_at"] == first_read_at
    # unknown alerts 404
    assert client.post(f"/api/alerts/{uuid_mod.uuid4()}/read").status_code == 404

    # status counts pick up the one alert still unread
    counts = client.get("/api/status").json()["counts"]
    assert counts["alerts_unread"] == 1
    assert isinstance(counts["contradictions_open"], int)

    r = client.post("/api/alerts/read-all").json()
    assert r["ok"] is True
    assert r["marked"] == 1
    assert client.get("/api/alerts").json()["unread"] == 0


# ---------------------------------------------------------------- digest


def test_api_digest(con):
    client = TestClient(webapp.app)
    con.execute("insert into watchlist (ticker, sector, active, added_by) "
                "values ('NVDA', 'tech_ai', true, 'test') "
                "on conflict (ticker) do update set active=true")
    watch = _mk_entity(con, "Digest watch corp", {"ticker": "NVDA"})
    other = _mk_entity(con, "Digest other corp")
    event_id, lineage_id = _mk_event(con, source_name="digest-feed",
                                     meta={"title": "Digest doc"})
    for i in range(3):
        _mk_claim(con, event_id, lineage_id, "Digest watch corp", "supplies",
                  f"Digest widget {i}", subject_entity=watch,
                  object_entity=other)
    old_evt, old_lin = _mk_event(con, source_name="digest-feed")
    _mk_claim(con, old_evt, old_lin, "Digest watch corp", "supplies",
              "old widget", subject_entity=watch, days_ago=10)  # outside 24h
    con.execute("update event set fetched_at = now() - interval '10 days' "
                "where event_id = %s", (old_evt,))  # outside the events window
    hyp = _mk_hypothesis(con, [watch, other], state="promoted")
    con.execute("insert into alert (kind, title, hypothesis_id) values "
                "('promoted_link', 'New link: A and B', %s)", (hyp,))
    con.execute("insert into alert (kind, title, hypothesis_id) values "
                "('hypothesis_wake', 'Hypothesis woke: A and B', %s)", (hyp,))
    _mk_event(con, status="failed", last_error="boom",
              meta={"title": "Digest failed doc"})
    c1 = _mk_claim(con, event_id, lineage_id, "Digest watch corp", "ceo is",
                   "Chief One", subject_entity=watch)
    c2 = _mk_claim(con, event_id, lineage_id, "Digest watch corp", "ceo is",
                   "Chief Two", subject_entity=watch)
    con.execute("insert into contradiction (subject_entity, predicate_canon, "
                "claim_a, claim_b, kind) values "
                "(%s, 'ceo', %s, %s, 'object_conflict')", (watch, c1, c2))
    con.commit()

    body = client.get("/api/digest").json()
    assert body["since"] is not None
    assert body["claims"] >= 5
    # the 24h window excludes the backdated claim: strictly fewer than the
    # table total (other suite files commit claims, so no exact total here)
    total = con.execute("select count(*) n from claim").fetchone()["n"]
    assert body["claims"] < total
    # the events window excludes the backdated event: count in-window rows
    # with the response's own 'since' so the boundary is race-free
    since = datetime.fromisoformat(body["since"])
    rss_in_window = con.execute(
        "select count(*) n from event where connector='rss' "
        "and fetched_at >= %s", (since,)).fetchone()["n"]
    assert rss_in_window >= 2
    assert body["events"].get("rss", 0) == rss_in_window
    rss_all = con.execute("select count(*) n from event "
                          "where connector='rss'").fetchone()["n"]
    assert rss_in_window < rss_all
    top = body["top_entities"]
    assert top[0]["entity_id"] == str(watch)  # watchlist first
    # exactly this test's 5 fresh claims: the entity is freshly inserted here,
    # so nothing else references it, and the 10-day-old claim is excluded
    assert top[0]["claims"] == 5
    assert {"hypothesis_id": str(hyp),
            "title": "New link: A and B"} in body["promoted"]
    assert {"hypothesis_id": str(hyp),
            "title": "Hypothesis woke: A and B"} in body["woke"]
    assert body["contradictions_open"] >= 1
    assert body["failed_events"] >= 1

    # hours clamps to a week: 'since' lands ~168h back, not 999999h
    r = client.get("/api/digest", params={"hours": 999999})
    assert r.status_code == 200
    age = (datetime.now(timezone.utc)
           - datetime.fromisoformat(r.json()["since"]))
    assert timedelta(hours=167) < age < timedelta(hours=169)


# ---------------------------------------------------------------- er queue


def test_api_er_queue_list_and_decide(con, tmp_path):
    client = TestClient(webapp.app)
    doc = tmp_path / "er-note.txt"
    doc.write_text("Costs rose at Vexatron Industries during the quarter.")
    event_id, lineage_id = _mk_event(con, source_name="er-feed",
                                     artifact_uri=str(doc),
                                     meta={"title": "ER doc"})
    claim_id = _mk_claim(con, event_id, lineage_id, "Vexatron Industries",
                         "reports", "rising costs")
    claim_new = _mk_claim(con, event_id, lineage_id, "Vexatron Labs",
                          "builds", "widgets")

    def queue(surface, candidates, decision=None):
        mention_id = con.execute(
            "insert into mention (event_id, surface, resolver) values "
            "(%s, %s, 'queued') returning mention_id",
            (event_id, surface)).fetchone()["mention_id"]
        con.execute(
            "insert into er_queue (mention_id, candidates, decision) "
            "values (%s, %s, %s)",
            (mention_id, Jsonb(candidates),
             Jsonb(decision) if decision else None))
        return mention_id

    m_match = queue("Vexatron Industries",
                    [{"cik": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}])
    m_new = queue("Vexatron Labs", [])
    m_not = queue("Vexatron Park", [])
    m_stuck = queue("Vexatron Passes", [], decision={"passes": 2})
    con.commit()

    body = client.get("/api/er-queue").json()
    assert body["pending"] >= 4
    items = {i["mention_id"]: i for i in body["items"]}
    row = items[str(m_match)]
    assert row["surface"] == "Vexatron Industries"
    assert "Vexatron Industries" in row["context"]
    assert row["doc_title"] == "ER doc"
    assert row["source_name"] == "er-feed"
    assert row["connector"] == "rss"
    assert row["candidates"] == [{"cik": 1045810, "ticker": "NVDA",
                                  "title": "NVIDIA CORP"}]
    assert row["passes"] == 0
    assert items[str(m_stuck)]["passes"] == 2
    assert isinstance(body["recent"], list)

    # match pins to the SEC candidate, re-links claims, labels the verdict
    r = client.post(f"/api/er-queue/{m_match}/decide",
                    json={"decision": "match", "cik": 1045810})
    assert r.status_code == 200
    entity_id = r.json()["entity_id"]
    assert entity_id is not None
    mention = con.execute("select * from mention where mention_id=%s",
                          (m_match,)).fetchone()
    assert str(mention["resolved_entity"]) == entity_id
    assert mention["resolver"] == "adjudicated-v1:match"
    q = con.execute("select * from er_queue where mention_id=%s",
                    (m_match,)).fetchone()
    assert q["status"] == "decided"
    assert q["decision"]["decision"] == "match"
    assert q["decided_at"] is not None
    linked = con.execute("select subject_entity from claim where claim_id=%s",
                         (claim_id,)).fetchone()
    assert str(linked["subject_entity"]) == entity_id
    assert con.execute(
        "select verdict from review_label where kind='er' and target=%s",
        (m_match,)).fetchone()["verdict"] == "match"
    recent = {i["mention_id"]: i
              for i in client.get("/api/er-queue").json()["recent"]}
    assert recent[str(m_match)]["decision"]["decision"] == "match"

    # new_entity creates the company under the given name...
    r = client.post(f"/api/er-queue/{m_new}/decide",
                    json={"decision": "new_entity", "name": "Vexatron Labs Inc"})
    assert r.status_code == 200
    new_entity_id = r.json()["entity_id"]
    ent = con.execute("select canonical_name from entity where entity_id=%s",
                      (new_entity_id,)).fetchone()
    assert ent["canonical_name"] == "Vexatron Labs Inc"
    # ...resolves the mention to it, marks the queue row decided, and re-links
    # claims carrying that surface — the decision's whole point
    mention = con.execute("select * from mention where mention_id=%s",
                          (m_new,)).fetchone()
    assert str(mention["resolved_entity"]) == new_entity_id
    assert mention["resolver"] == "adjudicated-v1:new_entity"
    assert con.execute("select status from er_queue where mention_id=%s",
                       (m_new,)).fetchone()["status"] == "decided"
    assert str(con.execute(
        "select subject_entity from claim where claim_id=%s",
        (claim_new,)).fetchone()["subject_entity"]) == new_entity_id

    # not_a_company clears the mention without an entity
    r = client.post(f"/api/er-queue/{m_not}/decide",
                    json={"decision": "not_a_company"})
    assert r.json()["entity_id"] is None
    assert con.execute("select resolver from mention where mention_id=%s",
                       (m_not,)).fetchone()["resolver"] == "not_company"

    # a match with no usable registry id is rejected, queue row untouched
    r = client.post(f"/api/er-queue/{m_stuck}/decide", json={"decision": "match"})
    assert r.status_code == 400
    assert con.execute("select status from er_queue where mention_id=%s",
                       (m_stuck,)).fetchone()["status"] == "pending"

    # decided or unknown mentions 404; bad decision and status values 400
    assert client.post(f"/api/er-queue/{m_match}/decide",
                       json={"decision": "match",
                             "cik": 1045810}).status_code == 404
    assert client.post(f"/api/er-queue/{uuid_mod.uuid4()}/decide",
                       json={"decision": "match"}).status_code == 404
    assert client.post(f"/api/er-queue/{m_stuck}/decide",
                       json={"decision": "maybe"}).status_code == 400
    assert client.get("/api/er-queue",
                      params={"status": "bogus"}).status_code == 400


# ---------------------------------------------------------------- hypotheses


def test_api_hypotheses_filter_and_detail(con):
    client = TestClient(webapp.app)
    a = _mk_entity(con, "Hyp corp A")
    b = _mk_entity(con, "Hyp corp B")
    event_id, lineage_id = _mk_event(con, source_name="hyp-feed",
                                     meta={"title": "Hyp doc"})
    c1 = _mk_claim(con, event_id, lineage_id, "Hyp corp A", "supplies",
                   "Hyp corp B", subject_entity=a, object_entity=b,
                   quote="A supplies B.")
    h_tri = _mk_hypothesis(con, [a, b], state="triaged", evidence=[c1],
                           lineages=[lineage_id], score=0.5)
    h_pro = _mk_hypothesis(con, [b, a], state="promoted", score=0.75)
    _mk_inferred_edge(con, b, a, h_pro, claim_ids=[c1],
                      trail={"verdict": "promote", "reasoning": "held up"})
    con.commit()

    rows = client.get("/api/hypotheses").json()
    by_id = {h["hypothesis_id"]: h for h in rows}
    assert by_id[str(h_tri)]["score"] == 0.5
    assert by_id[str(h_tri)]["updated_at"] is not None
    only = client.get("/api/hypotheses", params={"state": "triaged"}).json()
    assert str(h_tri) in {h["hypothesis_id"] for h in only}
    assert all(h["state"] == "triaged" for h in only)

    body = client.get(f"/api/hypotheses/{h_tri}").json()
    h = body["hypothesis"]
    assert h["state"] == "triaged"
    assert h["score"] == 0.5
    assert h["statement"]["text"] == "A and B share a supplier"
    assert h["test_plan"] == {"confirm": ["x"], "refute": ["y"]}
    assert h["parked_at"] is None
    assert {s["name"] for s in body["subjects"]} == {"Hyp corp A", "Hyp corp B"}
    assert body["lineages"] == 1
    assert body["verifier"] is None
    assert [c["claim_id"] for c in body["evidence"]] == [str(c1)]
    evidence = body["evidence"][0]
    assert "confidence_now" in evidence
    assert "predicate_canon" in evidence
    assert isinstance(body["history"], list)

    # promoted detail carries the edge's evidence trail as the verifier view
    body = client.get(f"/api/hypotheses/{h_pro}").json()
    assert body["verifier"]["verdict"] == "promote"
    assert body["verifier"]["hypothesis_id"] == str(h_pro)

    assert client.get(f"/api/hypotheses/{uuid_mod.uuid4()}").status_code == 404


def test_api_hypothesis_review(con):
    client = TestClient(webapp.app)
    a = _mk_entity(con, "Review corp A")
    b = _mk_entity(con, "Review corp B")
    h_pro = _mk_hypothesis(con, [a, b], state="promoted")
    _mk_inferred_edge(con, a, b, h_pro)
    con.commit()

    # accept keeps the promoted state and the live edge
    r = client.post(f"/api/hypotheses/{h_pro}/review",
                    json={"verdict": "accept"})
    assert r.json() == {"ok": True, "state": "promoted"}
    assert con.execute(
        "select archived from edge where evidence_trail->>'hypothesis_id'=%s",
        (str(h_pro),)).fetchone()["archived"] is False
    assert con.execute(
        "select verdict from review_label where kind='hypothesis' and target=%s "
        "order by created_at", (h_pro,)).fetchone()["verdict"] == "accept"

    # reject on promoted refutes and archives the inferred edge
    r = client.post(f"/api/hypotheses/{h_pro}/review",
                    json={"verdict": "reject"})
    assert r.json() == {"ok": True, "state": "refuted"}
    row = con.execute("select state, history from hypothesis "
                      "where hypothesis_id=%s", (h_pro,)).fetchone()
    assert row["state"] == "refuted"
    assert row["history"][-1]["to"] == "refuted"
    assert con.execute(
        "select archived from edge where evidence_trail->>'hypothesis_id'=%s",
        (str(h_pro),)).fetchone()["archived"] is True

    # triaged and parked take reject only; other states are not reviewable
    h_tri = _mk_hypothesis(con, [a, b], state="triaged")
    h_par = _mk_hypothesis(con, [a, b], state="parked")
    h_gen = _mk_hypothesis(con, [a, b], state="generated")
    con.commit()
    assert client.post(f"/api/hypotheses/{h_tri}/review",
                       json={"verdict": "accept"}).status_code == 400
    r = client.post(f"/api/hypotheses/{h_par}/review",
                    json={"verdict": "reject"})
    assert r.json() == {"ok": True, "state": "refuted"}
    assert client.post(f"/api/hypotheses/{h_gen}/review",
                       json={"verdict": "reject"}).status_code == 400
    assert client.post(f"/api/hypotheses/{h_tri}/review",
                       json={"verdict": "maybe"}).status_code == 400
    assert client.post(f"/api/hypotheses/{uuid_mod.uuid4()}/review",
                       json={"verdict": "accept"}).status_code == 404


# ---------------------------------------------------------------- contradictions


def test_api_contradictions_list_and_resolve(con):
    client = TestClient(webapp.app)
    subj = _mk_entity(con, "Conflict corp")
    event_id, lineage_id = _mk_event(con, source_name="conflict-feed",
                                     meta={"title": "Conflict doc"})
    ca = _mk_claim(con, event_id, lineage_id, "Conflict corp", "ceo is",
                   "Chief X", subject_entity=subj, quote="X runs it.")
    cb = _mk_claim(con, event_id, lineage_id, "Conflict corp", "ceo is",
                   "Chief Y", subject_entity=subj, quote="Y runs it.")
    cc = _mk_claim(con, event_id, lineage_id, "Conflict corp", "headquartered",
                   "Austin", subject_entity=subj)
    cd = _mk_claim(con, event_id, lineage_id, "Conflict corp", "headquartered",
                   "Boston", subject_entity=subj)

    def contradiction(a, b, canon):
        return con.execute(
            "insert into contradiction (subject_entity, predicate_canon, "
            "claim_a, claim_b, kind) values (%s, %s, %s, %s, 'object_conflict') "
            "returning contradiction_id", (subj, canon, a, b)
        ).fetchone()["contradiction_id"]

    t1 = contradiction(ca, cb, "ceo")
    t2 = contradiction(cc, cd, "headquartered")
    con.commit()

    rows = client.get("/api/contradictions").json()
    row = {r["contradiction_id"]: r for r in rows}[str(t1)]
    assert row["subject"] == {"entity_id": str(subj), "name": "Conflict corp"}
    assert row["predicate_canon"] == "ceo"
    assert row["kind"] == "object_conflict"
    assert row["status"] == "open"
    assert row["claims"]["a"]["claim_id"] == str(ca)
    assert row["claims"]["b"]["claim_id"] == str(cb)
    assert row["created_at"] is not None

    # keep a: the b claim is superseded by a, verdict labeled
    assert client.post(f"/api/contradictions/{t1}/resolve",
                       json={"keep": "a"}).json() == {"ok": True}
    loser = con.execute("select status, superseded_by from claim "
                        "where claim_id=%s", (cb,)).fetchone()
    assert loser["status"] == "superseded"
    assert loser["superseded_by"] == ca
    t = con.execute("select * from contradiction where contradiction_id=%s",
                    (t1,)).fetchone()
    assert t["status"] == "resolved"
    assert t["resolution"] == {"kept": str(ca)}
    assert t["resolved_at"] is not None
    assert con.execute(
        "select verdict from review_label where kind='contradiction' "
        "and target=%s", (t1,)).fetchone()["verdict"] == "a"

    # resolved rows leave the default list; a second resolve is refused
    assert str(t1) not in {r["contradiction_id"]
                           for r in client.get("/api/contradictions").json()}
    assert client.post(f"/api/contradictions/{t1}/resolve",
                       json={"keep": "b"}).status_code == 409

    # none dismisses without touching the claims
    assert client.post(f"/api/contradictions/{t2}/resolve",
                       json={"keep": "none"}).json() == {"ok": True}
    assert con.execute("select status from contradiction "
                       "where contradiction_id=%s",
                       (t2,)).fetchone()["status"] == "dismissed"
    assert con.execute("select status from claim where claim_id=%s",
                       (cc,)).fetchone()["status"] == "asserted"

    assert client.post(f"/api/contradictions/{t1}/resolve",
                       json={"keep": "x"}).status_code == 400
    assert client.post(f"/api/contradictions/{uuid_mod.uuid4()}/resolve",
                       json={"keep": "a"}).status_code == 404
    assert client.get("/api/contradictions",
                      params={"status": "bogus"}).status_code == 400


def test_api_contradiction_resolve_stale_side_dismisses(con):
    """Two open contradictions sharing a claim: resolving the second after its
    shared claim was superseded must NOT rewrite the existing superseded_by
    pointer or endorse a dead winner — it dismisses as stale instead."""
    client = TestClient(webapp.app)
    subj = _mk_entity(con, "Stale conflict corp")
    event_id, lineage_id = _mk_event(con, source_name="stale-feed")
    cx = _mk_claim(con, event_id, lineage_id, "Stale conflict corp", "ceo is",
                   "Chief X", subject_entity=subj)
    cy = _mk_claim(con, event_id, lineage_id, "Stale conflict corp", "ceo is",
                   "Chief Y", subject_entity=subj)
    cz = _mk_claim(con, event_id, lineage_id, "Stale conflict corp", "ceo is",
                   "Chief Z", subject_entity=subj)

    def contradiction(a, b):
        return con.execute(
            "insert into contradiction (subject_entity, predicate_canon, "
            "claim_a, claim_b, kind) values (%s, 'ceo', %s, %s, "
            "'object_conflict') returning contradiction_id",
            (subj, a, b)).fetchone()["contradiction_id"]

    t_xy = contradiction(cx, cy)
    t_xz = contradiction(cx, cz)
    con.commit()

    # resolve (X,Y) keeping Y: X superseded_by=Y
    assert client.post(f"/api/contradictions/{t_xy}/resolve",
                       json={"keep": "b"}).json() == {"ok": True}
    x = con.execute("select status, superseded_by from claim "
                    "where claim_id=%s", (cx,)).fetchone()
    assert x["status"] == "superseded"
    assert x["superseded_by"] == cy

    # (X,Z) is now stale in either direction: keep=b (superseding X again)
    # must not overwrite X.superseded_by, keep=a (keeping dead X) must not
    # record an ineffective verdict — both dismiss as stale
    r = client.post(f"/api/contradictions/{t_xz}/resolve", json={"keep": "b"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "stale": True}
    t = con.execute("select * from contradiction where contradiction_id=%s",
                    (t_xz,)).fetchone()
    assert t["status"] == "dismissed"
    assert t["resolution"] == {"reason": "stale",
                               "superseded_claim": str(cx)}
    # the earlier supersession's audit pointer is intact, Z untouched
    x = con.execute("select status, superseded_by from claim "
                    "where claim_id=%s", (cx,)).fetchone()
    assert x["superseded_by"] == cy
    assert con.execute("select status from claim where claim_id=%s",
                       (cz,)).fetchone()["status"] == "asserted"
    # no review label was written for the unapplied verdict
    assert con.execute(
        "select 1 from review_label where kind='contradiction' and target=%s",
        (t_xz,)).fetchone() is None


# ---------------------------------------------------------------- er race


def test_apply_decision_respects_prior_decision(con):
    """The worker's adjudicate pass must not clobber a decision that landed
    while its LLM call was in flight: the guarded queue-row claim returns the
    already_decided sentinel and writes nothing."""
    event_id, lineage_id = _mk_event(con, source_name="race-feed")
    mention_id = con.execute(
        "insert into mention (event_id, surface, resolver) values "
        "(%s, 'Racington Corp', 'queued') returning mention_id",
        (event_id,)).fetchone()["mention_id"]
    human = {"decision": "not_a_company", "decided_by": "web"}
    con.execute(
        "insert into er_queue (mention_id, candidates, status, decision, "
        "decided_at) values (%s, '[]'::jsonb, 'decided', %s, now())",
        (mention_id, Jsonb(human)))
    con.execute("update mention set resolver='not_company' "
                "where mention_id=%s", (mention_id,))

    row = {"mention_id": mention_id, "surface": "Racington Corp",
           "candidates": []}
    llm_verdict = {"decision": "new_entity", "entity_hint": "Racington Corp",
                   "confidence": 0.9}
    entities_before = con.execute("select count(*) n from entity"
                                  ).fetchone()["n"]
    assert adjudicate.apply_decision(con, row, llm_verdict) == \
        ("already_decided", None)
    # nothing written: the human decision stands, no spurious entity row
    q = con.execute("select status, decision from er_queue "
                    "where mention_id=%s", (mention_id,)).fetchone()
    assert q["status"] == "decided"
    assert q["decision"] == human
    assert con.execute("select resolver from mention where mention_id=%s",
                       (mention_id,)).fetchone()["resolver"] == "not_company"
    assert con.execute("select count(*) n from entity"
                       ).fetchone()["n"] == entities_before


# ---------------------------------------------------------------- retry-all


def test_api_events_retry_all(con):
    client = TestClient(webapp.app)
    e1, _ = _mk_event(con, status="failed", last_error="boom",
                      meta={"title": "Retry a"})
    e2, _ = _mk_event(con, status="failed", last_error="boom",
                      meta={"title": "Retry b"})
    ok_evt, _ = _mk_event(con, status="extracted")
    con.execute("update event set attempts=2 where event_id in (%s, %s)",
                (e1, e2))
    con.commit()

    r = client.post("/api/events/retry-all").json()
    assert r["ok"] is True
    assert r["retried"] >= 2
    rows = con.execute("select status, attempts from event "
                       "where event_id in (%s, %s)", (e1, e2)).fetchall()
    assert all(x["status"] == "pending" and x["attempts"] == 0 for x in rows)
    assert con.execute("select status from event where event_id=%s",
                       (ok_evt,)).fetchone()["status"] == "extracted"
    # nothing failed remains, so a second call is a no-op
    assert client.post("/api/events/retry-all").json()["retried"] == 0


# ---------------------------------------------------------------- merge


def test_api_entity_merge_unmerge(con):
    client = TestClient(webapp.app)
    a = _mk_entity(con, "Mergeco A")
    b = _mk_entity(con, "Mergeco B")
    c = _mk_entity(con, "Mergeco C")
    d = _mk_entity(con, "Mergeco D")
    event_id, lineage_id = _mk_event(con, source_name="merge-feed")
    _mk_claim(con, event_id, lineage_id, "Mergeco B", "acquires", "Mergeco D",
              subject_entity=b, object_entity=d)
    con.commit()

    # self-merge and unknown entities are rejected
    assert client.post(f"/api/entities/{a}/merge",
                       json={"into": str(a)}).status_code == 400
    assert client.post(f"/api/entities/{uuid_mod.uuid4()}/merge",
                       json={"into": str(b)}).status_code == 404
    assert client.post(f"/api/entities/{a}/merge",
                       json={"into": str(uuid_mod.uuid4())}).status_code == 404

    assert client.post(f"/api/entities/{a}/merge",
                       json={"into": str(b)}).json()["ok"] is True
    assert db.entity_canonical_for(con, a) == b

    # a second merge of a merged-away entity is refused
    assert client.post(f"/api/entities/{a}/merge",
                       json={"into": str(c)}).status_code == 409
    # merging b into its own absorbed child resolves to a self-merge
    assert client.post(f"/api/entities/{b}/merge",
                       json={"into": str(a)}).status_code == 400

    # merged-away entities leave the list; their page shows the canonical
    names = {e["name"] for e in client.get(
        "/api/entities", params={"q": "Mergeco"}).json()}
    assert "Mergeco A" not in names
    assert "Mergeco B" in names
    page = client.get(f"/api/entity/{a}").json()
    assert page["entity"]["entity_id"] == str(b)
    assert page["merged_from"] == [{"entity_id": str(a), "name": "Mergeco A"}]
    assert page["claims"][0]["subject"]["entity_id"] == str(b)

    # chains resolve at write: merging b into c re-points a to c
    assert client.post(f"/api/entities/{b}/merge",
                       json={"into": str(c)}).json()["ok"] is True
    assert db.entity_canonical_map(con) == {a: c, b: c}

    # merging into a merged-away target lands on its canonical
    assert client.post(f"/api/entities/{d}/merge",
                       json={"into": str(a)}).json()["ok"] is True
    assert db.entity_canonical_for(con, d) == c

    # unmerge restores d and only d; a second unmerge finds nothing
    assert client.post(f"/api/entities/{d}/unmerge").json() == {"ok": True}
    assert db.entity_canonical_for(con, d) == d
    assert db.entity_canonical_for(con, a) == c
    assert client.post(f"/api/entities/{d}/unmerge").status_code == 404


def test_entity_same_as_one_active_index_backstop(con):
    """The partial unique index turns concurrent duplicate merges (which slip
    past the endpoint's plain-read check) into a unique violation the endpoint
    maps to 409 — never duplicate active rows or an active cycle."""
    import pytest
    from psycopg import errors

    a = _mk_entity(con, "Idxco A")
    b = _mk_entity(con, "Idxco B")
    c = _mk_entity(con, "Idxco C")
    con.execute("insert into entity_same_as (a, b, decided_by) values "
                "(%s, %s, 'test')", (a, b))
    with pytest.raises(errors.UniqueViolation):
        con.execute("insert into entity_same_as (a, b, decided_by) values "
                    "(%s, %s, 'test')", (a, c))


def test_api_unmerge_undoes_chained_merge(con):
    """Merging A into B re-points C (previously merged into A) to B; undoing
    the A->B merge must also restore C->A — the association C->B was created
    by the merge being reverted, no human ever decided it."""
    client = TestClient(webapp.app)
    a = _mk_entity(con, "Chainco A")
    b = _mk_entity(con, "Chainco B")
    c = _mk_entity(con, "Chainco C")
    con.commit()

    assert client.post(f"/api/entities/{c}/merge",
                       json={"into": str(a)}).json()["ok"] is True
    assert client.post(f"/api/entities/{a}/merge",
                       json={"into": str(b)}).json()["ok"] is True
    active = {r["a"]: r["b"] for r in con.execute(
        "select a, b from entity_same_as where status='active' "
        "and a = any(%s)", ([a, b, c],))}
    assert active == {a: b, c: b}   # chained merge re-pointed C to B

    assert client.post(f"/api/entities/{a}/unmerge").json() == {"ok": True}
    active = {r["a"]: r["b"] for r in con.execute(
        "select a, b from entity_same_as where status='active' "
        "and a = any(%s)", ([a, b, c],))}
    assert active == {c: a}         # C restored to A, B clean
    assert db.entity_canonical_for(con, c) == a
    assert db.entity_canonical_for(con, a) == a


def test_api_unmerge_reverts_merge_caused_supersession(con):
    """A bad merge that let the same-lineage auto-resolve supersede another
    company's true claim is fully healed by unmerge: the claim returns to
    asserted and the contradiction re-opens (design §2 rule 5, §15)."""
    client = TestClient(webapp.app)
    corp = _mk_entity(con, "Healco Corp")
    inds = _mk_entity(con, "Healco Industries")
    event_id, lineage_id = _mk_event(con, source_name="heal-feed")
    con.execute(
        "insert into mention (event_id, surface, resolved_entity, resolver) "
        "values (%s, 'Healco Corp', %s, 'test-v1'), "
        "(%s, 'Healco Industries', %s, 'test-v1')",
        (event_id, corp, event_id, inds))
    c_corp = _mk_claim(con, event_id, lineage_id, "Healco Corp",
                       "reported_revenue", "3.0B", subject_entity=corp)
    c_inds = _mk_claim(con, event_id, lineage_id, "Healco Industries",
                       "reported_revenue", "1.2B", subject_entity=inds)
    con.commit()

    # wrong merge re-points the Industries claim onto Corp
    assert client.post(f"/api/entities/{inds}/merge",
                       json={"into": str(corp)}).json()["ok"] is True
    resolve.link_claims(con)
    # simulate the damage a pre-gate auto-resolve did: Industries' true claim
    # superseded by Corp's, contradiction recorded as same-lineage auto
    con.execute("update claim set status='superseded', superseded_by=%s "
                "where claim_id=%s", (c_corp, c_inds))
    tid = con.execute(
        "insert into contradiction (subject_entity, predicate_canon, claim_a, "
        "claim_b, kind, status, resolution, resolved_at) values "
        "(%s, 'reported_revenue', %s, %s, 'literal_conflict', "
        "'auto_resolved', %s, now()) returning contradiction_id",
        (corp, c_corp, c_inds,
         Jsonb({"kept": str(c_corp), "mode": "same_lineage"}))
    ).fetchone()["contradiction_id"]
    con.commit()

    assert client.post(f"/api/entities/{inds}/unmerge").json() == {"ok": True}
    healed = con.execute("select status, superseded_by from claim "
                         "where claim_id=%s", (c_inds,)).fetchone()
    assert healed["status"] == "asserted"
    assert healed["superseded_by"] is None
    t = con.execute("select * from contradiction where contradiction_id=%s",
                    (tid,)).fetchone()
    assert t["status"] == "open"
    assert t["resolution"]["reopened_by"] == "unmerge"
    # the winner claim was never touched
    assert con.execute("select status from claim where claim_id=%s",
                       (c_corp,)).fetchone()["status"] == "asserted"


# ---------------------------------------------------------------- claim status


def test_api_claims_hide_superseded_by_default(con):
    client = TestClient(webapp.app)
    ent = _mk_entity(con, "Statusco")
    event_id, lineage_id = _mk_event(con, source_name="status-feed")
    live = _mk_claim(con, event_id, lineage_id, "Statusco", "supplies",
                     "live widget qqstatus", subject_entity=ent)
    dead = _mk_claim(con, event_id, lineage_id, "Statusco", "supplies",
                     "dead widget qqstatus", subject_entity=ent)
    con.execute("update claim set status='superseded', superseded_by=%s "
                "where claim_id=%s", (live, dead))
    con.commit()

    # default view: asserted only, and rows carry their status
    body = client.get("/api/claims", params={"q": "qqstatus"}).json()
    assert body["total"] == 1
    assert [c["claim_id"] for c in body["claims"]] == [str(live)]
    assert body["claims"][0]["status"] == "asserted"
    assert body["claims"][0]["superseded_by"] is None

    # the audit view opts back in and links the loser to its winner
    body = client.get("/api/claims",
                      params={"q": "qqstatus", "status": "all"}).json()
    assert body["total"] == 2
    by_id = {c["claim_id"]: c for c in body["claims"]}
    assert by_id[str(dead)]["status"] == "superseded"
    assert by_id[str(dead)]["superseded_by"] == str(live)
    body = client.get("/api/claims",
                      params={"q": "qqstatus", "status": "superseded"}).json()
    assert [c["claim_id"] for c in body["claims"]] == [str(dead)]
    assert client.get("/api/claims",
                      params={"status": "bogus"}).status_code == 400

    # the entity timeline shows current claims only
    page = client.get(f"/api/entity/{ent}").json()
    assert [c["claim_id"] for c in page["claims"]] == [str(live)]


# ---------------------------------------------------------------- claim_json


def test_claim_json_staleness_and_predicate_canon(con):
    client = TestClient(webapp.app)
    event_id, lineage_id = _mk_event(con, source_name="canon-feed",
                                     meta={"title": "Canon doc"})
    fresh = _mk_claim(con, event_id, lineage_id, "Canonco", "Supplies",
                      "Widgetco", quote="Canonco supplies qqfilter widgets.")
    stale = _mk_claim(con, event_id, lineage_id, "Canonco", "guidance",
                      "flat revenue", days_ago=90,
                      quote="Canonco guidance qqfilter flat.")
    con.execute("insert into predicate_map (version, predicate_raw, "
                "predicate_canon) values (1, 'supplies', 'supplier_of')")
    con.commit()

    body = client.get("/api/claims", params={"q": "qqfilter"}).json()
    claims = {cl["claim_id"]: cl for cl in body["claims"]}
    f = claims[str(fresh)]
    assert f["predicate_canon"] == "supplier_of"  # lower(raw) resolves
    assert f["confidence_now"] == 0.9             # fresh: no visible decay
    s = claims[str(stale)]
    assert s["predicate_canon"] is None
    # guidance halves over 90 days: 0.9 -> 0.45
    assert s["confidence_now"] == 0.45

    # the predicate filter takes the canonical or the raw form
    for param in ("supplier_of", "Supplies"):
        got = client.get("/api/claims",
                         params={"q": "qqfilter", "predicate": param}).json()
        assert [cl["claim_id"] for cl in got["claims"]] == [str(fresh)]
