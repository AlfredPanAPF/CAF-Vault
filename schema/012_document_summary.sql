-- One summary per document (build spec v5). Derived data, like edges:
-- rebuildable from the artifact, overwritten when rewritten. The row doubles
-- as the queue entry: 'requested' is the page asking, 'pending' is an
-- automatic attempt that failed once, 'failed' is two strikes, 'skipped' is a
-- document too short to summarize.
create table document_summary (
    event_id     uuid primary key references event,
    status       text not null
                 check (status in ('requested','pending','done','failed','skipped')),
    summary      text,                       -- two to four sentences
    key_points   jsonb,                      -- list of strings
    model        text,
    attempts     integer not null default 0,
    error        text,
    requested_at timestamptz,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);
create index on document_summary (status) where status in ('requested','pending');
