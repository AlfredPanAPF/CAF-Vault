"""Discovery back half (build spec v3 §3): attribute_joins -> funnel ->
hypothesize -> investigate -> verify -> wake over SQL fixture data, with
graph.llm.complete_json monkeypatched. Database fixtures (database, con)
live in conftest.py."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from graph import config, db, llm
from graph.discovery import (attribute_joins, funnel, hypothesize,
                             investigate, verify, wake)
from graph.pipeline import materialize


@pytest.fixture(autouse=True)
def _clean_discovery_state(con):
    """The suite shares one DB and earlier files commit hypotheses, inferred
    edges and alerts; this file's stage runs and existence checks are
    table-wide. Delete leaked discovery state INSIDE this test's transaction
    (FK order: alert references hypothesis) — the con fixture rolls back, so
    other files' committed rows reappear afterwards."""
    con.execute("delete from alert")
    con.execute("delete from edge where origin='inferred'")
    con.execute("delete from hypothesis")


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
    """Sequenced responses; returns the list of prompts sent. An Exception
    instance in the sequence is raised instead of returned; a callable is
    called with the prompt first (side-effect hook) and its return used."""
    remaining = list(responses)
    calls = []

    def fake(prompt, model, **kw):
        calls.append(prompt)
        assert remaining, "unexpected LLM call"
        r = remaining.pop(0)
        if callable(r):
            r = r(prompt)
        if isinstance(r, Exception):
            raise r
        return r

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
    # established sources: never-scored (null) lineages collapse into one and
    # cannot pass the §10.4 established gate, which is covered separately
    con.execute("update source set reliability=0.6 "
                "where name in ('feed-a', 'feed-b') and connector='rss'")
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
    assert hypothesize.run(con) == {"refined": 1, "refuted": 0, "failed": 0,
                                    "paused": False}
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
    assert investigate.run(con) == {"investigated": 1, "failed": 0,
                                    "paused": False}
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
                               "failed": 0, "paused": False}
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
                               "failed": 0, "paused": False}
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
    assert hypothesize.run(con) == {"refined": 0, "refuted": 1, "failed": 0,
                                    "paused": False}
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
    assert hypothesize.run(con) == {"refined": 0, "refuted": 0, "failed": 0,
                                    "paused": True}
    assert _hyp(con, hid) == before


def test_investigate_pause_leaves_state(con, monkeypatch):
    a = _mk_entity(con, "Xi Corp")
    b = _mk_entity(con, "Omicron Inc")
    hid = _mk_hypothesis(con, [a, b], state="triaged",
                         history=[{"at": "2026-08-17T00:00:00+00:00",
                                   "refined": {"model": "m"}}])
    before = _hyp(con, hid)
    monkeypatch.setattr(llm, "complete_json", _unavailable)
    assert investigate.run(con) == {"investigated": 0, "failed": 0,
                                    "paused": True}
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
                               "failed": 0, "paused": True}
    assert _hyp(con, hid) == before
    assert con.execute("select 1 from edge where origin='inferred'"
                       ).fetchone() is None
    assert con.execute("select 1 from alert").fetchone() is None


# ------------------------------------------------------------ verify gates

REFINED = [{"at": "2026-08-17T00:00:00+00:00", "refined": {"model": "m"}}]
INVESTIGATED = [{"at": "2026-08-17T00:00:00+00:00",
                 "investigated": {"assessment": "supported", "turns": 1,
                                  "reasoning": "r"}}]


