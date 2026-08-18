# Handover — CAF Vault (company knowledge graph)

You are taking over an in-progress system. Read this fully, then the design
doc, before writing anything. The owner's working preference is explicit:
**build the system out to maturity, then tweak.** Do not hand back
half-finished states for testing or ask them to try things mid-build. They
will do one real population run when the app is mature. Work autonomously:
spec → build (workflow fan-out with a review agent) → verify everything
yourself (tests, browser, live server where safe) → ship.

## The goal

A self-updating company knowledge graph for a three-person investment team.
Agents watch sources (filings, podcasts, news feeds, uploaded premium
articles), extract structured claims with verbatim evidence, resolve every
company mention to a canonical entity, and store a temporal graph where old
links fade unless refreshed. A discovery layer proposes non-obvious
connections between companies and keeps only the ones backed by independent
sources. The product is provenance: every edge can show its evidence. It is
an auxiliary research tool — it reads everything so the team reads only what
matters, and it should surface links few people have made.

The spec is `docs/company-graph-design.md` (v2). The code cites its section
numbers (§) throughout. Its two make-or-break constraints: entity-resolution
precision (a bad merge poisons queries) and self-poisoning (inferred edges
must never serve as evidence for new inferences — enforced in the schema via
the `claim_asserted` view, not in prompts).

## Where it runs

- This repo: github.com/AlfredPanAPF/CAF-Vault, and submodule `Vault` of the
  `~/CAF` super-repo (github.com/APFundMO/CAF) — a production Docker stack of
  8 engines on a GCP VPS (alfred@34.126.95.106, **2 vCPU / 7.7GB / no swap**),
  behind host nginx doing HTTPS + mTLS at cafbrain.com, path `/vault/`.
- Two services from one image: `caf-vault` (web, :8600) and `caf-vault-worker`
  (pipeline loop, 15 min). Worker is capped (2g mem / 1.5 cpus) because an
  uncapped whisper run froze the host on 2026-08-17. Respect the box size.
- Deploys are tag-driven only: push a `deploy-*` tag on CAF main → GitHub
  Actions builds changed images → SSHes in → health gate → route smoke.
  Config-only changes still need the tag (server checkout follows it).
  Flow for code: commit Vault → push → bump submodule in CAF → push → tag.
- LLM engine (`graph/llm.py`): Claude Code subscription seats
  (`CLAUDE_CODE_OAUTH_TOKEN_VAULT_1..N`), ported from Filter's production
  backend. **No seat token is provisioned yet** — extraction pauses cleanly
  (never burns event attempts) until the owner runs `claude setup-token` and
  sets it in the server's `~/CAF/.env`. That is their step, not yours.
- Production data state: deliberately **empty** — the owner had all spike-era
  sources and events wiped. Sources are never preloaded (`graph seed
  --sources` is dev-only); the team adds real sources via `/vault/sources`.
  Do not repopulate production.

## What is built and verified

- **Phase-0 spike** (`spike/`, results in `spike/out/report/REPORT.md`):
  measured extraction faithfulness 0.97 (sonnet-class) / 0.89 (haiku-class),
  characterized ER on 530 real surfaces, and proved the open predicate
  vocabulary degrades instantly (1,065 predicates, 77% used once → the
  gardener is mandatory). `spike/eval/` holds labeled-set seeds awaiting the
  team's confirmation pass.
- **Pipeline** (`graph/`): connectors (EDGAR with exhibit-type discovery;
  podcast RSS with configurable ASR, default off in prod; generic RSS article
  feeds; manual/browser upload) → event envelope with content-hash dedup and
  lineage → LLM extraction (evidence quotes, defined-terms capture) →
  per-mention ER (SEC + GLEIF sqlite + alias seed + filer context, with
  precision guards born from real false positives: ticker matches need the
  registrant name in-document, single-token GLEIF matches never auto-resolve)
  → LLM adjudication queue → edge materialization (rebuildable from claims,
  per-predicate half-lives, `edge_relevance()` computed at query time) →
  attribute-join discovery writing falsifiable hypothesis records.
- **Worker loop**: per-stage telemetry (`stage_run`), heartbeat KV, run-now
  flag, GLEIF bootstrap, engine-outage pause, boot DB retry.
