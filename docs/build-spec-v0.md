# Vertical slice v0 — build spec

Module contracts for the phase-1 vertical slice. The core exists and is the source
of truth for interfaces: `graph/config.py`, `graph/db.py`, `graph/artifacts.py`,
`graph/envelope.py`, `graph/llm.py`, `graph/er_norm.py`, `schema/*.sql`. Read them
before writing a module. Spike reference implementations to port from:
`spike/fetch_edgar.py`, `spike/fetch_podcasts.py`, `spike/resolve_block.py`.

Conventions: plain Python, no type-annotation ceremony beyond what aids reading;
psycopg3 with dict rows (`db.connect()`); every module works against an open
connection passed in (`con`); commits happen in the CLI layer per command, not
inside library functions. Print one summary line per run function. No new
dependencies beyond pyproject.

## graph/connectors/edgar.py

`poll(con, tickers=None, filings_per_company=2) -> dict` — for each watchlist
ticker (default: all from `config.WATCHLIST`), fetch recent 8-Ks via the
submissions API, discover EX-99.* exhibits via the filing index page Type column
(port from spike), convert HTML to text (port `html_to_text`), compose one text
document per filing:

```
# title: <registrant> 8-K filed <date>
# source_type: filing
# published: <date>
# ticker: <ticker>
---
<primary text>

---- EXHIBIT <name> ----
<exhibit text>
```

Then `envelope.ingest(con, source_id, "edgar", content_bytes, "text/plain", ".txt",
published_at=<filingDate>, meta={"ticker","sector","form","title","origin"})`.
One source per company: `db.get_or_create_source(con, f"edgar:{ticker}", "edgar", url=<submissions url>)`.
Respect `config.USER_AGENT`, sleep 0.15s between requests. Skip filings whose
accession number already appears in event meta (`meta->>'accession'` — include it).
Return counts {new, duplicate, skipped, errors}.

## graph/connectors/podcast.py

`FEEDS = {"unhedged": "https://feeds.acast.com/public/shows/unhedged",
"aidailybrief": "https://anchor.fm/s/f7cac464/podcast/rss"}`

`poll(con, feeds=None, episodes_per_feed=2) -> dict` — fetch RSS live (browser
User-Agent for enclosure downloads, port from spike), for the N most recent
episodes: skip if an event with `meta->>'enclosure_url'` = url exists; else
download mp3 to a temp dir, transcribe via
`uvx --from mlx-whisper mlx_whisper <mp3> --model mlx-community/whisper-large-v3-turbo --output-dir <tmp> --output-format txt`,
compose text doc (header: title/source_type: podcast/published/feed), ingest with
connector "podcast", source `podcast:<feed>`, meta {"feed","title","enclosure_url"}.
Return counts. Transcription failures: log and continue.

## graph/connectors/manual.py

`ingest_file(con, path) -> event_id|None` — read the file; if .htm/.html, extract
title + paragraphs (port the container heuristic from `spike/parse_articles.py`);
if .txt use as-is. Compose header block (source_type: article for html, document
for txt; is_internal source `manual:uploads`). Ingest with connector "manual",
meta {"filename"}. Return event_id (None if duplicate).

## graph/pipeline/extract.py

`run(con, limit=10) -> dict` — select events `status='pending'` order by
fetched_at limit N. For each: set status 'extracting'; read text from
`artifacts.get(artifact_uri)`; build prompt = contents of
`config.PROMPTS/extraction.md` + "\n\n# Document (doc_id: <event_id>)\n\n" + text
(truncate text to 60k chars); `llm.complete_json(prompt, config.MODELS["extract"],
max_tokens=16000)`. Write results:

- mentions -> `mention` rows: event_id, surface, resolver='pending' (confidence
  null, spans null). Only one row per distinct surface per event; keep the
  mention type in... mention has no type column: store person/company/etc. by
  prefixing resolver: 'pending' for company|security types, 'skipped-v0'
  for other types (design resolves only companies in v0).
