-- The claim_asserted firewall view (001) is a select-* view, frozen at
-- creation, so it predates the 003 claim columns (object_surface,
-- evidence_quote) that the discovery agents serialize (design §8.4).
-- Recreate to pick them up; the asserted-only filter is unchanged.
create or replace view claim_asserted as
    select * from claim where status = 'asserted';
