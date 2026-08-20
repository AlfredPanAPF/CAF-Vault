"""Documents API + summarize stage tests (build spec v5). No network, no real
LLM: graph.llm.complete_json is monkeypatched. Endpoints open their own
connections and commit, so fixture rows are committed on the test connection
first (same idiom as test_review_api.py). The suite shares one database, so
every assertion is scoped to the ids this file creates. Run alone:

    CAF_DB_URL=postgresql:///caf_test_docs uv run pytest tests/test_documents_api.py -q
"""
import os
import uuid as uuid_mod

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from graph import artifacts, cli, config, db, llm, webapp
from graph.pipeline import summarize

BODY = ("Nvidia supplies accelerator boards to AMD. Advanced Micro Devices "
        "confirmed the arrangement in a statement to analysts on Tuesday. "
        "The deal covers two product generations and is worth about 3.2 "
        "billion dollars over three years, the companies said. ") * 4

SHORT_BODY = "Headline only. A teaser of one sentence."


def _artifact(connector, title, body, source_type="article", extra=""):
    text = (f"# title: {title}\n# source_type: {source_type}\n"
            f"# published: 2026-08-01\n{extra}---\n{body}\n")
    uri, _ = artifacts.put(connector, text.encode("utf-8"), ".txt")
    return uri


def _mk_source(con, name, connector, config_=None, status="active"):
    return con.execute(
        "insert into source (name, connector, status, config) values "
        "(%s, %s, %s, %s) returning source_id",
        (name, connector, status, Jsonb(config_) if config_ else None)
    ).fetchone()["source_id"]


def _mk_event(con, source_id, connector, title, body=BODY, status="extracted",
              meta=None, published=None, triage=None, artifact_uri=None,
              last_error=None, attempts=0):
    uri = artifact_uri or _artifact(connector, title, body)
    meta = {"title": title, **(meta or {})}
    if published and len(published) == 10:
        published += "T12:00:00+00:00"     # a plain date, pinned to UTC noon
    event_id = con.execute(
        "insert into event (source_id, connector, content_hash, artifact_uri, "
        "mime, status, meta, published_at, triage, last_error, attempts) values "
        "(%s, %s, %s, %s, 'text/plain', %s, %s, %s, %s, %s, %s) "
        "returning event_id",
        (source_id, connector, os.urandom(32), uri, status, Jsonb(meta),
         published, Jsonb(triage) if triage is not None else None,
         last_error, attempts)).fetchone()["event_id"]
    lineage_id = con.execute(
        "insert into lineage (root_event_id) values (%s) returning lineage_id",
        (event_id,)).fetchone()["lineage_id"]
    con.execute("update event set lineage_id=%s where event_id=%s",
                (lineage_id, event_id))
    return event_id, lineage_id


def _mk_copy(con, source_id, root_event_id, lineage_id, connector="rss",
             title="copy"):
    """A syndicated copy: status duplicate on the root's lineage."""
    return con.execute(
        "insert into event (source_id, connector, content_hash, artifact_uri, "
        "mime, status, meta, lineage_id) select %s, %s, content_hash, "
        "artifact_uri, mime, 'duplicate', %s, %s from event where event_id=%s "
        "returning event_id",
        (source_id, connector, Jsonb({"title": title}), lineage_id,
         root_event_id)).fetchone()["event_id"]


def _mk_entity(con, name, refs=None):
    return con.execute(
        "insert into entity (kind, canonical_name, registry_refs) values "
        "('company', %s, %s) returning entity_id",
        (name, Jsonb(refs or {}))).fetchone()["entity_id"]


def _mk_claim(con, event_id, lineage_id, subject, predicate, obj,
              subject_entity=None, object_entity=None, status="asserted",
              quote="Nvidia supplies accelerator boards to AMD.", age_min=0):
    return con.execute(
        "insert into claim (subject_surface, subject_entity, predicate_raw, "
        "object_surface, object_entity, qualifiers, event_id, lineage_id, "
        "observed_at, confidence, extractor, evidence_quote, status) values "
        "(%s, %s, %s, %s, %s, %s, %s, %s, now() - make_interval(mins => %s), "
        "0.9, 'test-v1', %s, %s) returning claim_id",
        (subject, subject_entity, predicate, obj, object_entity,
         Jsonb({"stance": "stated"}), event_id, lineage_id, age_min, quote,
         status)).fetchone()["claim_id"]


def _mk_mention(con, event_id, surface, resolver, entity=None):
    return con.execute(
        "insert into mention (event_id, surface, resolver, resolved_entity, "
        "confidence) values (%s, %s, %s, %s, 0.8) returning mention_id",
        (event_id, surface, resolver, entity)).fetchone()["mention_id"]


def _ids(docs):
    return [d["event_id"] for d in docs]


# ---------------------------------------------------------------- list


