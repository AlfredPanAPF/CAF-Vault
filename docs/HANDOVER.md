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
- 13 tests (`uv run pytest`), mocked-LLM end-to-end. Local dev: brew
  postgres, `uv run graph migrate && uv run graph seed --sources`, `graph
  run`, `graph serve`.

## What still needs building (recommended order)

1. **Discovery back half — the moat (design §8).** Only candidate generation
   exists. Build: hypothesis agent (typed, falsifiable output over serialized
   claim subgraphs), investigation agent (hard budget, tools over the
   `claim_asserted` view only, every evidence citation must be a claim ID
   that resolves), adversarial verifier (source-lineage dedup; promotion
   needs N independent lineages), promotion to inferred edges with the
   verifier's trail attached, parked hypotheses with wake conditions. Without
   this there are no inferred links and the second phase-1 gate (promoted-link
   precision ≥0.7) cannot be measured.
2. **Alerts + fast path (§3, §11).** Materiality triage at ingest, per-user
   watchlist subscriptions, a Telegram bot or email channel, morning digest.
   The stack precedent for Telegram lives in caf-market.
3. **Gardener (§5.3).** Cluster predicate embeddings, write versioned
   canonical mappings into `predicate_map`, make queries resolve through it.
4. **Quality layer (§10).** Contradiction detection queue, claim-confidence
   staleness decay, source reliability scores fed back into confidence,
   near-duplicate detection (only exact hash today).
5. **Review surfaces.** ER queue browser (approve/correct adjudications,
   merge/undo via `same_as`), hypothesis accept/reject buttons (these clicks
   are the calibration labels), failed-event bulk retry.
6. **Remaining design subsystems**: XBRL structured lane (§4.5), speaker
   diarization + speakers-as-entities (§5.5), publisher corrections (§4.8),
   historical backfill mode (§4.10), source scouting (§4.7), as-of rendering
   and a visual graph explorer (§11).
7. **Eval harness.** Wire the spike's labeled sets into repeatable
   measurements (ER merge precision vs the ~0.99 gate; extraction precision);
   iterate the extraction/adjudication prompts against them.
8. **Ops nits.** Deploy route-smoke counts 404 as reachable (add a
   vault-specific 200 assertion); check `scripts/backup-db.sh` covers
   caf_vault; JWT integration is deliberately absent (mTLS-only, Market
   precedent) unless the owner asks.

Items 1–5 are what "mature enough for the real run" means. When they are done
and verified, hand back to the owner for the population run — with the seat
token as their one manual step.

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
