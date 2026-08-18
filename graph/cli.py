"""CLI entry point (`graph` per pyproject). Each subcommand opens one
connection, calls the library, commits, and prints the returned dict —
library functions never commit."""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from . import config, credentials, db, er_norm, llm, router, watchlist
from .connectors import bridge, edgar, links, manual, podcast, rss, x, youtube
from .discovery import attribute_joins, funnel, hypothesize, investigate, verify, wake
from .pipeline import (adjudicate, extract, gardener, materialize, quality,
                       resolve, triage)


def cmd_migrate(args):
    db.migrate()


def migrate_with_retry(window_s=60, pause_s=5):
    """Migrate, retrying while the database comes up.

    After a host reboot Docker's restart policy starts containers in arbitrary
    order and the embedded DNS can lag; without this, serve/loop crash-loop on
    'failed to resolve host postgres' until backoff catches up.
    """
    import time
    deadline = time.time() + window_s
    while True:
        try:
            db.migrate()
            return
        except Exception as e:
            if time.time() >= deadline:
                raise
            print(f"db not ready ({e}); retrying in {pause_s}s")
            time.sleep(pause_s)


def seed(con, sources=False):
    """Load registry_sec + alias_seed from the ref files (reference data,
    always). With sources=True, also upsert the demo watchlist and podcast
    feeds from the spike — dev convenience only; production sources are added
    through the admin page, never preloaded. No commit (CLI commits)."""
    data = json.loads(config.SEC_TICKERS.read_text())
    registrants = {}
    for v in data.values():
        registrants.setdefault(int(v["cik_str"]), (v["ticker"], v["title"]))
    con.execute("truncate registry_sec")
    with con.cursor() as cur:
        cur.executemany(
            "insert into registry_sec (cik, ticker, title) values (%s,%s,%s)",
            [(cik, t, title) for cik, (t, title) in registrants.items()])

    con.execute("truncate alias_seed")
    seeded = 0
    for k, v in json.loads(config.ALIASES.read_text()).items():
        if k.startswith("_"):
            continue
        cur = con.execute(
            "insert into alias_seed (alias_norm, ticker) values (%s,%s) "
            "on conflict (alias_norm) do nothing", (er_norm.norm(k), v.upper()))
        seeded += cur.rowcount

    out = {"registry_sec": len(registrants), "alias_seed": seeded}
    if not sources:
        return out

    # watchlist: upsert from the ref file; never touch an existing row's active flag
    watch = 0
    for sector, tickers in json.loads(config.WATCHLIST.read_text()).items():
        for ticker in tickers:
            # dev seeding is offline: sector comes from the ref file and the
            # row is marked resolver='seed' (build spec v4 §2). A row a real
            # resolve already touched keeps its provenance.
            con.execute(
                "insert into watchlist (ticker, sector, added_by, resolver) "
                "values (%s,%s,'seed','seed') on conflict (ticker) do update "
                "set sector=excluded.sector, "
                "resolver=coalesce(watchlist.resolver, 'seed')",
                (ticker, sector))
            watch += 1

    # podcast feeds become source rows; poll() reads these, not podcast.FEEDS
    for feed, url in podcast.FEEDS.items():
        name = f"podcast:{feed}"
        row = con.execute("select source_id from source where name=%s "
                          "and connector='podcast'", (name,)).fetchone()
        if row:
            con.execute("update source set url=%s where source_id=%s",
                        (url, row["source_id"]))
        else:
            con.execute(
                "insert into source (name, connector, url, status, added_by) "
                "values (%s,'podcast',%s,'active','seed')", (name, url))
    out.update({"watchlist": watch, "podcast_sources": len(podcast.FEEDS)})
    return out


def cmd_seed(args):
    with db.connect() as con:
        out = seed(con, sources=args.sources)
        con.commit()
        print(out)


def cmd_ingest_edgar(args):
    with db.connect() as con:
        out = edgar.poll(con, tickers=args.tickers or None,
                         filings_per_company=args.per_company)
        con.commit()
        print(out)


def cmd_ingest_podcasts(args):
    with db.connect() as con:
        out = podcast.poll(con, episodes_per_feed=args.episodes)
        con.commit()
        print(out)