- **Web**: JSON API under `/api` + React SPA (Filter's stack: Vite, React 19,
  TS strict, Tailwind 4, TanStack Query). Pages: pipeline dashboard (cycle
  stages, seat status, counters, failed-item retry, run-now), claims feed
  (filters in URL params, evidence quotes), entities, entity detail (edges
  with relevance), sources admin (watchlist validated against SEC registry,
  feeds, upload), hypotheses. Copy rules are pinned in
  `docs/build-spec-v2-frontend.md` §7 — analyst-grade, sentence case, no AI
  jargon, no em dashes; follow them for every new string.
- **Schema** (`schema/001-005`): the design's invariants live here — claims
  append-only with supersede/retract, reversible merges via `entity_same_as`,
  the asserted/inferred firewall view, versioned `predicate_map` (empty, for
  the gardener), ops tables.
- **Maturity phase (2026-08-17, build spec v3 — `docs/build-spec-v3-maturity.md`),
  all five gate items built, adversarially reviewed, and verified end to end
  against a live local server with a browser pass:**
  1. *Discovery back half (§8):* funnel scoring (novelty × materiality ×
     prior) → LLM hypothesis refinement (no falsifiable test plan → refuted)
     → budgeted investigation tool loop reading only the `claim_asserted`
     view with claim-ID grounding → adversarial verifier with code-enforced
     gates (independent lineage count; unproven/low-reliability lineages
     collectively count as one; promotion needs an explicitly scored
     established source) → inferred edges with the verifier's trail →
     park/wake with recurring wake alerts. Per-item failure isolation,
     two-strike park, engine-outage pause everywhere.
  2. *Alerts + fast path (§3, §11):* heuristic materiality triage gates
     extraction (8-K items, title keywords; no LLM, never pauses), in-app
     alerts (material events on watchlist, promoted links, wakes, watchlist
     contradictions) with unread badge, morning digest endpoint + Alerts
     page. In-app only — the owner dropped Telegram and every external
     channel (2026-08-17).
  3. *Gardener (§5.3):* threshold-triggered, delta-prompted, versioned full
     mappings into `predicate_map`; materialize/contradictions/claims API
     resolve through the current version.
  4. *Quality layer (§10):* contradiction queue (object conflicts fall back
     to normalized surfaces for unresolved persons; same-lineage auto-resolve
     is merge-aware), source reliability scores, claim staleness at query
     time (`confidence_now`), simhash near-dup mirrors at ingest.
  5. *Review surfaces:* Review page (ER queue decide, contradiction resolve),
     hypothesis detail with accept/reject, entity merge/unmerge
     (whole-operation reversible via `merge_group`, schema 009), failed-event
     retry-all. Every verdict lands in `review_label` (calibration data).
- Unregistered companies dedup across documents (created-entity resolve tier
  + get-or-create on `new_entity`) so attribute joins can see a shared
  supplier — the design §8.8 case.
- **Schema** (`schema/001-009`): the design's invariants live here — claims
  append-only with supersede/retract, reversible merges via `entity_same_as`,
  the asserted/inferred firewall view (007 refreshes it for the 003 columns),
  versioned `predicate_map`, ops + alert + contradiction + review tables.
- 199 tests after v4 (`uv run pytest`, one shared DB — new tests must scope assertions,
  earlier files commit rows), mocked-LLM end-to-end including the full
  discovery funnel. Local dev unchanged: brew postgres, `uv run graph migrate
  && uv run graph seed --sources`, `graph run`, `graph serve`.

