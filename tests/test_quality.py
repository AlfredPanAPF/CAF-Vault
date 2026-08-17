"""Quality layer tests (build spec v3 §5): contradiction detection with
same-lineage auto-resolve, source reliability, claim staleness math, simhash
near-dup mirrors, and merge-aware materialize + claim linking.
No network, no real LLM. Database fixtures (database, con) live in conftest.py.
Run isolated:
    CAF_DB_URL=postgresql:///caf_test_quality uv run pytest tests/test_quality.py -q
"""
import os
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from graph import db, envelope, staleness
from graph.pipeline import materialize, quality, resolve


def _mk_event(con, source_name="quality-feed", connector="rss"):
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


def _mk_entity(con, name, refs=None):
    return con.execute(
        "insert into entity (kind, canonical_name, registry_refs) "
        "values ('company', %s, %s) returning entity_id",
        (name, Jsonb(refs) if refs else None)).fetchone()["entity_id"]


def _mk_claim(con, event_id, lineage_id, subject, predicate, obj=None,
              literal=None, observed_at=None, subject_surface="S",
              object_surface=None):
    return con.execute(
        "insert into claim (subject_entity, subject_surface, predicate_raw, "
        "object_entity, object_surface, object_literal, event_id, lineage_id, "
        "observed_at, confidence, extractor, status) values "
        "(%s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()), 0.9, 'test-v1', "
        "'asserted') returning claim_id",
        (subject, subject_surface, predicate, obj, object_surface,
         Jsonb(literal) if literal is not None else None,
         event_id, lineage_id, observed_at)).fetchone()["claim_id"]


# ---------------------------------------------------------------- contradictions


def test_contradiction_object_conflict_alerts_watchlist(con):
    # test-unique ticker/cik: the shared suite DB already holds committed
    # NVDA rows from other files
    subj = _mk_entity(con, "Quality Watch Co", {"cik": "9910001", "ticker": "QQXA"})
    x = _mk_entity(con, "Alice Corp")
    y = _mk_entity(con, "Bob Corp")
    con.execute("insert into watchlist (ticker, active, added_by) "
                "values ('QQXA', true, 'test') "
                "on conflict (ticker) do update set active=true")
    e1, l1 = _mk_event(con, "feed-a")
    e2, l2 = _mk_event(con, "feed-b")
    _mk_claim(con, e1, l1, subj, "appointed_ceo", obj=x)
    _mk_claim(con, e2, l2, subj, "appointed_ceo", obj=y)

    out = quality.contradictions(con)
    assert out["object"] >= 1
    row = con.execute("select * from contradiction where subject_entity=%s",
                      (subj,)).fetchone()
    assert row["kind"] == "object_conflict"
    assert row["status"] == "open"
    assert row["predicate_canon"] == "appointed_ceo"
    assert str(row["claim_a"]) < str(row["claim_b"])   # ordered pair

    alert = con.execute("select * from alert where kind='contradiction' "
                        "and %s = any(entity_ids)", (subj,)).fetchone()
    assert alert["title"] == "Conflicting claims on Quality Watch Co"
    assert alert["entity_ids"] == [subj]

    # re-run: pair already recorded -> nothing new, no duplicate alert
    quality.contradictions(con)
    assert con.execute("select count(*) n from contradiction "
                       "where subject_entity=%s", (subj,)).fetchone()["n"] == 1
    assert con.execute("select count(*) n from alert where kind='contradiction' "
                       "and %s = any(entity_ids)", (subj,)).fetchone()["n"] == 1


