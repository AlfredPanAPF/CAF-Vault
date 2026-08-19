"""JSON API + SPA serving (build spec v2 §3; alerts, digest and the review
surfaces per build spec v3 §4.3 and §7.1; the sources page — watchlist, one
URL box, one-off links and sign-ins — per build spec v4 §7).

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
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from . import (artifacts, config, credentials, db, fetch, probes, router,
               signin, staleness, watchlist)
from .connectors import links, manual
from .pipeline import adjudicate, resolve

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
    # every field null: render nothing rather than raw jsonb
    return out


def registry_short(refs):
    """One short registry string for list rows: ticker, else LEI country, else '-'."""
    refs = refs or {}
    return refs.get("ticker") or refs.get("country") or "-"


def entity_names(con, ids):
    """{entity_id: canonical_name} in one query (no N+1 across list rows)."""
    ids = sorted(set(ids))
    if not ids:
        return {}
    return {r["entity_id"]: r["canonical_name"] for r in con.execute(
        "select entity_id, canonical_name from entity where entity_id = any(%s)",
        (ids,))}


def predicate_canon_map(con):
    """Latest predicate_map version as {raw: canon}; {} before the gardener has
    run. Fetched once per request and passed into claim_json (design §5.3)."""
    return {r["predicate_raw"]: r["predicate_canon"] for r in con.execute(
        "select predicate_raw, predicate_canon from predicate_map "
        "where version = (select max(version) from predicate_map)")}


# ---------------------------------------------------------------- claims SQL

CLAIMS_SELECT = """
select c.claim_id, c.subject_entity, c.subject_surface, c.predicate_raw,
       c.object_entity, c.object_surface, c.object_literal,
       c.qualifiers->>'stance' as stance, c.status, c.superseded_by,
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
                 days=None, entity=None, status="asserted"):
    where, params = [], {}
    # asserted-only by default: superseded/retracted claims are withdrawn from
    # the current view; status='all' opts back into the append-only audit view
    # (design §2). Claim fetches by explicit id (contradiction panel,
    # hypothesis evidence) deliberately bypass this filter.
    if status != "all":
        where.append("c.status = %(status)s")
        params["status"] = status
    if q:
        where.append(
            "(c.subject_surface ilike %(pat)s or c.object_surface ilike %(pat)s "
            "or c.predicate_raw ilike %(pat)s or c.evidence_quote ilike %(pat)s)")
        params["pat"] = f"%{q}%"
    if predicate:
        # the filter takes either form: the raw predicate or its canonical
        # under the latest gardener mapping (design §5.3)
        where.append(
            "(c.predicate_raw = %(predicate)s or exists ("
            "select 1 from predicate_map pm "
            "where pm.version = (select max(version) from predicate_map) "
            "and pm.predicate_raw = lower(c.predicate_raw) "
            "and pm.predicate_canon = %(predicate)s))")
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


