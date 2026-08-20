# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CAF Vault: a self-updating company knowledge graph for a three-person investment team. Connectors pull filings, feeds, podcasts and premium articles; an LLM extracts claims with verbatim evidence; every company mention is resolved to a canonical entity; edges are materialized into a temporal graph that decays unless refreshed; a discovery layer proposes links and promotes only those with independent evidence. The product is provenance.

Read before changing anything substantive:
- `docs/company-graph-design.md` (v2): the spec. Code cites its section numbers (`§`); keep doing that in comments and commit messages.
- `docs/HANDOVER.md`: current state, what is built and verified, what is next, and hard-won ops knowledge. Update it at the end of each build phase (the repo history has "Handover:" commits for this).
- `docs/build-spec-v*.md`: per-phase build specs (v2 is the frontend spec; its §7 copy rules are mandatory for every user-facing string; v5 is the documents pages + summaries).

The owner's working preference: build to maturity, verify everything yourself (tests, browser, live local server), then ship. Do not hand back half-finished states for manual testing.

## Commands

Python side uses `uv`; Postgres is local (brew) and `CAF_DB_URL` defaults to `postgresql:///caf_graph`.

```
uv run graph migrate                 # apply schema/*.sql (append-only, tracked in schema_migrations)
uv run graph seed                    # reference data (SEC registry + alias seed)
uv run graph seed --sources          # dev only: also preload watchlist/feeds; never in production
uv run graph run                     # one full pipeline cycle in-process
uv run graph loop                    # the worker: cycles forever, per-stage isolation + telemetry
uv run graph serve                   # FastAPI + built SPA on :8642 (prod uses --port 8600)
uv run graph status                  # counts by stage
uv run graph <stage>                 # run one stage: extract, summarize, resolve, adjudicate,
                                     # materialize, discover, triage, garden, quality, funnel,
                                     # hypothesize, investigate, verify, links, ingest-edgar, ...
```

Tests (pytest, ~280 tests, Postgres required):

```
uv run pytest                                   # whole suite against caf_graph_test (dropped/recreated per session)
uv run pytest tests/test_router.py -q           # one file
uv run pytest tests/test_sources_api.py -k wsj  # one test by name
CAF_DB_URL=postgresql:///caf_test_x uv run pytest tests/test_quality.py   # separate DB, lets files run in parallel
CAF_E2E=1 uv run pytest tests/test_signin_fill.py -q                      # opt-in real-browser test
```

The suite shares one database and earlier files commit rows, so new tests must scope their assertions (filter by the ids they created, never assert global counts), and must not leave readable `pending` events behind (later files run triage + extract). LLM calls are mocked in tests; the pipeline and discovery funnel run end to end on those mocks.

Frontend (`frontend/`, Vite + React 19 + TS strict + Tailwind 4 + TanStack Query):

```
cd frontend && npm ci
npm run dev        # Vite on /vault/, proxies /api and /vault/api to the backend (VITE_BACKEND_PORT, default 8600)
npm run build      # tsc -b && vite build -> frontend/dist, which graph serve picks up
```

There is no ESLint/Prettier config; `tsc -b` is the gate. Ruff is used ad hoc for Python (`uvx ruff check graph tests`); there is no `[tool.ruff]` section.

Docker: one image (`Dockerfile`, node stage builds the SPA, python stage runs the app) serves both compose services, `caf-vault` (web) and `caf-vault-worker` (`graph loop`).

## Architecture

### Data model is the spec (`schema/`)

`schema/NNN_*.sql` files are applied once each, by filename, in sorted order (`graph/db.py:migrate`). Never edit an applied migration; add the next number. The design's invariants live here rather than in prompts:
- claims are append-only with supersede/retract;
- entity merges are reversible (`entity_same_as`, whole-operation `merge_group`);
- the asserted/inferred firewall is the `claim_asserted` view: inferred edges must never serve as evidence for new inferences, and discovery reads only through this view. It is a `select *` view, so a migration that adds claim columns must re-create it (see `007`);
- `predicate_map` is versioned (the gardener writes full mappings; materialize/contradictions/claims API resolve through the current version);
- `edge_relevance()` and claim `confidence_now` are computed at query time from per-predicate half-lives in `graph/config.py`.

