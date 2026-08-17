-- User-facing source management: feeds and the watchlist move from code/files
-- into the database so the webapp and CLI can add, pause, and inspect them.

alter table source add column config jsonb;          -- per-source settings
alter table source add column last_polled timestamptz;
alter table source add column added_by text;         -- 'seed' | 'web' | 'cli'

create table watchlist (
    ticker      text primary key,
    sector      text,
    active      boolean not null default true,
    added_at    timestamptz not null default now(),
    added_by    text
);