def cmd_add_source(args):
    """Same path as POST /api/sources: the router decides what the URL is
    (build spec v4 §4, §7)."""
    with db.connect() as con:
        res = router.resolve(con, args.url)
        if res.kind == "unsupported":
            print(f"error: {res.message}")
            return
        existing = router.duplicate_of(con, res)
        if existing:
            print(f"error: already added as {existing}")
            return
        if res.one_off:
            # a pasted article, post or video is fetched once, not polled
            row = links.enqueue(con, res.url, res.link_kind, res.site, "cli")
            if res.link_kind != "youtube_video":
                row = links.process_one(con, row["link_id"])
            con.commit()
            print({"link_id": str(row["link_id"]), "kind": row["kind"],
                   "status": row["status"], "error": row["error"]})
            return
        name = (args.name or "").strip() or res.name or res.url
        if res.connector == "podcast" and not name.startswith("podcast:"):
            name = f"podcast:{name}"
        row = con.execute(
            "insert into source (name, connector, url, config, status, added_by) "
            "values (%s,%s,%s,%s,'active','cli') returning source_id",
            (name, res.connector, res.feed_url or res.url,
             Jsonb(res.config or {}))).fetchone()
        con.commit()
        print({"source_id": str(row["source_id"]), "name": name,
               "connector": res.connector, "label": res.label})


def cmd_add_ticker(args):
    """Same path as POST /api/watchlist: Yahoo Finance first, SEC registry
    second (build spec v4 §2)."""
    with db.connect() as con:
        row = watchlist.resolve_and_upsert(con, args.ticker, "cli", args.sector)
        if row is None:
            ticker = watchlist.clean_ticker(args.ticker)
            print(f"error: unknown ticker {ticker} — Yahoo Finance and the "
                  f"SEC registry do not list it")
            return
        con.commit()
        print({"ticker": row["ticker"], "name": row["name"],
               "sector": row["sector"], "exchange": row["exchange"],
               "resolver": row["resolver"]})


def cmd_set_credential(args):
    """Store a cookies.txt export or a bearer token for a premium site
    (build spec v4 §3.1, §7)."""
    if not args.file and args.value is None:
        print("error: pass --file cookies.txt or --value TOKEN")
        return
    value = Path(args.file).read_text() if args.file else args.value
    with db.connect() as con:
        try:
            credentials.set(con, args.site, value, args.note)
        except credentials.InvalidCredential as e:
            print(f"error: {e}")
            return
        requeued = links.requeue_blocked(con, args.site)
        con.execute("update source set last_error=null, last_error_at=null "
                    "where config->>'site' = %s", (args.site,))
        con.commit()
        print({"site": args.site, "set": True, "requeued": requeued})


def cmd_ingest_file(args):
    with db.connect() as con:
        event_id = manual.ingest_file(con, args.path)
        con.commit()
        print({"event_id": str(event_id)} if event_id else {"duplicate": True})


def cmd_extract(args):
    with db.connect() as con:
        out = extract.run(con, limit=args.limit)
        con.commit()
        print(out)


def cmd_resolve(args):
    with db.connect() as con:
        out = resolve.run(con)
        con.commit()
        print(out)


def cmd_adjudicate(args):
    with db.connect() as con:
        out = adjudicate.run(con, limit=args.limit)
        con.commit()
        print(out)


def cmd_materialize(args):
    with db.connect() as con:
        out = materialize.run(con)
        con.commit()
        print(out)


def cmd_discover(args):
    with db.connect() as con:
        out = attribute_joins.run(con)
        con.commit()
        print(out)


def _cmd_simple(fn):
    """Subcommand wrapper for stages with a bare run(con)/callable(con)."""
    def cmd(args):
        with db.connect() as con:
            out = fn(con)
            con.commit()
            print(out)
    return cmd


def cmd_quality(args):
    with db.connect() as con:
        out = {"contradictions": quality.contradictions(con)}
        con.commit()
        out["reliability"] = quality.reliability(con)
        con.commit()
        print(out)


