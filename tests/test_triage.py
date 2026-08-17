"""Fast-path materiality triage tests (build spec v3 §4.1-4.2).
No network, no LLM — the stage is pure heuristics.
Database fixtures (database, con) live in conftest.py.

The suite shares one database and earlier files commit rows (entities,
watchlist tickers, pending events), so fixtures upsert and assertions are
scoped to this file's rows — never global counts.
"""
import os

from psycopg.types.json import Jsonb

from graph import alerts, db
from graph.pipeline import resolve, triage


def _mk_event(con, connector, meta, source_name="triagetest-feed"):
    """Pending event straight into the table (no artifact needed for triage)."""
    source_id = db.get_or_create_source(con, source_name, connector)
    return con.execute(
        "insert into event (source_id, connector, content_hash, artifact_uri, "
        "mime, status, meta) values "
        "(%s, %s, %s, 'file:///dev/null', 'text/plain', 'pending', %s) "
        "returning event_id",
        (source_id, connector, os.urandom(32), Jsonb(meta))).fetchone()["event_id"]


def _triage(con, event_id):
    return con.execute("select triage from event where event_id=%s",
                       (event_id,)).fetchone()["triage"]


def _watch(con, ticker, active=True):
    con.execute(
        "insert into watchlist (ticker, sector, active) values (%s, 'test', %s) "
        "on conflict (ticker) do update set active=excluded.active",
        (ticker, active))


def test_edgar_8k_item_scores_high_and_alerts_on_watchlist(con):
    _watch(con, "NVDA")
    entity_id = resolve.entity_for_sec(con, 1045810, "NVDA", "NVIDIA CORP")
    event_id = _mk_event(con, "edgar", {
        "ticker": "NVDA", "form": "8-K", "items": "2.01,9.01",
        "title": "NVIDIA CORP 8-K filed 2026-08-14", "accession": "0001-tri-1"})

    out = triage.run(con)
    assert out["scored"] >= 1

    t = _triage(con, event_id)
    assert t == {"materiality": 0.9, "route": "fast", "scorer": "heuristic-v1"}

    alert = con.execute("select * from alert where kind='material_event' "
                        "and event_id=%s", (event_id,)).fetchone()
    assert alert is not None
    assert alert["title"] == "NVDA: 8-K"
    assert alert["body"] == "NVIDIA CORP 8-K filed 2026-08-14"
    assert alert["entity_ids"] == [entity_id]

    # unique index (kind, event): a re-emit is silently dropped
    alerts.emit(con, "material_event", "NVDA: 8-K", event_id=event_id)
    n = con.execute("select count(*) as n from alert where kind='material_event' "
                    "and event_id=%s", (event_id,)).fetchone()["n"]
    assert n == 1


def test_fast_event_off_watchlist_does_not_alert(con):
    con.execute("delete from watchlist where ticker='AMD'")
    event_id = _mk_event(con, "edgar", {
        "ticker": "AMD", "form": "8-K", "items": "1.01",
        "title": "ADVANCED MICRO DEVICES INC 8-K filed 2026-08-14"})

    triage.run(con)
    assert _triage(con, event_id)["route"] == "fast"
    assert con.execute("select 1 from alert where event_id=%s",
                       (event_id,)).fetchone() is None


def test_edgar_form_fallback_without_items(con):
    plain_8k = _mk_event(con, "edgar", {"ticker": "NVDA", "form": "8-K",
                                        "title": "NVIDIA CORP 8-K"})
    other = _mk_event(con, "edgar", {"ticker": "NVDA", "form": "10-K",
                                     "title": "NVIDIA CORP 10-K"})
    officer_item = _mk_event(con, "edgar", {"ticker": "NVDA", "form": "8-K",
                                            "items": "5.02",
                                            "title": "NVIDIA CORP 8-K"})

    triage.run(con)
    assert _triage(con, plain_8k) == {"materiality": 0.5, "route": "slow",
                                      "scorer": "heuristic-v1"}
    assert _triage(con, other)["materiality"] == 0.4
    assert _triage(con, officer_item) == {"materiality": 0.7, "route": "fast",
                                          "scorer": "heuristic-v1"}


def test_rss_title_keywords(con):
    keyword = _mk_event(con, "rss", {
        "feed": "chipsblog", "title": "Hershey nears acquisition of CocoaCo",
        "item_url": "https://example.com/a"})
    dull = _mk_event(con, "rss", {
        "feed": "chipsblog", "title": "Weekly notes on the chip sector",
        "item_url": "https://example.com/b"})

    triage.run(con)
    assert _triage(con, keyword) == {"materiality": 0.7, "route": "fast",
                                     "scorer": "heuristic-v1"}
    assert _triage(con, dull) == {"materiality": 0.3, "route": "slow",
                                  "scorer": "heuristic-v1"}
    # no ticker in rss meta -> fast route alone never alerts
    for event_id in (keyword, dull):
        assert con.execute("select 1 from alert where event_id=%s",
                           (event_id,)).fetchone() is None


def test_rerun_never_rescores(con):
    _watch(con, "NVDA")
    event_id = _mk_event(con, "edgar", {
        "ticker": "NVDA", "form": "8-K", "items": "2.01",
        "title": "NVIDIA CORP 8-K filed 2026-08-14"})

    triage.run(con)
    first = _triage(con, event_id)
    assert first["route"] == "fast"

    # second pass: triage not null filters it out; the alert stays single
    triage.run(con)
    assert _triage(con, event_id) == first
    n = con.execute("select count(*) as n from alert where kind='material_event' "
                    "and event_id=%s", (event_id,)).fetchone()["n"]
    assert n == 1