def test_api_documents_list_filters_and_order(con):
    client = TestClient(webapp.app)
    tag = uuid_mod.uuid4().hex[:8]
    feed = _mk_source(con, f"docs-feed-{tag}", "rss")
    tk = f"DOCS{tag.upper()}"
    edgar = _mk_source(con, f"edgar:{tk}", "edgar")
    bucket = _mk_source(con, f"link:docs-{tag}.example", "link")
    old, _ = _mk_event(con, feed, "rss", f"Old article {tag}",
                       published="2026-01-05", meta={"thin": True},
                       triage={"materiality": 0.3, "route": "slow"})
    new, new_lin = _mk_event(con, feed, "rss", f"New article {tag}",
                             published="2026-08-10",
                             triage={"materiality": 0.7, "route": "fast"})
    # pending rows get a dead artifact: the suite shares one database and
    # test_pipeline runs triage + extract later, which must not find a
    # readable pending document of ours
    pend, _ = _mk_event(con, feed, "rss", f"Pending article {tag}",
                        status="pending", published="2026-08-11",
                        artifact_uri="file:///dev/null")
    fail, _ = _mk_event(con, feed, "rss", f"Failed article {tag}",
                        status="failed", last_error="boom", published="2026-08-12")
    filing, _ = _mk_event(con, edgar, "edgar", f"{tk} 8-K filed 2026-08-09",
                          meta={"ticker": tk, "form": "8-K",
                                "origin": "https://www.sec.gov/Archives/x"},
                          published="2026-08-09")
    linked, _ = _mk_event(con, bucket, "link", f"Linked article {tag}",
                          meta={"item_url": "https://docs.example/a"},
                          published="2026-08-08")
    # one document per remaining connector, all older than the rows above
    others = {}
    for i, (connector, label) in enumerate([
            ("podcast", "Podcast episode"), ("youtube", "Video"),
            ("x", "X posts"), ("bridge", "Feed item"), ("manual", "Upload")]):
        src = _mk_source(con, f"docs-{connector}-{tag}", connector)
        others[connector], _ = _mk_event(
            con, src, connector, f"{connector} doc {tag}",
            published=f"2026-07-{10 + i:02d}")
        others[f"{connector}:label"] = label
    today, _ = _mk_event(con, feed, "rss", f"Today {tag}")   # published null
    percent, _ = _mk_event(con, feed, "rss", f"100% sure_{tag}",
                           published="2025-12-01")
    copy = _mk_copy(con, feed, new, new_lin, title=f"Copy {tag}")
    ent = _mk_entity(con, f"Docs corp {tag}", {"ticker": tk})
    _mk_mention(con, new, "Docs corp", "sec:v1", ent)
    _mk_mention(con, new, "Docs corp again", "sec:v1", ent)
    _mk_claim(con, new, new_lin, "Docs corp", "supplies", "Other corp",
              subject_entity=ent)
    _mk_claim(con, new, new_lin, "Docs corp", "old_claim", "Other corp",
              status="superseded")
    con.execute(
        "insert into document_summary (event_id, status, summary, key_points, "
        "model) values (%s, 'done', 'Docs corp supplies Other corp.', %s, 'm')",
        (new, Jsonb(["A point."])))
    con.commit()

    mine = {str(x) for x in (old, new, pend, fail, filing, linked, copy, today,
                             percent,
                             *[v for k, v in others.items() if ":" not in k])}

    def fetch(qs=""):
        body = client.get(f"/api/documents?limit=200{qs}").json()
        return body["total"], [d for d in body["documents"]
                               if d["event_id"] in mine]

    # default: every status but duplicate, newest published first (a
    # document without a published date sorts by its fetch time: now)
    total, docs = fetch()
    assert total >= 12
    assert str(copy) not in _ids(docs)
    assert _ids(docs) == [str(today), str(fail), str(pend), str(new),
                          str(filing), str(linked)] + [
        str(others[c]) for c in ("manual", "bridge", "x", "youtube", "podcast")
    ] + [str(old), str(percent)]
    for connector in ("podcast", "youtube", "x", "bridge", "manual"):
        row = next(d for d in docs if d["event_id"] == str(others[connector]))
        assert row["type"] == others[f"{connector}:label"]
        assert row["connector"] == connector
    row = next(d for d in docs if d["event_id"] == str(new))
    assert row["title"] == f"New article {tag}"
    assert row["type"] == "Article"
    assert row["source"]["name"] == f"docs-feed-{tag}"
    assert row["source"]["label"] == "News feed"
    assert row["claims"] == 1            # asserted only
    assert row["entities"] == 1          # distinct across mentions
    assert row["materiality"] == 0.7
    assert row["summary_status"] == "done"
    assert row["summary"] == "Docs corp supplies Other corp."
    assert row["status"] == "extracted" and row["thin"] is False
    assert row["published_at"].startswith("2026-08-10")
    old_row = next(d for d in docs if d["event_id"] == str(old))
    assert old_row["thin"] is True and old_row["summary_status"] == "none"
    filing_row = next(d for d in docs if d["event_id"] == str(filing))
    assert filing_row["type"] == "Filing"
    assert filing_row["source"]["label"] == "Filings"
    assert filing_row["url"] == "https://www.sec.gov/Archives/x"
    linked_row = next(d for d in docs if d["event_id"] == str(linked))
    assert linked_row["type"] == "Article"
    assert linked_row["source"]["label"] == "Links"
    assert linked_row["url"] == "https://docs.example/a"

    # filters
    assert _ids(fetch(f"&source={feed}")[1]) == [str(today), str(fail),
                                                 str(pend), str(new), str(old),
                                                 str(percent)]
    assert _ids(fetch("&type=filing")[1]) == [str(filing)]
    assert set(_ids(fetch("&type=article")[1])) == {str(today), str(fail),
                                                    str(pend), str(new),
                                                    str(linked), str(old),
                                                    str(percent)}
    for connector, type_ in (("podcast", "podcast"), ("youtube", "video"),
                             ("x", "x"), ("bridge", "bridge"),
                             ("manual", "upload")):
        assert _ids(fetch(f"&type={type_}")[1]) == [str(others[connector])]
    assert _ids(fetch("&status=pending")[1]) == [str(pend)]
    assert _ids(fetch("&status=failed")[1]) == [str(fail)]
    assert _ids(fetch("&status=duplicate")[1]) == [str(copy)]
    assert str(old) in _ids(fetch("&status=extracted")[1])
    assert str(pend) not in _ids(fetch("&status=extracted")[1])
    assert _ids(fetch(f"&q=new+article+{tag}")[1]) == [str(new)]
    assert set(_ids(fetch(f"&q=docs-feed-{tag}")[1])) == {str(today), str(fail),
                                                          str(pend), str(new),
                                                          str(old), str(percent)}
    # a typed % or _ is a character to find, not a wildcard
    assert _ids(fetch("&q=100%25+sure")[1]) == [str(percent)]
    assert _ids(fetch(f"&q=sure_{tag}")[1]) == [str(percent)]
    assert str(new) not in _ids(fetch("&q=%25")[1])
    assert str(new) not in _ids(fetch("&q=_")[1])
    assert _ids(fetch(f"&ticker={tk.lower()}")[1]) == [str(filing)]
    # days: the just-fetched document is inside the window, January is not
    assert str(today) in _ids(fetch("&days=7")[1])
    assert str(old) not in _ids(fetch("&days=60")[1])
    # pagination keeps the total; limit is clamped to 200
    page = client.get(f"/api/documents?source={feed}&limit=2&offset=2").json()
    assert page["total"] == 6 and len(page["documents"]) == 2
    assert len(client.get("/api/documents?limit=500").json()["documents"]) <= 200
    # bad filters
    assert client.get("/api/documents?type=nope").status_code == 400
    assert client.get("/api/documents?status=nope").status_code == 400