def cmd_run(args):
    with db.connect() as con:
        stages = [
            ("edgar", lambda: edgar.poll(con)),
            ("rss", lambda: rss.poll(con)),
            ("youtube", lambda: youtube.poll(con)),
            ("x", lambda: x.poll(con)),
            ("bridge", lambda: bridge.poll(con)),
            ("links", lambda: links.process(con)),
            ("triage", lambda: triage.run(con)),
            ("extract", lambda: extract.run(con, limit=args.limit)),
            ("resolve", lambda: resolve.run(con)),
            ("adjudicate", lambda: adjudicate.run(con)),
            ("garden", lambda: gardener.run(con)),
            ("materialize", lambda: materialize.run(con)),
            ("contradictions", lambda: quality.contradictions(con)),
            ("reliability", lambda: quality.reliability(con)),
            ("discover", lambda: attribute_joins.run(con)),
            ("funnel", lambda: funnel.run(con)),
            ("hypothesize", lambda: hypothesize.run(con)),
            ("investigate", lambda: investigate.run(con)),
            ("verify", lambda: verify.run(con)),
            ("wake", lambda: wake.run(con)),
        ]
        for name, fn in stages:
            out = fn()
            con.commit()
            print(f"{name}: {out}")


def cmd_status(args):
    with db.connect() as con:
        out = {
            "events": {r["status"]: r["n"] for r in con.execute(
                "select status, count(*) n from event group by 1 order by 1")},
            "mentions": {r["prefix"]: r["n"] for r in con.execute(
                "select split_part(resolver, ':', 1) prefix, count(*) n "
                "from mention group by 1 order by 1")},
            "claims": con.execute("select count(*) n from claim").fetchone()["n"],
            "entities": con.execute("select count(*) n from entity").fetchone()["n"],
            "edges": {r["origin"]: r["n"] for r in con.execute(
                "select origin, count(*) n from edge group by 1 order by 1")},
            "er_queue": {r["status"]: r["n"] for r in con.execute(
                "select status, count(*) n from er_queue group by 1 order by 1")},
            "hypotheses": {r["state"]: r["n"] for r in con.execute(
                "select state, count(*) n from hypothesis group by 1 order by 1")},
        }
        print(out)


def cmd_serve(args):
    import uvicorn
    migrate_with_retry()
    uvicorn.run("graph.webapp:app", host=args.host, port=args.port)


def _extract_drain(con, batch=10):
    """Run extraction in batches until no pending event makes progress."""
    total = {"extracted": 0, "failed": 0, "claims": 0, "mentions": 0}
    while True:
        out = extract.run(con, limit=batch)
        con.commit()
        for k in total:
            total[k] += out.get(k, 0)
        if out.get("paused"):
            total["paused"] = True
            return total
        if out["extracted"] + out["failed"] == 0:
            return total


def _stage_begin(name):
    """Open a stage_run row on its own connection. Bookkeeping failures are
    logged and never block the stage itself."""
    try:
        with db.connect() as con:
            run_id = con.execute(
                "insert into stage_run (stage, started_at) values (%s, now()) "
                "returning id", (name,)).fetchone()["id"]
            con.commit()
            return run_id
    except Exception as e:
        print(f"loop: stage_run insert failed ({e!r})")
        return None


def _stage_finish(run_id, summary=None, error=None):
    if run_id is None:
        return
    try:
        with db.connect() as con:
            con.execute(
                "update stage_run set finished_at=now(), summary=%s, error=%s "
                "where id=%s",
                (Jsonb(summary) if summary is not None else None, error, run_id))
            con.commit()
    except Exception as e:
        print(f"loop: stage_run update failed ({e!r})")


def _cycle_bookkeeping(interval_s):
    """Once per cycle: heartbeat upsert + 7-day stage_run prune."""
    heartbeat = {
        "last_cycle_at": datetime.now(timezone.utc).isoformat(),
        "interval_s": interval_s,
        "seats": llm.seat_status(),
    }
    try:
        with db.connect() as con:
            con.execute(
                "insert into app_kv (key, value, updated_at) values "
                "('worker_heartbeat', %s, now()) on conflict (key) do update "
                "set value=excluded.value, updated_at=now()", (Jsonb(heartbeat),))
            con.execute(
                "delete from stage_run where started_at < now() - interval '7 days'")
            con.commit()
    except Exception as e:
        print(f"loop: heartbeat upsert failed ({e!r})")


