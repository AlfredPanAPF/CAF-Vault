# Build spec v2 — dashboard, claims feed, React frontend

Goal: replace the server-rendered pages with a proper SPA matching CAF-Filter's
stack, add the two missing operational surfaces (pipeline dashboard, claims
feed), and port the existing pages into it. Target users are professional
analysts; every user-facing string follows the copy rules at the bottom.

Reference stack (read these): ~/CAF/Filter/frontend/package.json,
vite.config.ts, src/ layout; ~/CAF/Filter/src/caf_filter/api/app.py lines
105-121 (SPA serving). Vault differs in ONE way: no @caf/auth dependency — the
stack's mTLS fronts everything (Market precedent).

## 1. Schema — schema/005_ops.sql

```sql
create table stage_run (
    id          bigint generated always as identity primary key,
    stage       text not null,
    started_at  timestamptz not null,
    finished_at timestamptz,
    summary     jsonb,
    error       text
);
create index on stage_run (started_at desc);

create table app_kv (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);
```

## 2. Worker instrumentation (graph/cli.py, graph/llm.py)

- graph/llm.py: add `seat_status() -> list[dict]`: per seat {seat, has_token,
  latched, kind, reason (first 160 chars), latched_at}. Read-only view of
  _seats().
- cmd_loop: wrap every stage call in a stage_run row (insert started_at, update
  finished_at + summary jsonb on success, error text on exception — the
  existing per-stage try/except gains this bookkeeping). After each full cycle
  upsert app_kv 'worker_heartbeat' = {last_cycle_at, interval_s, seats:
  llm.seat_status()}. Delete stage_run rows older than 7 days once per cycle.
- Run-now: the sleep becomes a loop of <=15s naps; between naps read app_kv
  'run_requested'; if value is {"v": true}, reset it to {"v": false} and start
  the next cycle immediately.

## 3. Backend API (graph/webapp.py rewrite)

FastAPI, all JSON under /api. Keep GET /health as-is. Library style stays:
open one db.connect() per request (a tiny dependency helper is fine). POSTs
commit. Errors: HTTPException {detail}. Timestamps ISO 8601 UTC.

- `GET /api/status` →
  ```
  {
    heartbeat: {last_cycle_at, interval_s, seats: [...], run_requested: bool}
                | null,                       // null until first cycle
    stages: [ {stage, started_at, finished_at, summary, error} ],  // latest run per stage, most recent cycle
    counts: {
      events: {pending, extracting, extracted, failed, duplicate},
      claims: int, claims_7d: int,
      mentions: {resolved, queued, unresolved, skipped},   // by resolver prefix
      entities: int,
      edges: {asserted, inferred},
      er_queue: {pending, decided, failed},
      hypotheses: {generated, ...by state}
    },
    sources: [ {source_id, name, connector, status, last_polled, events} ],
    failed_events: [ {event_id, connector, title, last_error, fetched_at} ]  // status='failed', newest 20; title from meta->>'title'
  }
  ```
- `GET /api/claims` — params: q (ILIKE across subject_surface, object_surface,
  predicate_raw, evidence_quote), predicate, source_type (event meta / source
  join via manifest-style: use source.connector: edgar|podcast|rss|manual),
  stance (qualifiers->>'stance'), sector (event meta->>'sector'), days (int),
  entity (uuid — subject_entity or object_entity), limit (default 50, max
  200), offset. Returns {total, claims:[{claim_id, subject: {surface,
  entity_id, name}, predicate, object: {surface|literal|entity_id, name},
  stance, confidence, evidence_quote, observed_at, published_at (event),
  connector, source_name, doc_title (event meta->>'title'), event_id}]},
  ordered observed_at desc. Join once; no N+1.
- `GET /api/entities?q=&limit=` → [{entity_id, name, kind, registry
  (short string: ticker or LEI country or "-"), claims}] ordered by claims desc.
- `GET /api/entity/{id}` → {entity: {entity_id, name, kind, registry_refs},
  aliases: [...], claims: [same shape as feed, subject+object of this entity],
  edges: {asserted: [{edge_id, peer: {entity_id, name}, predicate, direction:
  out|in, claims: int, confidence, last_evidence_at, relevance (2dp, via
  edge_relevance), archived}], inferred: [...]}}
- `GET /api/hypotheses` → [{hypothesis_id, type, subjects: [{entity_id,
  name}], state, rationale, created_at}]