def test_api_document_sources_lists_buckets_and_dropped(con):
    client = TestClient(webapp.app)
    tag = uuid_mod.uuid4().hex[:8]
    dropped = _mk_source(con, f"dropped-feed-{tag}", "rss", status="dropped")
    bucket = _mk_source(con, f"link:src-{tag}.example", "link")
    empty = _mk_source(con, f"empty-feed-{tag}", "rss")
    _mk_event(con, dropped, "rss", f"d {tag}")
    _mk_event(con, bucket, "link", f"l {tag}")
    _mk_event(con, bucket, "link", f"l2 {tag}")
    con.commit()

    # a syndicated copy is not counted: the number an analyst clicks on the
    # Sources page equals the rows the documents list shows by default
    b1, b1_lin = _mk_event(con, bucket, "link", f"l3 {tag}")
    _mk_copy(con, bucket, b1, b1_lin, connector="link")
    con.commit()
    rows = {r["source_id"]: r for r in client.get("/api/documents/sources").json()}
    assert rows[str(dropped)]["events"] == 1
    assert rows[str(bucket)]["events"] == 3
    feed = _mk_source(con, f"counted-feed-{tag}", "rss")
    f1, f1_lin = _mk_event(con, feed, "rss", f"f1 {tag}")
    _mk_copy(con, feed, f1, f1_lin)
    con.commit()
    sources = {r["source_id"]: r for r in client.get("/api/sources").json()["sources"]}
    assert sources[str(feed)]["events"] == 1
    assert client.get(f"/api/documents?source={feed}").json()["total"] == 1
    assert rows[str(bucket)]["label"] == "Links"
    # a live source with no document yet is listed (the Sources page links
    # its Docs count here); a dropped one without documents is not
    assert rows[str(empty)]["events"] == 0
    gone = _mk_source(con, f"gone-feed-{tag}", "rss", status="dropped")
    con.commit()
    assert str(gone) not in {r["source_id"] for r in
                             client.get("/api/documents/sources").json()}


# ---------------------------------------------------------------- detail