def _run_requested():
    """Read-and-reset the app_kv run_requested flag (webapp POST /api/run-now)."""
    try:
        with db.connect() as con:
            row = con.execute(
                "select value from app_kv where key='run_requested'").fetchone()
            if not (row and isinstance(row["value"], dict)
                    and row["value"].get("v") is True):
                return False
            con.execute(
                "update app_kv set value=%s, updated_at=now() "
                "where key='run_requested'", (Jsonb({"v": False}),))
            con.commit()
            return True
    except Exception as e:
        print(f"loop: run_requested check failed ({e!r})")
        return False


def cmd_loop(args):
    """Pipeline worker: migrate + seed-if-empty + GLEIF bootstrap, then poll
    -> extract -> resolve -> adjudicate -> materialize -> discover forever.
    Per-stage errors are logged and never kill the loop. Every stage call is
    recorded in stage_run; each cycle upserts the worker_heartbeat KV; between
    cycles the sleep is a series of <=15s naps that watch for run_requested."""
    migrate_with_retry(window_s=120)
    with db.connect() as con:
        # Reference data only. Sources (watchlist, feeds) are never preloaded
        # in production — they come in through the admin page.
        if con.execute("select count(*) n from registry_sec").fetchone()["n"] == 0:
            print("loop: registry_sec empty — seeding reference data")
            out = seed(con)
            con.commit()
            print(f"seed: {out}")

    if not config.GLEIF_SQLITE.exists():
        if os.environ.get("CAF_FETCH_GLEIF", "1") != "0":
            print(f"loop: {config.GLEIF_SQLITE} missing — fetching GLEIF golden "
                  f"copy (~500MB download; set CAF_FETCH_GLEIF=0 to skip)")
            try:
                from . import gleif_fetch
                gleif_fetch.ensure()
            except Exception as e:
                print(f"loop: GLEIF bootstrap failed ({e!r}) — "
                      f"resolve will skip GLEIF tiers")
        else:
            print("loop: CAF_FETCH_GLEIF=0 — resolve will skip GLEIF tiers")

    def stage(name, fn):
        # fresh connection per stage: one stage's failure (or a dropped
        # postgres connection) never poisons the rest of the cycle
        run_id = _stage_begin(name)
        try:
            with db.connect() as con:
                out = fn(con)
                con.commit()
            print(f"{name}: {out}")
            _stage_finish(run_id, summary=out)
        except Exception as e:
            print(f"{name}: ERROR {e!r}")
            _stage_finish(run_id, error=repr(e))

    cycle = 0
    while True:
        cycle += 1
        print(f"[loop] cycle {cycle} start")
        stage("edgar", lambda con: edgar.poll(con))
        stage("podcast", lambda con: podcast.poll(
            con, episodes_per_feed=args.episodes))
        stage("rss", lambda con: rss.poll(con))
        stage("youtube", lambda con: youtube.poll(con))
        stage("x", lambda con: x.poll(con))
        stage("bridge", lambda con: bridge.poll(con))
        stage("links", lambda con: links.process(con))
        stage("triage", lambda con: triage.run(con))
        stage("extract", _extract_drain)
        stage("resolve", lambda con: resolve.run(con))
        stage("adjudicate", lambda con: adjudicate.run(con))
        stage("garden", lambda con: gardener.run(con))
        stage("materialize", lambda con: materialize.run(con))
        stage("contradictions", lambda con: quality.contradictions(con))
        stage("reliability", lambda con: quality.reliability(con))
        stage("discover", lambda con: attribute_joins.run(con))
        stage("funnel", lambda con: funnel.run(con))
        stage("hypothesize", lambda con: hypothesize.run(con))
        stage("investigate", lambda con: investigate.run(con))
        stage("verify", lambda con: verify.run(con))
        stage("wake", lambda con: wake.run(con))
        _cycle_bookkeeping(args.interval)
        print(f"[loop] cycle {cycle} done — sleeping {args.interval}s")
        deadline = time.time() + args.interval
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(15, remaining))
            if _run_requested():
                print("[loop] run requested — starting next cycle early")
                break


