"""Discovery back half (build spec v3 §3): attribute_joins -> funnel ->
hypothesize -> investigate -> verify -> wake over SQL fixture data, with
graph.llm.complete_json monkeypatched. Database fixtures (database, con)
live in conftest.py."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from graph import db, llm
from graph.discovery import (attribute_joins, funnel, hypothesize,
                             investigate, verify, wake)
from graph.pipeline import materialize


def _mk_entity(con, name, ticker=None):
    return con.execute(
        "insert into entity (kind, canonical_name, registry_refs) "
        "values ('company', %s, %s) returning entity_id",
        (name, Jsonb({"ticker": ticker} if ticker else {}))
    ).fetchone()["entity_id"]


def _mk_event(con, source_name, artifact_uri="file:///dev/null"):
    source_id = db.get_or_create_source(con, source_name, "rss")
    event_id = con.execute(
        "insert into event (source_id, connector, content_hash, artifact_uri, "
        "mime, status) values (%s, 'rss', %s, %s, 'text/plain', 'extracted') "
        "returning event_id",
        (source_id, os.urandom(32), artifact_uri)).fetchone()["event_id"]
    lineage_id = con.execute(
        "insert into lineage (root_event_id) values (%s) returning lineage_id",
        (event_id,)).fetchone()["lineage_id"]
    con.execute("update event set lineage_id=%s where event_id=%s",
                (lineage_id, event_id))
    return event_id, lineage_id


def _mk_claim(con, event_id, lineage_id, subject, predicate, obj=None,
              object_surface=None, quote="", observed_at=None):
    return con.execute(
        "insert into claim (subject_entity, subject_surface, predicate_raw, "
        "object_entity, object_surface, event_id, lineage_id, observed_at, "
        "confidence, extractor, status, evidence_quote) values "
        "(%s, 'S', %s, %s, %s, %s, %s, coalesce(%s, now()), 0.9, 'test-v1', "
        "'asserted', %s) returning claim_id",
        (subject, predicate, obj, object_surface, event_id, lineage_id,
         observed_at, quote)).fetchone()["claim_id"]


def _mk_hypothesis(con, subjects, state="triaged", via=None, history=(),
                   evidence=()):
    return con.execute(
        "insert into hypothesis (type, subjects, statement, rationale, "
        "test_plan, origin, budget, state, evidence, history) values "
        "('shared_dependency', %s, %s, 'both depend on the same supplier', "
        "%s, %s, %s, %s, %s, %s) returning hypothesis_id",
        (list(subjects),
         Jsonb({"template": "shared dependency",
                "via_entity": str(via) if via else None,
                "text": "A and B depend on the same supplier."}),
         Jsonb({"confirm": ["independent filing"],
                "refute": ["generic commentary"]}),
         Jsonb({"strategy": "attribute_join_v0"}),
         Jsonb({"tokens": 0, "tool_calls": 8}),
         state, list(evidence), Jsonb(list(history)))).fetchone()["hypothesis_id"]


def _hyp(con, hid):
    return con.execute("select * from hypothesis where hypothesis_id=%s",
                       (hid,)).fetchone()


def _mock_llm(monkeypatch, responses):
    """Sequenced responses; returns the list of prompts sent."""
    remaining = list(responses)
    calls = []

    def fake(prompt, model, **kw):
        calls.append(prompt)
        assert remaining, "unexpected LLM call"
        return remaining.pop(0)

    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


def _unavailable(*a, **k):
    raise llm.EngineUnavailable("all seats latched")


# ------------------------------------------------------------ full funnel


def test_full_funnel_promotes_inferred_edge(con, tmp_path, monkeypatch):
    a = _mk_entity(con, "Alpha Devices", ticker="NVDA")
    b = _mk_entity(con, "Beta Systems", ticker="AMD")
    via = _mk_entity(con, "Gamma Materials")
    con.execute("insert into watchlist (ticker, active) values "
                "('NVDA', true), ('AMD', true) "
                "on conflict (ticker) do update set active=true")

    quote_a = "Alpha sources key electrolyte materials from Gamma."
    doc = tmp_path / "alpha-note.txt"
    doc.write_text("pad " * 300 + quote_a + " tail" * 300)
    ev_a, lin_a = _mk_event(con, "feed-a", artifact_uri=str(doc))
    ev_b, lin_b = _mk_event(con, "feed-b")
    claim_a = _mk_claim(con, ev_a, lin_a, a, "supplied_by", via, quote=quote_a)
    claim_b = _mk_claim(con, ev_b, lin_b, b, "supplied_by", via,
                        quote="Beta relies on Gamma for materials.")

    assert attribute_joins.run(con)["hypotheses"] == 1
    hid = con.execute("select hypothesis_id from hypothesis"
                      ).fetchone()["hypothesis_id"]

    # funnel: novelty 1.0 (no co-mentions) * materiality 1.0 (both watched)
    # * prior 0.6 (attribute_join_v0)
    assert funnel.run(con) == {"scored": 1, "triaged": 1}
    h = _hyp(con, hid)
    assert h["score"] == pytest.approx(0.6)
    assert h["state"] == "triaged"
    assert h["history"][0]["from"] == "generated"
    assert h["history"][0]["to"] == "triaged"
    assert h["history"][0]["at"]

    # hypothesize: statement gains text, template fields kept, budget capped
    calls = _mock_llm(monkeypatch, [{
        "statement": "Alpha Devices and Beta Systems both depend on "
                     "Gamma Materials.",
        "type": "shared_dependency",
        "rationale": "Both carry supply claims naming Gamma.",
        "test_plan": {"confirm": ["an independent filing naming Gamma"],
                      "refute": ["only generic sector commentary"]},
        "budget": {"tool_calls": 99},
    }])
    assert hypothesize.run(con) == {"refined": 1, "refuted": 0, "paused": False}
    assert "No recorded relationship between" in calls[0]
    assert "Alpha Devices" in calls[0] and "Beta Systems" in calls[0]
    assert str(claim_a) in calls[0]            # evidence claims serialized
    h = _hyp(con, hid)
    assert h["state"] == "triaged"
    assert h["statement"]["template"]          # structured fields kept
    assert h["statement"]["via_entity"] == str(via)
    assert h["statement"]["text"].startswith("Alpha Devices and Beta")
    assert h["test_plan"]["confirm"] == ["an independent filing naming Gamma"]
    assert h["budget"]["tool_calls"] == 8      # capped at config budget
    assert any("refined" in e for e in h["history"])

    # investigate: tool loop with one malformed action and one bogus claim id
    calls = _mock_llm(monkeypatch, [
        {"tool": "claims_about", "entity_id": str(a)},
        {"tool": "fetch_segment", "claim_id": str(claim_a)},
        {"what": "not an action"},
        {"tool": "conclude", "assessment": "supported",
         "evidence": [str(claim_a), str(claim_b), str(uuid.uuid4())],
         "confidence": 0.8, "reasoning": "both supply lines are asserted"},
    ])
    assert investigate.run(con) == {"investigated": 1, "paused": False}
    assert str(claim_a) in calls[1]            # claims_about result fed back
    assert " tail tail" in calls[2]            # segment context fed back
    assert "not a valid action" in calls[3]    # corrective reinjection
    h = _hyp(con, hid)
    assert h["state"] == "investigating"
    assert h["evidence"] == [claim_a, claim_b]   # bogus id dropped silently
    assert set(h["lineages"]) == {lin_a, lin_b}
    assert h["confidence"] == pytest.approx(0.8)
    inv = [e for e in h["history"] if "investigated" in e]
    assert inv and inv[0]["investigated"]["assessment"] == "supported"
    assert inv[0]["investigated"]["turns"] == 4

    # verify: two independent lineages -> promoted inferred edge + alert
    _mock_llm(monkeypatch, [{
        "verdict": "promote",
        "surviving_evidence": [str(claim_a), str(claim_b)],
        "confidence": 0.72,
        "reasoning": "two independent lineages name the same supplier",
    }])
    assert verify.run(con) == {"promoted": 1, "parked": 0, "refuted": 0,
                               "paused": False}
    h = _hyp(con, hid)
    assert h["state"] == "promoted"
    assert h["confidence"] == pytest.approx(0.72)
    edge = con.execute("select * from edge where origin='inferred'").fetchone()
    assert {edge["src"], edge["dst"]} == {a, b}
    assert (edge["src"], edge["dst"]) == (h["subjects"][0], h["subjects"][1])
    assert edge["predicate"] == "shared_dependency"
    assert set(edge["claim_ids"]) == {claim_a, claim_b}
    assert edge["confidence"] == pytest.approx(0.72)
    assert edge["half_life_days"] == 540
    assert edge["last_evidence_at"] is not None
    trail = edge["evidence_trail"]
    assert trail["hypothesis_id"] == str(hid)
    assert trail["verdict"] == "promote"
    assert trail["lineages"] == 2
    assert trail["verifier"] and trail["verified_at"]
    assert "independent lineages" in trail["reasoning"]

    names = {a: "Alpha Devices", b: "Beta Systems"}
    alert = con.execute(
        "select * from alert where kind='promoted_link' and hypothesis_id=%s",
        (hid,)).fetchone()
    first, second = h["subjects"]
    assert alert["title"] == f"New link: {names[first]} and {names[second]}"
    assert alert["body"].startswith("Alpha Devices and Beta")
    assert set(alert["entity_ids"]) == {a, b}

    # the asserted materializer rebuilds its own edges, never inferred ones
    materialize.run(con)
    assert con.execute("select count(*) as n from edge where origin='inferred'"
                       ).fetchone()["n"] == 1


# ------------------------------------------------------------ park + wake


def test_single_lineage_parks_with_wake_conditions(con, monkeypatch):
    delta = _mk_entity(con, "Delta Corp")
    eps = _mk_entity(con, "Epsilon Inc")
    zeta = _mk_entity(con, "Zeta Supply")
    ev, lin = _mk_event(con, "feed-single")
    c1 = _mk_claim(con, ev, lin, delta, "supplied_by", zeta,
                   quote="Delta buys from Zeta.")
    c2 = _mk_claim(con, ev, lin, eps, "supplied_by", zeta,
                   quote="Epsilon buys from Zeta.")

    assert attribute_joins.run(con)["hypotheses"] == 1
    funnel.run(con)
    hid = con.execute("select hypothesis_id from hypothesis"
                      ).fetchone()["hypothesis_id"]
    h = _hyp(con, hid)
    assert h["state"] == "triaged"
    # co-mentioned in one shared event: novelty 0.5 * materiality 0.4 * 0.6
    assert h["score"] == pytest.approx(0.12)

    _mock_llm(monkeypatch, [
        {"statement": "Delta Corp and Epsilon Inc both depend on Zeta Supply.",
         "test_plan": {"confirm": ["an independent second source"],
                       "refute": ["generic commentary"]}},
        {"tool": "conclude", "assessment": "supported",
         "evidence": [str(c1), str(c2)], "confidence": 0.7, "reasoning": "r"},
        {"verdict": "promote", "surviving_evidence": [str(c1), str(c2)],
         "confidence": 0.7, "reasoning": "looks supported"},
    ])
    hypothesize.run(con)
    investigate.run(con)
    # one lineage < 2 required: the code gate degrades promote to park
    assert verify.run(con) == {"promoted": 0, "parked": 1, "refuted": 0,
                               "paused": False}
    h = _hyp(con, hid)
    assert h["state"] == "parked"
    assert h["parked_at"] is not None
    assert h["lineages"] == [lin]
    assert (h["wake_conditions"]["entities"]
            == [str(s) for s in h["subjects"]] + [str(zeta)])
    assert any(e.get("note", "").startswith("promote degraded")
               for e in h["history"])
    assert con.execute("select 1 from edge where origin='inferred'"
                       ).fetchone() is None
    assert con.execute("select 1 from alert where kind='promoted_link'"
                       ).fetchone() is None


def test_wake_revives_parked(con):
    a = _mk_entity(con, "Eta Corp")
    b = _mk_entity(con, "Theta Inc")
    hid = _mk_hypothesis(con, [a, b], state="parked")
    con.execute("update hypothesis set parked_at = now() - interval '2 days', "
                "wake_conditions = %s where hypothesis_id=%s",
                (Jsonb({"entities": [str(a), str(b)]}), hid))
    ev, lin = _mk_event(con, "feed-wake")

    # a claim observed before parking does not wake it
    _mk_claim(con, ev, lin, a, "announces", object_surface="a product",
              observed_at=datetime.now(timezone.utc) - timedelta(days=3))
    assert wake.run(con) == {"woke": 0}
    assert _hyp(con, hid)["state"] == "parked"

    _mk_claim(con, ev, lin, a, "announces", object_surface="a recall")
    assert wake.run(con) == {"woke": 1}
    h = _hyp(con, hid)
    assert h["state"] == "triaged"
    assert any(e.get("note") == "woke" for e in h["history"])
    alert = con.execute("select * from alert where kind='hypothesis_wake' "
                        "and hypothesis_id=%s", (hid,)).fetchone()
    assert alert["title"] == "Hypothesis woke: Eta Corp and Theta Inc"
    assert set(alert["entity_ids"]) == {a, b}


# ------------------------------------------------------------ refuted


def test_refuted_stays_refuted_and_not_regenerated(con, monkeypatch):
    iota = _mk_entity(con, "Iota Corp")
    kappa = _mk_entity(con, "Kappa Inc")
    lam = _mk_entity(con, "Lambda Supply")
    ev, lin = _mk_event(con, "feed-refuted")
    _mk_claim(con, ev, lin, iota, "supplied_by", lam, quote="q1")
    _mk_claim(con, ev, lin, kappa, "supplied_by", lam, quote="q2")
    assert attribute_joins.run(con)["hypotheses"] == 1
    funnel.run(con)
    hid = con.execute("select hypothesis_id from hypothesis"
                      ).fetchone()["hypothesis_id"]

    # an empty confirm list is no falsifiable test plan: refuted on the spot
    _mock_llm(monkeypatch, [{"statement": "s",
                             "test_plan": {"confirm": [], "refute": ["y"]}}])
    assert hypothesize.run(con) == {"refined": 0, "refuted": 1, "paused": False}
    h = _hyp(con, hid)
    assert h["state"] == "refuted"
    assert any(e.get("note") == "no falsifiable test plan"
               for e in h["history"])

    # negative memory: candidate generation sees the refuted row, skips it
    assert attribute_joins.run(con)["hypotheses"] == 0
    assert con.execute("select count(*) as n from hypothesis"
                       ).fetchone()["n"] == 1

    # and no later stage picks it back up (any LLM call would fail loudly)
    def boom(*a, **k):
        raise AssertionError("no LLM call expected")

    monkeypatch.setattr(llm, "complete_json", boom)
    funnel.run(con)
    hypothesize.run(con)
    investigate.run(con)
    verify.run(con)
    wake.run(con)
    assert _hyp(con, hid)["state"] == "refuted"


# ------------------------------------------------------------ engine outage


def test_hypothesize_pause_leaves_state(con, monkeypatch):
    a = _mk_entity(con, "Mu Corp")
    b = _mk_entity(con, "Nu Inc")
    hid = _mk_hypothesis(con, [a, b], state="triaged")
    before = _hyp(con, hid)
    monkeypatch.setattr(llm, "complete_json", _unavailable)
    assert hypothesize.run(con) == {"refined": 0, "refuted": 0, "paused": True}
    assert _hyp(con, hid) == before


def test_investigate_pause_leaves_state(con, monkeypatch):
    a = _mk_entity(con, "Xi Corp")
    b = _mk_entity(con, "Omicron Inc")
    hid = _mk_hypothesis(con, [a, b], state="triaged",
                         history=[{"at": "2026-08-17T00:00:00+00:00",
                                   "refined": {"model": "m"}}])
    before = _hyp(con, hid)
    monkeypatch.setattr(llm, "complete_json", _unavailable)
    assert investigate.run(con) == {"investigated": 0, "paused": True}
    # the flip to 'investigating' rolled back with the savepoint
    assert _hyp(con, hid) == before


def test_verify_pause_leaves_state(con, monkeypatch):
    a = _mk_entity(con, "Pi Corp")
    b = _mk_entity(con, "Rho Inc")
    ev, lin = _mk_event(con, "feed-pause")
    c = _mk_claim(con, ev, lin, a, "supplied_by", b, quote="q")
    hid = _mk_hypothesis(con, [a, b], state="investigating", evidence=[c],
                         history=[{"at": "2026-08-17T00:00:00+00:00",
                                   "investigated": {"assessment": "supported",
                                                    "turns": 1,
                                                    "reasoning": "r"}}])
    before = _hyp(con, hid)
    monkeypatch.setattr(llm, "complete_json", _unavailable)
    assert verify.run(con) == {"promoted": 0, "parked": 0, "refuted": 0,
                               "paused": True}
    assert _hyp(con, hid) == before
    assert con.execute("select 1 from edge where origin='inferred'"
                       ).fetchone() is None
    assert con.execute("select 1 from alert").fetchone() is None


# ------------------------------------------------------------ fetch_segment


def test_fetch_segment_quote_context_and_fallback(con, tmp_path):
    a = _mk_entity(con, "Sigma Corp")
    doc = tmp_path / "sigma.txt"
    doc.write_text("HEAD " + "z" * 3000)
    ev, lin = _mk_event(con, "feed-seg", artifact_uri=str(doc))
    c = _mk_claim(con, ev, lin, a, "announces", object_surface="thing",
                  quote="a quote that is not in the artifact")
    # quote not found -> first 1200 chars
    seg = investigate._fetch_segment(con, str(c))
    assert seg.startswith("HEAD")
    assert len(seg) == 1200
    # model-supplied garbage never reaches SQL
    assert investigate._fetch_segment(con, "not-a-uuid").startswith("error")
    assert investigate._fetch_segment(con, str(uuid.uuid4())).startswith("error")
