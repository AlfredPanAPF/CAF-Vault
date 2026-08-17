"""Materialize asserted edges from claims (design §2, §7, §9).

Edges with origin='asserted' are a rebuildable view over asserted claims: every
run deletes them and regroups from scratch. Claims group by the canonical
predicate (gardener mapping, §5.3) and by canonical entities (active same_as
merges, §2 rule 5) — reverting a merge and rebuilding restores the old edges.
Inferred edges are untouched.
"""
from .. import config, db
from .quality import canon, predicate_canon_map


def half_life_for(predicate: str):
    """First HALF_LIFE_RULES entry with a keyword contained in the predicate wins;
    None means structural (no decay) and becomes SQL null."""
    for keywords, days in config.HALF_LIFE_RULES:
        if any(k in predicate for k in keywords):
            return days
    return config.HALF_LIFE_DEFAULT


def run(con) -> dict:
    pmap = predicate_canon_map(con)
    canonical = db.entity_canonical_map(con)
    con.execute("delete from edge where origin='asserted'")
    claims = con.execute(
        "select claim_id, subject_entity, object_entity, predicate_raw, "
        "confidence, valid_from, valid_to, observed_at from claim "
        "where status = 'asserted' and subject_entity is not null "
        "and object_entity is not null").fetchall()

    groups = {}
    for c in claims:
        src = canonical.get(c["subject_entity"], c["subject_entity"])
        dst = canonical.get(c["object_entity"], c["object_entity"])
        groups.setdefault((src, dst, canon(pmap, c["predicate_raw"])), []).append(c)

    for (src, dst, predicate), cs in groups.items():
        froms = [c["valid_from"] for c in cs if c["valid_from"] is not None]
        # any open-ended claim keeps the edge open (matches SQL bool_or semantics)
        valid_to = (None if any(c["valid_to"] is None for c in cs)
                    else max(c["valid_to"] for c in cs))
        con.execute(
            "insert into edge (src, dst, predicate, origin, claim_ids, valid_from, "
            "valid_to, confidence, last_evidence_at, half_life_days) "
            "values (%s, %s, %s, 'asserted', %s, %s, %s, %s, %s, %s)",
            (src, dst, predicate, [c["claim_id"] for c in cs],
             min(froms) if froms else None, valid_to,
             max(c["confidence"] for c in cs),
             max(c["observed_at"] for c in cs), half_life_for(predicate)))
    print(f"materialize: {len(groups)} asserted edges")
    return {"edges": len(groups)}
