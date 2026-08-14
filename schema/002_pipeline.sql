-- Pipeline operational state + ER queue + registry seeds.
-- Events stay append-only as records of what was fetched; status/attempts are
-- processing bookkeeping, not content mutation.

alter table event add column status text not null default 'pending'
    check (status in ('pending','extracting','extracted','resolved','failed','duplicate'));
alter table event add column attempts integer not null default 0;
alter table event add column last_error text;
alter table event add column meta jsonb;   -- connector context: ticker, feed, title...
create index on event (status);

-- SEC registrant seed (loaded by `graph seed`)
create table registry_sec (
    cik     bigint primary key,
    ticker  text not null,
    title   text not null
);
create index on registry_sec (ticker);

-- alias seed (normalized alias -> ticker), loaded from aliases.json
create table alias_seed (
    alias_norm  text primary key,
    ticker      text not null
);

-- ER adjudication queue: ambiguous mentions wait here for the LLM adjudicator
create table er_queue (
    mention_id  uuid primary key references mention,
    candidates  jsonb not null,
    status      text not null default 'pending'
                check (status in ('pending','decided','failed')),
    decision    jsonb,
    created_at  timestamptz not null default now(),
    decided_at  timestamptz
);
create index on er_queue (status);

-- lazy entity creation is keyed on registry identifiers
create unique index entity_cik_uniq on entity ((registry_refs->>'cik'))
    where registry_refs ? 'cik';
create unique index entity_lei_uniq on entity ((registry_refs->>'lei'))
    where registry_refs ? 'lei';

-- edge upsert key for the materializer
create unique index edge_identity_uniq on edge (src, dst, predicate, origin);
