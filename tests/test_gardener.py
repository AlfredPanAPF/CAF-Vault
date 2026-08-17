"""Gardener tests (build spec v3 §6): trigger thresholds, mocked-LLM mapping
with code-side validation (snake_case canons, first-assignment dedup, self-map
fallback), append-only full-version writes, and canon-aware materialize.
The claims API's predicate_canon surface belongs to the API builder.
No network, no real LLM. Database fixtures (database, con) live in conftest.py.
Run isolated:
    CAF_DB_URL=postgresql:///caf_test_gardener uv run pytest tests/test_gardener.py -q
"""
import os

from psycopg.types.json import Jsonb

from graph import db, llm
from graph.pipeline import gardener, materialize


def _clean_slate(con):
    """Delete global-aggregate tables inside this test's transaction; the con
    fixture rolls back, so committed rows from other files reappear afterward."""
    for table in ("contradiction", "edge", "claim", "predicate_map"):
        con.execute(f"delete from {table}")


def _mk_event(con, source_name="gardener-feed", connector="rss"):
    source_id = db.get_or_create_source(con, source_name, connector)
    event_id = con.execute(
        "insert into event (source_id, connector, content_hash, artifact_uri, "
        "mime, status) values (%s, %s, %s, 'file:///dev/null', 'text/plain', "
        "'extracted') returning event_id",
        (source_id, connector, os.urandom(32))).fetchone()["event_id"]
    lineage_id = con.execute(
        "insert into lineage (root_event_id) values (%s) returning lineage_id",
        (event_id,)).fetchone()["lineage_id"]
    con.execute("update event set lineage_id=%s where event_id=%s",
                (lineage_id, event_id))
    return event_id, lineage_id


def _mk_claim(con, event_id, lineage_id, predicate, subject=None, obj=None):
    return con.execute(
        "insert into claim (subject_entity, subject_surface, predicate_raw, "
        "object_entity, object_surface, event_id, lineage_id, observed_at, "
        "confidence, extractor, status) values "
        "(%s, 'S', %s, %s, 'O', %s, %s, now(), 0.9, 'test-v1', 'asserted') "
        "returning claim_id",
        (subject, predicate, obj, event_id, lineage_id)).fetchone()["claim_id"]


def _version_map(con, version):
    return {r["predicate_raw"]: r["predicate_canon"] for r in con.execute(
        "select predicate_raw, predicate_canon from predicate_map "
        "where version=%s", (version,))}


def test_gardener_trigger_mapping_and_versions(con, monkeypatch):
    _clean_slate(con)
    event_id, lineage_id = _mk_event(con)
    calls = []
    responses = [
        {"mapping": {
            "supplies": ["supplies", "supplier_of"],
            "Acquired!": ["acquired", "bought", "supplies"],  # dup + bad case
            "reported_revenue": ["not_in_input"],             # unknown raw
        }},
        {"mapping": {}},                                      # v2: all self-map
    ]

    def fake(prompt, model, **kw):
        calls.append(prompt)
        return responses[len(calls) - 1]

    monkeypatch.setattr(llm, "complete_json", fake)

    # 29 distinct raws, no mapping yet -> below the first-run threshold
    named = ["supplies", "supplier_of", "acquired", "bought"]
    for p in named + [f"pred_{i:02d}" for i in range(25)]:
        _mk_claim(con, event_id, lineage_id, p)
    _mk_claim(con, event_id, lineage_id, "Supplies")   # same raw, lowered
    out = gardener.run(con)
    assert out == {"triggered": False, "raws": 29, "unmapped": 29}
    assert calls == []
    assert con.execute("select count(*) n from predicate_map"
                       ).fetchone()["n"] == 0

    # the 30th raw trips the first run
    _mk_claim(con, event_id, lineage_id, "took_over")
    out = gardener.run(con)
    assert out["triggered"] is True
    assert out["version"] == 1
    assert out["raws"] == 30
    assert out["self_mapped"] == 26            # 4 assigned by the model
    assert out["duplicates"] == 1              # "supplies" listed twice
    assert "supplies, 2" in calls[0]           # counts fed to the model
    assert "supplier_of, 1" in calls[0]

    v1 = _version_map(con, 1)
    assert len(v1) == 30                       # full mapping, every raw once
    assert v1["supplies"] == "supplies"        # first assignment kept
    assert v1["supplier_of"] == "supplies"
    assert v1["acquired"] == "acquired"        # canon snake_cased
    assert v1["bought"] == "acquired"
    assert v1["took_over"] == "took_over"      # missing from output: self-map
    assert "not_in_input" not in v1            # unknown raws dropped

    # 19 unmapped raws -> below the re-run threshold
    for i in range(19):
        _mk_claim(con, event_id, lineage_id, f"new_{i:02d}")
    out = gardener.run(con)
    assert out == {"triggered": False, "raws": 49, "unmapped": 19}
    assert len(calls) == 1

    # the 20th unmapped raw triggers version 2. Delta prompting: the model
    # sees only the unmapped raws plus the existing canon list — but the
    # write is still a FULL mapping version (append-only invariant)
    _mk_claim(con, event_id, lineage_id, "new_19")
    out = gardener.run(con)
    assert out["version"] == 2
    assert out["raws"] == 50
    assert out["self_mapped"] == 20            # only the new raws self-map
    assert "new_19, 1" in calls[1]             # unmapped raws sent
    assert "supplies, 2" not in calls[1]       # mapped raws not re-sent
    assert "# Existing canonical predicates" in calls[1]
    assert "\nsupplies\n" in calls[1]          # prior canons offered for reuse
    v2 = _version_map(con, 2)
    assert len(v2) == 50
    assert v2["supplier_of"] == "supplies"     # prior canon carried forward
    assert v2["new_00"] == "new_00"            # new raws self-mapped
    assert len(_version_map(con, 1)) == 30     # append-only: v1 untouched