- `GET /api/sources` → {watchlist: [{ticker, sector, active, company (registry
  title), events}], feeds: [{source_id, name, connector, url, status,
  last_polled, events}]}
- `POST /api/watchlist` {ticker, sector} → {ok, ticker, company} (validate
  against registry_sec; 400 unknown ticker)
- `POST /api/watchlist/{ticker}/toggle` → {ok, active}
- `POST /api/feeds` {name, url, kind: podcast|rss} → {ok, source_id} (409 on
  duplicate name+kind)
- `POST /api/sources/{source_id}/toggle` → {ok, status}   // active <-> demoted
- `POST /api/upload` multipart files[] → {events: [{filename, event_id|null,
  duplicate: bool}]}
- `POST /api/events/{event_id}/retry` → {ok} (set status pending, attempts 0;
  404 if not failed)
- `POST /api/run-now` → {ok} (app_kv run_requested {"v": true})

SPA serving (port Filter's pattern): dist at REPO/frontend/dist; mount
/assets; catch-all GET (excluding /api and /health) returns index.html.
Local-dev convenience: when CAF_ROOT_PATH is empty, ALSO serve the same SPA
under /vault (assets included) and redirect / -> /vault/ — the built bundle
hardcodes /vault/ asset URLs (vite base), and locally there is no nginx to
strip the prefix. In prod (CAF_ROOT_PATH=/vault) nginx strips, so the plain
mounts serve it.

Old Jinja pages and python-multipart dep: Jinja templates go away entirely
(webapp.py shrinks to API + serving); python-multipart STAYS (upload).

## 4. Frontend — frontend/

Stack (mirror Filter, minus auth): react 19, react-dom, react-router-dom 7,
@tanstack/react-query 5, tailwindcss 4 + @tailwindcss/vite, lucide-react,
date-fns, clsx + tailwind-merge, sonner (toasts), @fontsource-variable/geist
+ geist-mono, typescript, vite, @vitejs/plugin-react. No zustand, no framer,
no shadcn CLI — hand-write small components in the same idiom.

vite.config.ts: base "/vault/", alias @ -> ./src, dev proxy /api ->
http://localhost:8600.

Structure:
```
frontend/src/
  main.tsx  App.tsx            // router + QueryClientProvider + Toaster
  index.css                    // tailwind, geist fonts, css vars for the palette
  lib/api.ts                   // apiFetch<T>(path, opts) using import.meta.env.BASE_URL; ApiError with detail
  lib/format.ts                // fmtDate (date-fns, "MMM d, HH:mm"), fmtNum, timeAgo
  components/ui/               // button.tsx, card.tsx, badge.tsx, input.tsx,
                               // select.tsx, table.tsx, tooltip.tsx, empty.tsx,
                               // spinner.tsx — small, tailwind-styled, typed
  components/shell.tsx         // left sidebar nav (Dashboard, Claims, Entities,
                               // Sources, Hypotheses) + content outlet; collapses
                               // to a top bar on small screens
  pages/dashboard.tsx
  pages/claims.tsx
  pages/entities.tsx
  pages/entity.tsx
  pages/sources.tsx
  pages/hypotheses.tsx
```

Design language: dark-first single theme, near-black background (#0b0e11
family), one accent (emerald or similar, used sparingly for status), Geist
for text, Geist Mono for numbers/tickers/predicates. Density high but not
cramped: tables with 8px vertical padding, 13px base font. Cards with 1px
borders, no shadows, 8px radius. Status colors: ok=muted green, warn=amber,
fail=red, idle=gray. No gradients, no illustrations, no emoji.

Pages:

**Dashboard** (`/`) — three rows:
1. Worker card row: "Worker" (Last cycle X min ago via timeAgo; "Next run"
   estimate = last_cycle_at + interval; "Run now" button -> POST /api/run-now,
   toast "Requested. The worker picks it up within 15 seconds."); "Claude
   seats" card (per seat: number, "active" / "limit reached" / "signed out"
   for latched kind quota/auth, retry time when known); stage list from the
   last cycle (stage name, duration, one-line summary from the jsonb —
   render k:v pairs plainly; red row + error text on failure).
   Heartbeat null → single empty card: "No cycles recorded yet. The worker
   runs every 15 minutes."