def test_api_document_detail(con):
    client = TestClient(webapp.app)
    tag = uuid_mod.uuid4().hex[:8]
    feed = _mk_source(con, f"detail-feed-{tag}", "podcast")
    mirror_feed = _mk_source(con, f"detail-mirror-{tag}", "rss")
    event_id, lineage_id = _mk_event(
        con, feed, "podcast", f"Episode {tag}", published="2026-08-05",
        meta={"feed": f"detail-feed-{tag}", "enclosure_url": "https://cdn/x.mp3"},
        triage={"materiality": 0.7, "route": "fast"})
    copy = _mk_copy(con, mirror_feed, event_id, lineage_id, title=f"Copy {tag}")
    con.execute("update event set meta = meta || %s where event_id=%s",
                (Jsonb({"near_duplicate_of": str(event_id)}), copy))
    a = _mk_entity(con, f"Alpha {tag}", {"ticker": f"ALP{tag}"})
    b = _mk_entity(con, f"Beta {tag}")
    b_old = _mk_entity(con, f"Beta old {tag}")
    # b_old merged into b: its mention folds into b on the page
    con.execute("insert into entity_same_as (a, b, decided_by) values "
                "(%s, %s, 'test')", (b_old, b))
    _mk_mention(con, event_id, "Alpha", "sec:v1", a)
    _mk_mention(con, event_id, "Beta", "gleif:v1", b)
    _mk_mention(con, event_id, "Beta Old", "gleif:v1", b_old)
    _mk_mention(con, event_id, "Gamma", "queued")
    _mk_mention(con, event_id, "Delta", "unresolved-v1")
    _mk_mention(con, event_id, "Epsilon", "pending")
    _mk_mention(con, event_id, "Someone", "skipped-v0")
    c1 = _mk_claim(con, event_id, lineage_id, "Alpha", "supplies", "Beta",
                   subject_entity=a, object_entity=b, age_min=3)
    c2 = _mk_claim(con, event_id, lineage_id, "Alpha", "old_revenue", "x",
                   subject_entity=a, status="superseded", age_min=2)
    c3 = _mk_claim(con, event_id, lineage_id, "Alpha", "reported_revenue", "y",
                   subject_entity=a, age_min=1)
    c4 = _mk_claim(con, event_id, lineage_id, "Alpha", "withdrawn", "z",
                   subject_entity=a, status="retracted", age_min=0)
    # both ends fold to b after the merge: one claim, one count for b
    c5 = _mk_claim(con, event_id, lineage_id, "Beta old", "renamed_to", "Beta",
                   subject_entity=b_old, object_entity=b, age_min=0)
    # c4 and c5 share an observed_at; the tie breaks on claim_id
    c4, c5 = sorted([c4, c5], key=str)
    # an edge backed by c1 plus a claim from elsewhere
    other_ev, other_lin = _mk_event(con, feed, "podcast", f"Other {tag}")
    c_other = _mk_claim(con, other_ev, other_lin, "Alpha", "supplies", "Beta",
                        subject_entity=a, object_entity=b)
    con.execute(
        "insert into edge (src, dst, predicate, origin, claim_ids, confidence, "
        "last_evidence_at, half_life_days) values (%s, %s, 'supplies', "
        "'asserted', %s::uuid[], 0.9, now(), 540)", (a, b, [c1, c_other]))
    # an inferred edge that does not touch this document
    c_far = _mk_claim(con, other_ev, other_lin, "Alpha", "competes", "Beta",
                      subject_entity=a, object_entity=b)
    con.execute(
        "insert into edge (src, dst, predicate, origin, claim_ids, confidence, "
        "last_evidence_at) values (%s, %s, 'shared_dependency', 'inferred', "
        "%s::uuid[], 0.6, now())", (a, b, [c_far]))
    hyp = con.execute(
        "insert into hypothesis (type, subjects, statement, test_plan, origin, "
        "budget, state, evidence) values ('shared_dependency', %s::uuid[], "
        "'{}', '{}', '{}', '{}', 'promoted', %s::uuid[]) returning hypothesis_id",
        ([a, b], [c3])).fetchone()["hypothesis_id"]
    contradiction = con.execute(
        "insert into contradiction (subject_entity, predicate_canon, claim_a, "
        "claim_b, kind) values (%s, 'revenue', %s, %s, 'literal_conflict') "
        "returning contradiction_id", (a, c3, c_far)).fetchone()["contradiction_id"]
    alert = con.execute(
        "insert into alert (kind, title, event_id) values "
        "('material_event', %s, %s) returning alert_id",
        (f"ALP{tag}: episode", event_id)).fetchone()["alert_id"]
    con.execute(
        "insert into document_summary (event_id, status, summary, key_points, "
        "model) values (%s, 'done', 'Alpha supplies Beta.', %s, 'test-model')",
        (event_id, Jsonb(["Point one.", "Point two."])))
    con.commit()

    try:
        _check_detail(client, con, tag, event_id, copy, a, b, b_old, c1, c2, c3,
                      c4, c5, hyp, contradiction, alert)
    finally:
        # undo the merge on the way out, even when an assertion above fails:
        # the merge tests later in the suite assert on the whole active
        # same_as map
        con.execute("update entity_same_as set status='reverted' where a=%s",
                    (b_old,))
        con.commit()
    assert {e["entity_id"] for e in client.get(
        f"/api/documents/{event_id}").json()["entities"]} == {str(a), str(b),
                                                               str(b_old)}


