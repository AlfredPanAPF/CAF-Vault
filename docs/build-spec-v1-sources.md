# Build spec v1 — user-facing source management

Goal: population can be started and steered from the browser. Feeds and the
watchlist move from code/files into the database; a generic RSS article
connector joins EDGAR/podcasts; documents can be uploaded through the webapp.

Read first: docs/build-spec-v0.md conventions, graph/db.py, graph/envelope.py,
graph/connectors/*.py, graph/webapp.py, graph/cli.py, schema/*.sql.

## schema/004_sources.sql

```sql
alter table source add column config jsonb;          -- per-source settings
alter table source add column last_polled timestamptz;
alter table source add column added_by text;         -- 'seed' | 'web' | 'cli'

create table watchlist (
    ticker      text primary key,
    sector      text,
    active      boolean not null default true,
    added_at    timestamptz not null default now(),
    added_by    text
);
```

## DB-backed sources

- `graph/cli.py` seed(): in addition to registries, (a) upsert watchlist rows
  from config.WATCHLIST json (added_by 'seed', never overwriting an existing
  row's active flag), (b) upsert the two podcast feeds as source rows
  (connector 'podcast', name 'podcast:unhedged' / 'podcast:aidailybrief',
  url = the RSS URLs currently hardcoded in podcast.py, status 'active',
  added_by 'seed').
- `graph/connectors/edgar.py` poll(): tickers default now comes from
  `select ticker, sector from watchlist where active` (the tickers= argument
  still overrides for the CLI). Remove the config.WATCHLIST read from poll.
- `graph/connectors/podcast.py` poll(): iterate source rows
  (connector='podcast', status='active') instead of the FEEDS constant; feed
  name = source.name minus the 'podcast:' prefix; update source.last_polled.
  Keep FEEDS only as the seed data referenced by cli seed (move the dict to
  cli or keep here — either, but single-sourced).
- `graph/connectors/manual.py`: add `ingest_bytes(con, filename, data: bytes)`
  — same logic as ingest_file but from memory (webapp upload calls this);
  ingest_file becomes a thin wrapper.

## graph/connectors/rss.py (new)

Generic article-feed connector for blogs/newsletters/news RSS.

`poll(con, limit_per_feed=5) -> dict` — for each source row
(connector='rss', status='active'): fetch the feed (browser UA), parse items
(title, link, pubDate — reuse the regex approach from podcast.py), for the
newest N items: skip if an event with meta->>'item_url' = link exists; else
fetch the article page (browser UA, timeout 30, size cap 2MB), extract title +
paragraphs with the same container heuristic as manual.py (share it: move the
HTML-extraction helper into manual.py and import it), compose the standard
header block (source_type: article, origin: feed name), envelope.ingest
connector 'rss', meta {feed, title, item_url}. Per-item failures: count and
continue. Paywalled/JS-only pages will yield thin text — if extracted body
< 400 chars, ingest anyway but add meta flag thin: true (the upload path is
the fallback for paywalled sources). Update source.last_polled. Return
{new, duplicate, thin, errors}.

`graph/cli.py` loop + run: rss poll joins the cycle after podcasts.

## Webapp: /admin page + endpoints

Same style/templating as existing pages, all hrefs root-prefixed. Sections:

1. **Watchlist** — table (ticker, sector, active, event count) with
   activate/pause buttons; add form: ticker + sector dropdown (existing
   sectors + free text). Validation: ticker must exist in registry_sec
   (show the matched company title in a confirmation line after add).
2. **Feeds** — table of source rows where connector in ('podcast','rss'):
   name, connector, url, status, last_polled, event count; pause/resume
   button. Add form: url + name + type radio (podcast | rss).
3. **Upload** — file input (multiple), posts to the upload endpoint; lists
   the resulting event ids (or "duplicate").

Endpoints (POST, form-encoded; redirect 303 back to {root}/admin):
- `POST /admin/watchlist` {ticker, sector} — insert active watchlist row
- `POST /admin/watchlist/{ticker}/toggle`
- `POST /admin/feeds` {name, url, kind} — insert source row (connector=kind,
  status 'active', added_by 'web'; podcast names get the 'podcast:' prefix)
- `POST /admin/sources/{source_id}/toggle` — active <-> paused... source.status
  values are ('sandbox','active','demoted','dropped') per 001; use
  active <-> demoted for pause/resume (no schema change), render demoted as
  "paused"
- `POST /admin/upload` — multipart files -> manual.ingest_bytes each

Add `python-multipart` to pyproject dependencies (FastAPI needs it for
multipart forms). Nav: every page gets a small header link row (Entities |
Hypotheses | Admin), root-prefixed.

## CLI parity (thin)

`graph add-feed NAME URL --kind podcast|rss`, `graph add-ticker TICKER
[--sector S]` — same inserts as the endpoints, for scripting.

## Tests

Extend tests/test_pipeline.py (same fixtures): (a) seed creates watchlist rows
+ podcast source rows, edgar.poll reads DB watchlist (monkeypatch network call
to assert the ticker set only — no live fetch); (b) rss.poll with feed +
article fetches monkeypatched ingests an item and dedups on second run;
(c) webapp TestClient: /admin renders; POST /admin/watchlist adds a row
(registry_sec fixture ticker); POST /admin/upload with a small .txt creates an
event; POST /admin/feeds adds an rss source. Keep it in the existing file's
style — fast, no network, no LLM.
