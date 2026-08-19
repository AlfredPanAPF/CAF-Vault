-- Sessions derived from a stored credential, one per site and host (build
-- spec v4 §3.4). Substack custom domains (newsletter.semianalysis.com, ...)
-- run their own session: the substack.com sid is not honoured there, and the
-- publication host issues its own cookie (connect.sid) through Substack's
-- cross-domain sign-in. The fetcher mints one from the stored sid when it
-- first needs it and keeps it here. Values never leave the API; an empty
-- value records a sign-in that did not take, with expires_at saying when to
-- try again. Replacing or deleting the credential drops the rows.
create table credential_session (
    site       text not null references credential(site) on delete cascade,
    host       text not null,
    value      text not null,                 -- cookie header form, or '' when minting failed
    updated_at timestamptz not null default now(),
    expires_at timestamptz,
    primary key (site, host)
);
