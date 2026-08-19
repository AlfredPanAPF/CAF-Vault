-- Per-document lookups the documents pages run (build spec v5 §5): the list
-- counts a document's mentions per row, the detail page finds the edges and
-- hypotheses its claims back. None of these columns were indexed before.
create index on mention (event_id);
create index on edge using gin (claim_ids);
create index on hypothesis using gin (evidence);