def _check_detail(client, con, tag, event_id, copy, a, b, b_old, c1, c2, c3,
                  c4, c5, hyp, contradiction, alert):
    body = client.get(f"/api/documents/{event_id}").json()
    doc = body["document"]
    assert doc["title"] == f"Episode {tag}"
    assert doc["type"] == "Podcast episode"
    assert doc["source"]["label"] == "Podcast"
    assert doc["url"] == "https://cdn/x.mp3"
    assert doc["status"] == "extracted"
    assert doc["chars"] == len(BODY)
    assert doc["materiality"] == 0.7 and doc["route"] == "fast"
    assert doc["facts"] == {"feed": f"detail-feed-{tag}"}
    assert doc["lineage"]["root"] is None
    assert [c["event_id"] for c in doc["lineage"]["copies"]] == [str(copy)]
    assert doc["lineage"]["copies"][0]["source_name"] == f"detail-mirror-{tag}"
    assert doc["near_duplicate_of"] is None
    assert doc["attempts"] == 0 and doc["last_error"] is None
    assert "summary" not in doc and "summary_status" not in doc

    assert body["summary"]["status"] == "done"
    assert body["summary"]["summary"] == "Alpha supplies Beta."
    assert body["summary"]["key_points"] == ["Point one.", "Point two."]
    assert body["summary"]["model"] == "test-model"

    # every status, observed order; withdrawn ones are marked
    claims = body["claims"]
    assert [c["claim_id"] for c in claims] == [str(x) for x in
                                               (c1, c2, c3, c4, c5)]
    assert {c["status"] for c in claims} == {"asserted", "superseded",
                                             "retracted"}
    assert claims[1]["status"] == "superseded"
    assert claims[0]["subject"]["entity_id"] == str(a)
    # the list row counts asserted claims only and folds the merge
    row = next(d for d in client.get(
        f"/api/documents?q=Episode+{tag}").json()["documents"]
               if d["event_id"] == str(event_id))
    assert row["claims"] == 3 and row["entities"] == 2

    states = {m["surface"]: m["state"] for m in body["mentions"]}
    assert states == {"Alpha": "resolved", "Beta": "resolved",
                      "Beta Old": "resolved", "Gamma": "queued",
                      "Delta": "unresolved", "Epsilon": "pending",
                      "Someone": "skipped"}
    alpha = next(m for m in body["mentions"] if m["surface"] == "Alpha")
    assert alpha["entity"] == {"entity_id": str(a), "name": f"Alpha {tag}"}

    ents = {e["entity_id"]: e for e in body["entities"]}
    assert set(ents) == {str(a), str(b)}          # b_old folded into b
    # c5 (Beta old -> Beta) folds both ends onto b and counts once for it
    assert ents[str(a)]["claims"] == 4 and ents[str(a)]["mentions"] == 1
    assert ents[str(a)]["registry"] == f"ALP{tag}"
    assert ents[str(a)]["ticker"] == f"ALP{tag}" and ents[str(b)]["ticker"] is None
    assert ents[str(b)]["claims"] == 2 and ents[str(b)]["mentions"] == 2
    assert body["entities"][0]["entity_id"] == str(a)   # claims desc

    assert len(body["edges"]) == 1
    edge = body["edges"][0]
    assert edge["predicate"] == "supplies" and edge["origin"] == "asserted"
    assert edge["claims_here"] == 1 and edge["claims_total"] == 2
    assert edge["src"]["name"] == f"Alpha {tag}"
    assert edge["dst"]["entity_id"] == str(b)
    assert 0 < edge["relevance"] <= 1

    assert [h["hypothesis_id"] for h in body["hypotheses"]] == [str(hyp)]
    assert body["hypotheses"][0]["state"] == "promoted"
    assert {s["name"] for s in body["hypotheses"][0]["subjects"]} == {
        f"Alpha {tag}", f"Beta {tag}"}
    assert [t["contradiction_id"] for t in body["contradictions"]] == [
        str(contradiction)]
    assert body["contradictions"][0]["subject"]["name"] == f"Alpha {tag}"
    assert [x["alert_id"] for x in body["alerts"]] == [str(alert)]

    # the copy points back at its root and carries no summary of its own
    copy_body = client.get(f"/api/documents/{copy}").json()
    assert copy_body["document"]["status"] == "duplicate"
    assert copy_body["document"]["near_duplicate_of"] == {
        "event_id": str(event_id), "title": f"Episode {tag}"}
    assert copy_body["document"]["lineage"]["root"] == {
        "event_id": str(event_id), "title": f"Episode {tag}"}
    # its siblings, not the original the header already names
    assert copy_body["document"]["lineage"]["copies"] == []
    assert copy_body["summary"]["status"] == "none"
    assert client.post(f"/api/documents/{copy}/summarize").status_code == 400

    assert client.get(f"/api/documents/{uuid_mod.uuid4()}").status_code == 404

    # a failed extraction carries its error and strike count for the page
    feed = con.execute("select source_id from event where event_id=%s",
                       (event_id,)).fetchone()["source_id"]
    failed, _ = _mk_event(con, feed, "podcast", f"Failed {tag}", status="failed",
                          last_error="boom", attempts=2)
    con.commit()
    fdoc = client.get(f"/api/documents/{failed}").json()["document"]
    assert fdoc["status"] == "failed" and fdoc["attempts"] == 2
    assert fdoc["last_error"] == "boom"


def test_api_document_text_and_missing_artifact(con, monkeypatch):
    client = TestClient(webapp.app)
    tag = uuid_mod.uuid4().hex[:8]
    feed = _mk_source(con, f"text-feed-{tag}", "rss")
    event_id, _ = _mk_event(con, feed, "rss", f"Text {tag}")
    gone, _ = _mk_event(con, feed, "rss", f"Gone {tag}",
                        artifact_uri="/nonexistent/path.txt")
    # an upload from before titles were stored in meta shows its filename
    # on the list and the page alike (one title rule, spec §5)
    uri = _artifact("manual", f"Real title {tag}", BODY)
    untitled = con.execute(
        "insert into event (source_id, connector, content_hash, artifact_uri, "
        "mime, status, meta) values (%s, 'manual', %s, %s, 'text/plain', "
        "'extracted', %s) returning event_id",
        (feed, os.urandom(32), uri, Jsonb({"filename": "old.htm"}))
    ).fetchone()["event_id"]
    con.commit()

    body = client.get(f"/api/documents/{event_id}/text").json()
    assert body["header"] == {"title": f"Text {tag}", "source_type": "article",
                              "published": "2026-08-01"}
    assert body["body"] == BODY
    assert body["chars"] == len(BODY) and body["truncated"] is False

    r = client.get(f"/api/documents/{gone}/text")
    assert r.status_code == 404
    assert r.json()["detail"] == "The document text is no longer on disk."
    # the detail page still serves the row, with no text to count
    assert client.get(f"/api/documents/{gone}").json()["document"]["chars"] == 0
    # a real file outside the artifact store is not served either: the
    # reader is contained to config.ARTIFACTS (a stale artifact_uri must
    # never become a file-read primitive)
    outside, _ = _mk_event(con, feed, "rss", f"Outside {tag}",
                           artifact_uri=os.path.abspath(__file__))
    con.commit()
    assert client.get(f"/api/documents/{outside}/text").status_code == 404
    assert client.get(f"/api/documents/{outside}").json()["document"]["chars"] == 0

    assert client.get(f"/api/documents/{untitled}").json()["document"]["title"] \
        == "old.htm"
    assert client.get(f"/api/documents/{untitled}/text").json()["header"]["title"] \
        == f"Real title {tag}"

    monkeypatch.setattr(webapp, "DOC_TEXT_CAP", 100)
    body = client.get(f"/api/documents/{event_id}/text").json()
    assert body["truncated"] is True and len(body["body"]) == 100
    assert body["chars"] == len(BODY)
    # the read itself is bounded: past DOC_TEXT_BYTES the text is cut while
    # the character count stays exact (counted in chunks, never held)
    monkeypatch.setattr(webapp, "DOC_TEXT_BYTES", 300)
    body = client.get(f"/api/documents/{event_id}/text").json()
    assert body["truncated"] is True and len(body["body"]) == 100
    assert body["chars"] == len(BODY)
    assert client.get(f"/api/documents/{event_id}").json()["document"]["chars"] \
        == len(BODY)
    # multibyte text: characters, not bytes, on both the count and the cut
    accents, _ = _mk_event(con, feed, "rss", f"Accents {tag}", body="é" * 1000)
    con.commit()
    body = client.get(f"/api/documents/{accents}/text").json()
    assert body["chars"] == 1000 and body["truncated"] is True
    assert body["body"] == "é" * 100
    # out-of-range day windows are clamped, never a 500
    assert client.get("/api/documents?days=2147483648").status_code == 200
    assert client.get("/api/claims?days=2147483648").status_code == 200