def test_gardener_pauses_on_engine_outage(con, monkeypatch):
    _clean_slate(con)
    event_id, lineage_id = _mk_event(con)
    for i in range(30):
        _mk_claim(con, event_id, lineage_id, f"pred_{i:02d}")

    def unavailable(prompt, model, **kw):
        raise llm.EngineUnavailable("all seats latched")

    monkeypatch.setattr(llm, "complete_json", unavailable)
    out = gardener.run(con)
    assert out == {"triggered": True, "paused": True}
    assert con.execute("select count(*) n from predicate_map"
                       ).fetchone()["n"] == 0


def test_gardener_records_failure_without_crashing(con, monkeypatch):
    """Malformed/truncated model output must return a failure record, not
    crash the stage — the trigger simply re-fires next cycle."""
    _clean_slate(con)
    event_id, lineage_id = _mk_event(con)
    for i in range(30):
        _mk_claim(con, event_id, lineage_id, f"fail_{i:02d}")

    def bad(prompt, model, **kw):
        raise ValueError("no valid JSON after 2 attempts: truncated")

    monkeypatch.setattr(llm, "complete_json", bad)
    out = gardener.run(con)
    assert out["triggered"] is True
    assert "no valid JSON" in out["failed"]
    assert con.execute("select count(*) n from predicate_map"
                       ).fetchone()["n"] == 0

    # a mapping that is not a dict takes the same failure path
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **kw: {"mapping": ["not", "a", "dict"]})
    out = gardener.run(con)
    assert out["triggered"] is True
    assert "no mapping object" in out["failed"]
    assert con.execute("select count(*) n from predicate_map"
                       ).fetchone()["n"] == 0


def test_materialize_groups_by_canon(con):
    _clean_slate(con)
    a = con.execute(
        "insert into entity (kind, canonical_name) values ('company', 'A Co') "
        "returning entity_id").fetchone()["entity_id"]
    c = con.execute(
        "insert into entity (kind, canonical_name) values ('company', 'C Co') "
        "returning entity_id").fetchone()["entity_id"]
    event_id, lineage_id = _mk_event(con)
    _mk_claim(con, event_id, lineage_id, "supplies", subject=a, obj=c)
    _mk_claim(con, event_id, lineage_id, "supplier_of", subject=a, obj=c)

    # no mapping yet: raw predicates stay separate
    materialize.run(con)
    assert con.execute("select count(*) n from edge where origin='asserted'"
                       ).fetchone()["n"] == 2

    con.execute(
        "insert into predicate_map (version, predicate_raw, predicate_canon) "
        "values (1, 'supplies', 'supplies'), (1, 'supplier_of', 'supplies')")
    materialize.run(con)
    edges = con.execute("select * from edge where origin='asserted'").fetchall()
    assert len(edges) == 1
    assert edges[0]["predicate"] == "supplies"
    assert len(edges[0]["claim_ids"]) == 2
    assert edges[0]["half_life_days"] == 540   # 'suppl' keyword on the canon
