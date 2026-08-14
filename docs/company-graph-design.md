# Company Graph — Design v2

Rewrite of v1 in plainer language, plus the pieces it was missing.

New in v2: edge decay and growth control (§9), a structured-data lane (§4.5), historical backfill (§4.10), source scouting (§4.7), uploaded documents (§4.6), correction handling (§4.8), numeric normalization (§5.4), adversarial input defenses (§10.4), an extraction eval set (§12), and two rules v1 left ambiguous (promoted-edge evidence in §8.4, predicate mapping in §5.3).

Context: this is an internal research tool for a three-person investment team. Coverage grows sector by sector, as far as the approach carries. It does not need every company on day one.

---

## 1. What this is

A repository of company information that updates itself.

Agents watch sources: registries, filings, paid press, podcasts, video channels, blogs, newsletters, and documents we upload ourselves. New material is fetched within seconds to minutes of release and turned into structured claims, each tied to its source. Every company mention is resolved to one canonical entity. Everything is stored as a temporal knowledge graph. A discovery layer proposes non-obvious links between entities, tests them against the corpus, and keeps only the ones with independent evidence. A webapp serves entity pages, a graph explorer, and alerts.

Purpose: save research time. The system reads everything so we read only what matters, and it surfaces connections we would not have found by hand.

Two problems decide whether this works:

1. **Entity resolution.** "Apple", "AAPL", "Apple Inc.", and its subsidiaries must map to one node. Get this wrong and the graph is noise.
2. **Self-poisoning.** A system that generates links and then treats those links as evidence will invent facts within months. Prevented in the database schema, not by prompt instructions.

---

## 2. Rules

The decisions everything else follows from.

1. **Store claims, not facts.** Every assertion is a record: subject, predicate, object, source, timestamps, confidence. Graph edges are views built from claims. This gives provenance for free, lets contradicting sources coexist instead of overwriting each other, and means every edge can show why it exists.
2. **Append-only.** Claims are superseded, never edited or deleted. The graph can be rebuilt from the log at any time. This matters because extractors improve: when a better one ships, re-run it over history and rebuild instead of living with old mistakes.
3. **Two timestamps on everything.** Valid time (when it became true in the world) and observed time (when we learned it). A CEO change announced in March, effective June, is two dates. Retrofitting this later is painful, so it exists from day one.
4. **Asserted and inferred are separated in the database.** Extracted claims live in one layer, discovered links in another. Discovery agents query a view that physically excludes inferred material. Promotion into the main graph requires independent sources. Enforced by the data layer, not by prompts.
5. **Merges are reversible.** Entities merge via `same_as` edges, never by rewriting records. Bad merges will happen; undoing them must be cheap.
6. **Spend where the information is.** A few hundred thousand companies produce nearly all events. The long tail sits as static registry records and costs almost nothing.
7. **Old links fade.** Edges lose relevance over time unless new evidence refreshes them. The active graph stays tight; nothing is deleted. (§9.)
8. **Every edge can show its evidence** in a form the app renders directly.

---

## 3. Architecture

```
sources → monitoring agents → event bus → extraction → entity resolution
        → stores (knowledge graph | vector index | raw artifacts)
        → discovery agents → inferred layer → (corroboration) → graph
        → serving (API, webapp, alerts)
```

Two paths over the same spine:

- **Fast path.** A cheap classifier triages each new event within seconds. High-materiality items get light extraction (headline entities, event type) and fire alerts immediately.
- **Slow path.** The same event then gets full extraction, resolution, claim writing, and discovery rescans, within minutes.

The fast path makes the tool feel instant. The slow path makes it correct. Neither blocks the other.

---

## 4. Sources and monitoring

### 4.1 Connectors

One connector per source type, all normalizing into a common event envelope:

- Registries and filings (EDGAR, Companies House, national equivalents)
- Paid press and licensed feeds
- Podcasts and video (YouTube analyst channels, earnings interviews, conference talks) — audio goes through ASR (Whisper-class) plus speaker diarization, then is treated as an ordinary document
- Blogs, newsletters, social
- Uploaded documents (§4.6)

### 4.2 Push first, learned polling second

