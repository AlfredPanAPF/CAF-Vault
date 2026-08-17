"""JSON API + SPA serving (build spec v2 §3).

All data endpoints live under /api and return JSON; the built React bundle at
REPO/frontend/dist is served for everything else (catch-all -> index.html,
ported from Filter's api/app.py). When the bundle is absent the API still
works and the catch-all answers 404 "Frontend not built.".

Library style: one db.connect() per request via the get_con dependency; POST
handlers commit; library functions never do. Errors are HTTPException {detail}.
Timestamps are ISO 8601 UTC. Run with `graph serve` (uvicorn graph.webapp:app).
"""
import json
import os
import uuid
from datetime import timezone

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from . import config, db
from .connectors import manual

# ---------------------------------------------------------------- helpers


def get_con():
    con = db.connect()
    try:
        yield con
    finally:
        con.close()


def iso(v):
    """timestamptz -> ISO 8601 UTC string (psycopg hands back aware datetimes
    in the session timezone, which is not necessarily UTC)."""
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat()


def fmt_literal(lit):
    """Human line for an object_literal jsonb: '3.2 billion USD (as_of=2025-01-01)'."""
    if not isinstance(lit, dict):
        return json.dumps(lit, ensure_ascii=False)
    parts = [str(lit[k]) for k in ("value", "unit", "currency")
             if lit.get(k) is not None]
    extras = {k: v for k, v in lit.items()
              if k not in ("value", "unit", "currency") and v is not None}
    out = " ".join(parts)
    if extras:
        tail = ", ".join(f"{k}={v}" for k, v in sorted(extras.items()))
        out = f"{out} ({tail})" if out else tail
    return out or json.dumps(lit, ensure_ascii=False)


def registry_short(refs):
    """One short registry string for list rows: ticker, else LEI country, else '-'."""
    refs = refs or {}
    return refs.get("ticker") or refs.get("country") or "-"


# ---------------------------------------------------------------- claims SQL

CLAIMS_SELECT = """
select c.claim_id, c.subject_entity, c.subject_surface, c.predicate_raw,
       c.object_entity, c.object_surface, c.object_literal,
       c.qualifiers->>'stance' as stance,
       c.evidence_quote, c.confidence, c.observed_at, c.event_id,
       se.canonical_name as subject_name, oe.canonical_name as object_name,
       ev.published_at, ev.meta->>'title' as doc_title,
       s.connector, s.name as source_name
"""

CLAIMS_FROM = """
from claim c
join event ev on ev.event_id = c.event_id
join source s on s.source_id = ev.source_id
left join entity se on se.entity_id = c.subject_entity
left join entity oe on oe.entity_id = c.object_entity
"""

SOURCE_TYPES = ("edgar", "podcast", "rss", "manual")


def claims_where(q="", predicate="", source_type="", stance="", sector="",
                 days=None, entity=None):
    where, params = [], {}
    if q:
        where.append(
            "(c.subject_surface ilike %(pat)s or c.object_surface ilike %(pat)s "
            "or c.predicate_raw ilike %(pat)s or c.evidence_quote ilike %(pat)s)")
        params["pat"] = f"%{q}%"
    if predicate:
        where.append("c.predicate_raw = %(predicate)s")
        params["predicate"] = predicate
    if source_type:
        where.append("s.connector = %(source_type)s")
        params["source_type"] = source_type
    if stance:
        where.append("c.qualifiers->>'stance' = %(stance)s")
        params["stance"] = stance
    if sector:
        where.append("ev.meta->>'sector' = %(sector)s")
        params["sector"] = sector
    if days is not None:
        where.append("c.observed_at >= now() - make_interval(days => %(days)s)")
        params["days"] = days
    if entity is not None:
        where.append("(c.subject_entity = %(entity)s or c.object_entity = %(entity)s)")
        params["entity"] = entity
    return ("where " + " and ".join(where)) if where else "", params