- claims -> `claim` rows: subject_surface, predicate_raw (snake_case as emitted),
  object_surface OR object_literal (Jsonb of the literal dict), qualifiers (Jsonb),
  event_id, lineage_id (from event), observed_at=now(), valid_from/valid_to if
  parseable ISO, confidence, extractor=model name, status 'asserted',
  evidence_quote.
- defined_terms -> merge into event.meta under key "defined_terms".

Set status 'extracted'. On exception: attempts+1, status 'failed' if attempts>=2
else back to 'pending', last_error truncated. Return {extracted, failed, claims,
mentions}.

## graph/pipeline/resolve.py

Port the spike resolver per-mention against the DB. `run(con, limit=1000) -> dict`.

Work set: mentions `resolver='pending'` joined to their event (need meta, artifact).
In-memory indexes built once per run from `registry_sec` and `alias_seed` tables
(load with norm/strip from `graph.er_norm`). GLEIF sqlite at
`config.GLEIF_SQLITE` (skip GLEIF tiers with a printed note if missing). Tiers,
in order (port logic + precision guards exactly from `spike/resolve_block.py`):
filer_coref, defined_term (event.meta defined_terms), alias, ticker (with
registrant-name-in-doc-text guard; doc text = artifact content lowercased),
name_exact, name_stripped, filer_initials, filer_context, gleif_exact /
gleif_stripped / gleif_prefix (single-token guards), fuzzy candidates.

Resolution target: entities are created lazily —
`entity_for_sec(con, cik, ticker, title)` and
`entity_for_gleif(con, lei, name, country)`: insert into entity
(kind 'company', canonical_name, registry_refs Jsonb({'cik': str(cik),
'ticker': ...} / {'lei': ..., 'country': ...})) with on-conflict-do-nothing then
select by the unique registry index. Resolved mention: set resolved_entity,
resolver='blocking-v1:<tier>', confidence (0.95 exact tiers, 0.85 gleif/filer
tiers). Ambiguous: insert into er_queue (candidates Jsonb list), set
resolver='queued'. No candidates: resolver='unresolved-v1'.

After resolving mentions, link claims of the same event: update claim set
subject_entity=<entity> where event_id matches and subject_surface = mention
surface (same for object_entity via object_surface). Do this with two UPDATE...
FROM mention JOIN statements at the end of the run (covers all newly resolved
mentions). Return tier counts.

## graph/pipeline/adjudicate.py

`run(con, limit=20) -> dict` — pull er_queue 'pending' rows (join mention +
event). Build ONE batch prompt: contents of `config.PROMPTS/er_adjudication.md`
+ for each mention: surface, ±300 chars of artifact text around the first
occurrence, candidate list. Ask for a JSON object
`{"decisions": [{"surface", "mention_id", "decision", "cik"|null,
"entity_hint"|null, "reasoning", "confidence"}]}`. Apply: match+cik ->
entity_for_sec (look up registry_sec); match with lei in candidates ->
entity_for_gleif; new_entity -> create entity (kind 'company', canonical_name =
entity_hint or surface, registry_refs Jsonb({})); not_a_company -> mention
resolver='not_company'; ambiguous -> leave pending (it will retry when more
context exists; cap: after 3 passes mark 'failed'). Update er_queue rows
(status 'decided', decision Jsonb, decided_at) and re-run the claim-linking
UPDATEs from resolve.py (import and call a shared `link_claims(con)` helper —
put that helper in resolve.py). Return decision counts.

## graph/pipeline/materialize.py

`run(con) -> dict` — edges are rebuildable views over claims (§2): delete all
edges origin='asserted', then insert one edge per (subject_entity, object_entity,
lower(predicate_raw)) group over claims status='asserted' with both entities set:
claim_ids = array_agg, confidence = max, valid_from = min(valid_from),
valid_to = max(valid_to) (null-safe), last_evidence_at = max(observed_at),
half_life_days from `config.HALF_LIFE_RULES` (first rule whose keyword set has a
member contained in the predicate string; else HALF_LIFE_DEFAULT; None means
structural/no decay -> null). origin 'asserted'. Inferred edges untouched.
Return {edges}.

