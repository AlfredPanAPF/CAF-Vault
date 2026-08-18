-- Sources overhaul (build spec v4): watchlist resolved from Yahoo Finance,
-- one-off link queue, premium-site credentials, per-source health.

-- watchlist: resolved company facts (yfinance first, SEC registry fallback)
alter table watchlist
    add column name        text,
    add column industry    text,
    add column exchange    text,
    add column country     text,
    add column currency    text,
    add column quote_type  text,
    add column website     text,
    add column cik         bigint,
    add column resolver    text,           -- 'yfinance' | 'sec' | 'manual' | 'seed'
    add column resolved_at timestamptz;

-- sources: per-source health surfaced in the UI ("Sign-in needed", ...)
alter table source add column last_error text;
alter table source add column last_error_at timestamptz;

-- one-off links pasted into the sources page (articles, videos, posts)
create table link_queue (
    link_id     uuid primary key default gen_random_uuid(),
    url         text not null,
    kind        text not null check (kind in
                ('article','substack_post','youtube_video','x_post')),
    site        text,                              -- ft | wsj | substack | youtube | x | null
    source_id   uuid references source,            -- link:<host> bucket
    status      text not null default 'queued'
                check (status in ('queued','done','duplicate','blocked','failed')),
    attempts    int not null default 0,
    event_id    uuid references event,
    title       text,
    error       text,
    added_by    text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create unique index link_queue_url_uniq on link_queue (url);
create index on link_queue (status, created_at);

-- credentials for premium sites; the value never leaves the API
create table credential (
    site          text primary key check (site in ('ft','wsj','substack','x','youtube')),
    kind          text not null check (kind in ('cookies','bearer')),
    value         text not null,
    note          text,
    updated_at    timestamptz not null default now(),
    checked_at    timestamptz,
    check_ok      boolean,
    check_message text
);
