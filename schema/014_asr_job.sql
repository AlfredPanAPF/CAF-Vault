-- Remote ASR queue (build spec v7 §2). One row per audio item awaiting a
-- transcript from the off-site agent. The unique (connector, external_id)
-- makes enqueue idempotent across worker cycles; a terminal 'error' row
-- deliberately blocks re-enqueue (delete the row to retry).

create table asr_job (
    job_id        uuid primary key default gen_random_uuid(),
    source_id     uuid not null references source(source_id),
    connector     text not null check (connector in ('podcast','youtube')),
    external_id   text not null,
    title         text not null default '',
    published_at  text,                 -- carried verbatim to envelope.ingest
    audio_url     text,                 -- podcast: agent downloads the enclosure
    audio_path    text,                 -- youtube: server-held audio in the spool
    meta          jsonb not null default '{}'::jsonb,  -- doc_prefix + event_meta
    status        text not null default 'pending'
                  check (status in ('pending','leased','done','error')),
    attempts      int not null default 0,
    leased_by     text,
    lease_expires timestamptz,
    not_before    timestamptz,          -- retry backoff after a failed attempt
    error         text,
    event_id      uuid references event(event_id),
    created_at    timestamptz not null default now(),
    done_at       timestamptz
);

create unique index asr_job_external on asr_job (connector, external_id);
create index asr_job_queue on asr_job (status, created_at);
