"""End-to-end pipeline test: ingest -> extract -> resolve -> materialize.
No network, no real LLM — graph.llm.complete_json is monkeypatched.

CAF_DB_URL must be set before graph.config is imported (it reads env at import).
"""
import copy
import os
import subprocess

os.environ["CAF_DB_URL"] = "postgresql:///caf_graph_test"

import pytest

from graph import config, db, llm
from graph.connectors import manual
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
