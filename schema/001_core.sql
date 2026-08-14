-- CAF Company Graph — core schema (v0, translated from docs/company-graph-design.md)
-- Postgres 16+. pgvector is used for embeddings when the vector index moves in-db.
--
-- Invariants enforced here, not by convention (design §2, §10.5):
--   * claims are append-only: status changes, rows are never deleted
--   * merges are same_as rows, never rewrites
--   * edges carry origin (asserted|inferred) and their backing claim ids
--   * predicate canonicalization is a versioned mapping, never a rewrite

create extension if not exists "pgcrypto";  -- gen_random_uuid()
-- create extension if not exists vector;   -- enable when embeddings move in-db

-- ---------------------------------------------------------------- sources

create table source (
    source_id       uuid primary key default gen_random_uuid(),
    name            text not null,
    connector       text not null,              -- edgar | rss | podcast | manual | ...
    url             text,
    status          text not null default 'active'
                    check (status in ('sandbox', 'active', 'demoted', 'dropped')),
    is_internal     boolean not null default false,   -- uploaded docs (§4.6)
    reliability     real,                       -- learned score (§10.3)
    poll_schedule   jsonb,                      -- learned cadence (§4.2)
    created_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------- ingestion

-- event envelope (§4.3); one row per fetched artifact
create table event (
    event_id        uuid primary key default gen_random_uuid(),
    source_id       uuid not null references source,
    connector       text not null,
    fetched_at      timestamptz not null default now(),
    published_at    timestamptz,                -- as claimed by the source
    content_hash    bytea not null,             -- sha256, dedup across mirrors
    artifact_uri    text not null,              -- raw bytes in object storage
    mime            text,
    triage          jsonb,                      -- {materiality, entities, route}
    lineage_id      uuid                        -- set after dedup; fk added below
);
create index on event (content_hash);
create index on event (source_id, fetched_at);

-- reporting-origin lineage (§4.4); mirrors attach to the root
create table lineage (
    lineage_id      uuid primary key default gen_random_uuid(),
    root_event_id   uuid not null references event,
    created_at      timestamptz not null default now()
);
alter table event add constraint event_lineage_fk
    foreign key (lineage_id) references lineage;

-- ---------------------------------------------------------------- entities

create table entity (
    entity_id       uuid primary key default gen_random_uuid(),
    kind            text not null
                    check (kind in ('company','person','product','security','event',
                                    'filing','location','patent','org_other')),
    canonical_name  text not null,
    registry_refs   jsonb,                      -- {cik, lei, ticker, ...} seed identifiers
    created_at      timestamptz not null default now()
);
create index on entity (kind);

-- former names, brands, tickers, transliterations (§6); script-aware
create table entity_alias (
    alias_id        uuid primary key default gen_random_uuid(),
    entity_id       uuid not null references entity,
    alias           text not null,
    alias_kind      text not null
                    check (alias_kind in ('former_name','brand','ticker','domain',
                                          'transliteration','registry_id','abbreviation')),
    lang            text,
    valid_from      date,
    valid_to        date,
    source_claim    uuid                        -- claim that established the alias
);
create index on entity_alias (alias);

-- reversible merges (§2 rule 5); never rewrite entity rows
create table entity_same_as (
    a               uuid not null references entity,
    b               uuid not null references entity,
    status          text not null default 'active'
                    check (status in ('active','reverted')),
    decided_by      text not null,              -- resolver version or human
    note            text,
    created_at      timestamptz not null default now(),
    primary key (a, b, created_at)
);

-- every resolution decision is auditable and re-runnable (§6)
create table mention (
    mention_id      uuid primary key default gen_random_uuid(),
    event_id        uuid not null references event,
    span_start      integer,
    span_end        integer,
    surface         text not null,
    resolved_entity uuid references entity,     -- null = unresolved
    resolver        text not null,              -- model/rule + version
    confidence      real,
    created_at      timestamptz not null default now()
);
create index on mention (resolved_entity);
create index on mention (surface);

-- ---------------------------------------------------------------- claims

-- the core record (§5.2); append-only
create table claim (
    claim_id        uuid primary key default gen_random_uuid(),
    subject_entity  uuid references entity,
    subject_surface text,                       -- kept when provisional
    predicate_raw   text not null,              -- open vocabulary at write time
    object_entity   uuid references entity,
    object_literal  jsonb,                      -- {value, unit, currency, usd_value, as_of}
    qualifiers      jsonb,                      -- role, jurisdiction, stance, attributed_to...
    event_id        uuid not null references event,
    lineage_id      uuid not null references lineage,
    observed_at     timestamptz not null,
    valid_from      timestamptz,
    valid_to        timestamptz,
    confidence      real not null,
    extractor       text not null,              -- model + version
    status          text not null default 'asserted'
                    check (status in ('asserted','superseded','retracted')),
    superseded_by   uuid references claim,
    check (object_entity is not null or object_literal is not null)
);
create index on claim (subject_entity, predicate_raw);
create index on claim (event_id);
create index on claim (lineage_id);

-- versioned gardener mapping (§5.3); claims keep raw predicates,
-- queries resolve through the current mapping version
create table predicate_map (
    version         integer not null,
    predicate_raw   text not null,
    predicate_canon text not null,
    created_at      timestamptz not null default now(),
    primary key (version, predicate_raw)
);

-- ---------------------------------------------------------------- graph

-- edges are materialized views over claims; rebuildable (§7)
create table edge (
    edge_id         uuid primary key default gen_random_uuid(),
    src             uuid not null references entity,
    dst             uuid not null references entity,
    predicate       text not null,              -- canonical
    origin          text not null check (origin in ('asserted','inferred')),
    claim_ids       uuid[] not null,
    evidence_trail  jsonb,                      -- verifier output for inferred edges (§8.5)
    valid_from      timestamptz,
    valid_to        timestamptz,
    confidence      real not null,
    -- decay fields (§9): relevance computed at query time, never stored
    last_evidence_at timestamptz not null,
    half_life_days  integer,                    -- null = structural, no decay
    archived        boolean not null default false
);
create index on edge (src);
create index on edge (dst);
create index on edge (origin);

-- relevance at query time (§9)
create or replace function edge_relevance(e edge, at timestamptz default now())
returns real language sql immutable as $$
    select case
        when e.half_life_days is null then 1.0
        else power(2.0, -extract(epoch from (at - e.last_evidence_at))
                        / (e.half_life_days * 86400.0))::real
    end
$$;

-- ---------------------------------------------------------------- discovery

-- hypothesis record (§8.3)
create table hypothesis (
    hypothesis_id   uuid primary key default gen_random_uuid(),
    type            text not null,              -- typed relation
    subjects        uuid[] not null,
    statement       jsonb not null,             -- structured claim template
    rationale       text,
    test_plan       jsonb not null,             -- {confirm: [], refute: []} — required
    origin          jsonb not null,             -- strategy_id + signal snapshot
    budget          jsonb not null,             -- {tokens, tool_calls}
    state           text not null default 'generated'
                    check (state in ('generated','triaged','investigating',
                                     'promoted','parked','refuted')),
    evidence        uuid[] not null default '{}',   -- claim ids, asserted layer only
    lineages        uuid[] not null default '{}',   -- independent source lines
    confidence      real,
    wake_conditions jsonb,                      -- entity + claim-type filters (§8.6)
    history         jsonb not null default '[]',
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create index on hypothesis (state);

-- the firewall (§8.4): investigation agents query this view, which contains
-- asserted claims only. enforced here, not in prompts.
create view claim_asserted as
    select * from claim where status = 'asserted';
