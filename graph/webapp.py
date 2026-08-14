"""Read-only web UI over the graph (build spec: graph/webapp.py).

Three server-rendered pages: entity search/list, entity detail (claims timeline
+ edges with query-time relevance via edge_relevance()), hypotheses. Jinja2 via
DictLoader, self-contained CSS, no external assets. Run with `graph serve`
(uvicorn graph.webapp:app).
"""
import json
import os
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from jinja2 import DictLoader, Environment, select_autoescape

from . import db

# ---------------------------------------------------------------- templates

BASE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}CAF graph{% endblock %}</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
       color: #22272e; background: #f7f7f5; }
header { background: #22272e; }
nav { max-width: 1000px; margin: 0 auto; padding: 10px 20px; display: flex; gap: 18px; }
nav a { color: #d0d5dc; text-decoration: none; font-size: 14px; }
nav a.brand { color: #fff; font-weight: 600; }
nav a:hover { color: #fff; }
main { max-width: 1000px; margin: 0 auto; padding: 24px 20px 60px; }
h1 { font-size: 24px; margin: 0 0 12px; }
h2 { font-size: 17px; margin: 32px 0 8px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border: 1px solid #e2e2de; font-size: 14px; }
th { text-align: left; font-size: 12px; text-transform: uppercase;
     letter-spacing: .04em; color: #6a7178; background: #f0f0ec;
     padding: 7px 10px; border-bottom: 1px solid #e2e2de; }
td { padding: 7px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; white-space: nowrap; }
a { color: #1660a6; text-decoration: none; }
a:hover { text-decoration: underline; }
.muted { color: #8a9097; }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 9px;
       background: #e7ecf2; color: #3c4854; margin-right: 6px; }
.tag.warn { background: #f6e3c8; color: #7a4d12; }
.quote { color: #555c63; font-size: 13px; font-style: italic; }
.empty { color: #8a9097; font-style: italic; }
.meta { margin: 2px 0; }
.ref { margin-right: 10px; font-size: 13px; color: #555c63; }
form.search { margin: 0 0 16px; display: flex; gap: 8px; }
input[type=search] { flex: 1; max-width: 420px; padding: 6px 10px;
                     border: 1px solid #ccc; border-radius: 6px;
                     font-size: 14px; background: #fff; }
button { padding: 6px 14px; border: 1px solid #ccc; border-radius: 6px;
         background: #fff; cursor: pointer; font-size: 14px; }
code { background: #f0f0ec; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
.tablewrap { overflow-x: auto; }
</style>
</head>
<body>
<header>
<nav>
  <a class="brand" href="{{ root }}/">CAF graph</a>
  <a href="{{ root }}/">entities</a>
  <a href="{{ root }}/hypotheses">hypotheses</a>
</nav>
</header>
<main>
{% block body %}{% endblock %}
</main>
</body>
</html>
"""

INDEX = """{% extends "base.html" %}
{% block title %}Entities — CAF graph{% endblock %}
{% block body %}
<h1>Entities</h1>
<form class="search" method="get" action="{{ root }}/">
  <input type="search" name="q" value="{{ q }}" placeholder="search canonical name or alias">
  <button>Search</button>
</form>
<div class="tablewrap">
<table>
<thead><tr><th>Name</th><th>Kind</th><th class="num">Claims</th></tr></thead>
<tbody>
{% for e in entities %}
<tr>
  <td><a href="{{ root }}/entity/{{ e.entity_id }}">{{ e.canonical_name }}</a></td>
  <td><span class="tag">{{ e.kind }}</span></td>
  <td class="num">{{ e.claims }}</td>
</tr>
{% else %}
<tr><td colspan="3" class="empty">no entities{% if q %} matching "{{ q }}"{% endif %}</td></tr>
{% endfor %}
</tbody>
</table>
</div>
{% endblock %}
"""

ENTITY = """{% extends "base.html" %}
{% block title %}{{ ent.canonical_name }} — CAF graph{% endblock %}
{% block body %}
<h1>{{ ent.canonical_name }}</h1>
<p class="meta">
  <span class="tag">{{ ent.kind }}</span>
  {% for k, v in (ent.registry_refs or {}).items() %}<span class="ref">{{ k }}: {{ v }}</span>{% endfor %}
</p>
{% if aliases %}
<p class="meta">aliases:
{% for a in aliases %}{{ a.alias }} <span class="muted">({{ a.alias_kind }})</span>{% if not loop.last %}, {% endif %}{% endfor %}
</p>
{% endif %}

<h2>Claims ({{ claims|length }})</h2>
<div class="tablewrap">
<table>
<thead><tr>
  <th>Subject</th><th>Predicate</th><th>Object</th><th>Evidence</th>
  <th class="num">Conf.</th><th>Source</th><th class="num">Published</th>
</tr></thead>
<tbody>
{% for c in claims %}
<tr>
  <td>{% if c.subject_entity %}<a href="{{ root }}/entity/{{ c.subject_entity }}">{{ c.subject_name }}</a>{% else %}{{ c.subject_surface or "—" }}{% endif %}</td>
  <td>{{ c.predicate_raw }}{% if c.status != 'asserted' %} <span class="tag warn">{{ c.status }}</span>{% endif %}</td>
  <td>{% if c.object_entity %}<a href="{{ root }}/entity/{{ c.object_entity }}">{{ c.object_name }}</a>{% elif c.literal_pretty is not none %}<code>{{ c.literal_pretty }}</code>{% else %}{{ c.object_surface or "—" }}{% endif %}</td>
  <td>{% if c.evidence_quote %}<span class="quote">“{{ c.evidence_quote }}”</span>{% else %}<span class="muted">—</span>{% endif %}</td>
  <td class="num">{{ "%.2f"|format(c.confidence) }}</td>
  <td>{{ c.connector }} · {{ c.source_name }}</td>
  <td class="num">{{ c.published_at|dt }}</td>
</tr>
{% else %}
<tr><td colspan="7" class="empty">no claims yet</td></tr>
{% endfor %}
</tbody>
</table>
</div>

<h2>Asserted edges ({{ asserted|length }})</h2>
{% with edges = asserted %}{% include "edges.html" %}{% endwith %}

<h2>Inferred edges ({{ inferred|length }})</h2>
{% with edges = inferred %}{% include "edges.html" %}{% endwith %}
{% endblock %}
"""

EDGES = """<div class="tablewrap">
<table>
<thead><tr>
  <th>Predicate</th><th>Peer</th><th class="num">Claims</th>
  <th class="num">Relevance</th><th>Flags</th>
</tr></thead>
<tbody>
{% for e in edges %}
<tr>
  <td>{% if e.src == ent.entity_id %}{{ e.predicate }} →{% else %}← {{ e.predicate }}{% endif %}</td>
  <td><a href="{{ root }}/entity/{{ e.peer_id }}">{{ e.peer_name }}</a></td>
  <td class="num">{{ e.claim_count }}</td>
  <td class="num">{{ e.relevance }}</td>
  <td>{% if e.archived %}<span class="tag warn">archived</span>{% else %}<span class="muted">—</span>{% endif %}</td>
</tr>
{% else %}
<tr><td colspan="5" class="empty">none</td></tr>
{% endfor %}
</tbody>
</table>
</div>
"""

HYPOTHESES = """{% extends "base.html" %}
{% block title %}Hypotheses — CAF graph{% endblock %}
{% block body %}
<h1>Hypotheses</h1>
<div class="tablewrap">
<table>
<thead><tr>
  <th>Type</th><th>Subjects</th><th>State</th><th>Rationale</th><th class="num">Created</th>
</tr></thead>
<tbody>
{% for h in hypotheses %}
<tr>
  <td>{{ h.type }}</td>
  <td>{% for sid, name in h.subject_links %}<a href="{{ root }}/entity/{{ sid }}">{{ name }}</a>{% if not loop.last %}, {% endif %}{% endfor %}</td>
  <td><span class="tag">{{ h.state }}</span></td>
  <td>{{ h.rationale or "—" }}</td>
  <td class="num">{{ h.created_at|dt }}</td>
</tr>
{% else %}
<tr><td colspan="5" class="empty">no hypotheses yet</td></tr>
{% endfor %}
</tbody>
</table>
</div>
{% endblock %}
"""

TEMPLATES = {
    "base.html": BASE,
    "index.html": INDEX,
    "entity.html": ENTITY,
    "edges.html": EDGES,
    "hypotheses.html": HYPOTHESES,
}

# ---------------------------------------------------------------- queries

INDEX_SQL = """
select e.entity_id, e.canonical_name, e.kind,
       (select count(*) from claim c
         where c.subject_entity = e.entity_id or c.object_entity = e.entity_id) as claims
from entity e
{where}
order by claims desc, e.canonical_name
limit 50
"""

INDEX_WHERE = """
where e.canonical_name ilike %(pat)s
   or exists (select 1 from entity_alias a
               where a.entity_id = e.entity_id and a.alias ilike %(pat)s)
"""

CLAIMS_SQL = """
select c.claim_id, c.subject_entity, c.subject_surface, c.predicate_raw,
       c.object_entity, c.object_surface, c.object_literal,
       c.evidence_quote, c.confidence, c.observed_at, c.status,
       se.canonical_name as subject_name, oe.canonical_name as object_name,
       ev.connector, ev.published_at, s.name as source_name
from claim c
join event ev on ev.event_id = c.event_id
join source s on s.source_id = ev.source_id
left join entity se on se.entity_id = c.subject_entity
left join entity oe on oe.entity_id = c.object_entity
where c.subject_entity = %(eid)s or c.object_entity = %(eid)s
order by c.observed_at desc
"""

EDGES_SQL = """
select edge.edge_id, edge.src, edge.dst, edge.predicate, edge.origin,
       cardinality(edge.claim_ids) as claim_count,
       round(edge_relevance(edge.*)::numeric, 2) as relevance,
       edge.archived,
       peer.entity_id as peer_id, peer.canonical_name as peer_name
from edge
join entity peer on peer.entity_id =
     case when edge.src = %(eid)s then edge.dst else edge.src end
where edge.src = %(eid)s or edge.dst = %(eid)s
order by relevance desc, edge.predicate, peer.canonical_name
"""

HYPOTHESES_SQL = """
select hypothesis_id, type, subjects, state, rationale, created_at
from hypothesis
order by created_at desc
"""

# ---------------------------------------------------------------- helpers


def fmt_dt(v):
    return v.strftime("%Y-%m-%d") if v is not None else "—"


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


# ---------------------------------------------------------------- app


def create_app():
    env = Environment(loader=DictLoader(TEMPLATES),
                      autoescape=select_autoescape(["html"]))
    env.filters["dt"] = fmt_dt
    # Behind a prefix-stripping proxy (nginx location /vault/ -> /), root_path
    # puts the public prefix back into generated hrefs. Local dev: env unset,
    # root_path "", output byte-identical to before.
    app = FastAPI(title="CAF graph",
                  root_path=os.environ.get("CAF_ROOT_PATH", ""))

    def render(request, name, **ctx):
        ctx["root"] = request.scope.get("root_path", "")
        return HTMLResponse(env.get_template(name).render(**ctx))

    @app.get("/health")
    def health():
        # liveness only — deliberately no DB touch
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, q: str = ""):
        q = q.strip()
        sql = INDEX_SQL.format(where=INDEX_WHERE if q else "")
        with db.connect() as con:
            entities = con.execute(sql, {"pat": f"%{q}%"} if q else {}).fetchall()
        return render(request, "index.html", q=q, entities=entities)

    @app.get("/entity/{entity_id}", response_class=HTMLResponse)
    def entity_page(request: Request, entity_id: uuid.UUID):
        with db.connect() as con:
            ent = con.execute("select * from entity where entity_id=%s",
                              (entity_id,)).fetchone()
            if ent is None:
                raise HTTPException(status_code=404, detail="no such entity")
            aliases = con.execute(
                "select alias, alias_kind from entity_alias where entity_id=%s "
                "order by alias", (entity_id,)).fetchall()
            claims = con.execute(CLAIMS_SQL, {"eid": entity_id}).fetchall()
            edges = con.execute(EDGES_SQL, {"eid": entity_id}).fetchall()
        for c in claims:
            c["literal_pretty"] = (fmt_literal(c["object_literal"])
                                   if c["object_literal"] is not None else None)
        asserted = [e for e in edges if e["origin"] == "asserted"]
        inferred = [e for e in edges if e["origin"] == "inferred"]
        return render(request, "entity.html", ent=ent, aliases=aliases,
                      claims=claims, asserted=asserted, inferred=inferred)

    @app.get("/hypotheses", response_class=HTMLResponse)
    def hypotheses_page(request: Request):
        with db.connect() as con:
            hypotheses = con.execute(HYPOTHESES_SQL).fetchall()
            ids = sorted({sid for h in hypotheses for sid in h["subjects"]})
            names = {}
            if ids:
                names = {r["entity_id"]: r["canonical_name"] for r in con.execute(
                    "select entity_id, canonical_name from entity "
                    "where entity_id = any(%s)", (ids,))}
        for h in hypotheses:
            h["subject_links"] = [(sid, names.get(sid, str(sid)[:8]))
                                  for sid in h["subjects"]]
        return render(request, "hypotheses.html", hypotheses=hypotheses)

    return app


app = create_app()