def main():
    p = argparse.ArgumentParser(prog="graph", description="CAF company graph pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply schema/*.sql").set_defaults(func=cmd_migrate)
    p_seed = sub.add_parser("seed", help="load registry_sec + alias_seed")
    p_seed.add_argument("--sources", action="store_true",
                        help="also seed the demo watchlist + podcast feeds (dev only)")
    p_seed.set_defaults(func=cmd_seed)

    s = sub.add_parser("ingest-edgar", help="poll EDGAR 8-Ks for the watchlist")
    s.add_argument("--tickers", nargs="*", default=None)
    s.add_argument("--per-company", type=int, default=2)
    s.set_defaults(func=cmd_ingest_edgar)

    s = sub.add_parser("ingest-podcasts", help="poll podcast feeds + transcribe")
    s.add_argument("--episodes", type=int, default=2)
    s.set_defaults(func=cmd_ingest_podcasts)

    s = sub.add_parser("ingest-file", help="ingest a local .txt/.html file")
    s.add_argument("path")
    s.set_defaults(func=cmd_ingest_file)

    s = sub.add_parser("add-source", help="add a source (or a one-off link) "
                       "from a pasted URL")
    s.add_argument("url")
    s.add_argument("--name", default=None, help="override the suggested name")
    s.set_defaults(func=cmd_add_source)

    s = sub.add_parser("add-ticker", help="add a ticker to the watchlist")
    s.add_argument("ticker")
    s.add_argument("--sector", default=None)
    s.set_defaults(func=cmd_add_ticker)

    s = sub.add_parser("set-credential", help="store a sign-in for a premium site")
    s.add_argument("site", choices=sorted(credentials.SITES))
    s.add_argument("--file", default=None, help="path to a cookies.txt export")
    s.add_argument("--value", default=None, help="the token or cookie text")
    s.add_argument("--note", default=None)
    s.set_defaults(func=cmd_set_credential)

    sub.add_parser("links", help="process the one-off link queue once"
                   ).set_defaults(func=_cmd_simple(links.process))

    s = sub.add_parser("extract", help="extract claims from pending events")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_extract)

    sub.add_parser("resolve", help="resolve pending mentions").set_defaults(func=cmd_resolve)

    s = sub.add_parser("adjudicate", help="LLM-adjudicate queued mentions")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_adjudicate)

    sub.add_parser("materialize", help="rebuild asserted edges from claims"
                   ).set_defaults(func=cmd_materialize)
    sub.add_parser("discover", help="generate hypotheses from attribute joins"
                   ).set_defaults(func=cmd_discover)
    sub.add_parser("triage", help="materiality-score pending events"
                   ).set_defaults(func=_cmd_simple(triage.run))
    sub.add_parser("garden", help="version the predicate canonical mapping"
                   ).set_defaults(func=_cmd_simple(gardener.run))
    sub.add_parser("quality", help="contradiction scan + source reliability"
                   ).set_defaults(func=cmd_quality)
    sub.add_parser("funnel", help="triage-score generated hypotheses"
                   ).set_defaults(func=_cmd_simple(funnel.run))
    sub.add_parser("hypothesize", help="refine triaged hypotheses (LLM)"
                   ).set_defaults(func=_cmd_simple(hypothesize.run))
    sub.add_parser("investigate", help="investigate triaged hypotheses (LLM)"
                   ).set_defaults(func=_cmd_simple(investigate.run))
    sub.add_parser("verify", help="verify + promote investigated hypotheses (LLM)"
                   ).set_defaults(func=_cmd_simple(verify.run))
    sub.add_parser("wake", help="revive parked hypotheses with new evidence"
                   ).set_defaults(func=_cmd_simple(wake.run))

    s = sub.add_parser("run", help="one full cycle: ingest -> extract -> resolve "
                       "-> adjudicate -> materialize -> discover")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_run)

    sub.add_parser("status", help="row counts across the pipeline").set_defaults(func=cmd_status)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8642)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("loop", help="pipeline worker: full cycle forever "
                       "(migrate + seed-if-empty + GLEIF bootstrap at boot)")
    s.add_argument("--interval", type=int, default=900,
                   help="seconds to sleep between cycles")
    s.add_argument("--episodes", type=int, default=2,
                   help="podcast episodes per feed per cycle")
    s.set_defaults(func=cmd_loop)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