def test_split_artifact():
    header, body = summarize.split_artifact(
        "# title: A: b\n# source_type: filing\n# ticker: NVDA\n---\nline one\n\n"
        "line two\n")
    assert header == {"title": "A: b", "source_type": "filing", "ticker": "NVDA"}
    assert body == "line one\n\nline two"
    assert summarize.split_artifact("plain text, no header") == \
        ({}, "plain text, no header")
    # a '---' deep in a headerless text is not a header rule
    assert summarize.split_artifact("para\n---\nmore") == ({}, "para\n---\nmore")
    # a title that runs over several lines (Telegram items through the
    # bridge) continues the value; the body keeps its own rules
    header, body = summarize.split_artifact(
        "# title: Telegram turns 13 today. \r\n\r\nFor over half that time...\n"
        "# source_type: article\n# published: 2026-08-14\n# origin: durov\n---\n"
        "the post\n---\nmore\n")
    assert header == {"title": "Telegram turns 13 today. For over half that time...",
                      "source_type": "article", "published": "2026-08-14",
                      "origin": "durov"}
    assert body == "the post\n---\nmore"
    # no rule within the header scan: no header, and the text is untouched
    long = "# title: x\n" + "y" * (summarize.HEADER_SCAN + 10) + "\n---\nbody"
    assert summarize.split_artifact(long) == ({}, long)
    # an empty body after the rule
    assert summarize.split_artifact("# title: t\n---\n") == ({"title": "t"}, "")


# ---------------------------------------------------------------- summarize


def _candidate_ids(con, **kw):
    return [r["event_id"] for r in con.execute(
        summarize.CANDIDATES_SQL,
        {"auto": not kw.get("requested_only", False), "limit": 500,
         "exclude": []})]