def _mk_verify_pair(con, monkeypatch, feed_a, feed_b, rel_a, rel_b):
    """Two-lineage investigated hypothesis whose root sources carry the given
    reliability scores (None = never scored), with a 'promote' verdict mocked
    citing both claims."""
    a = _mk_entity(con, f"{feed_a} Corp")
    b = _mk_entity(con, f"{feed_b} Inc")
    ev_a, lin_a = _mk_event(con, feed_a)
    ev_b, lin_b = _mk_event(con, feed_b)
    for feed, rel in ((feed_a, rel_a), (feed_b, rel_b)):
        if rel is not None:
            con.execute("update source set reliability=%s "
                        "where name=%s and connector='rss'", (rel, feed))
    c1 = _mk_claim(con, ev_a, lin_a, a, "supplied_by", b, quote="q1")
    c2 = _mk_claim(con, ev_b, lin_b, b, "supplied_by", a, quote="q2")
    hid = _mk_hypothesis(con, [a, b], state="investigating",
                         evidence=[c1, c2], history=INVESTIGATED)
    _mock_llm(monkeypatch, [{"verdict": "promote",
                             "surviving_evidence": [str(c1), str(c2)],
                             "confidence": 0.8, "reasoning": "r"}])
    return hid


def _assert_parked_with_note(con, hid, note):
    h = _hyp(con, hid)
    assert h["state"] == "parked"
    notes = [e.get("note") for e in h["history"] if "verified" in e]
    assert notes == [note]
    assert con.execute("select 1 from edge where origin='inferred'"
                       ).fetchone() is None
    assert con.execute("select 1 from alert where kind='promoted_link'"
                       ).fetchone() is None
    return h


def test_verify_low_reliability_lineages_collapse_to_one(con, monkeypatch):
    # both lineages below LOW: they collectively count as ONE independent
    # lineage, so the count gate (not the established gate) parks it
    hid = _mk_verify_pair(con, monkeypatch, "vlow-a", "vlow-b", 0.3, 0.3)
    assert verify.run(con) == {"promoted": 0, "parked": 1, "refuted": 0,
                               "failed": 0, "paused": False}
    h = _assert_parked_with_note(
        con, hid, "promote degraded: 1 independent lineage(s), 2 required")
    verified = [e["verified"] for e in h["history"] if "verified" in e]
    assert verified[0]["lineages"] == 1


def test_verify_no_established_lineage_parks(con, monkeypatch):
    # both >= LOW but < ESTABLISHED: two independent lineages, none proven
    hid = _mk_verify_pair(con, monkeypatch, "vmid-a", "vmid-b", 0.45, 0.45)
    assert verify.run(con) == {"promoted": 0, "parked": 1, "refuted": 0,
                               "failed": 0, "paused": False}
    _assert_parked_with_note(
        con, hid, "promote degraded: no lineage from an established source")


def test_verify_unscored_lineages_count_as_one(con, monkeypatch):
    # two never-scored sources (reliability null) must NOT pass as two
    # established lineages (§10.4): unproven collapses into one
    hid = _mk_verify_pair(con, monkeypatch, "vnull-a", "vnull-b", None, None)
    assert verify.run(con) == {"promoted": 0, "parked": 1, "refuted": 0,
                               "failed": 0, "paused": False}
    _assert_parked_with_note(
        con, hid, "promote degraded: 1 independent lineage(s), 2 required")


def test_verify_unscored_plus_low_scored_parks(con, monkeypatch):
    # null + 0.45: two independent lineages, but neither is explicitly
    # ESTABLISHED — null can never satisfy the established gate
    hid = _mk_verify_pair(con, monkeypatch, "vmix-a", "vmix-b", None, 0.45)
    assert verify.run(con) == {"promoted": 0, "parked": 1, "refuted": 0,
                               "failed": 0, "paused": False}
    _assert_parked_with_note(
        con, hid, "promote degraded: no lineage from an established source")


def test_verify_unscored_plus_established_promotes(con, monkeypatch):
    # one explicitly established lineage plus the unproven group (counts as
    # one) = 2 independent lineages -> promote
    hid = _mk_verify_pair(con, monkeypatch, "vest-a", "vest-b", None, 0.6)
    assert verify.run(con) == {"promoted": 1, "parked": 0, "refuted": 0,
                               "failed": 0, "paused": False}
    assert _hyp(con, hid)["state"] == "promoted"
    assert con.execute("select 1 from edge where origin='inferred'"
                       ).fetchone() is not None


# ------------------------------------------------------------ investigate budget