def claim_json(c, canon=None):
    canon = canon or {}
    return {
        "claim_id": c["claim_id"],
        "subject": {"surface": c["subject_surface"],
                    "entity_id": c["subject_entity"],
                    "name": c["subject_name"]},
        "predicate": c["predicate_raw"],
        "predicate_canon": canon.get((c["predicate_raw"] or "").lower()),
        "object": {"surface": c["object_surface"],
                   "literal": (fmt_literal(c["object_literal"])
                               if c["object_literal"] is not None else None),
                   "entity_id": c["object_entity"],
                   "name": c["object_name"]},
        "stance": c["stance"],
        "status": c["status"],
        "superseded_by": c["superseded_by"],
        "confidence": c["confidence"],
        "confidence_now": staleness.confidence_now(
            c["confidence"], c["observed_at"], c["predicate_raw"]),
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
where not exists (select 1 from entity_same_as sa
                   where sa.a = e.entity_id and sa.status = 'active')
{extra}
order by claims desc, e.canonical_name
limit %(limit)s
"""

ENTITIES_WHERE = """
and (e.canonical_name ilike %(pat)s
     or exists (select 1 from entity_alias a
                 where a.entity_id = e.entity_id and a.alias ilike %(pat)s))
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
select hypothesis_id, type, subjects, state, score, rationale,
       created_at, updated_at
from hypothesis
{where}
order by created_at desc
"""

ER_QUEUE_SQL = """
select q.mention_id, q.candidates, q.decision, q.created_at, q.decided_at,
       m.surface, m.event_id, ev.artifact_uri,
       ev.meta->>'title' as doc_title, s.connector, s.name as source_name
from er_queue q
join mention m on m.mention_id = q.mention_id
join event ev on ev.event_id = m.event_id
join source s on s.source_id = ev.source_id
"""

WATCHLIST_SQL = """
select w.ticker, coalesce(w.name, r.title) as name, w.sector, w.industry,
       w.exchange, w.country, w.active, coalesce(w.cik, r.cik) as cik,
       (select count(*) from event e where e.meta->>'ticker' = w.ticker) as events
from watchlist w
left join registry_sec r on r.ticker = w.ticker
order by w.ticker
"""

SOURCE_COLUMNS = """
select s.source_id, s.name, s.connector, s.url, s.config, s.status,
       s.last_polled, s.last_error,
       (select count(*) from event e where e.source_id = s.source_id) as events
from source s
"""

# the sources card: no dropped rows, no internal buckets (build spec v4 §7)
SOURCES_SQL = SOURCE_COLUMNS + """
where s.status <> 'dropped' and not s.is_internal
  and s.name not like 'link:%' and s.name not like 'edgar:%'
  and s.name <> 'manual:uploads'
order by s.name
"""

SOURCE_ROW_SQL = SOURCE_COLUMNS + "where s.source_id = %s"

STATUS_SOURCES_SQL = SOURCE_COLUMNS + """
where s.status <> 'dropped'
order by s.connector, s.name
"""

LINKS_SQL = """
select link_id, url, title, kind, site, status, error, event_id, created_at
from link_queue order by created_at desc limit 30
"""

# connectors whose source.url is the feed the poller reads
FEED_CONNECTORS = ("rss", "podcast", "youtube", "bridge")
BUCKET_LABELS = {"edgar": "Filings", "manual": "Uploads", "link": "Links"}


def source_label(connector, config):
    """User-facing type for a source row (build spec v4 §4 label list); shared
    by /api/sources and /api/status."""
    cfg = config or {}
    site = cfg.get("site")
    if connector == "rss":
        if site == "ft":
            return "FT feed"
        if site == "wsj":
            return "WSJ feed"
        if site == "substack" or cfg.get("substack"):
            return "Substack"
        return "News feed"
    if connector == "podcast":
        return "Podcast"
    if connector == "youtube":
        return "YouTube playlist" if cfg.get("playlist_id") else "YouTube channel"
    if connector == "x":
        return "X account"
    if connector == "bridge":
        name = ((cfg.get("bridge") or {}).get("name") or "").strip()
        if name.endswith("Bridge") and len(name) > 6:
            name = name[:-6]
        return name or "Bridge"
    return BUCKET_LABELS.get(connector, connector)


def credential_ref(site, sites_set):
    """{site, set} for a source that needs a sign-in, else None. Never carries
    the stored value."""
    if not site or site not in credentials.SITES:
        return None
    return {"site": site, "set": site in sites_set}


def source_json(r, sites_set=()):
    cfg = r["config"] or {}
    site = cfg.get("site")
    return {"source_id": r["source_id"], "name": r["name"],
            "connector": r["connector"],
            "label": source_label(r["connector"], cfg),
            "site": site,
            "url": r["url"],
            "feed_url": (r["url"] if r["connector"] in FEED_CONNECTORS
                         else cfg.get("feed_url")),
            "status": r["status"],
            "last_polled": iso(r["last_polled"]),
            "last_error": r["last_error"],
            "events": r["events"],
            "credential": credential_ref(site, sites_set)}


def link_json(r):
    return {"link_id": r["link_id"], "url": r["url"], "title": r["title"],
            "kind": r["kind"], "site": r["site"], "status": r["status"],
            "error": r["error"], "event_id": r["event_id"],
            "created_at": iso(r["created_at"])}


def credential_sites(con):
    """The sites that have a value stored (the value itself never leaves
    credentials.py)."""
    return {r["site"] for r in con.execute("select site from credential")}


def credential_saved(con, site):
    """What a saved sign-in unblocks, whether it was pasted or signed into in
    the browser (§7): the site's blocked links go back in the queue and its
    sources drop the error that was waiting on it. The caller commits."""
    links.requeue_blocked(con, site)
    con.execute("update source set last_error=null, last_error_at=null "
                "where config->>'site' = %s", (site,))

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


class ResolveBody(BaseModel):
    url: str


class SourceBody(BaseModel):
    url: str
    name: str | None = None


class CredentialBody(BaseModel):
    value: str
    note: str | None = None


class CredentialTestBody(BaseModel):
    # the pasted link a probe runs against (Substack: a paid post the account
    # subscribes to); the other sites' probes take none
    url: str | None = None


class SignInEventBody(BaseModel):
    kind: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    key: str | None = None
    dy: int | None = None
    url: str | None = None


class SignInSubmitBody(BaseModel):
    email: str
    password: str


class ErDecisionBody(BaseModel):
    decision: str
    cik: int | None = None
    lei: str | None = None
    name: str | None = None


class HypothesisReviewBody(BaseModel):
    verdict: str


class ContradictionResolveBody(BaseModel):
    keep: str


class MergeBody(BaseModel):
    into: uuid.UUID


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
            "alerts_unread": con.execute(
                "select count(*) n from alert where read_at is null"
            ).fetchone()["n"],
            "contradictions_open": con.execute(
                "select count(*) n from contradiction where status='open'"
            ).fetchone()["n"],
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
                         "connector": r["connector"],
                         "label": source_label(r["connector"], r["config"]),
                         "status": r["status"],
                         "last_polled": iso(r["last_polled"]),
                         "last_error": r["last_error"],
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
                   entity: uuid.UUID | None = None, status: str = "asserted",
                   limit: int = 50, offset: int = 0, con=Depends(get_con)):
        if source_type and source_type not in SOURCE_TYPES:
            raise HTTPException(
                400, "source_type must be edgar, podcast, rss or manual")
        if status not in ("asserted", "superseded", "retracted", "all"):
            raise HTTPException(
                400, "status must be asserted, superseded, retracted or all")
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        where, params = claims_where(q.strip(), predicate, source_type,
                                     stance, sector, days, entity, status)
        total = con.execute(
            f"select count(*) n {CLAIMS_FROM} {where}", params).fetchone()["n"]
        rows = con.execute(
            f"{CLAIMS_SELECT} {CLAIMS_FROM} {where} "
            f"order by c.observed_at desc, c.claim_id "
            f"limit %(limit)s offset %(offset)s",
            {**params, "limit": limit, "offset": offset}).fetchall()
        canon = predicate_canon_map(con)
        return {"total": total, "claims": [claim_json(r, canon) for r in rows]}

    # ------------------------------------------------------------ entities

    @app.get("/api/entities")
    def api_entities(q: str = "", limit: int = 50, con=Depends(get_con)):
        q = q.strip()
        limit = max(1, min(limit, 200))
        sql = ENTITIES_SQL.format(extra=ENTITIES_WHERE if q else "")
        params = {"limit": limit, **({"pat": f"%{q}%"} if q else {})}
        return [{"entity_id": r["entity_id"], "name": r["canonical_name"],
                 "kind": r["kind"], "registry": registry_short(r["registry_refs"]),
                 "claims": r["claims"]}
                for r in con.execute(sql, params)]

    @app.get("/api/entity/{entity_id}")
    def api_entity(entity_id: uuid.UUID, con=Depends(get_con)):
        # a merged-away id serves its canonical entity's page (spec v3 §7.1)
        canonical = db.entity_canonical_for(con, entity_id)
        ent = con.execute("select * from entity where entity_id=%s",
                          (canonical,)).fetchone()
        if ent is None:
            raise HTTPException(404, "No such entity.")
        merged_from = [{"entity_id": r["a"], "name": r["canonical_name"]}
                       for r in con.execute(
                           "select sa.a, e.canonical_name from entity_same_as sa "
                           "join entity e on e.entity_id = sa.a "
                           "where sa.b=%s and sa.status='active' "
                           "order by e.canonical_name", (canonical,))]
        aliases = [r["alias"] for r in con.execute(
            "select distinct alias from entity_alias where entity_id=%s "
            "order by alias", (canonical,))]
        where, params = claims_where(entity=canonical)
        claims = con.execute(
            f"{CLAIMS_SELECT} {CLAIMS_FROM} {where} "
            f"order by c.observed_at desc, c.claim_id", params).fetchall()
        canon = predicate_canon_map(con)
        edges = con.execute(EDGES_SQL, {"eid": canonical}).fetchall()

        def edge_json(e):
            return {"edge_id": e["edge_id"],
                    "peer": {"entity_id": e["peer_id"], "name": e["peer_name"]},
                    "predicate": e["predicate"],
                    "direction": "out" if e["src"] == canonical else "in",
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
            "merged_from": merged_from,
            "aliases": aliases,
            "claims": [claim_json(c, canon) for c in claims],
            "edges": {
                "asserted": [edge_json(e) for e in edges
                             if e["origin"] == "asserted"],
                "inferred": [edge_json(e) for e in edges
                             if e["origin"] == "inferred"],
            },
        }

    @app.post("/api/entities/{entity_id}/merge")
    def api_merge_entity(entity_id: uuid.UUID, body: MergeBody,
                         con=Depends(get_con)):
        # Merges are rare human admin actions: serialize them all on one
        # transaction-scoped advisory lock so every guard below runs against
        # committed state — two concurrent opposite merges would otherwise
        # both pass the plain-read checks and insert an active same_as cycle.
        con.execute(
            "select pg_advisory_xact_lock(hashtext('entity_same_as:merge'))")
        if body.into == entity_id:
            raise HTTPException(400, "Cannot merge an entity into itself.")
        ids = [entity_id, body.into]
        found = {r["entity_id"] for r in con.execute(
            "select entity_id from entity where entity_id = any(%s)", (ids,))}
        if found != set(ids):
            raise HTTPException(404, "No such entity.")
        # chains resolve at write (design §2 rule 5): a merged-away target
        # redirects to its canonical, keeping the same_as map single-hop
        target = db.entity_canonical_for(con, body.into)
        if target == entity_id:
            raise HTTPException(400, "Cannot merge an entity into itself.")
        if con.execute("select 1 from entity_same_as "
                       "where a=%s and status='active'",
                       (entity_id,)).fetchone():
            raise HTTPException(409, "Entity is already merged.")
        # one merge_group per request ties the direct row and every chained
        # re-point to this operation, so unmerge can undo all of it
        merge_group = uuid.uuid4()
        try:
            con.execute(
                "insert into entity_same_as (a, b, decided_by, merge_group) "
                "values (%s, %s, 'web', %s)", (entity_id, target, merge_group))
            # entities absorbed into this one re-point to the new canonical
            for r in con.execute(
                    "select a from entity_same_as where b=%s and status='active'",
                    (entity_id,)).fetchall():
                con.execute(
                    "update entity_same_as set status='reverted' "
                    "where a=%s and b=%s and status='active'", (r["a"], entity_id))
                con.execute(
                    "insert into entity_same_as (a, b, decided_by, note, "
                    "merge_group) values (%s, %s, 'web', 'chained merge', %s)",
                    (r["a"], target, merge_group))
        except pg_errors.UniqueViolation:
            # backstop (entity_same_as_one_active): a double submit that
            # slipped past the check surfaces like the sequential path
            raise HTTPException(409, "Entity is already merged.")
        con.commit()
        return {"ok": True, "into": target}

    @app.post("/api/entities/{entity_id}/unmerge")
    def api_unmerge_entity(entity_id: uuid.UUID, con=Depends(get_con)):
        row = con.execute(
            "update entity_same_as set status='reverted' "
            "where a=%s and status='active' returning merge_group",
            (entity_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No active merge for that entity.")
        # Undo the whole merge operation, not just the direct link: the merge
        # that created this row also re-pointed every entity previously merged
        # into entity_id (chained rows, same merge_group). Restore each one
        # that is STILL ACTIVE back to entity_id — rows a human already
        # unmerged separately stay unmerged. The restored row keeps the
        # merge_group of the link it restores, so multi-level chains keep
        # unwinding one operation at a time.
        restored = []
        if row["merge_group"] is not None:
            for r in con.execute(
                    "select a from entity_same_as where merge_group=%s "
                    "and a<>%s and status='active'",
                    (row["merge_group"], entity_id)).fetchall():
                con.execute(
                    "update entity_same_as set status='reverted' "
                    "where a=%s and status='active'", (r["a"],))
                orig = con.execute(
                    "select merge_group from entity_same_as "
                    "where a=%s and b=%s and status='reverted' "
                    "order by created_at desc limit 1",
                    (r["a"], entity_id)).fetchone()
                con.execute(
                    "insert into entity_same_as (a, b, decided_by, note, "
                    "merge_group) values (%s, %s, 'web', 'unmerge restore', %s)",
                    (r["a"], entity_id,
                     orig["merge_group"] if orig else None))
                restored.append(r["a"])
        # Heal supersessions the merge caused (design §2 rule 5, §15): a
        # same-lineage auto-resolve whose two claims belong to DIFFERENT raw
        # identities can only have grouped them through a merge — if either
        # side's raw identity is being unmerged here, the supersession is
        # merge damage: restore the losing claim and re-open the pair.
        affected = [entity_id] + restored
        for t in con.execute(
                "select t.contradiction_id, t.claim_a, t.claim_b, t.resolution "
                "from contradiction t "
                "join claim ca on ca.claim_id = t.claim_a "
                "join claim cb on cb.claim_id = t.claim_b "
                "join mention ma on ma.event_id = ca.event_id "
                "and ma.surface = ca.subject_surface "
                "join mention mb on mb.event_id = cb.event_id "
                "and mb.surface = cb.subject_surface "
                "where t.status = 'auto_resolved' "
                "and t.resolution->>'mode' = 'same_lineage' "
                "and ma.resolved_entity is distinct from mb.resolved_entity "
                "and (ma.resolved_entity = any(%s) "
                "or mb.resolved_entity = any(%s))",
                (affected, affected)).fetchall():
            kept = (t["resolution"] or {}).get("kept")
            loser = t["claim_b"] if str(t["claim_a"]) == kept else t["claim_a"]
            con.execute(
                "update claim set status='asserted', superseded_by=null "
                "where claim_id=%s and status='superseded'", (loser,))
            con.execute(
                "update contradiction set status='open', resolved_at=null, "
                "resolution = coalesce(resolution, '{}'::jsonb) || %s "
                "where contradiction_id=%s",
                (Jsonb({"reopened_by": "unmerge",
                        "reopened_at": datetime.now(timezone.utc).isoformat()}),
                 t["contradiction_id"]))
        con.commit()
        return {"ok": True}

    # ------------------------------------------------------------ hypotheses

    @app.get("/api/hypotheses")
    def api_hypotheses(state: str = "", con=Depends(get_con)):
        sql = HYPOTHESES_SQL.format(
            where="where state = %(state)s" if state else "")
        hypotheses = con.execute(sql, {"state": state}).fetchall()
        names = entity_names(
            con, [sid for h in hypotheses for sid in h["subjects"]])
        return [{"hypothesis_id": h["hypothesis_id"], "type": h["type"],
                 "subjects": [{"entity_id": sid,
                               "name": names.get(sid, str(sid)[:8])}
                              for sid in h["subjects"]],
                 "state": h["state"], "score": h["score"],
                 "rationale": h["rationale"],
                 "created_at": iso(h["created_at"]),
                 "updated_at": iso(h["updated_at"])}
                for h in hypotheses]

    @app.get("/api/hypotheses/{hypothesis_id}")
    def api_hypothesis(hypothesis_id: uuid.UUID, con=Depends(get_con)):
        h = con.execute("select * from hypothesis where hypothesis_id=%s",
                        (hypothesis_id,)).fetchone()
        if h is None:
            raise HTTPException(404, "No such hypothesis.")
        names = entity_names(con, h["subjects"])
        evidence = []
        if h["evidence"]:
            canon = predicate_canon_map(con)
            rows = con.execute(
                f"{CLAIMS_SELECT} {CLAIMS_FROM} where c.claim_id = any(%s) "
                f"order by c.observed_at desc, c.claim_id",
                (h["evidence"],)).fetchall()
            evidence = [claim_json(r, canon) for r in rows]
        verifier = None
        if h["state"] == "promoted":
            edge = con.execute(
                "select evidence_trail from edge where origin='inferred' "
                "and evidence_trail->>'hypothesis_id' = %s",
                (str(hypothesis_id),)).fetchone()
            verifier = edge["evidence_trail"] if edge else None
        return {
            "hypothesis": {"hypothesis_id": h["hypothesis_id"],
                           "type": h["type"], "statement": h["statement"],
                           "rationale": h["rationale"],
                           "test_plan": h["test_plan"], "score": h["score"],
                           "state": h["state"], "confidence": h["confidence"],
                           "wake_conditions": h["wake_conditions"],
                           "parked_at": iso(h["parked_at"]),
                           "created_at": iso(h["created_at"]),
                           "updated_at": iso(h["updated_at"])},
            "subjects": [{"entity_id": sid,
                          "name": names.get(sid, str(sid)[:8])}
                         for sid in h["subjects"]],
            "evidence": evidence,
            "lineages": len(h["lineages"] or []),
            "verifier": verifier,
            "history": h["history"],
        }

    @app.post("/api/hypotheses/{hypothesis_id}/review")
    def api_review_hypothesis(hypothesis_id: uuid.UUID,
                              body: HypothesisReviewBody, con=Depends(get_con)):
        if body.verdict not in ("accept", "reject"):
            raise HTTPException(400, "Verdict must be accept or reject.")
        h = con.execute("select state from hypothesis where hypothesis_id=%s",
                        (hypothesis_id,)).fetchone()
        if h is None:
            raise HTTPException(404, "No such hypothesis.")
        state = h["state"]
        if state in ("triaged", "parked") and body.verdict == "accept":
            raise HTTPException(400, "Accept applies to promoted hypotheses only.")
        if state not in ("promoted", "triaged", "parked"):
            raise HTTPException(
                400, "Review applies to promoted, triaged or parked hypotheses.")
        if body.verdict == "reject":
            # state guard: the verdict was rendered against the state read
            # above; if the worker moved the row meanwhile, apply nothing
            # (no state flip, no label, no edge archive) and ask for a reload
            cur = con.execute(
                "update hypothesis set state='refuted', updated_at=now(), "
                "history = history || %s where hypothesis_id=%s and state=%s",
                (Jsonb([{"at": datetime.now(timezone.utc).isoformat(),
                         "from": state, "to": "refuted",
                         "note": "rejected in review"}]), hypothesis_id, state))
            if cur.rowcount == 0:
                raise HTTPException(409, "State changed; reload.")
            if state == "promoted":
                # the promoted link loses its human backing; archive, not delete
                con.execute(
                    "update edge set archived=true where origin='inferred' "
                    "and evidence_trail->>'hypothesis_id' = %s",
                    (str(hypothesis_id),))
            state = "refuted"
        con.execute(
            "insert into review_label (kind, target, verdict) values "
            "('hypothesis', %s, %s)", (hypothesis_id, body.verdict))
        con.commit()
        return {"ok": True, "state": state}

    # ------------------------------------------------------------ alerts

    @app.get("/api/alerts")
    def api_alerts(limit: int = 50, unread: bool = False, con=Depends(get_con)):
        limit = max(1, min(limit, 200))
        unread_n = con.execute(
            "select count(*) n from alert where read_at is null"
        ).fetchone()["n"]
        rows = con.execute(
            "select * from alert "
            + ("where read_at is null " if unread else "")
            + "order by created_at desc, alert_id limit %s", (limit,)).fetchall()
        names = entity_names(con, [e for r in rows for e in r["entity_ids"]])
        return {
            "unread": unread_n,
            "alerts": [{"alert_id": r["alert_id"], "kind": r["kind"],
                        "title": r["title"], "body": r["body"],
                        "entities": [{"entity_id": e,
                                      "name": names.get(e, str(e)[:8])}
                                     for e in r["entity_ids"]],
                        "event_id": r["event_id"],
                        "hypothesis_id": r["hypothesis_id"],
                        "created_at": iso(r["created_at"]),
                        "read_at": iso(r["read_at"])} for r in rows],
        }

    @app.post("/api/alerts/read-all")
    def api_alerts_read_all(con=Depends(get_con)):
        cur = con.execute(
            "update alert set read_at=now() where read_at is null")
        con.commit()
        return {"ok": True, "marked": cur.rowcount}

    @app.post("/api/alerts/{alert_id}/read")
    def api_alert_read(alert_id: uuid.UUID, con=Depends(get_con)):
        # idempotent: a second read keeps the original read_at
        row = con.execute(
            "update alert set read_at=coalesce(read_at, now()) "
            "where alert_id=%s returning alert_id", (alert_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such alert.")
        con.commit()
        return {"ok": True}

    @app.get("/api/digest")
    def api_digest(hours: int = 24, con=Depends(get_con)):
        # the morning digest (design §11.4), computed at query time
        hours = max(1, min(hours, 168))
        since = con.execute("select now() - make_interval(hours => %s) as t",
                            (hours,)).fetchone()["t"]
        claims = con.execute(
            "select count(*) n from claim where observed_at >= %s",
            (since,)).fetchone()["n"]
        events = {r["k"]: r["n"] for r in con.execute(
            "select connector as k, count(*) n from event "
            "where fetched_at >= %s group by 1", (since,))}
        top = con.execute(
            """
            with recent as (
                select subject_entity as ent from claim
                 where observed_at >= %(since)s and subject_entity is not null
                union all
                select object_entity from claim
                 where observed_at >= %(since)s and object_entity is not null
            )
            select e.entity_id, e.canonical_name, count(*) as claims,
                   exists (select 1 from watchlist w where w.active
                            and w.ticker = e.registry_refs->>'ticker') as watch
            from recent r join entity e on e.entity_id = r.ent
            group by e.entity_id, e.canonical_name, e.registry_refs
            order by watch desc, claims desc, e.canonical_name
            limit 8
            """, {"since": since}).fetchall()
        # promotions and wakes in the window are exactly their alerts
        promoted, woke = [], []
        for r in con.execute(
                "select kind, hypothesis_id, title from alert "
                "where kind in ('promoted_link', 'hypothesis_wake') "
                "and created_at >= %s order by created_at desc", (since,)):
            (promoted if r["kind"] == "promoted_link" else woke).append(
                {"hypothesis_id": r["hypothesis_id"], "title": r["title"]})
        return {
            "since": iso(since),
            "claims": claims,
            "events": events,
            "top_entities": [{"entity_id": r["entity_id"],
                              "name": r["canonical_name"],
                              "claims": r["claims"]} for r in top],
            "promoted": promoted,
            "woke": woke,
            "contradictions_open": con.execute(
                "select count(*) n from contradiction where status='open'"
            ).fetchone()["n"],
            "failed_events": con.execute(
                "select count(*) n from event where status='failed'"
            ).fetchone()["n"],
        }

    # ------------------------------------------------------------ review

    @app.get("/api/er-queue")
    def api_er_queue(status: str = "pending", limit: int = 50,
                     con=Depends(get_con)):
        if status not in ("pending", "decided", "failed"):
            raise HTTPException(400, "Status must be pending, decided or failed.")
        limit = max(1, min(limit, 200))
        pending = con.execute(
            "select count(*) n from er_queue where status='pending'"
        ).fetchone()["n"]
        rows = con.execute(
            f"{ER_QUEUE_SQL} where q.status = %s order by q.created_at limit %s",
            (status, limit)).fetchall()

        texts = {}

        def context_for(r):
            if r["event_id"] not in texts:
                try:
                    texts[r["event_id"]] = artifacts.get(
                        r["artifact_uri"]).decode("utf-8", errors="replace")
                except OSError:
                    texts[r["event_id"]] = ""
            return adjudicate._snippet(texts[r["event_id"]], r["surface"],
                                       width=150)

        recent = con.execute(
            f"{ER_QUEUE_SQL} where q.status = 'decided' "
            f"order by q.decided_at desc nulls last limit 20").fetchall()
        return {
            "pending": pending,
            "items": [{"mention_id": r["mention_id"], "surface": r["surface"],
                       "context": context_for(r), "doc_title": r["doc_title"],
                       "source_name": r["source_name"],
                       "connector": r["connector"],
                       "candidates": r["candidates"],
                       "created_at": iso(r["created_at"]),
                       "passes": (r["decision"] or {}).get("passes") or 0}
                      for r in rows],
            "recent": [{"mention_id": r["mention_id"], "surface": r["surface"],
                        "doc_title": r["doc_title"],
                        "source_name": r["source_name"],
                        "connector": r["connector"],
                        "decision": r["decision"],
                        "decided_at": iso(r["decided_at"])} for r in recent],
        }

    @app.post("/api/er-queue/{mention_id}/decide")
    def api_er_decide(mention_id: uuid.UUID, body: ErDecisionBody,
                      con=Depends(get_con)):
        if body.decision not in ("match", "new_entity", "not_a_company"):
            raise HTTPException(
                400, "Decision must be match, new_entity or not_a_company.")
        row = con.execute(
            "select q.mention_id, q.candidates, m.surface from er_queue q "
            "join mention m on m.mention_id = q.mention_id "
            "where q.mention_id=%s and q.status='pending'",
            (mention_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No pending review for that mention.")
        d = {"decision": body.decision, "cik": body.cik, "lei": body.lei,
             "entity_hint": body.name, "confidence": 1.0, "decided_by": "web"}
        decision, entity_id = adjudicate.apply_decision(con, row, d)
        if decision == "already_decided":
            # the worker's adjudicate pass claimed the row between our
            # pending-check and the write; that verdict stands
            raise HTTPException(
                409, "Mention was decided concurrently; reload.")
        if decision != body.decision:
            # apply_decision degraded the match; nothing was written
            raise HTTPException(400, "Match needs a registry id from the candidates.")
        con.execute(
            "insert into review_label (kind, target, verdict) values "
            "('er', %s, %s)", (mention_id, body.decision))
        resolve.link_claims(con)
        con.commit()
        return {"ok": True, "entity_id": entity_id}

    @app.get("/api/contradictions")
    def api_contradictions(status: str = "open", con=Depends(get_con)):
        if status not in ("open", "auto_resolved", "resolved", "dismissed"):
            raise HTTPException(
                400, "Status must be open, auto_resolved, resolved or dismissed.")
        rows = con.execute(
            "select t.contradiction_id, t.subject_entity, t.predicate_canon, "
            "t.kind, t.status, t.claim_a, t.claim_b, t.created_at, "
            "e.canonical_name from contradiction t "
            "join entity e on e.entity_id = t.subject_entity "
            "where t.status = %s order by t.created_at desc",
            (status,)).fetchall()
        claim_ids = sorted({c for r in rows for c in (r["claim_a"], r["claim_b"])})
        claims = {}
        if claim_ids:
            canon = predicate_canon_map(con)
            claims = {r["claim_id"]: claim_json(r, canon) for r in con.execute(
                f"{CLAIMS_SELECT} {CLAIMS_FROM} where c.claim_id = any(%s)",
                (claim_ids,))}
        return [{"contradiction_id": r["contradiction_id"],
                 "subject": {"entity_id": r["subject_entity"],
                             "name": r["canonical_name"]},
                 "predicate_canon": r["predicate_canon"],
                 "kind": r["kind"], "status": r["status"],
                 "claims": {"a": claims.get(r["claim_a"]),
                            "b": claims.get(r["claim_b"])},
                 "created_at": iso(r["created_at"])} for r in rows]

    @app.post("/api/contradictions/{contradiction_id}/resolve")
    def api_resolve_contradiction(contradiction_id: uuid.UUID,
                                  body: ContradictionResolveBody,
                                  con=Depends(get_con)):
        if body.keep not in ("a", "b", "none"):
            raise HTTPException(400, "Keep must be a, b or none.")
        # the initial read serves the 404 and the claim ids; every state
        # transition below is guarded, so two concurrent resolves can never
        # both apply (the loser of the race gets a 409)
        t = con.execute(
            "select * from contradiction where contradiction_id=%s",
            (contradiction_id,)).fetchone()
        if t is None:
            raise HTTPException(404, "No such contradiction.")
        if t["status"] != "open":
            raise HTTPException(409, "Already resolved.")
        if body.keep == "none":
            cur = con.execute(
                "update contradiction set status='dismissed', resolved_at=now() "
                "where contradiction_id=%s and status='open'",
                (contradiction_id,))
            if cur.rowcount == 0:
                con.rollback()
                raise HTTPException(409, "Already resolved.")
        else:
            winner = t["claim_a"] if body.keep == "a" else t["claim_b"]
            loser = t["claim_b"] if body.keep == "a" else t["claim_a"]
            statuses = {r["claim_id"]: r["status"] for r in con.execute(
                "select claim_id, status from claim where claim_id = any(%s)",
                ([t["claim_a"], t["claim_b"]],))}
            if (statuses.get(winner) != "asserted"
                    or statuses.get(loser) != "asserted"):
                # stale: a side was already superseded elsewhere (another
                # contradiction's resolution, an auto-resolve). Touching the
                # claims now would rewrite an existing superseded_by pointer
                # or endorse a dead winner — dismiss instead, recording why,
                # and skip the review label (the verdict was not applied).
                dead = winner if statuses.get(winner) != "asserted" else loser
                cur = con.execute(
                    "update contradiction set status='dismissed', "
                    "resolution=%s, resolved_at=now() "
                    "where contradiction_id=%s and status='open'",
                    (Jsonb({"reason": "stale", "superseded_claim": str(dead)}),
                     contradiction_id))
                if cur.rowcount == 0:
                    con.rollback()
                    raise HTTPException(409, "Already resolved.")
                con.commit()
                return {"ok": True, "stale": True}
            # claim the resolution FIRST (guarded), then supersede: a raced
            # request 409s here instead of writing a second supersession
            cur = con.execute(
                "update contradiction set status='resolved', resolution=%s, "
                "resolved_at=now() where contradiction_id=%s and status='open'",
                (Jsonb({"kept": str(winner)}), contradiction_id))
            if cur.rowcount == 0:
                con.rollback()
                raise HTTPException(409, "Already resolved.")
            # append-only: the loser is superseded, never deleted (design §2);
            # the status guard keeps a concurrent supersession's audit pointer
            # intact — if it fires, this resolution must not claim the write
            cur = con.execute(
                "update claim set status='superseded', superseded_by=%s "
                "where claim_id=%s and status='asserted'", (winner, loser))
            if cur.rowcount == 0:
                con.rollback()
                raise HTTPException(
                    409, "Claim was superseded concurrently; reload.")
        con.execute(
            "insert into review_label (kind, target, verdict) values "
            "('contradiction', %s, %s)", (contradiction_id, body.keep))
        con.commit()
        return {"ok": True}

    # ------------------------------------------------------------ sources

    @app.get("/api/sources")
    def api_sources(con=Depends(get_con)):
        sites_set = credential_sites(con)
        return {
            "watchlist": [{"ticker": r["ticker"], "name": r["name"],
                           "sector": r["sector"], "industry": r["industry"],
                           "exchange": r["exchange"], "country": r["country"],
                           "active": r["active"], "events": r["events"],
                           "cik": r["cik"]}
                          for r in con.execute(WATCHLIST_SQL)],
            "sources": [source_json(r, sites_set)
                        for r in con.execute(SOURCES_SQL)],
            "links": [link_json(r) for r in con.execute(LINKS_SQL)],
            "credentials": credentials.status(con),
        }

    @app.post("/api/watchlist")
    def api_add_ticker(body: WatchlistBody, con=Depends(get_con)):
        ticker = watchlist.clean_ticker(body.ticker)
        if not ticker:
            raise HTTPException(400, "Enter a ticker.")
        sector = (body.sector or "").strip() or None
        row = watchlist.resolve_and_upsert(con, ticker, "web", sector)
        if row is None:
            raise HTTPException(
                400, f"Unknown ticker {ticker}. Yahoo Finance and the SEC "
                     f"registry do not list it.")
        con.commit()
        return {"ok": True, "ticker": row["ticker"], "name": row["name"],
                "sector": row["sector"], "exchange": row["exchange"]}

    @app.get("/api/watchlist/search")
    def api_watchlist_search(q: str = ""):
        # the suggestion box is a convenience: a short query and any Yahoo
        # failure both answer with an empty list, never an error
        q = (q or "").strip()
        if len(q) < 2:
            return []
        return watchlist.search(q)

    @app.post("/api/watchlist/{ticker}/toggle")
    def api_toggle_ticker(ticker: str, con=Depends(get_con)):
        row = con.execute(
            "update watchlist set active = not active where ticker=%s "
            "returning active", (ticker,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such ticker.")
        con.commit()
        return {"ok": True, "active": row["active"]}

    def resolved(con, raw_url):
        """Route a pasted URL, or 400 when it is not a link at all."""
        # link_queue.url carries a plain btree unique index, which refuses a
        # value past roughly 2.7 KB; without this the insert raises and the
        # page gets a 500 instead of the copy below
        if len(raw_url or "") > 2048:
            raise HTTPException(
                400, "That does not look like a link. Paste a full address.")
        url = router.normalize((raw_url or "").strip())
        host = ""
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            host = ""
        # a single-label host is a container or LAN name, never a public site
        if not url or fetch.blocked_host(host):
            raise HTTPException(
                400, "That does not look like a link. Paste a full address.")
        return router.resolve(con, url)

    @app.post("/api/sources/resolve")
    def api_resolve_source(body: ResolveBody, con=Depends(get_con)):
        return resolved(con, body.url).as_dict()

    @app.post("/api/sources")
    def api_add_source(body: SourceBody, con=Depends(get_con)):
        res = resolved(con, body.url)
        if res.kind == "unsupported":
            raise HTTPException(400, res.message or router.NO_FEED)
        existing = router.duplicate_of(con, res)
        if existing:
            raise HTTPException(409, f"Already added as {existing}.")
        if res.one_off:
            if not res.link_kind:
                raise HTTPException(400, router.NO_FEED)
            # a pasted article, post or video is fetched once, not polled;
            # everything but video runs now so the page can show the outcome
            row = links.enqueue(con, res.url, res.link_kind, res.site, "web")
            con.commit()          # the row survives even if the fetch blows up
            if not row.get("created") and row["status"] == "done":
                # pasted a second time: the vault already holds it, and the
                # queue row is reported as such instead of as a fresh add
                return {"ok": True, "kind": "link",
                        "link": {**link_json(row), "status": "duplicate"}}
            if res.link_kind != "youtube_video":
                try:
                    row = links.process_one(con, row["link_id"])
                    con.commit()
                except Exception as e:
                    con.rollback()
                    print(f"link {row['link_id']}: {e!r}")
            return {"ok": True, "kind": "link", "link": link_json(row)}
        name = (body.name or "").strip() or res.name or \
            (urlsplit(res.url).hostname or res.url)
        if res.connector == "podcast" and not name.startswith("podcast:"):
            name = f"podcast:{name}"
        new = con.execute(
            "insert into source (name, connector, url, config, status, "
            "added_by) values (%s, %s, %s, %s, 'active', 'web') "
            "returning source_id",
            (name, res.connector, res.feed_url or res.url,
             Jsonb(res.config or {}))).fetchone()
        con.commit()
        row = con.execute(SOURCE_ROW_SQL, (new["source_id"],)).fetchone()
        return {"ok": True, "kind": "source",
                "source": source_json(row, credential_sites(con))}

    @app.post("/api/sources/{source_id}/toggle")
    def api_toggle_source(source_id: uuid.UUID, con=Depends(get_con)):
        # pause/resume without a schema change: active <-> demoted
        row = con.execute(
            "update source set status = case when status='active' "
            "then 'demoted' else 'active' end where source_id=%s "
            "and status <> 'dropped' returning status", (source_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such source.")
        con.commit()
        return {"ok": True, "status": row["status"]}

    @app.delete("/api/sources/{source_id}")
    def api_drop_source(source_id: uuid.UUID, con=Depends(get_con)):
        # removed from the page, never polled again; the rows stay for lineage
        row = con.execute(
            "update source set status='dropped' where source_id=%s "
            "returning source_id", (source_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such source.")
        con.commit()
        return {"ok": True}

    @app.post("/api/links/{link_id}/retry")
    def api_retry_link(link_id: uuid.UUID, con=Depends(get_con)):
        row = con.execute(
            "update link_queue set status='queued', attempts=0, error=null, "
            "updated_at=now() where link_id=%s returning *",
            (link_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such link.")
        con.commit()              # queued again even if the fetch blows up
        try:
            row = links.process_one(con, link_id)
            con.commit()
        except Exception as e:
            con.rollback()
            print(f"link {link_id}: {e!r}")
        return link_json(row)

    # ------------------------------------------------------------ sign-ins

    @app.put("/api/credentials/{site}")
    def api_set_credential(site: str, body: CredentialBody,
                           con=Depends(get_con)):
        if site not in credentials.SITES:
            raise HTTPException(400, "No sign-in for that site.")
        try:
            credentials.set(con, site, body.value or "", body.note)
        except credentials.InvalidCredential as e:
            raise HTTPException(400, str(e)) from None
        # the sign-in is what those rows were waiting for
        credential_saved(con, site)
        con.commit()
        return {"ok": True, "site": site, "set": True}

    @app.delete("/api/credentials/{site}")
    def api_delete_credential(site: str, con=Depends(get_con)):
        if site not in credentials.SITES:
            raise HTTPException(400, "No sign-in for that site.")
        credentials.delete(con, site)
        con.commit()
        return {"ok": True}

    @app.post("/api/credentials/{site}/test")
    def api_test_credential(site: str, body: CredentialTestBody | None = None,
                            con=Depends(get_con)):
        if site not in credentials.SITES:
            raise HTTPException(400, "No sign-in for that site.")
        try:
            ok, message = probes.run(con, site, url=body.url if body else None)
        except probes.BadLink as e:
            # the link could not test anything, so no check is recorded: the
            # badge keeps saying what the last real check found. A session the
            # probe minted on the way (substack_session.py) is a cache, kept.
            con.commit()
            raise HTTPException(400, str(e)) from None
        credentials.record_check(con, site, ok, message)
        con.commit()
        return {"ok": ok, "message": message}

    # ---------------------------------------------- sign in from the browser
    #
    # These drive one page in the sidecar (graph/signin.py). They are async
    # because the Playwright objects belong to the event loop that made them,
    # and the sessions live in this process: one uvicorn worker, at most one
    # session per site. `submit` fills the login form and waits for the cookie;
    # the other five are the live-view fallback it drops into.

    @app.post("/api/signin/{site}")
    async def api_signin_start(site: str):
        return await signin.start(site)

    @app.post("/api/signin/{site}/submit")
    async def api_signin_submit(site: str, body: SignInSubmitBody,
                                con=Depends(get_con)):
        return await signin.submit(site, body.email, body.password, con)

    @app.get("/api/signin/{session_id}")
    async def api_signin_frame(session_id: str):
        return await signin.frame(session_id)

    @app.post("/api/signin/{session_id}/event")
    async def api_signin_event(session_id: str, body: SignInEventBody):
        return await signin.event(session_id, body.model_dump())

    @app.post("/api/signin/{session_id}/finish")
    async def api_signin_finish(session_id: str, con=Depends(get_con)):
        return await signin.finish(session_id, con)

    @app.delete("/api/signin/{session_id}")
    async def api_signin_close(session_id: str):
        return await signin.close(session_id)

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

    @app.post("/api/events/retry-all")
    def api_retry_all_events(con=Depends(get_con)):
        cur = con.execute(
            "update event set status='pending', attempts=0 "
            "where status='failed'")
        con.commit()
        return {"ok": True, "retried": cur.rowcount}

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