Where a source offers push (webhooks, fast RSS, filing feeds), use it. Everywhere else, pollers learn each source's publishing pattern from history: a filings feed that fires at 16:05 daily gets polled tightly around 16:05 and loosely otherwise; a weekly podcast gets checked around its release day. Polling schedules are learned per source, not set globally. This is the biggest cost lever in ingestion.

### 4.3 Event envelope

```
Event {
  event_id        uuid
  source_id       uuid            // the feed/channel
  connector       string
  fetched_at      timestamp
  published_at    timestamp?      // as claimed by the source
  content_hash    sha256          // dedup across mirrors
  artifact_uri    uri             // raw bytes in the artifact store
  mime            string
  triage {
    materiality   float           // cheap-model score
    entities      [string]        // rough guesses, pre-resolution
    route         fast | slow
  }
}
```

### 4.4 Dedup and lineage

Content hashing plus near-duplicate detection (shingling or embedding similarity) collapses syndicated copies before they cost extraction tokens. The original publication is recorded as the lineage root; mirrors attach to it. The verifier (§8.5) later uses this lineage record to count independent sources. Lineage must be captured at ingestion — it is nearly impossible to reconstruct later.

### 4.5 Structured-data lane

Data that is already structured skips the LLM entirely: XBRL in filings, registry records, ticker and exchange reference data. Deterministic mappers turn these into claims directly. Cheaper, exact, and they give the graph a reliable numeric spine (financials, incorporation facts, listings). LLM extraction handles prose; it should never be parsing tables that XBRL already provides.

### 4.6 Uploaded documents

A drop folder and an upload button in the webapp. Broker research, expert-call notes, IR decks, internal memos. Same envelope, connector = `manual`, source flagged `internal`. Internal documents carry provenance like everything else ("internal report, uploaded by X, date") and are marked so they can never leave the team if the tool is ever shared.

### 4.7 Source scouting

The source list grows on its own, the same way the graph does.

- **Signals:** an unknown outlet repeatedly cited by tracked sources; a podcast guest who runs their own publication; a new feed from an author already in the graph.
- **Trial:** candidate sources are ingested in a sandbox. Their claims are marked provisional — excluded from promotion evidence and alerts.
- **Admission:** after a trial period, a source is admitted if it yields claims not available elsewhere and those claims hold up against later evidence. One of us approves the admission; it takes a minute.
- Admitted sources carry reliability scores like everyone else. Sources that rot get demoted back to sandbox or dropped.
- Trial slots are capped so scouting can't blow the ingestion budget.

### 4.8 Corrections and retractions

Sources that signal updates get re-fetched (feed update flags, content-hash change at a known URL). A corrected article produces new claims that supersede the old ones (`status: superseded`). A retraction marks them `retracted`. Corrections are normal publishing and don't hurt a source's reliability score; quiet rewrites and retractions do.

### 4.9 Raw artifact storage

Every fetched artifact goes to object storage, per-source buckets, per-source retention config. Derived data (claims, embeddings) is stored separately from raw content, so whatever access terms apply to a given source are enforced as bucket policy, not a redesign. Nothing downstream reads raw artifacts except extraction and, on demand, investigation agents.

### 4.10 Backfill

Forward monitoring and historical backfill are separate modes on the same pipeline.

- Backfill bulk-fetches archives: filing history, news archives, podcast and video back-catalogs. No latency requirement, so it runs on batch APIs at batch prices.
- Bitemporality handles it naturally: `observed_at` is now, `valid_from` is then.
- Discovery stays off during a backfill wave and rescans run once the wave settles — otherwise the hypothesis queue floods with stale candidates.
- Backfilled event edges enter pre-decayed (§9): their relevance is computed from their age, so a 2019 partnership lands directly in the archive layer instead of swamping the active graph. Structural facts (ownership, incorporation) don't decay and land normally.

---

## 5. Extraction

### 5.1 Pipeline

Parse (or transcribe) → segment → extract entity mentions → extract claims → attach provenance. LLM-based, tiered: a small model handles routine documents; a strong model handles dense material (filings, transcripts) and anything the small model flags as low-confidence.

### 5.2 Claim schema

The core record of the system:

```
Claim {
  claim_id      uuid
  subject       entity_ref            // canonical or provisional
  predicate     string                // open vocabulary at write time
  object        entity_ref | literal
  qualifiers    map                   // amount, role, jurisdiction, ...
  source_id     uuid                  // document segment of origin
  lineage_id    uuid                  // root reporting origin
  observed_at   timestamp             // when we learned it
  valid_from    timestamp?            // when it became true, if stated
  valid_to      timestamp?
  confidence    float                 // extraction confidence
  extractor     string                // model + version
  status        asserted | superseded | retracted
}
```

`extractor` versioning allows selective re-extraction when models improve. `lineage_id` allows independent-source counting. `status` allows correction without deletion.

### 5.3 Predicates: small spine, open vocabulary, gardener

The graph has a small fixed set of node types (~15–20: Company, Person, Product, Event, Filing, Location, Security, Patent, and similar). The predicate vocabulary is open at write time: extractors write whatever relationship the source expresses.

A periodic gardener job keeps the vocabulary usable: it clusters predicate embeddings, maps synonyms to one canonical predicate ("acquired", "bought", "took over"), and promotes frequently recurring new predicates into the spine.

The canonical mapping is a **versioned table over raw predicates**. Claims keep the raw predicate they were written with; queries resolve through the mapping. Re-mapping produces a new version — never a rewrite of claims. This keeps the gardener consistent with rule 2 (append-only).

Without the gardener, an open vocabulary turns into synonym soup and queries break. With a closed vocabulary, the system can't represent relationships nobody anticipated. This is the middle.

### 5.4 Numbers

Literals get normalized at extraction, keeping the original alongside:

- **Money:** original currency and amount, plus a USD value converted at the claim's `valid_from` date.
- **Units:** one canonical unit per quantity type.
- **Fiscal periods:** each company's fiscal calendar is mapped to calendar dates once (filings state it), so "FY2025" resolves correctly per company.

Without this, contradiction detection and co-movement break on numbers — "€3.2B revenue" and "$3.5B revenue" is one fact, not a conflict.

### 5.5 Speakers are entities

Diarized podcast and video speakers resolve to Person nodes. Each speaker accumulates a track record: how their past claims held up against later evidence. That reliability score feeds claim confidence, and "independent analyst with a strong record" becomes something you can query.

---

## 6. Entity resolution

The service everything else depends on. It maps every mention in every document to a canonical entity; everything downstream inherits its error rate.

**Seeding.** Canonical entities are bootstrapped from registries: GLEIF LEI data, OpenCorporates, national registers, ticker/exchange listings, domain ownership. This gives a spine of identifiers before any document is processed. Long-tail entities exist as registry shells carrying only their registry claims.

**Two stages.**

1. *Blocking:* cheap candidate generation — name n-grams, legal-suffix stripping, domain matches, jurisdiction, phonetic keys — narrows millions of candidates to a handful.
2. *Adjudication:* probabilistic matching on the candidates, escalating to an LLM only for genuinely ambiguous cases (shared names across jurisdictions, post-rebrand mentions, subsidiary vs. parent). LLM cost scales with ambiguity, not volume.

**Reversibility.**

```
- Merges are same_as edges between records. No record is ever
  rewritten or deleted by a merge.
- Former names, brands, tickers, and subsidiaries are alias nodes
  attached to the canonical entity, each with validity intervals.
- Every mention keeps a Mention record (surface form, document, span,
  resolved entity, resolver version, confidence) so any decision can
  be audited and re-run.
```

A bad merge fuses two companies' histories and poisons every query touching either. Reversible merges plus a labeled eval set (§12) are the defense, along with an alert on post-merge contradiction spikes (a merged entity suddenly contradicting itself is the symptom).

**Languages and scripts.** Alias nodes are script-aware: transliterations ("腾讯" ↔ Tencent) are aliases like any former name. Start English-first; adding a language means adding registry seeds and language-specific blocking keys, not redesigning resolution.

**Corporate structure.** Parent–subsidiary and brand relationships are ordinary claims with validity intervals, not part of entity identity. Ownership changes constantly; identity shouldn't.

---

## 7. Storage

Four stores, one job each:

1. **Knowledge graph.** Entities and materialized edges. Every edge carries its backing claim IDs, validity interval, confidence, an `origin` flag (`asserted` or `inferred`), and decay fields (§9). Rebuildable from the claim log.
2. **Vector index.** Chunk embeddings (retrieval) and entity embeddings (similarity, candidate generation), recomputed incrementally as claims accumulate.
3. **Raw artifact store.** §4.9.
4. **Metadata DB (Postgres).** Sources, connectors, schedules, mentions, hypotheses, budgets, audit logs.

The append-only claim log is the source of truth. Graph and vector index are derived and disposable.

---

## 8. Discovery

Where new links come from. Designed as a funnel because the search space is hopeless for LLMs: one million tracked entities is ~10^12 pairs. Nothing LLM-shaped touches that space directly. The governing metric is **cost per promoted link**.

### 8.1 Candidate generation (algorithmic, continuous)

Five signal families run against the **active graph** (§9 — archived edges are excluded, which keeps this space bounded):

1. **Similarity gaps.** Pairs close in embedding space but distant or disconnected in the graph.
2. **Structural link prediction.** Graph topology scores (Adamic-Adar, neighborhood Jaccard; a GNN only if it earns its keep) — pairs the graph's own shape expects to be connected.
3. **Attribute joins.** Shared auditors, agents, addresses, board members, patent citations, the same obscure supplier in two unrelated transcripts. Cheapest high-precision signal; direct payoff of good entity resolution.
4. **Temporal co-movement.** One entity's event stream consistently leading another's by a stable lag.
5. **Event-driven rescans.** When a new claim lands on entity X, signals 1–4 re-run on X's neighborhood only. This makes discovery continuous instead of a nightly batch.

### 8.2 Triage (cheap model)

Score = novelty × materiality × prior.

- **Novelty:** mainly inverse co-mention frequency — if two companies always appear in the same articles, linking them is not a discovery. Graph distance is secondary.
- **Materiality:** entity watch-scores and edge type (a dependency link outranks a shared-conference-panel link).
- **Prior:** strength of the generating signal.

Only the top slice of the queue reaches agents.

### 8.3 Hypothesis agent

Input: a serialized subgraph of claims (with sources, dates, confidence — not bare triples) plus the explicit negative space ("no recorded relationship between A and B").

Output contract, enforced:

```
Hypothesis {
  hypothesis_id    uuid
  type             typed relation (e.g. shared_supplier_dependency)
  subjects         [entity_ref]
  statement        structured claim template
  rationale        text
  test_plan {
    confirm        [what evidence would confirm]
    refute         [what evidence would refute]
  }
  origin           strategy_id + signal snapshot
  budget           { tokens, tool_calls }
  state            generated | triaged | investigating |
                   promoted | parked | refuted
  evidence         [claim_id]        // asserted layer only
  lineages         [lineage_id]      // independent source lines
  confidence       float
  wake_conditions  [entity + claim-type filters]
  history          audit log
}
```

No test plan → discarded on the spot. New hypotheses are embedded and deduplicated against all prior ones **including refuted ones** — negative memory stops the system rediscovering the same dead end weekly.

### 8.4 Investigation agent