def test_contradiction_literal_conflict(con):
    subj = _mk_entity(con, "Widget Inc")
    e1, l1 = _mk_event(con, "feed-a")
    e2, l2 = _mk_event(con, "feed-b")
    usd = {"currency": "USD", "unit": None, "as_of": "2026-06-30"}
    # >5% relative difference, same period + currency -> conflict
    _mk_claim(con, e1, l1, subj, "reported_revenue", literal={**usd, "value": 100})
    _mk_claim(con, e2, l2, subj, "reported_revenue", literal={**usd, "value": 110})
    # 3% difference -> no conflict
    _mk_claim(con, e1, l1, subj, "reported_profit", literal={**usd, "value": 100})
    _mk_claim(con, e2, l2, subj, "reported_profit", literal={**usd, "value": 103})
    # different currency -> not comparable
    _mk_claim(con, e1, l1, subj, "reported_ebitda", literal={**usd, "value": 100})
    _mk_claim(con, e2, l2, subj, "reported_ebitda",
              literal={**usd, "currency": "EUR", "value": 200})
    # different fiscal period -> not comparable
    _mk_claim(con, e1, l1, subj, "reported_sales", literal={**usd, "value": 100})
    _mk_claim(con, e2, l2, subj, "reported_sales",
              literal={**usd, "as_of": "2025-06-30", "value": 200})

    out = quality.contradictions(con)
    assert out["literal"] >= 1
    rows = con.execute("select * from contradiction where subject_entity=%s",
                       (subj,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "literal_conflict"
    assert rows[0]["predicate_canon"] == "reported_revenue"
    assert rows[0]["status"] == "open"
    # not on the watchlist -> no alert
    assert con.execute("select count(*) n from alert where %s = any(entity_ids)",
                       (subj,)).fetchone()["n"] == 0


def test_contradiction_same_lineage_auto_resolves(con):
    subj = _mk_entity(con, "Quality Cfo Co", {"cik": "9910002", "ticker": "QQXB"})
    x = _mk_entity(con, "Carol LLC")
    y = _mk_entity(con, "Dave LLC")
    con.execute("insert into watchlist (ticker, active, added_by) "
                "values ('QQXB', true, 'test') "
                "on conflict (ticker) do update set active=true")
    event_id, lineage_id = _mk_event(con)
    now = datetime.now(timezone.utc)
    older = _mk_claim(con, event_id, lineage_id, subj, "appointed_cfo", obj=x,
                      observed_at=now - timedelta(days=1))
    newer = _mk_claim(con, event_id, lineage_id, subj, "appointed_cfo", obj=y,
                      observed_at=now)

    out = quality.contradictions(con)
    assert out["auto_resolved"] >= 1
    row = con.execute("select * from contradiction where subject_entity=%s",
                      (subj,)).fetchone()
    assert row["status"] == "auto_resolved"
    assert row["resolution"] == {"kept": str(newer), "mode": "same_lineage"}
    assert row["resolved_at"] is not None
    old = con.execute("select status, superseded_by from claim where claim_id=%s",
                      (older,)).fetchone()
    assert old["status"] == "superseded"
    assert old["superseded_by"] == newer
    # auto-resolved conflicts never alert, watchlist or not
    assert con.execute("select count(*) n from alert where kind='contradiction' "
                       "and %s = any(entity_ids)", (subj,)).fetchone()["n"] == 0


def test_same_lineage_pair_from_merge_stays_open(con):
    """A wrong merge makes two different companies' claims share a subject;
    the same-lineage auto-resolve must NOT supersede across that merge —
    the pair stays open for review and unmerge fully heals (design §15)."""
    corp = _mk_entity(con, "Acme Corp Q")
    inds = _mk_entity(con, "Acme Industries Q")
    event_id, lineage_id = _mk_event(con, "feed-mergegate")
    con.execute(
        "insert into mention (event_id, surface, resolved_entity, resolver) "
        "values (%s, 'Acme Corp Q', %s, 'test-v1'), "
        "(%s, 'Acme Industries Q', %s, 'test-v1')",
        (event_id, corp, event_id, inds))
    usd = {"currency": "USD", "unit": "B", "as_of": "2026-06-30"}
    c1 = _mk_claim(con, event_id, lineage_id, corp, "reported_revenue",
                   literal={**usd, "value": 3.0}, subject_surface="Acme Corp Q")
    c2 = _mk_claim(con, event_id, lineage_id, inds, "reported_revenue",
                   literal={**usd, "value": 1.2},
                   subject_surface="Acme Industries Q")

    # reviewer wrongly merges Industries into Corp; link re-points the claim
    con.execute("insert into entity_same_as (a, b, decided_by) values "
                "(%s, %s, 'test')", (inds, corp))
    resolve.link_claims(con)
    assert con.execute("select subject_entity from claim where claim_id=%s",
                       (c2,)).fetchone()["subject_entity"] == corp

    quality.contradictions(con)
    row = con.execute(
        "select * from contradiction where claim_a in (%s, %s)",
        (c1, c2)).fetchone()
    assert row is not None
    assert row["status"] == "open"           # not silently absorbed
    for cid in (c1, c2):
        assert con.execute("select status from claim where claim_id=%s",
                           (cid,)).fetchone()["status"] == "asserted"


def test_same_lineage_auto_resolve_still_works_with_mentions(con):
    """The merge gate must not break legitimate same-lineage supersession:
    same raw identity on both sides -> newer supersedes older."""
    corp = _mk_entity(con, "Restate Corp Q")
    event_id, lineage_id = _mk_event(con, "feed-restate")
    con.execute(
        "insert into mention (event_id, surface, resolved_entity, resolver) "
        "values (%s, 'Restate Corp Q', %s, 'test-v1')", (event_id, corp))
    usd = {"currency": "USD", "unit": "B", "as_of": "2026-06-30"}
    now = datetime.now(timezone.utc)
    older = _mk_claim(con, event_id, lineage_id, corp, "reported_revenue",
                      literal={**usd, "value": 3.0},
                      subject_surface="Restate Corp Q",
                      observed_at=now - timedelta(days=1))
    newer = _mk_claim(con, event_id, lineage_id, corp, "reported_revenue",
                      literal={**usd, "value": 3.4},
                      subject_surface="Restate Corp Q", observed_at=now)

    out = quality.contradictions(con)
    assert out["auto_resolved"] >= 1
    old = con.execute("select status, superseded_by from claim "
                      "where claim_id=%s", (older,)).fetchone()
    assert old["status"] == "superseded"
    assert old["superseded_by"] == newer


# ---------------------------------------------------------------- reliability


def test_reliability_scores_sources(con):
    a = _mk_entity(con, "Acme")
    x = _mk_entity(con, "Xylo")
    y = _mk_entity(con, "Yonder")
    e1, l1 = _mk_event(con, "big-feed")
    e2, l2 = _mk_event(con, "small-feed")

    # 4 asserted claims corroborated by the second lineage, 2 superseded
    for _ in range(4):
        _mk_claim(con, e1, l1, a, "supplies", obj=x)
    for _ in range(2):
        cid = _mk_claim(con, e1, l1, a, "buys_from", obj=y)
        con.execute("update claim set status='superseded' where claim_id=%s",
                    (cid,))
    _mk_claim(con, e2, l2, a, "supplies", obj=x)

    out = quality.reliability(con)
    assert out["sources"] >= 1   # big-feed scored; small-feed has < 5 claims

    big = con.execute("select reliability, reliability_detail from source "
                      "where name='big-feed'").fetchone()
    expected = 0.5 + 0.3 * (4 / 6) - 0.4 * (2 / 6)
    assert abs(big["reliability"] - expected) < 1e-5
    detail = big["reliability_detail"]
    assert detail["n_claims"] == 6
    assert abs(detail["corroborated_share"] - 4 / 6) < 1e-2
    assert abs(detail["superseded_share"] - 2 / 6) < 1e-2
    assert detail["computed_at"]

    small = con.execute("select reliability from source where name='small-feed'"
                        ).fetchone()
    assert small["reliability"] is None


# ---------------------------------------------------------------- staleness


def test_staleness_math():
    assert staleness.half_life_for("reported_revenue") == 365
    assert staleness.half_life_for("incorporated_in") is None
    assert staleness.half_life_for("mystery_predicate") == 540

    observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # one half-life on a 365-day predicate halves the confidence
    assert staleness.confidence_now(
        0.8, observed, "reported_revenue",
        at=observed + timedelta(days=365)) == 0.4
    # structural predicates never decay
    assert staleness.confidence_now(
        0.8, observed, "incorporated_in",
        at=observed + timedelta(days=3650)) == 0.8


# ---------------------------------------------------------------- near-dup


LONG_TEXT = (
    "Chipmaker Nvidia said on Tuesday it will supply its newest accelerator "
    "boards to Advanced Micro Devices under a multi year agreement covering "
    "data center deployments in North America and Europe. The companies said "
    "the arrangement includes joint validation work, priority allocation of "
    "high bandwidth memory, and an option to extend the agreement for two "
    "further years if volume targets are met. Analysts called the deal a "
    "notable shift in the competitive landscape for artificial intelligence "
    "hardware and said rivals would face pressure to respond quickly. "
    "The agreement follows months of negotiation between the two "
    "companies and is expected to close before the end of the current "
    "fiscal year, pending customary regulatory approvals in both the "
    "United States and the European Union. Executives from both firms "
    "described the partnership as complementary rather than exclusive, "
    "noting that existing supply commitments to other customers remain "
    "in place and unchanged for the duration of the contract period.")
NEAR_TEXT = LONG_TEXT.replace("Tuesday", "Wednesday")
OTHER_TEXT = (
    "Cocoa futures fell sharply after West African harvest reports showed "
    "better than expected pod counts, easing supply fears that had driven "
    "chocolate makers to hedge aggressively through the spring season. "
    "Traders said the market remains volatile heading into the grind data.")


def test_near_duplicate_becomes_mirror(con):
    source_id = db.get_or_create_source(con, "neardup-feed", "rss")

    e1, new1 = envelope.ingest(con, source_id, "rss", LONG_TEXT.encode(),
                               "text/plain", ".txt")
    assert new1 is True
    row1 = con.execute("select * from event where event_id=%s", (e1,)).fetchone()
    assert row1["simhash"] is not None
    assert row1["status"] == "pending"

    # a few words changed -> mirror on the original's lineage, never extracted
    e2, new2 = envelope.ingest(con, source_id, "rss", NEAR_TEXT.encode(),
                               "text/plain", ".txt")
    assert new2 is False
    row2 = con.execute("select * from event where event_id=%s", (e2,)).fetchone()
    assert row2["status"] == "duplicate"
    assert row2["lineage_id"] == row1["lineage_id"]
    assert row2["meta"]["near_duplicate_of"] == str(e1)
    assert row2["simhash"] is not None          # its own simhash, stored
    assert row2["content_hash"] != row1["content_hash"]

    # unrelated text does not collapse
    e3, new3 = envelope.ingest(con, source_id, "rss", OTHER_TEXT.encode(),
                               "text/plain", ".txt")
    assert new3 is True
    row3 = con.execute("select * from event where event_id=%s", (e3,)).fetchone()
    assert row3["lineage_id"] != row1["lineage_id"]
    assert row3["status"] == "pending"

    # exact re-ingest of the near-dup content hits the exact-hash path first
    e4, new4 = envelope.ingest(con, source_id, "rss", NEAR_TEXT.encode(),
                               "text/plain", ".txt")
    assert new4 is False
    row4 = con.execute("select * from event where event_id=%s", (e4,)).fetchone()
    assert row4["lineage_id"] == row1["lineage_id"]
    assert "near_duplicate_of" not in (row4["meta"] or {})
    assert row4["simhash"] == row2["simhash"]   # copied from the mirror


def test_simhash_signed_and_stable():
    h = envelope.simhash64(LONG_TEXT)
    assert h == envelope.simhash64(LONG_TEXT)           # process-stable
    assert -(1 << 63) <= h < (1 << 63)                  # fits a bigint
    assert envelope.simhash64("") == 0


# ---------------------------------------------------------------- merges


def test_materialize_through_merge_and_revert(con):
    a = _mk_entity(con, "Merged Away Co")
    b = _mk_entity(con, "Canonical Co")
    c = _mk_entity(con, "Counterparty Co")
    event_id, lineage_id = _mk_event(con)
    _mk_claim(con, event_id, lineage_id, a, "supplies", obj=c)

    def edges():
        # scoped to this test's counterparty: the rebuild sweeps committed
        # claims from other suite files too
        return [(e["src"], e["dst"]) for e in con.execute(
            "select src, dst from edge where origin='asserted' and dst=%s",
            (c,)).fetchall()]

    materialize.run(con)
    assert edges() == [(a, c)]

    con.execute("insert into entity_same_as (a, b, decided_by) values "
                "(%s, %s, 'test')", (a, b))
    materialize.run(con)
    assert edges() == [(b, c)]

    con.execute("update entity_same_as set status='reverted' where a=%s", (a,))
    materialize.run(con)
    assert edges() == [(a, c)]


def test_link_claims_maps_through_canonical(con):
    a = _mk_entity(con, "Acme Robotics Inc")
    b = _mk_entity(con, "Acme Holdings")
    event_id, lineage_id = _mk_event(con)
    con.execute(
        "insert into mention (event_id, surface, resolved_entity, resolver, "
        "confidence) values (%s, 'Acme Robotics', %s, 'blocking-v1:name_exact', "
        "0.95)", (event_id, a))
    subj_claim = _mk_claim(con, event_id, lineage_id, None, "supplies",
                           subject_surface="Acme Robotics",
                           object_surface="Someone Else")
    obj_claim = _mk_claim(con, event_id, lineage_id, None, "buys_from",
                          subject_surface="Someone Else",
                          object_surface="Acme Robotics")
    con.execute("insert into entity_same_as (a, b, decided_by) values "
                "(%s, %s, 'test')", (a, b))

    assert resolve.link_claims(con) >= 2
    assert con.execute("select subject_entity from claim where claim_id=%s",
                       (subj_claim,)).fetchone()["subject_entity"] == b
    assert con.execute("select object_entity from claim where claim_id=%s",
                       (obj_claim,)).fetchone()["object_entity"] == b
    # the mention keeps the raw resolution for audit
    assert con.execute("select resolved_entity from mention where event_id=%s",
                       (event_id,)).fetchone()["resolved_entity"] == a


def test_ceo_conflict_on_unresolved_person_surfaces(con):
    """Persons never resolve in v0, so single-valued conflicts must fall back
    to normalized object surfaces; mixed entity/surface pairs stay silent."""
    subj = _mk_entity(con, "Person Conflict Co")
    real = _mk_entity(con, "Real Object Co")
    e1, l1 = _mk_event(con, "feed-a")
    e2, l2 = _mk_event(con, "feed-b")
    # same person, case difference only -> no conflict
    _mk_claim(con, e1, l1, subj, "appointed_ceo", object_surface="Maria Voss")
    _mk_claim(con, e2, l2, subj, "appointed_ceo", object_surface="maria voss")
    # two different people -> conflict
    _mk_claim(con, e1, l1, subj, "appointed_cfo", object_surface="Alice Ang")
    _mk_claim(con, e2, l2, subj, "appointed_cfo", object_surface="Bob Beam")
    # resolved entity vs bare surface -> cannot compare, silent
    _mk_claim(con, e1, l1, subj, "headquartered_in", obj=real)
    _mk_claim(con, e2, l2, subj, "headquartered_in", object_surface="Elsewhere")

    quality.contradictions(con)
    rows = con.execute("select * from contradiction where subject_entity=%s",
                       (subj,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "object_conflict"
    assert rows[0]["predicate_canon"] == "appointed_cfo"


def test_attribute_join_matches_sourcing_predicates(con):
    """sources_from / depends_on are dependency phrasings the join must see."""
    from graph.discovery import attribute_joins
    a = _mk_entity(con, "Sourcer Alpha Co")
    b = _mk_entity(con, "Sourcer Beta Co")
    via = _mk_entity(con, "Shared Niche Supplier")
    e1, l1 = _mk_event(con, "feed-a")
    e2, l2 = _mk_event(con, "feed-b")
    _mk_claim(con, e1, l1, a, "sources_from", obj=via)
    _mk_claim(con, e2, l2, b, "depends_on", obj=via)

    attribute_joins.run(con)
    row = con.execute(
        "select 1 from hypothesis where type='shared_dependency' "
        "and subjects @> array[%s, %s]::uuid[]", (a, b)).fetchone()
    assert row is not None
