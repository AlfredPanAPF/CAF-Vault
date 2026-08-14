"""Materialize asserted edges from claims (design §2, §7, §9).

Edges with origin='asserted' are a rebuildable view over asserted claims: every
run deletes them and regroups from scratch. Inferred edges are untouched.
"""
from .. import config


def half_life_for(predicate: str):
    """First HALF_LIFE_RULES entry with a keyword contained in the predicate wins;
    None means structural (no decay) and becomes SQL null."""
    for keywords, days in config.HALF_LIFE_RULES:
        if any(k in predicate for k in keywords):
            return days
    return config.HALF_LIFE_DEFAULT


def run(con) -> dict:
    con.execute("delete from edge where origin='asserted'")
    groups = con.execute(
        """
        select subject_entity as src, object_entity as dst,
               lower(predicate_raw) as predicate,
               array_agg(claim_id) as claim_ids,
               max(confidence) as confidence,
               min(valid_from) as valid_from,
               case when bool_or(valid_to is null) then null
                    else max(valid_to) end as valid_to,
               max(observed_at) as last_evidence_at
        from claim
        where status = 'asserted'
          and subject_entity is not null
          and object_entity is not null
        group by subject_entity, object_entity, lower(predicate_raw)
        """).fetchall()
    for g in groups:
        con.execute(
            "insert into edge (src, dst, predicate, origin, claim_ids, valid_from, "
            "valid_to, confidence, last_evidence_at, half_life_days) "
            "values (%s, %s, %s, 'asserted', %s, %s, %s, %s, %s, %s)",
            (g["src"], g["dst"], g["predicate"], g["claim_ids"], g["valid_from"],
             g["valid_to"], g["confidence"], g["last_evidence_at"],
             half_life_for(g["predicate"])))
    print(f"materialize: {len(groups)} asserted edges")
    return {"edges": len(groups)}
