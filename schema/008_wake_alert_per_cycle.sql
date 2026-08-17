-- Wake alerts must recur: park -> wake -> park -> wake cycling is the designed
-- hypothesis lifecycle (design §8.6), and the digest's "woke" list is computed
-- from alert rows, so every wake needs its own row. No cross-time dedup is
-- required for hypothesis_wake — wake.py emits inside the same transaction
-- that moves the hypothesis out of 'parked', so it can fire at most once per
-- park cycle. The unique index keeps deduping the other hypothesis-carrying
-- kind (promoted_link).
drop index alert_hyp_uniq;
create unique index alert_hyp_uniq on alert (kind, hypothesis_id)
    where hypothesis_id is not null and kind <> 'hypothesis_wake';
