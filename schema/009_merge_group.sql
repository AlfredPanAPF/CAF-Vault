-- Reversible merges, whole-operation edition (design §2 rule 5). A merge of A
-- into B also re-points every entity previously merged into A (chained rows);
-- merge_group ties the direct row and its chained rows to the one request that
-- created them, so unmerge can undo the entire operation, not just the direct
-- link. Nullable: rows predating this migration have no group and unmerge
-- falls back to reverting the direct row alone.
alter table entity_same_as add column merge_group uuid;

-- One active merge per merged-away entity. The merge endpoint's "already
-- merged" pre-check races under concurrency; this index is the backstop that
-- turns a double submit into a unique violation (surfaced as 409) instead of
-- duplicate active rows or an active cycle.
create unique index entity_same_as_one_active on entity_same_as (a)
    where status = 'active';
