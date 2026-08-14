"""CLI entry point (`graph` per pyproject). Each subcommand opens one
connection, calls the library, commits, and prints the returned dict —
library functions never commit."""
import argparse
import json
import os
import time

from . import config, db, er_norm
from .connectors import edgar, manual, podcast
from .discovery import attribute_joins
from .pipeline import adjudicate, extract, materialize, resolve


def cmd_migrate(args):
    db.migrate()


def seed(con):
    """Load registry_sec + alias_seed from the ref files. No commit (CLI commits)."""
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
    return {"registry_sec": len(registrants), "alias_seed": seeded}


def cmd_seed(args):
    with db.connect() as con:
        out = seed(con)
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


def cmd_run(args):
    with db.connect() as con:
        stages = [
            ("edgar", lambda: edgar.poll(con)),
            ("extract", lambda: extract.run(con, limit=args.limit)),
            ("resolve", lambda: resolve.run(con)),
            ("adjudicate", lambda: adjudicate.run(con)),
            ("materialize", lambda: materialize.run(con)),
            ("discover", lambda: attribute_joins.run(con)),
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
    db.migrate()
    uvicorn.run("graph.webapp:app", host=args.host, port=args.port)


def _extract_drain(con, batch=10):
    """Run extraction in batches until no pending event makes progress."""
    total = {"extracted": 0, "failed": 0, "claims": 0, "mentions": 0}
    while True:
        out = extract.run(con, limit=batch)
        con.commit()
        for k in total:
            total[k] += out.get(k, 0)
        if out["extracted"] + out["failed"] == 0:
            return total


def cmd_loop(args):
    """Pipeline worker: migrate + seed-if-empty + GLEIF bootstrap, then poll
    -> extract -> resolve -> adjudicate -> materialize -> discover forever.
    Per-stage errors are logged and never kill the loop."""
    db.migrate()
    with db.connect() as con:
        if con.execute("select count(*) n from registry_sec").fetchone()["n"] == 0:
            print("loop: registry_sec empty — seeding")
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
        try:
            with db.connect() as con:
                out = fn(con)
                con.commit()
            print(f"{name}: {out}")
        except Exception as e:
            print(f"{name}: ERROR {e!r}")

    cycle = 0
    while True:
        cycle += 1
        print(f"[loop] cycle {cycle} start")
        stage("edgar", lambda con: edgar.poll(con))
        stage("podcast", lambda con: podcast.poll(
            con, episodes_per_feed=args.episodes))
        stage("extract", _extract_drain)
        stage("resolve", lambda con: resolve.run(con))
        stage("adjudicate", lambda con: adjudicate.run(con))
        stage("materialize", lambda con: materialize.run(con))
        stage("discover", lambda con: attribute_joins.run(con))
        print(f"[loop] cycle {cycle} done — sleeping {args.interval}s")
        time.sleep(args.interval)


def main():
    p = argparse.ArgumentParser(prog="graph", description="CAF company graph pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply schema/*.sql").set_defaults(func=cmd_migrate)
    sub.add_parser("seed", help="load registry_sec + alias_seed").set_defaults(func=cmd_seed)

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