2. Counter row: Documents (events extracted + pending split as sub-line),
   Claims (with "+N this week"), Companies, Links (asserted/inferred
   sub-line), Awaiting review (er_queue pending), Hypotheses. Each counter
   links to its page where one exists.
3. Two tables: Sources (name, type, status badge, last polled, docs; row
   click -> Sources page) and Failed items (title, connector, error truncated
   to one line, Retry button -> POST retry, toast "Queued for another
   attempt."). Failed empty state: "Nothing has failed."
Poll /api/status every 30s (react-query refetchInterval).

**Claims** (`/claims`) — the daily reading surface. Sticky filter bar:
search input (placeholder "Search claims"), selects for Predicate (populated
from the current result set's distinct predicates), Source (EDGAR / Podcasts /
News feeds / Uploads), Stance (Stated / Reported / Speculative), Sector, and
Since (Today / 7 days / 30 days / All). Filters live in URL search params.
Below: claim rows — subject name (link when resolved, mono ticker-style),
predicate in mono, object (link / literal pretty-printed / plain surface),
then a second line: evidence quote in quotation marks, truncated to ~200
chars, expandable on click; third line small+muted: source name · doc title ·
published date · stance badge (only when not "stated") · confidence as "0.9"
with tooltip. Pagination: "Load more" appending (keep it simple, no virtual
scroll). Empty: "No claims match the filters."

**Entities** (`/entities`) — search box + table (Company, Registry, Claims);
row -> entity page.

**Entity** (`/entity/:id`) — header: name, registry chips (ticker, LEI,
country), aliases line. Two-column below on wide screens: left = claims about
this company (same row component as the feed, reused); right = "Links" card:
asserted list (peer name link, predicate mono, claim count, relevance shown
as a subtle progress-dot or percent with tooltip), inferred section beneath
with its own heading and, when empty: "None yet. Links appear here once the
system can back them with independent sources." Archived edges behind a
"Show faded links" toggle.

**Sources** (`/sources`) — three cards: Watchlist (table + add form: ticker
input, sector input with datalist, Add button; confirmation toast shows the
matched company name; pause/resume per row), Feeds (table + add form: name,
URL, type radio Podcast/News feed), Upload (drop zone + file picker,
per-file result list: "added" / "already in the vault"). Port the /admin
behaviors 1:1 against the new endpoints.

**Hypotheses** (`/hypotheses`) — table: the two companies (links), type
rendered plainly ("Shared dependency"), rationale, date. Header note (muted,
one line): "Candidates the system generated but has not yet verified."

## 5. Dockerfile (two-stage, mirror Market's)

```
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npx vite build        # tsc -b first, matching Filter's build script via npm run build
FROM python:3.12-slim
... (existing stage unchanged) ...
COPY --from=frontend /build/dist/ ./frontend/dist/
```
(Adjust paths so webapp.py's REPO/frontend/dist lookup works in-image; keep
PYTHONUNBUFFERED, EXPOSE 8600, CMD.) Update .dockerignore: frontend/node_modules,
frontend/dist.

## 6. Tests

Rewrite the webapp TestClient cases against the JSON API: /api/status shape
(with and without heartbeat), /api/claims filters (stance + q), watchlist
POST valid/unknown ticker, feeds POST + duplicate 409, upload multipart,
retry on a failed event, run-now sets the kv flag. Frontend: `npm run build`
green is the test (no vitest in v1); tsc strict.

## 7. Copy rules (user-facing strings)

Target reader: a professional analyst. Rules, non-negotiable:
- Sentence case everywhere ("Last cycle", never "Last Cycle").
- Short. A tooltip is one or two sentences. A label is one or two words.
- No AI/tech marketing words: no "intelligent", "powered", "leverage",
  "seamless", "insights", "delve", "robust", "cutting-edge".
- No emoji, no exclamation marks, no em dashes in any string.
- Plain verbs on buttons: Add, Retry, Pause, Resume, Upload, Run now.
- Numbers unrounded in tables; timeAgo for recency ("4 min ago").
Fixed strings (use verbatim):
- Confidence tooltip: "How confident extraction was in reading the source.
  Not a truth score."
- Stance tooltip: "Stated: the source asserts it. Reported: the source cites
  someone else. Speculative: opinion or forecast."
- Relevance tooltip: "Fades as evidence ages. Ownership and role links do
  not fade."
- Seat states: "active", "limit reached", "signed out".
- Empty states as written in the page specs above.