def test_summarize_request_run_and_states(con, monkeypatch, tmp_path):
    client = TestClient(webapp.app)
    tag = uuid_mod.uuid4().hex[:8]
    feed = _mk_source(con, f"sum-feed-{tag}", "rss")
    triaged = {"materiality": 0.3, "route": "slow"}
    # published in 2099: the suite shares one database, and the ordering
    # assertions below must hold against whatever earlier files left behind
    auto_new, _ = _mk_event(con, feed, "rss", f"Auto new {tag}", triage=triaged,
                            published="2099-08-10")
    auto_old, _ = _mk_event(con, feed, "rss", f"Auto old {tag}", triage=triaged,
                            published="2099-08-01")
    untriaged, _ = _mk_event(con, feed, "rss", f"Untriaged {tag}")
    # mid-extraction (status extracting, never pending: later test files
    # run extract against the shared database and must not touch these)
    pending, _ = _mk_event(con, feed, "rss", f"Pending {tag}",
                           status="extracting", triage=triaged)
    failed_ev, _ = _mk_event(con, feed, "rss", f"Failed extract {tag}",
                             status="failed", triage=triaged, published="2099-07-01")
    short, _ = _mk_event(con, feed, "rss", f"Short {tag}", body=SHORT_BODY,
                         triage=triaged, published="2099-08-12")
    root, lin = _mk_event(con, feed, "rss", f"Root {tag}", triage=triaged,
                          published="2099-06-01")
    copy = _mk_copy(con, feed, root, lin)
    requested, _ = _mk_event(con, feed, "rss", f"Requested {tag}",
                             status="extracting")
    con.commit()
    mine = {auto_new, auto_old, untriaged, pending, failed_ev, short, root,
            copy, requested}

    # the page asks: the row is queued even though extraction has not finished
    r = client.post(f"/api/documents/{requested}/summarize")
    assert r.json() == {"ok": True, "status": "requested"}
    row = con.execute("select * from document_summary where event_id=%s",
                      (requested,)).fetchone()
    assert row["status"] == "requested" and row["requested_at"] is not None
    detail = client.get(f"/api/documents/{requested}").json()
    assert detail["summary"]["status"] == "requested"
    assert summarize.requested_pending(con)

    # candidates: requested first, then extracted/failed triaged documents
    # newest first; never the copy, the untriaged or the one still extracting
    ids = [i for i in _candidate_ids(con) if i in mine]
    assert ids == [requested, short, auto_new, auto_old, failed_ev, root]
    assert [i for i in _candidate_ids(con, requested_only=True) if i in mine] \
        == [requested]

    prompts = []

    def fake(prompt, model, max_tokens=8000, **kw):
        prompts.append(prompt)
        return {"summary": "  Alpha   supplies Beta.\nA second sentence. ",
                "key_points": ["  Point one. ", "", 42, "Point two."] + [
                    f"extra {i}" for i in range(20)]}

    monkeypatch.setattr(llm, "complete_json", fake)
    monkeypatch.setitem(config.MODELS, "summarize", "test-model")
    if True:  # noqa: one block, shared monkeypatches
        out = summarize.run(con, limit=2, requested_only=True)
        assert out == {"summarized": 1, "skipped": 0, "failed": 0,
                       "transient": 0, "paused": False, "requested_left": 0}
        con.commit()
        row = con.execute("select * from document_summary where event_id=%s",
                          (requested,)).fetchone()
        assert row["status"] == "done"
        assert row["summary"] == "Alpha supplies Beta. A second sentence."
        assert row["key_points"] == ["Point one.", "Point two."] + [
            f"extra {i}" for i in range(8)]
        assert row["model"] == "test-model" and row["error"] is None
        assert prompts and f"Requested {tag}" in prompts[0]
        assert "# Document text" in prompts[0]
        detail = client.get(f"/api/documents/{requested}").json()
        assert detail["summary"]["status"] == "done"
        assert detail["summary"]["summary"] == "Alpha supplies Beta. A second sentence."
        assert not any(i in mine for i in _candidate_ids(con, requested_only=True))

        # the automatic pass: the short one is skipped without a model call
        prompts.clear()
        out = summarize.run(con, limit=2)
        assert out == {"summarized": 1, "skipped": 1, "failed": 0,
                       "transient": 0, "paused": False, "requested_left": 0}
        con.commit()
        # limit 0 is a no-op, not the default
        assert summarize.run(con, limit=0)["summarized"] == 0
        assert len(prompts) == 1 and f"Auto new {tag}" in prompts[0]
        short_row = con.execute(
            "select status, summary from document_summary where event_id=%s",
            (short,)).fetchone()
        assert short_row["status"] == "skipped" and short_row["summary"] is None
        assert client.get(f"/api/documents/{short}").json()["summary"]["status"] \
            == "skipped"
        # a cut body says so in the prompt
        monkeypatch.setattr(config, "SUMMARY_MAX_CHARS", 700)
        prompts.clear()
        summarize.run(con, limit=1)
        assert "(The text is cut here; the document continues.)" in prompts[0]
        monkeypatch.setattr(config, "SUMMARY_MAX_CHARS", 80000)

        # engine outage: nothing changes, no attempt burnt
        def unavailable(*a, **k):
            raise llm.EngineUnavailable("no seat")

        monkeypatch.setattr(llm, "complete_json", unavailable)
        before = con.execute(
            "select event_id, status, attempts from document_summary "
            "where event_id = any(%s) order by event_id", (list(mine),)).fetchall()
        out = summarize.run(con)
        assert out["paused"] is True and out["summarized"] == 0
        after = con.execute(
            "select event_id, status, attempts from document_summary "
            "where event_id = any(%s) order by event_id", (list(mine),)).fetchall()
        assert before == after

        # a throwing model: pending after one strike, failed after two; a
        # requested row keeps its place in the queue for the second try
        client.post(f"/api/documents/{failed_ev}/summarize")

        def boom(*a, **k):
            raise RuntimeError("bad json " * 100)

        monkeypatch.setattr(llm, "complete_json", boom)
        out = summarize.run(con, limit=1, requested_only=True)
        assert out["failed"] == 1
        row = con.execute("select * from document_summary where event_id=%s",
                          (failed_ev,)).fetchone()
        assert row["status"] == "requested" and row["attempts"] == 1
        assert row["error"].startswith("bad json") and len(row["error"]) <= 500
        # a fresh strike waits five minutes before the second try (the nap
        # loop answers every fifteen seconds and must not burn both strikes
        # on one transient error); the request no longer counts as waiting
        assert failed_ev not in _candidate_ids(con, requested_only=True)
        assert summarize.requested_count(con) == 0
        summarize.run(con, limit=1, requested_only=True)
        row = con.execute("select * from document_summary where event_id=%s",
                          (failed_ev,)).fetchone()
        assert row["status"] == "requested" and row["attempts"] == 1
        _age_summary(con, failed_ev)
        assert summarize.requested_count(con) == 1
        summarize.run(con, limit=1, requested_only=True)
        row = con.execute("select * from document_summary where event_id=%s",
                          (failed_ev,)).fetchone()
        assert row["status"] == "failed" and row["attempts"] == 2
        con.commit()
        detail = client.get(f"/api/documents/{failed_ev}").json()
        assert detail["summary"]["status"] == "failed"
        assert detail["summary"]["error"].startswith("bad json")
        # a missing artifact is a strike with fixed copy, not a server path
        gone, _ = _mk_event(con, feed, "rss", f"Gone {tag}",
                            artifact_uri=str(tmp_path / "gone.txt"))
        con.commit()
        client.post(f"/api/documents/{gone}/summarize")
        summarize.run(con, limit=1, requested_only=True)
        _age_summary(con, gone)
        summarize.run(con, limit=1, requested_only=True)
        row = con.execute("select * from document_summary where event_id=%s",
                          (gone,)).fetchone()
        assert row["status"] == "failed"
        assert row["error"] == "The document text is no longer on disk."
        # an automatic candidate goes pending first
        out = summarize.run(con, limit=1)
        assert out["failed"] == 1
        row = con.execute(
            "select status, attempts from document_summary where event_id=%s",
            (root,)).fetchone()
        assert row["status"] == "pending" and row["attempts"] == 1
        assert root in _candidate_ids(con)        # retried on the next pass

        # a new request resets the strikes and keeps the old summary text
        client.post(f"/api/documents/{requested}/summarize")
        row = con.execute("select * from document_summary where event_id=%s",
                          (requested,)).fetchone()
        assert row["status"] == "requested" and row["attempts"] == 0
        assert row["summary"] == "Alpha supplies Beta. A second sentence."
        detail = client.get(f"/api/documents/{requested}").json()
        assert detail["summary"]["status"] == "requested"
        assert detail["summary"]["summary"].startswith("Alpha supplies")
        summarize.run(con, limit=1, requested_only=True)     # boom, strike 1
        row = con.execute("select * from document_summary where event_id=%s",
                          (requested,)).fetchone()
        assert row["status"] == "requested" and row["attempts"] == 1
        assert row["summary"] == "Alpha supplies Beta. A second sentence."

        # a document that comes back thin keeps the summary it already has
        monkeypatch.setattr(config, "SUMMARY_MIN_CHARS", 10 ** 6)
        _age_summary(con, requested)
        out = summarize.run(con, limit=1, requested_only=True)
        assert out["skipped"] == 1
        row = con.execute("select * from document_summary where event_id=%s",
                          (requested,)).fetchone()
        assert row["status"] == "skipped"
        assert row["summary"] == "Alpha supplies Beta. A second sentence."
        assert row["key_points"] == ["Point one.", "Point two."] + [
            f"extra {i}" for i in range(8)]
        monkeypatch.setattr(config, "SUMMARY_MIN_CHARS", 600)
        con.commit()