- **Sources overhaul (2026-08-18, build spec v4 —
  `docs/build-spec-v4-sources.md`; rss-bridge research brief in
  `docs/rss-bridge-brief.md`):**
  1. *Watchlist by ticker alone* (`graph/watchlist.py`): name, sector,
     industry, exchange, country, currency from yfinance (10 s budget in a
     worker thread), SEC registry fallback, `cik` join; non-US symbols
     accepted, EDGAR still polls SEC registrants only.
     `GET /api/watchlist/search?q=` gives name suggestions.
  2. *One URL box* (`graph/router.py`, 25 ordered rules): YouTube (channel,
     @handle, playlist, video), X (account, post), FT (`?format=rss` section
     feeds, `/content/<uuid>` articles), WSJ (live host
     `feeds.content.dowjones.io/public/rss/<SLUG>`; the widely cited
     `feeds.a.dj.com` feeds froze in Jan 2025 and are remapped), Apple
     Podcasts, Bluesky, Telegram/Reddit/Mastodon/Threads via the bridge,
     GitHub, Medium, Substack incl. custom domains (`x-cluster: substack`
     header, generator tag, archive API), raw RSS/Atom, podcast sniff, HTML
     autodiscovery, article pages, well-known feed paths, then rss-bridge
     `findfeed`. `POST /api/sources/resolve` previews, `POST /api/sources`
     creates. One-off links (article, Substack post, video, X post) land in
     `link_queue`: processed synchronously when cheap, by the worker `links`
     stage otherwise; retry, blocked, duplicate states shown on the page.
  3. *Premium sources* (`graph/credentials.py`, `graph/fetch.py`,
     `graph/probes.py`): cookies.txt or bearer per site (ft, wsj, substack,
     x, youtube) pasted on the Sign-ins card, `curl_cffi` Chrome
     impersonation, per-site wall detection raising `SignInNeeded` → source
     row / link shows "Sign-in needed" (or, for FT/WSJ once a cookie is
     saved and the edge still walls the fetch, "Headlines only, article
     pages are blocked"; those feeds ingest headline + teaser as thin
     events); a Test button runs a live probe (FT: `session-next.ft.com`
     liveness first, so a live cookie behind the wall is reported as such).
     Values never leave the API. Substack sends `substack.sid` to the
     publication host (custom domains too), the rss-bridge SubstackBridge
     trick, and accepts a bare sid paste.
  4. *New connectors:* `youtube.py` (native channel feed → yt-dlp captions
     json3 → whisper fallback only when ASR is on; cookies required from a
     server IP, from a spare Google account), `x.py` (X API v2 bearer,
     per-cycle account digest event, `since_id`, 429/402/401 handling),
     `links.py`, `connectors/bridge.py` (rss-bridge JSON Feed). rss.py parses
     Atom + `content:encoded`, fetches with the site credential, flags feeds
     that answer 200 with nothing new for 45 days. Worker cycle: edgar →
     podcast → rss → youtube → x → bridge → links → triage → …
  5. *Private rss-bridge* (`caf-rss-bridge` in the CAF compose,
     `rssbridge/rss-bridge:2025-08-05`, no port, no nginx route, env-only
     config, allowlist of 15 bridges, optional `CAF_BRIDGE_TOKEN`; Vault
     reads `CAF_BRIDGE_URL`). Verified locally: health, whitelist (400 for
     TwitterBridge), token gating, findfeed/display shapes.
  6. *Frontend:* `pages/sources.tsx` rewritten (Watchlist, Sources, Links,
     Sign-ins, Upload). Image: `yfinance`, `yt-dlp`, `yt-dlp-ejs`,
     `curl_cffi`; node copied from the frontend stage as yt-dlp's JS runtime.
     Schema 010. Tests: 199 (`uv run pytest`).

  What the research settled (details and citations in the brief):
  rss-bridge's `findfeed`/`detect` cannot classify Substack, YouTube, X,
  FT or WSJ URLs (no `detectParameters`), so our router owns detection and
  asks the bridge last; `TwitterBridge`/Nitter are dead and `TwitterV2Bridge`
  needs the same paid X token our `x.py` uses directly (X bills per read,
  about $0.005 per post, deduplicated per UTC day); no FT or WSJ bridge
  exists — one would be an `EconomistBridge`-pattern custom bridge with the
  cookie in `RSSBRIDGE_<Bridge>_cookie`, which buys nothing over our fetcher.
  **FT (Cloudflare JS challenge) and WSJ (DataDome) reject non-browser
  clients at the edge before cookies are read**, verified from two networks:
  their section feeds work (headline + teaser), the credential path
  degrades honestly, and full text from the server needs the FT Content API
  (Datamining licence) or Dow Jones Factiva, a browser-based fetcher on a
  non-datacenter egress, or (WSJ) transcribing the public full-article
  narration MP3s the Dow Jones GraphQL gateway lists — all recorded as
  follow-ups in spec §11, none built.

  **CloakBrowser sidecar (2026-08-18, owner direction: FT/WSJ full text is
  scraped with the team's paid accounts, no APIs, ToS settled).**
  `caf-cloakbrowser` (cloakhq/cloakbrowser:0.5.7, `cloakserve` headed under
  Xvfb, internal only, 1.5 GB / 1 CPU) + `graph/browser.py` (Playwright over
  CDP, persistent context reuse, cookie injection, wall-clear polling with
  fresh-context retries) + the `fetch.get` hook for FT/WSJ article pages.
  Spec §10c. Verified in production: FT probe "Signed in.", FT markets feed
  ingesting 3-5k-char bodies (~8 s each). Cookies are harvested with a
  headed CloakBrowser on the owner's Mac (`scratchpad/cloak/login.py`
  pattern: persistent profile, sign in, export Netscape cookies.txt) and
  pushed with `PUT /api/credentials/<site>`; FT session = `FTSession_s`,
  WSJ = `DJSESSION`/`sso`/`session`/`usr_prof_v2`. When a cookie expires the
  source row flips to "Sign-in needed": harvest again. Optional
  `CAF_CLOAK_LICENSE_KEY` in the server `.env` selects the current keyed
  CloakBrowser build (free key = 1 concurrent session; the keyless v146
  build is what runs without it).

  Owner steps: paste the Substack sid, YouTube cookies (spare account) and
  the X bearer on `/vault/sources` → Sign-ins and press Test. FT is loaded;
  WSJ needs a subscriber sign-in harvest. Optional `CAF_BRIDGE_TOKEN` /
  `CAF_CLOAK_LICENSE_KEY` in the server `.env`. Deploy: Vault commit → submodule bump → `deploy-*` tag (the
  bridge image is pulled by `compose up`; its health check is part of the
  gate).