## graph/discovery/attribute_joins.py

`run(con) -> dict` — the first discovery signal (§8.1 #3). SQL: company-entity
pairs (a.entity_id < b.entity_id) that share a claim object entity C through
"dependency-ish" predicates (predicate_raw ILIKE any of: %suppl%, %customer%,
%audit%, %agent%, %board%, %backs%, %partner%, %invest%, %contract%) where no
edge exists between A and B in either direction. For each pair (cap 50 per run):
skip if a hypothesis with the same sorted subjects and type exists; else insert
hypothesis: type 'shared_dependency', subjects=[A,B],
statement Jsonb({"template": "shared dependency via <C canonical_name>",
"via_entity": str(C)}), rationale (one sentence naming the shared predicate +
the claims' evidence quotes), test_plan Jsonb({"confirm": ["independent source
naming both A-C and B-C relationships", "filing risk-factor mention"],
"refute": ["the shared mention is generic/sector-wide commentary"]}),
origin Jsonb({"strategy": "attribute_join_v0", "via": str(C)}),
budget Jsonb({"tokens": 0}), state 'generated', evidence = claim ids backing
both sides (uuid[] array). Return {hypotheses}.

## graph/webapp.py

FastAPI `create_app()` + module-level `app`. Jinja2 via DictLoader, minimal clean
CSS (no external assets), three pages:

- `/` — search box (?q= ILIKE canonical_name/alias) + table of entities with
  claim counts (subject or object), link to entity pages. Default: top 50 by
  claim count.
- `/entity/{entity_id}` — canonical name, registry refs, aliases; claims
  timeline (observed_at desc): predicate, object (entity link / literal pretty /
  surface), evidence_quote, confidence, connector + source name, published_at;
  edges split asserted/inferred with peer entity link, predicate, backing claim
  count, relevance (select edge_relevance(edge.*) — cast to 2 decimals),
  archived flag.
- `/hypotheses` — table: type, subjects (linked names), state, rationale,
  created_at.

## graph/cli.py

argparse, `main()` (entry point `graph` per pyproject). Subcommands, each opening
one connection, calling the library, committing, printing the returned dict:

- `migrate` -> db.migrate()
- `seed` -> load registry_sec from config.SEC_TICKERS json (truncate+insert) and
  alias_seed from config.ALIASES (norm keys via er_norm.norm, skip "_" keys);
  print counts
- `ingest-edgar [--tickers T ...] [--per-company N]`
- `ingest-podcasts [--episodes N]`
- `ingest-file PATH`
- `extract [--limit N]`
- `resolve`
- `adjudicate [--limit N]`
- `materialize`
- `discover`
- `run [--limit N]` — one cycle: edgar poll, extract, resolve, adjudicate,
  materialize, discover; print each stage's summary
- `status` — row counts: events by status, mentions by resolver prefix, claims,
  entities, edges by origin, er_queue by status, hypotheses by state
- `serve [--port 8642]` -> uvicorn graph.webapp:app

## tests/test_pipeline.py

pytest, no network, no real LLM. Fixture: env CAF_DB_URL=postgresql:///caf_graph_test
(create/drop via psql in a session fixture — subprocess `createdb`/`dropdb`,
ignore create errors), CAF_ARTIFACTS=tmp_path. migrate; insert two registry_sec
rows (NVDA/1045810, AMD/2488) directly. Monkeypatch `graph.llm.complete_json` to
return a canned extraction dict: defined_terms {}, mentions
[Nvidia/company, AMD/company], claims [Nvidia supplies AMD (object_surface AMD,
evidence quote, stance stated, confidence .9)]. Then: manual.ingest_file of a tmp
.txt fixture -> extract.run -> resolve.run -> materialize.run. Assert: event
status extracted; both mentions resolved to entities with correct cik refs; claim
has subject_entity+object_entity; exactly one asserted edge with predicate
'supplies'; envelope dedup: ingesting the same file again returns None and the
duplicate event's lineage matches the original's. Keep it one file, fast.