def claim_json(c):
    return {
        "claim_id": c["claim_id"],
        "subject": {"surface": c["subject_surface"],
                    "entity_id": c["subject_entity"],
                    "name": c["subject_name"]},
        "predicate": c["predicate_raw"],
        "object": {"surface": c["object_surface"],
                   "literal": (fmt_literal(c["object_literal"])
                               if c["object_literal"] is not None else None),
                   "entity_id": c["object_entity"],
                   "name": c["object_name"]},
        "stance": c["stance"],
        "confidence": c["confidence"],
        "evidence_quote": c["evidence_quote"],
        "observed_at": iso(c["observed_at"]),
        "published_at": iso(c["published_at"]),
        "connector": c["connector"],
        "source_name": c["source_name"],
        "doc_title": c["doc_title"],
        "event_id": c["event_id"],
    }


# ---------------------------------------------------------------- other SQL

ENTITIES_SQL = """
select e.entity_id, e.canonical_name, e.kind, e.registry_refs,
       (select count(*) from claim c
         where c.subject_entity = e.entity_id or c.object_entity = e.entity_id) as claims
from entity e
{where}
order by claims desc, e.canonical_name
limit %(limit)s
"""

ENTITIES_WHERE = """
where e.canonical_name ilike %(pat)s
   or exists (select 1 from entity_alias a
               where a.entity_id = e.entity_id and a.alias ilike %(pat)s)
"""

EDGES_SQL = """
select e.edge_id, e.src, e.dst, e.predicate, e.origin,
       cardinality(e.claim_ids) as claims, e.confidence, e.last_evidence_at,
       e.archived, round(edge_relevance(e.*)::numeric, 2) as relevance,
       peer.entity_id as peer_id, peer.canonical_name as peer_name
from edge e
join entity peer on peer.entity_id =
     case when e.src = %(eid)s then e.dst else e.src end
where e.src = %(eid)s or e.dst = %(eid)s
order by relevance desc, e.predicate, peer.canonical_name
"""

HYPOTHESES_SQL = """
select hypothesis_id, type, subjects, state, rationale, created_at
from hypothesis
order by created_at desc
"""

WATCHLIST_SQL = """
select w.ticker, w.sector, w.active, r.title as company,
       (select count(*) from event e where e.meta->>'ticker' = w.ticker) as events
from watchlist w
left join registry_sec r on r.ticker = w.ticker
order by w.ticker
"""

FEEDS_SQL = """
select s.source_id, s.name, s.connector, s.url, s.status, s.last_polled,
       (select count(*) from event e where e.source_id = s.source_id) as events
from source s
where s.connector in ('podcast', 'rss')
order by s.connector, s.name
"""

STATUS_SOURCES_SQL = """
select s.source_id, s.name, s.connector, s.status, s.last_polled,
       (select count(*) from event e where e.source_id = s.source_id) as events
from source s
order by s.connector, s.name
"""

FAILED_EVENTS_SQL = """
select e.event_id, e.connector,
       coalesce(e.meta->>'title', e.meta->>'filename') as title,
       e.last_error, e.fetched_at
from event e
where e.status = 'failed'
order by e.fetched_at desc
limit 20
"""

STAGES_SQL = """
select distinct on (stage) stage, started_at, finished_at, summary, error
from stage_run
order by stage, started_at desc
"""

MENTIONS_SQL = """
select case when resolved_entity is not null then 'resolved'
            when resolver = 'queued' then 'queued'
            when resolver = 'not_company' then 'skipped'
            else 'unresolved' end as k, count(*) n
from mention group by 1
"""


def group_counts(con, sql, keys=()):
    out = {k: 0 for k in keys}
    for r in con.execute(sql):
        out[r["k"]] = r["n"]
    return out


# ---------------------------------------------------------------- bodies


class WatchlistBody(BaseModel):
    ticker: str
    sector: str | None = None


class FeedBody(BaseModel):
    name: str
    url: str
    kind: str


# ---------------------------------------------------------------- app