def test_investigate_budget_exhaustion_defaults_insufficient(con, monkeypatch):
    a = _mk_entity(con, "Budget Corp")
    b = _mk_entity(con, "Budget Inc")
    hid = _mk_hypothesis(con, [a, b], state="triaged", history=REFINED)
    _mock_llm(monkeypatch,
              [{"tool": "claims_about", "entity_id": str(a)}] * 8)
    assert investigate.run(con) == {"investigated": 1, "failed": 0,
                                    "paused": False}
    h = _hyp(con, hid)
    assert h["state"] == "investigating"
    assert h["evidence"] == []
    assert h["confidence"] is None
    inv = [e for e in h["history"] if "investigated" in e]
    assert inv and inv[0]["investigated"]["assessment"] == "insufficient"
    assert inv[0]["investigated"]["turns"] == 8


def test_investigate_char_cap_breaks_loop(con, monkeypatch):
    a = _mk_entity(con, "Charcap Corp")
    b = _mk_entity(con, "Charcap Inc")
    hid = _mk_hypothesis(con, [a, b], state="triaged", history=REFINED)
    monkeypatch.setitem(config.DISCOVERY, "budget_tokens", 0)
    _mock_llm(monkeypatch, [{"tool": "claims_about", "entity_id": str(a)}])
    assert investigate.run(con) == {"investigated": 1, "failed": 0,
                                    "paused": False}
    inv = [e for e in _hyp(con, hid)["history"] if "investigated" in e]
    assert inv and inv[0]["investigated"]["assessment"] == "insufficient"
    assert inv[0]["investigated"]["turns"] == 1


# ------------------------------------------------------------ state guards


def test_investigate_skips_row_moved_during_batch(con, monkeypatch):
    a = _mk_entity(con, "Guard Corp")
    b = _mk_entity(con, "Guard Inc")
    h1 = _mk_hypothesis(con, [a, b], state="triaged", history=REFINED)
    h2 = _mk_hypothesis(con, [b, a], state="triaged", history=REFINED)
    con.execute("update hypothesis set score=0.9 where hypothesis_id=%s", (h1,))
    con.execute("update hypothesis set score=0.8 where hypothesis_id=%s", (h2,))

    def reject_h2_mid_batch(prompt):
        # a human rejects h2 while the worker is busy investigating h1
        con.execute("update hypothesis set state='refuted' "
                    "where hypothesis_id=%s", (h2,))
        return {"tool": "conclude", "assessment": "insufficient",
                "evidence": [], "confidence": 0.5, "reasoning": "r"}

    _mock_llm(monkeypatch, [reject_h2_mid_batch])
    # h2's guarded flip matches nothing: no flip back, no LLM budget burned
    # (the mock would fail loudly on a second call)
    assert investigate.run(con) == {"investigated": 1, "failed": 0,
                                    "paused": False}
    assert _hyp(con, h1)["state"] == "investigating"
    assert _hyp(con, h2)["state"] == "refuted"


# ------------------------------------------------------------ item failure


def test_investigate_item_failure_is_isolated_then_latches(con, monkeypatch):
    a = _mk_entity(con, "Isolate Corp")
    b = _mk_entity(con, "Isolate Inc")
    good = _mk_hypothesis(con, [a, b], state="triaged", history=REFINED)
    bad = _mk_hypothesis(con, [b, a], state="triaged", history=REFINED)
    con.execute("update hypothesis set score=0.9 where hypothesis_id=%s",
                (good,))
    con.execute("update hypothesis set score=0.8 where hypothesis_id=%s",
                (bad,))

    # cycle 1: good concludes, bad's model output never parses
    _mock_llm(monkeypatch, [
        {"tool": "conclude", "assessment": "supported", "evidence": [],
         "confidence": 0.6, "reasoning": "ok"},
        ValueError("no valid JSON after 2 attempts"),
    ])
    assert investigate.run(con) == {"investigated": 1, "failed": 1,
                                    "paused": False}
    assert _hyp(con, good)["state"] == "investigating"   # work kept
    bd = _hyp(con, bad)
    assert bd["state"] == "triaged"                      # flip rolled back
    errs = [e for e in bd["history"] if "error" in e]
    assert len(errs) == 1
    assert errs[0]["stage"] == "investigate"

    # cycle 2: only bad is still selectable; the second failure parks it
    _mock_llm(monkeypatch, [ValueError("no valid JSON after 2 attempts")])
    assert investigate.run(con) == {"investigated": 0, "failed": 1,
                                    "paused": False}
    bd = _hyp(con, bad)
    assert bd["state"] == "parked"
    assert sum(1 for e in bd["history"] if "error" in e) == 2

    # cycle 3: the poison item is off the queue — no LLM call at all
    def boom(*a_, **k_):
        raise AssertionError("no LLM call expected")

    monkeypatch.setattr(llm, "complete_json", boom)
    assert investigate.run(con) == {"investigated": 0, "failed": 0,
                                    "paused": False}
    assert _hyp(con, bad)["state"] == "parked"