A tool-using loop with a hard budget proportional to the triage score. Tools: graph queries, hybrid search, targeted raw-document fetches (a filing's risk section, a transcript segment), and the ability to ask monitoring to prioritize a source.

Three mechanical rules:

1. **Claim-ID grounding.** Every piece of evidence must be a claim ID that resolves in the store. Citations that don't resolve are dropped. This is the hallucination guard.
2. **Firewall by construction.** The agent queries a view that contains only asserted claims. Open hypotheses and their edges are invisible to it. Enforced by the database, not the prompt.
3. **Promoted edges are context, never evidence.** Promoted links (§8.6) are visible for navigation — they are conclusions with independent backing — but the evidence validator accepts only asserted claims from source documents. Edges citing edges is how feedback loops start; this rule closes the last one.

### 8.5 Adversarial verifier

A separate prompt (ideally a separate model) whose job is to kill the hypothesis. Its most important task is **lineage deduplication**: two articles citing the same wire story are one source, not two. Evidence is traced through claim provenance to publication lineage; only independent lineages count. Promotion requires N independent lineages, N scaled to how consequential the edge type is. The verifier's output — surviving evidence, confidence, reasoning — is stored as the edge's permanent evidence trail, which is exactly what the app shows when we ask why a link exists.

### 8.6 Lifecycle

```
generated → triaged → investigating → { promoted | parked | refuted }
```

- **Promoted:** enters the graph flagged `inferred`, evidence trail attached, decay clock running (§9).
- **Refuted:** kept as negative knowledge.
- **Parked:** not enough evidence yet. The hypothesis registers wake conditions — effectively subscribing to its entities — and when a matching claim arrives months later, investigation resumes with its old context. The system accumulates open questions, and the ingestion stream keeps answering them.

### 8.7 Learning loop

Per candidate-generation strategy, track promoted links per dollar, and run a bandit over exploration budget: strategies with better hit rates get more. High-materiality promotions pass through a review step — one of us accepts or rejects in the app, which takes seconds and doubles as calibration data for the verifier. Our flags, saves, and evidence click-throughs are free labels.

### 8.8 Worked example

A podcast transcript lands: an analyst says a niche electrolyte producer supplies "most of the mid-tier battery names." Resolution maps the producer to a canonical entity. The rescan fires an attribute join: two battery companies with no graph connection share a supplier mention. Triage scores it high (near-zero co-mention, high materiality). The hypothesis agent proposes a typed shared-dependency link with a test plan: check both companies' filing risk sections and procurement claims. Investigation finds the supplier in one filing and one other independent transcript. The verifier counts three independent lineages and promotes at 0.72 confidence. Nobody wrote a rule about batteries; the link came from the data.

---

## 9. Edge decay and growth control

Confidence and relevance are different numbers. Confidence says how sure we are the claim was true. Relevance says how much the link matters now. An NVDA–AMD deal from last year can be high-confidence and mostly irrelevant today. v1 decayed claim confidence; v2 makes relevance a separate, explicit mechanism — this is what keeps the graph from blowing up.

**Mechanics.**

- Every edge stores `last_evidence_at` — the newest claim backing it — and its edge type's half-life.
- Relevance is computed at query time: `relevance = 2^(-(now - last_evidence_at) / half_life)`. Nothing is written by decay, no batch job runs, and the graph stays rebuildable.
- New evidence resets `last_evidence_at`. Links that keep getting mentioned stay hot on their own.
- Below a threshold (~0.1), an edge is **archived**: hidden from default app views, excluded from discovery candidate generation and alert scoring. Never deleted. Still visible in as-of and history views, and instantly revived if new evidence lands.
- **Structural edges don't decay while valid**: ownership, subsidiaries, executive roles, listings. They end by `valid_to` when the world ends them (divestiture, resignation). Validity is about truth; relevance is about attention. Separate axes.

**Starting half-lives** (per edge type, tuned with real data):

| Edge type | Half-life |
|---|---|
| Co-mention / event co-occurrence | 1–2 months |
| Deal, partnership announcement | 6 months |
| Supplier / customer dependency | 12–18 months |
| Litigation | until resolved, then 6 months |
| Ownership, subsidiary, roles, listings | none — validity-bounded |

**Why this section exists.** Without decay, the active graph only grows, discovery's candidate space grows with it, and stale links crowd the pages and alerts. With it, candidate generation runs against a graph whose size tracks current activity rather than accumulated history, so cost and noise stay roughly flat as the corpus grows. Backfill (§4.10) leans on the same mechanism: old events enter pre-decayed instead of flooding the present.

Dashboard: active edges per entity, archive rate, revival rate. A rising revival rate means half-lives are too short; endless growth means too long.

---

## 10. Quality control

### 10.1 Contradictions

Conflicting claims on the same subject–predicate with overlapping validity go to an adjudication queue — not silently coexisting, not overwriting. Resolution is automatic where safe (prefer higher-reliability lineage, prefer more recent `valid_from`), escalating to us above a materiality threshold.

### 10.2 Claim staleness

Claim confidence decays by predicate type: a headcount claim ages fast, an incorporation date doesn't. Per-predicate config. (Edge relevance is the separate mechanism in §9.)

### 10.3 Source reliability

Publications, feeds, and individual speakers carry reliability scores computed from how their past claims fared against later evidence. Scores feed claim confidence at extraction and lineage weighting at verification.

### 10.4 Adversarial inputs

Three attack shapes matter for an investment tool, where poisoned data means bad trades:

1. **Coordinated narratives** — pump-and-dumps, short attacks. Lineage counting doesn't catch these because the sources really are separate.
2. **AI content farms** — many "independent" outlets that are one operation, defeating independence counting.
3. **Prompt injection** — documents containing text written to steer the agents that read them.

Defenses:

- **The sandbox is border control.** New sources only enter through scouting (§4.7), and provisional claims can't serve as promotion evidence.
- **Unproven sources count as one.** A burst of new or low-reliability sources agreeing counts as a single lineage until an established source corroborates. High-materiality promotions require at least one established lineage.
- **Anomaly flag.** A spike of claims on one entity from low-reliability sources goes to review instead of auto-processing.
- **Documents are data.** Agent prompts keep instructions structurally separate from corpus content; imperative text inside a document is just text. Agents can't take any action based on document content except fetching and searching more corpus, and evidence is claim IDs only (§8.4).

### 10.5 Invariants

The failure mode of self-growing systems, restated as the short list that must never be relaxed: inferred edges used as evidence, promotion without independent lineages, destructive merges, extraction without claim-level provenance. Each produces errors that compound silently. They live in the schema, not in policy.

---

## 11. Serving

**API-first.** GraphQL over the graph plus hybrid search (BM25 + vector). The webapp is a thin client. Three users, so no tenancy machinery — just accounts and the `internal` flag on uploaded docs (§4.6).

Four surfaces:

1. **Entity pages.** A company as a timeline of claims, each expandable to source, confidence, lineage. "As-of" rendering: the page as it would have looked on any past date.
2. **Graph explorer.** Neighborhood browsing. Asserted and inferred edges look different; decayed edges fade and archived ones hide behind a toggle; every inferred edge is one tap from its evidence trail.
3. **Alerts.** Subscriptions on entities, claim types, and hypothesis promotions, fed by the fast path, delivered wherever we want them (push, email, chat). The contract: "tell me the moment anything material lands on these 40 names."
4. **Morning digest.** Per watchlist: what landed overnight, what got promoted, which parked hypotheses woke up.

Every accept, reject, flag, and save we make in the app is a calibration label. The evidence trail is the point of the product: aggregators show conclusions, this shows its work.

---

## 12. Build order and metrics

Phases are a build order, not a schedule.

**Phase 1 — vertical slice.** 300–500 companies in one sector, full loop end to end: monitoring, extraction, resolution, graph, discovery, minimal entity page. Two numbers gate everything else:

- **ER merge precision** on a labeled eval set: ~0.99 before scaling. Precision over recall — a missed match is recoverable, a bad merge poisons queries.
- **Promoted-link precision** as judged by us: ~0.7 to start, tightening as the verifier calibrates. Below ~0.5 the discovery layer is making work, not value.

Three small labeled sets get built during this phase, and they are the highest-leverage early investment: ER merge decisions, promoted-link judgments, and a gold extraction set (a few hundred documents across source types, measuring claim precision/recall per extractor version — this is what gates extractor upgrades).

**Phase 2.** More sectors and source classes. Scouting on. Backfill the head. Strategy bandit on. Review step stays on high-materiality promotions.

**Phase 3.** Registry shells for the long tail, broad monitoring across the head, decay tuning against real usage.

**Dashboard from day one:** freshness per source class (fast-path p50/p95; filings should alert under a minute), extraction throughput and unit cost, ER precision/recall, contradiction backlog size and age, hypothesis funnel conversion per stage, cost per promoted link, parked-hypothesis wake rate, active edges per entity, archive/revival rates.

**What the three of us actually do in the loop:** review promoted high-materiality links (minutes a day), approve source admissions, occasionally label eval samples, watch the cost dashboard. Everything else runs itself.

---

## 13. Reference stack

Shapes are the commitment; vendors are swappable within each shape.

| Layer | Reference choice | Notes |
|---|---|---|
| Event bus | Kafka or Redpanda | The append-only log is the commitment. |
| Raw artifacts | S3-compatible object storage | Per-source buckets and retention. |
| Metadata | Postgres | Mentions, hypotheses, schedules, audit. |
| Graph | Neo4j, or Postgres + Apache AGE | Whichever is easiest to run; revisit past ~100M edges. |
| Vector index | pgvector, Qdrant at scale | Entity + chunk embeddings. |
| Orchestration | Temporal | Durable workflows fit parked hypotheses and long investigations. |
| ASR | Whisper-class + diarization (pyannote) | Podcasts and video become documents. |
| LLMs | Tiered: small for triage/routine extraction, strong for dense extraction, hypotheses, verification | Tiering is the commitment; models will change often. |
| API | GraphQL + hybrid search | Thin webapp client. |

---

## 14. Settled vs. open

**Settled — would defend as stated:**

- Claims as the atomic unit, edges as views (provenance, coexisting contradictions, explainability all follow; nothing provides them retroactively)
- Append-only log, rebuildable graph (re-running improved extractors over history is the only clean upgrade path)
- Bitemporality from day one (can't be retrofitted cheaply)
- Asserted/inferred firewall in the data layer, promoted-edges-as-context-only (prompt-level enforcement is not enforcement)
- Reversible merges: same_as, alias nodes, mention records
- Small type spine + open predicates + versioned gardener mapping
- Funnel economics for discovery (pairwise LLM brainstorming is combinatorially impossible at any model quality)
- Falsifiability requirement on hypotheses (cheapest filter for testable vs. plausible-sounding)
- Claim-ID grounding of all agent evidence (verifies instead of trusts)
- Lineage dedup in verification (syndication is everywhere; counting mirrors inflates confidence system-wide)
- Inverse co-mention as the novelty core (directly measures "links few people have made")
- Relevance decay separate from confidence, archive-not-delete, query-time computation (§9)
- Structured data bypasses the LLM (§4.5)
- Fast/slow path split
- Phase-1 gates: ER precision and promoted-link precision, measured before widening scope

**Open — only real data settles these:**

Gardener clustering thresholds and cadence. Triage weights. Verifier lineage thresholds per edge type. Half-lives and the archive threshold (§9 table is a starting guess). GNN vs. classical link prediction. Graph store at scale. Actual hit rates and cost per promoted link — the economics can't be honestly estimated in advance; phase 1 exists to measure them. ASR quality on low-production podcasts. Alias coverage in weak-registry jurisdictions. How often parked hypotheses wake usefully. Scouting admission rates. Model choices at every tier.

---

## 15. Failure modes

- **Graph poisoning.** Covered by the firewall, lineage counting, claim grounding, and §10.4. The residual risk is us relaxing an invariant "temporarily" under pressure. The invariants are schema, not policy.
- **Silent merge corruption.** A wrong same_as fuses two companies. Mitigations: reversibility, the eval set, alerts on post-merge contradiction spikes.
- **Cost blowout.** Agent layers fail economically before technically. Mitigations: hard per-hypothesis budgets, the bandit, tiered models, learned polling, decay bounding the candidate space, and cost-per-promoted-link on the main dashboard.
- **Circular sourcing.** Wire syndication makes one report look like ten. Lineage at ingestion is the fix; it cannot be reconstructed later.
- **Coordinated fakes.** Genuinely separate sources that are one operation. §10.4: unproven sources count as one lineage until an established source corroborates.
- **Stale links.** Old claims and links presented as current. Per-predicate confidence decay, edge relevance decay, and as-of rendering keep the tool honest about what it knew and when.
- **Graph bloat.** Unbounded growth of active edges degrades discovery and UI. Decay + archive (§9), watched via active-edges-per-entity.
- **Source fragility.** Premium sources change formats and terms. Per-source buckets and connector isolation localize the blast radius. (Licensing handled separately, per project owner.)

---

*The two prompts that will absorb the most iteration: the hypothesis agent's (typed, falsifiable output) and the verifier's (skeptical enough to kill weak links, not so skeptical it kills everything). The three labeled sets from phase 1 are what make that iteration measurable.*