def create_app():
    # CAF_ROOT_PATH set (prod, nginx strips /vault/ -> /): serve plain paths
    # only. Empty (local dev, no proxy): additionally serve under /vault and
    # redirect / there, since the built bundle hardcodes /vault/ URLs.
    #
    # Deliberately NOT passed to FastAPI(root_path=...): with root_path set,
    # Starlette expects UNSTRIPPED request paths and strips the prefix itself,
    # which is the opposite of what a prefix-stripping proxy sends — mounted
    # assets 404 and the SPA ships blank pages (2026-08-17 incident; Filter's
    # app.py does the same thing for the same reason).
    root_path = os.environ.get("CAF_ROOT_PATH", "")
    app = FastAPI(title="CAF graph")

    @app.get("/health")
    def health():
        # liveness only — deliberately no DB touch
        return {"ok": True}

    # ------------------------------------------------------------ status

    @app.get("/api/status")
    def api_status(con=Depends(get_con)):
        hb_row = con.execute(
            "select value from app_kv where key='worker_heartbeat'").fetchone()
        rr_row = con.execute(
            "select value from app_kv where key='run_requested'").fetchone()
        heartbeat = None
        if hb_row and isinstance(hb_row["value"], dict):
            heartbeat = dict(hb_row["value"])
            heartbeat["run_requested"] = bool(
                rr_row and isinstance(rr_row["value"], dict)
                and rr_row["value"].get("v"))

        stages = sorted(con.execute(STAGES_SQL).fetchall(),
                        key=lambda r: r["started_at"])
        claims_row = con.execute(
            "select count(*) as total, count(*) filter "
            "(where observed_at >= now() - interval '7 days') as last7 "
            "from claim").fetchone()
        counts = {
            "events": group_counts(
                con, "select status as k, count(*) n from event group by 1",
                ("pending", "extracting", "extracted", "failed", "duplicate")),
            "claims": claims_row["total"],
            "claims_7d": claims_row["last7"],
            "mentions": group_counts(
                con, MENTIONS_SQL, ("resolved", "queued", "unresolved", "skipped")),
            "entities": con.execute(
                "select count(*) n from entity").fetchone()["n"],
            "edges": group_counts(
                con, "select origin as k, count(*) n from edge group by 1",
                ("asserted", "inferred")),
            "er_queue": group_counts(
                con, "select status as k, count(*) n from er_queue group by 1",
                ("pending", "decided", "failed")),
            "hypotheses": group_counts(
                con, "select state as k, count(*) n from hypothesis group by 1",
                ("generated",)),
        }
        return {
            "heartbeat": heartbeat,
            "stages": [{"stage": r["stage"],
                        "started_at": iso(r["started_at"]),
                        "finished_at": iso(r["finished_at"]),
                        "summary": r["summary"],
                        "error": r["error"]} for r in stages],
            "counts": counts,
            "sources": [{"source_id": r["source_id"], "name": r["name"],
                         "connector": r["connector"], "status": r["status"],
                         "last_polled": iso(r["last_polled"]),
                         "events": r["events"]}
                        for r in con.execute(STATUS_SOURCES_SQL)],
            "failed_events": [{"event_id": r["event_id"],
                               "connector": r["connector"],
                               "title": r["title"],
                               "last_error": r["last_error"],
                               "fetched_at": iso(r["fetched_at"])}
                              for r in con.execute(FAILED_EVENTS_SQL)],
        }

    # ------------------------------------------------------------ claims

    @app.get("/api/claims")
    def api_claims(q: str = "", predicate: str = "", source_type: str = "",
                   stance: str = "", sector: str = "", days: int | None = None,
                   entity: uuid.UUID | None = None, limit: int = 50,
                   offset: int = 0, con=Depends(get_con)):
        if source_type and source_type not in SOURCE_TYPES:
            raise HTTPException(
                400, "source_type must be edgar, podcast, rss or manual")
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        where, params = claims_where(q.strip(), predicate, source_type,
                                     stance, sector, days, entity)
        total = con.execute(
            f"select count(*) n {CLAIMS_FROM} {where}", params).fetchone()["n"]
        rows = con.execute(
            f"{CLAIMS_SELECT} {CLAIMS_FROM} {where} "
            f"order by c.observed_at desc, c.claim_id "
            f"limit %(limit)s offset %(offset)s",
            {**params, "limit": limit, "offset": offset}).fetchall()
        return {"total": total, "claims": [claim_json(r) for r in rows]}

    # ------------------------------------------------------------ entities

    @app.get("/api/entities")
    def api_entities(q: str = "", limit: int = 50, con=Depends(get_con)):
        q = q.strip()
        limit = max(1, min(limit, 200))
        sql = ENTITIES_SQL.format(where=ENTITIES_WHERE if q else "")
        params = {"limit": limit, **({"pat": f"%{q}%"} if q else {})}
        return [{"entity_id": r["entity_id"], "name": r["canonical_name"],
                 "kind": r["kind"], "registry": registry_short(r["registry_refs"]),
                 "claims": r["claims"]}
                for r in con.execute(sql, params)]

    @app.get("/api/entity/{entity_id}")
    def api_entity(entity_id: uuid.UUID, con=Depends(get_con)):
        ent = con.execute("select * from entity where entity_id=%s",
                          (entity_id,)).fetchone()
        if ent is None:
            raise HTTPException(404, "No such entity.")
        aliases = [r["alias"] for r in con.execute(
            "select distinct alias from entity_alias where entity_id=%s "
            "order by alias", (entity_id,))]
        where, params = claims_where(entity=entity_id)
        claims = con.execute(
            f"{CLAIMS_SELECT} {CLAIMS_FROM} {where} "
            f"order by c.observed_at desc, c.claim_id", params).fetchall()
        edges = con.execute(EDGES_SQL, {"eid": entity_id}).fetchall()

        def edge_json(e):
            return {"edge_id": e["edge_id"],
                    "peer": {"entity_id": e["peer_id"], "name": e["peer_name"]},
                    "predicate": e["predicate"],
                    "direction": "out" if e["src"] == entity_id else "in",
                    "claims": e["claims"],
                    "confidence": e["confidence"],
                    "last_evidence_at": iso(e["last_evidence_at"]),
                    "relevance": float(e["relevance"]),
                    "archived": e["archived"]}

        return {
            "entity": {"entity_id": ent["entity_id"],
                       "name": ent["canonical_name"],
                       "kind": ent["kind"],
                       "registry_refs": ent["registry_refs"] or {}},
            "aliases": aliases,
            "claims": [claim_json(c) for c in claims],
            "edges": {
                "asserted": [edge_json(e) for e in edges
                             if e["origin"] == "asserted"],
                "inferred": [edge_json(e) for e in edges
                             if e["origin"] == "inferred"],
            },
        }

    # ------------------------------------------------------------ hypotheses

    @app.get("/api/hypotheses")
    def api_hypotheses(con=Depends(get_con)):
        hypotheses = con.execute(HYPOTHESES_SQL).fetchall()
        ids = sorted({sid for h in hypotheses for sid in h["subjects"]})
        names = {}
        if ids:
            names = {r["entity_id"]: r["canonical_name"] for r in con.execute(
                "select entity_id, canonical_name from entity "
                "where entity_id = any(%s)", (ids,))}
        return [{"hypothesis_id": h["hypothesis_id"], "type": h["type"],
                 "subjects": [{"entity_id": sid,
                               "name": names.get(sid, str(sid)[:8])}
                              for sid in h["subjects"]],
                 "state": h["state"], "rationale": h["rationale"],
                 "created_at": iso(h["created_at"])}
                for h in hypotheses]

    # ------------------------------------------------------------ sources

    @app.get("/api/sources")
    def api_sources(con=Depends(get_con)):
        return {
            "watchlist": [{"ticker": r["ticker"], "sector": r["sector"],
                           "active": r["active"], "company": r["company"],
                           "events": r["events"]}
                          for r in con.execute(WATCHLIST_SQL)],
            "feeds": [{"source_id": r["source_id"], "name": r["name"],
                       "connector": r["connector"], "url": r["url"],
                       "status": r["status"],
                       "last_polled": iso(r["last_polled"]),
                       "events": r["events"]}
                      for r in con.execute(FEEDS_SQL)],
        }

    @app.post("/api/watchlist")
    def api_add_ticker(body: WatchlistBody, con=Depends(get_con)):
        ticker = body.ticker.strip().upper()
        sector = (body.sector or "").strip() or None
        reg = con.execute("select title from registry_sec where ticker=%s",
                          (ticker,)).fetchone()
        if reg is None:
            raise HTTPException(
                400, f"Unknown ticker {ticker}. Not in the SEC registry.")
        con.execute(
            "insert into watchlist (ticker, sector, active, added_by) "
            "values (%s, %s, true, 'web') on conflict (ticker) do update "
            "set active = true, "
            "sector = coalesce(excluded.sector, watchlist.sector)",
            (ticker, sector))
        con.commit()
        return {"ok": True, "ticker": ticker, "company": reg["title"]}

    @app.post("/api/watchlist/{ticker}/toggle")
    def api_toggle_ticker(ticker: str, con=Depends(get_con)):
        row = con.execute(
            "update watchlist set active = not active where ticker=%s "
            "returning active", (ticker,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such ticker.")
        con.commit()
        return {"ok": True, "active": row["active"]}

    @app.post("/api/feeds")
    def api_add_feed(body: FeedBody, con=Depends(get_con)):
        if body.kind not in ("podcast", "rss"):
            raise HTTPException(400, "Kind must be podcast or rss.")
        name, url = body.name.strip(), body.url.strip()
        if body.kind == "podcast" and not name.startswith("podcast:"):
            name = f"podcast:{name}"
        if con.execute("select 1 from source where name=%s and connector=%s",
                       (name, body.kind)).fetchone():
            raise HTTPException(409, f"Feed {name} already exists.")
        row = con.execute(
            "insert into source (name, connector, url, status, added_by) "
            "values (%s, %s, %s, 'active', 'web') returning source_id",
            (name, body.kind, url)).fetchone()
        con.commit()
        return {"ok": True, "source_id": row["source_id"]}

    @app.post("/api/sources/{source_id}/toggle")
    def api_toggle_source(source_id: uuid.UUID, con=Depends(get_con)):
        # pause/resume without a schema change: active <-> demoted
        row = con.execute(
            "update source set status = case when status='active' "
            "then 'demoted' else 'active' end where source_id=%s "
            "returning status", (source_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such source.")
        con.commit()
        return {"ok": True, "status": row["status"]}

    @app.post("/api/upload")
    def api_upload(files: list[UploadFile] = File(...), con=Depends(get_con)):
        events = []
        for f in files:
            event_id = manual.ingest_bytes(con, f.filename or "upload.txt",
                                           f.file.read())
            events.append({"filename": f.filename,
                           "event_id": event_id,
                           "duplicate": event_id is None})
        con.commit()
        return {"events": events}

    @app.post("/api/events/{event_id}/retry")
    def api_retry_event(event_id: uuid.UUID, con=Depends(get_con)):
        cur = con.execute(
            "update event set status='pending', attempts=0 "
            "where event_id=%s and status='failed'", (event_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "No failed event with that id.")
        con.commit()
        return {"ok": True}

    @app.post("/api/run-now")
    def api_run_now(con=Depends(get_con)):
        con.execute(
            "insert into app_kv (key, value, updated_at) values "
            "('run_requested', %s, now()) on conflict (key) do update "
            "set value=excluded.value, updated_at=now()", (Jsonb({"v": True}),))
        con.commit()
        return {"ok": True}

    # ------------------------------------------------------------ SPA

    dist = config.REPO / "frontend" / "dist"

    def spa_file(path):
        """Resolve a bundle file safely; fall back to index.html (SPA routes)."""
        file = (dist / path).resolve() if path else None
        if file is not None and file.is_file() and file.is_relative_to(dist.resolve()):
            return FileResponse(file)
        return FileResponse(dist / "index.html")

    if dist.is_dir():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"),
                      name="assets")
        if not root_path:
            # Local-dev convenience: the built bundle hardcodes /vault/ asset
            # URLs (vite base) and there is no nginx to strip the prefix, so
            # the same SPA is also served under /vault. The bundle likewise
            # fetches /vault/api/...; strip the prefix the way nginx would.
            @app.middleware("http")
            async def vault_api_rewrite(request, call_next):
                path = request.scope["path"]
                if path == "/vault/api" or path.startswith("/vault/api/"):
                    request.scope["path"] = path[len("/vault"):]
                return await call_next(request)

            if (dist / "assets").is_dir():
                app.mount("/vault/assets",
                          StaticFiles(directory=dist / "assets"),
                          name="vault-assets")

            @app.get("/")
            def root_redirect():
                return RedirectResponse("/vault/")

            @app.get("/vault/{path:path}")
            def vault_spa(path: str):
                return spa_file(path)

        @app.get("/{path:path}")
        def spa(path: str):
            if path == "health" or path == "api" or path.startswith("api/"):
                raise HTTPException(404, "Not found.")
            return spa_file(path)
    else:
        @app.get("/{path:path}")
        def no_frontend(path: str):
            if path == "health" or path == "api" or path.startswith("api/"):
                raise HTTPException(404, "Not found.")
            return PlainTextResponse("Frontend not built.", status_code=404)

    return app


app = create_app()