def test_hypothesize_item_failure_records_error(con, monkeypatch):
    a = _mk_entity(con, "Hypfail Corp")
    b = _mk_entity(con, "Hypfail Inc")
    hid = _mk_hypothesis(con, [a, b], state="triaged")
    _mock_llm(monkeypatch, [ValueError("no valid JSON after 2 attempts")])
    assert hypothesize.run(con) == {"refined": 0, "refuted": 0, "failed": 1,
                                    "paused": False}
    h = _hyp(con, hid)
    assert h["state"] == "triaged"
    errs = [e for e in h["history"] if "error" in e]
    assert len(errs) == 1
    assert errs[0]["stage"] == "hypothesize"


def test_verify_item_failure_records_error(con, monkeypatch):
    a = _mk_entity(con, "Verfail Corp")
    b = _mk_entity(con, "Verfail Inc")
    ev, lin = _mk_event(con, "feed-verfail")
    c = _mk_claim(con, ev, lin, a, "supplied_by", b, quote="q")
    hid = _mk_hypothesis(con, [a, b], state="investigating", evidence=[c],
                         history=INVESTIGATED)
    _mock_llm(monkeypatch, [ValueError("no valid JSON after 2 attempts")])
    assert verify.run(con) == {"promoted": 0, "parked": 0, "refuted": 0,
                               "failed": 1, "paused": False}
    h = _hyp(con, hid)
    assert h["state"] == "investigating"
    errs = [e for e in h["history"] if "error" in e]
    assert len(errs) == 1
    assert errs[0]["stage"] == "verify"
    assert con.execute("select 1 from edge where origin='inferred'"
                       ).fetchone() is None


# ------------------------------------------------------------ wake re-alert


def test_wake_alerts_on_every_park_cycle(con):
    a = _mk_entity(con, "Waketwice Corp")
    b = _mk_entity(con, "Waketwice Inc")
    hid = _mk_hypothesis(con, [a, b], state="parked")
    con.execute("update hypothesis set parked_at = now() - interval '2 days', "
                "wake_conditions = %s where hypothesis_id=%s",
                (Jsonb({"entities": [str(a)]}), hid))
    ev, lin = _mk_event(con, "feed-waketwice")
    _mk_claim(con, ev, lin, a, "announces", object_surface="a recall")
    assert wake.run(con) == {"woke": 1}

    # verify parks it again; a later material claim wakes it a second time
    con.execute("update hypothesis set state='parked', "
                "parked_at = now() - interval '1 hour' "
                "where hypothesis_id=%s", (hid,))
    _mk_claim(con, ev, lin, a, "announces", object_surface="another recall")
    assert wake.run(con) == {"woke": 1}

    # every wake has its own alert row (schema 008), and both fall inside a
    # fresh 24h digest window — the digest's woke list is exactly these rows
    n = con.execute(
        "select count(*) n from alert where kind='hypothesis_wake' "
        "and hypothesis_id=%s and created_at >= now() - interval '24 hours'",
        (hid,)).fetchone()["n"]
    assert n == 2


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
