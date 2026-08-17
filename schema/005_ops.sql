-- Ops surface (build spec v2 §1): worker stage bookkeeping + a tiny KV for
-- heartbeat and run-now signalling between the worker and the webapp.

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