### Pipeline (`graph/`)

One cycle, in order (`cli.py:cmd_run` / `cmd_loop`): connectors (`connectors/edgar.py`, `podcast.py`, `rss.py`, `youtube.py`, `x.py`, `bridge.py`, `links.py`, `manual.py`) → `envelope.py` (event envelope, content-hash dedup, lineage) → `pipeline/triage.py` (heuristic materiality, no LLM) → `pipeline/extract.py` → `pipeline/summarize.py` (one LLM summary per document into `document_summary`; build spec v5) → `pipeline/resolve.py` (per-mention ER: SEC registry + GLEIF sqlite + alias seed + filer context + created-entity tier) → `pipeline/adjudicate.py` (LLM on the ER queue) → `pipeline/gardener.py` → `pipeline/materialize.py` (edges rebuilt from claims) → `pipeline/quality.py` (contradictions, source reliability) → `discovery/` (attribute joins → funnel → hypothesize → investigate → verify → wake). Prompts are markdown files in `graph/prompts/`.

The worker runs each stage on a fresh connection so one failure never poisons the cycle; every call lands in `stage_run`, cycles upsert a heartbeat KV, and a run-now flag shortens the sleep. Between cycles the nap loop also answers document-summary requests from the page (`summarize.run(requested_only=True)`), so the web process never calls the model.

ER precision guards exist because of real false positives: a ticker match needs the registrant name in-document; single-token GLEIF matches never auto-resolve. Do not loosen them.

### LLM engine (`graph/llm.py`)

Two engines behind `complete()` / `complete_json()`: `claude_code` (default; `claude-agent-sdk` on subscription seats `CLAUDE_CODE_OAUTH_TOKEN_VAULT_1..N`, local `claude` login on dev Macs) and `api` (`ANTHROPIC_API_KEY`; legacy, never extended). Models per stage are in `config.MODELS` (env-overridable; a typo fails at worker boot via `llm.validate_models`). When no engine path is live, every LLM stage pauses cleanly and never burns an event's attempts; heuristic stages keep running. Per-call env must scrub `ANTHROPIC_API_KEY` last or an inherited key silently bills the metered account. Hardening (build spec v6): every stage passes its `graph/schemas.py` JSON schema, which the CLI enforces server-side (`output_format`), with the static prompt file riding the system turn; `llm.TransientError` (timeout, process death) burns no attempts or strikes anywhere; quota latches key off the SDK's RateLimitEvent, honour `resets_at`, and survive restarts via the `app_kv` `seat_latches` mirror; seat token values are scrubbed from every reason, exception and status.

### Sources (build spec v4)

- `graph/router.py`: 25 ordered rules turn one pasted URL into a source kind (YouTube, X, FT, WSJ — feeds, articles, and `/news/` listing pages such as Heard on the Street, which have no public feed and are polled from the page itself through the sidecar — Substack incl. custom domains, podcasts, rss-bridge sites, raw feeds, article pages, ...). Our router owns detection; rss-bridge `findfeed` is asked last.
- `graph/watchlist.py`: ticker-only watchlist, metadata from yfinance with SEC fallback; EDGAR polls SEC registrants only.
- `graph/credentials.py`, `fetch.py`, `probes.py`: per-site cookies.txt / bearer stored in the `credential` table; `curl_cffi` Chrome impersonation; per-site wall detection raises `SignInNeeded`. Credential values never leave the API, are never logged, and never appear in artifacts or error messages. `graph/substack_session.py`: a Substack publication on its own domain does not honour the substack.com sid; a per-host session is minted through Substack's cross-domain sign-in and kept in `credential_session`, same privacy rules.
- `graph/browser.py` + `signin.py`: FT/WSJ article bodies come through the CloakBrowser sidecar (Playwright over CDP); "Sign in" on the Sources page fills the site's login form server-side, stores only the resulting session cookie, and falls back to a live view for steps the server cannot type. Email and password are used once and never stored or logged.

