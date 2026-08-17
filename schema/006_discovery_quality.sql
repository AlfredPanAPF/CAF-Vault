-- Maturity phase (build spec v3): discovery lifecycle, in-app alerts,
-- contradiction queue, near-dup + reliability fields, calibration labels.
-- Append-only invariants hold: nothing here rewrites claims or entities.

-- discovery lifecycle
alter table hypothesis add column score real;              -- triage score (§8.2)
alter table hypothesis add column parked_at timestamptz;   -- set on park (§8.6)

-- calibration labels (§8.7): every human verdict in the review surfaces
create table review_label (
    label_id    uuid primary key default gen_random_uuid(),
    kind        text not null check (kind in ('hypothesis','er','contradiction')),
    target      uuid not null,          -- hypothesis_id / mention_id / contradiction_id
    verdict     text not null,
    note        text,
    created_at  timestamptz not null default now()
);

-- in-app alerts (§11.3); team-wide read state, three users, no per-user fanout
create table alert (
    alert_id      uuid primary key default gen_random_uuid(),
    kind          text not null check (kind in
                  ('material_event','promoted_link','hypothesis_wake','contradiction')),
    title         text not null,
    body          text,
    entity_ids    uuid[] not null default '{}',
    event_id      uuid references event,
    hypothesis_id uuid references hypothesis,
    created_at    timestamptz not null default now(),
    read_at       timestamptz
);
create index on alert (created_at desc);
create index on alert (read_at) where read_at is null;
-- one alert per (kind, event) and per (kind, hypothesis)
create unique index alert_event_uniq on alert (kind, event_id)
    where event_id is not null;
create unique index alert_hyp_uniq on alert (kind, hypothesis_id)
    where hypothesis_id is not null;

-- contradiction queue (§10.1)
create table contradiction (
    contradiction_id uuid primary key default gen_random_uuid(),
    subject_entity   uuid not null references entity,
    predicate_canon  text not null,
    claim_a          uuid not null references claim,
    claim_b          uuid not null references claim,
    kind             text not null check (kind in ('object_conflict','literal_conflict')),
    status           text not null default 'open'
                     check (status in ('open','auto_resolved','resolved','dismissed')),
    resolution       jsonb,
    created_at       timestamptz not null default now(),
    resolved_at      timestamptz,
    unique (claim_a, claim_b)
);
create index on contradiction (status);

-- near-duplicate detection (§4.4): 64-bit simhash of the normalized text,
-- stored signed as postgres has no unsigned bigint
alter table event add column simhash bigint;

-- source reliability detail (§10.3); the score itself is source.reliability
alter table source add column reliability_detail jsonb;
