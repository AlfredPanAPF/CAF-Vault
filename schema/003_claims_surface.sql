-- Claims arrive before their objects are resolved: keep the object surface form
-- on the claim until ER links object_entity. Evidence quotes are product surface
-- (§10), so they are a real column, not a qualifier.

alter table claim add column object_surface text;
alter table claim add column evidence_quote text;
alter table claim drop constraint if exists claim_check;
alter table claim add constraint claim_object_check
    check (object_entity is not null or object_literal is not null
           or object_surface is not null);