### Documents (build spec v5)

The `event` row is the document (one article, episode, filing, video transcript, post digest or upload). `GET /api/documents` (filters: q, source, type, status, days, ticker), `/api/documents/sources`, `/api/documents/{id}` (summary, claims of every status, mentions with resolution state, the companies it touches with merges folded, the edges its claims back, lineage copies, alerts, hypotheses, contradictions), `/{id}/text` (artifact header + body) and `POST /{id}/summarize` (queues a `document_summary` row as `requested`; the worker writes it). Pages `/documents` and `/document/:id`.

### Web (`graph/webapp.py`, `frontend/`)

FastAPI serves the JSON API under `/api` and the built SPA for everything else. Vault is mounted at `/vault/` behind host nginx (prefix-stripping): Vite `base` is `/vault/`, `frontend/src/lib/api.ts` prefixes `BASE_URL`, and the backend must not set `FastAPI(root_path=...)` (assets 404, blank SPA). Pages live in `frontend/src/pages/` (dashboard, claims, documents, document, entities, entity, sources, hypotheses, review, alerts) with a shared shell in `components/shell.tsx`; data fetching is TanStack Query, filters live in URL params.

Copy rules (`docs/build-spec-v2-frontend.md` §7) for every user-facing string: sentence case, short, plain verbs on buttons, no marketing words, no emoji, no exclamation marks, no em dashes, fixed tooltip strings used verbatim.

## Deploy and production

- This repo is the `Vault` submodule of the `~/CAF` super-repo (8 engines, one Docker stack on a 2 vCPU / 7.7 GB / no-swap GCP box, HTTPS + mTLS at cafbrain.com, path `/vault/`).
- Deploys are tag-driven only: commit here → push → bump the `Vault` submodule in `~/CAF` → push → push a `deploy-YYYYMMDD-HHMMSS` tag → GitHub Actions builds, SSHes in, health gate, route smoke. Config-only changes still need the tag.
- Respect the box: the worker is capped (2g mem / 1.5 cpu) after an uncapped whisper run froze the host; ASR is `off` or `CAF_VAULT_ASR=remote` (build spec v7: the worker enqueues into `asr_job`, the owner's Mac mini runs `python -m graph.asr_agent` over an SSH tunnel and posts transcripts back through the token-gated `/api/asr` endpoints; the box itself must never transcribe). Each Claude Code subprocess is ~300 MB (`CAF_VAULT_CC_MAX_SUBPROCESSES`, default 2).
- Production data is deliberately empty until the owner's population run; sources are added through `/vault/sources`, never preloaded. The seat token (`claude setup-token` → server `.env`) is the owner's step.
- Env knobs all have local defaults (`graph/config.py`): `CAF_DB_URL`, `CAF_ARTIFACTS`, `CAF_GLEIF` (~500 MB golden copy, fetched on first worker boot unless `CAF_FETCH_GLEIF=0`), `CAF_MODEL_*` (incl. `CAF_MODEL_SUMMARIZE`), `CAF_VAULT_HYP_*`, `CAF_VAULT_SUMMARIZE_PER_CYCLE`, `CAF_BRIDGE_URL`/`CAF_BRIDGE_TOKEN`, `CAF_CLOAK_LICENSE_KEY`.
- SEC EDGAR: descriptive User-Agent, ≤10 req/s. Acast CDNs 403 the default python UA.

## Layout notes

`spike/` is the phase-0 throwaway harness; only `spike/corpus/ref/` (watchlist, SEC tickers, aliases, GLEIF sqlite) is used by the pipeline, and the Dockerfile copies just those files. `var/` is local runtime state (artifacts) and is gitignored.