def _age_summary(con, event_id, minutes=6):
    """Backdate a summary row's last touch past the retry wait."""
    con.execute(
        "update document_summary set updated_at = now() - make_interval("
        "mins => %s) where event_id=%s", (minutes, event_id))


def test_summarize_cli_and_drain(con, monkeypatch, capsys):
    tag = uuid_mod.uuid4().hex[:8]
    feed = _mk_source(con, f"cli-feed-{tag}", "rss")
    triaged = {"materiality": 0.3, "route": "slow"}
    # published in 2099 so the automatic pass reaches these rows first
    # whatever earlier tests left behind (shared database)
    event_id, _ = _mk_event(con, feed, "rss", f"CLI {tag}", triage=triaged,
                            published="2099-09-01")
    others = [_mk_event(con, feed, "rss", f"CLI {tag} {i}", triage=triaged,
                        published=f"2099-08-{10 + i:02d}")[0]
              for i in range(7)]
    asked, _ = _mk_event(con, feed, "rss", f"CLI asked {tag}", status="extracting")
    summarize.request(con, asked)
    con.commit()

    def row_of(event):
        with db.connect() as c:
            return c.execute("select status, attempts, summary from "
                             "document_summary where event_id=%s",
                             (event,)).fetchone()

    # --requested serves the page's request and nothing else of ours
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **k: {"summary": "Done.", "key_points": []})
    monkeypatch.setattr("sys.argv", ["graph", "summarize", "--requested"])
    cli.main()
    out = capsys.readouterr().out
    assert "summarized" in out
    assert row_of(asked)["status"] == "done" and row_of(asked)["summary"] == "Done."
    assert row_of(event_id) is None

    # --limit 0 does nothing
    monkeypatch.setattr("sys.argv", ["graph", "summarize", "--limit", "0"])
    cli.main()
    assert row_of(event_id) is None

    # the drain commits chunk by chunk: a model that dies on its seventh
    # call leaves the first chunk of five committed plus the one done in the
    # second chunk, so a restart mid-batch loses at most one chunk
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 7:
            raise llm.EngineUnavailable("seat gone")
        return {"summary": f"Done {calls['n']}.", "key_points": []}

    monkeypatch.setattr(llm, "complete_json", flaky)
    with db.connect() as c:
        out = cli._summarize_drain(c, limit=8)
        c.rollback()          # anything uncommitted by the drain is dropped
    assert out["paused"] is True and out["summarized"] == 6
    assert out["requested_left"] == 0
    done = [e for e in [event_id] + others if row_of(e) is not None
            and row_of(e)["status"] == "done"]
    assert len(done) == 6 and event_id in done

    # a dispose failure (no summary text) is a strike with fixed copy, not
    # a crash; the drain stops when a chunk makes no progress
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"nope": 1})
    monkeypatch.setattr("sys.argv", ["graph", "summarize", "--limit", "2"])
    cli.main()
    struck = [e for e in others if row_of(e) is not None
              and row_of(e)["status"] == "pending"]
    assert len(struck) == 2
    with db.connect() as c:
        err = c.execute("select error from document_summary where event_id=%s",
                        (struck[0],)).fetchone()["error"]
    assert err == "The model did not return a summary."


def test_nap_loop_answers_summary_requests(monkeypatch):
    """cli._answer_summary_requests (build spec v5 §4): runs the requested-only
    stage when a request waits; backs off five minutes when the engine is
    paused, the stage errored, or the call cleared nothing."""
    calls = []

    def stage(name, fn):
        calls.append(name)
        return stage.result

    # nothing waiting: no stage call
    monkeypatch.setattr(cli, "_summaries_requested", lambda: 0)
    stage.result = {"transient": 0, "paused": False, "requested_left": 0}
    assert cli._answer_summary_requests(stage, 0.0, 1000.0) == 0.0
    assert calls == []

    # one waiting, cleared: stage ran, no back-off
    monkeypatch.setattr(cli, "_summaries_requested", lambda: 1)
    assert cli._answer_summary_requests(stage, 0.0, 1000.0) == 0.0
    assert calls == ["summarize"]

    # paused engine: back off 300 s, and the back-off is honoured
    stage.result = {"paused": True, "requested_left": 1}
    assert cli._answer_summary_requests(stage, 0.0, 2000.0) == 2300.0
    assert cli._answer_summary_requests(stage, 2300.0, 2100.0) == 2300.0
    assert calls == ["summarize", "summarize"]
    assert cli._answer_summary_requests(stage, 2300.0, 2300.0) == 2600.0

    # nothing cleared (a request the stage cannot act on): back off
    stage.result = {"paused": False, "requested_left": 1}
    assert cli._answer_summary_requests(stage, 0.0, 3000.0) == 3300.0
    # stage exception (returns None): back off
    stage.result = None
    assert cli._answer_summary_requests(stage, 0.0, 4000.0) == 4300.0