## What still needs building (recommended order)

1. **Remaining design subsystems**: XBRL structured lane (§4.5), speaker
   diarization + speakers-as-entities (§5.5), publisher corrections (§4.8),
   historical backfill mode (§4.10), source scouting (§4.7), as-of rendering
   and a visual graph explorer (§11), remaining candidate-generation signal
   families (§8.1: similarity gaps, structural link prediction, temporal
   co-movement — attribute joins ship today).
2. **Eval harness.** Wire the spike's labeled sets into repeatable
   measurements (ER merge precision vs the ~0.99 gate; extraction precision);
   iterate the extraction/adjudication prompts against them. The
   `review_label` table now accumulates live calibration labels.
3. **JWT integration** is deliberately absent (mTLS-only, Market precedent)
   unless the owner asks. Ops nits from the previous handover are done
   (backup covers caf_vault; deploy smoke asserts 200 on /vault/).

The maturity bar is met: the app is ready for the owner's population run.
Their one manual step remains the seat token (`claude setup-token` →
`CLAUDE_CODE_OAUTH_TOKEN_VAULT_1` in the server's `~/CAF/.env`); until it is
set, every LLM stage pauses cleanly and the heuristic stages keep running.

## Hard-won operational knowledge

- The box froze once: whisper large-v3 on 2 CPU cores, no swap. Caps are in
  compose; keep them. ASR stays off until the team opts in
  (`CAF_VAULT_ASR=faster-whisper`, model default `small`).
- `nginx.conf` is a single-file bind mount: git checkout swaps the inode and
  the running container keeps the old config through reloads. The deploy now
  hash-compares container vs disk and force-recreates on drift.
- Do not set `FastAPI(root_path=...)` behind the prefix-stripping proxy —
  Starlette then expects unstripped paths and assets 404 (blank SPA).
- After a host reboot, Docker's restart policy ignores `depends_on`; services
  crash-loop on DNS until recreated in order. Boot DB retry in the app
  absorbs most of it.
- Per-call env for LLM seats must scrub `ANTHROPIC_API_KEY` last, or an
  inherited key silently bills the metered account. Never share another
  service's seat token (halves both weekly pools).
- SEC EDGAR: descriptive User-Agent, ≤10 req/s. Acast CDNs 403 the default
  python UA — use the browser UA for feeds and enclosures.
