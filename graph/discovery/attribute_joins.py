"""Shared-attribute join discovery (design §8.1 signal #3).

Two companies that both point at the same third entity through dependency-ish
predicates, with no existing edge between them, become a 'shared_dependency'
hypothesis. Evidence is asserted-layer claim ids only (§8.4 firewall).
"""
from psycopg.types.json import Jsonb

PATTERNS = ["%suppl%", "%customer%", "%audit%", "%agent%", "%board%",
            "%backs%", "%partner%", "%invest%", "%contract%"]

TEST_PLAN = {
    "confirm": ["independent source naming both A-C and B-C relationships",
                "filing risk-factor mention"],
    "refute": ["the shared mention is generic/sector-wide commentary"],
}


def _side_claims(con, subject, via):
    return con.execute(
        "select claim_id, predicate_raw, evidence_quote from claim "
        "where status = 'asserted' and subject_entity = %s and object_entity = %s "
        "and predicate_raw ilike any(%s)",
        (subject, via, PATTERNS)).fetchall()


def run(con) -> dict:
    pairs = con.execute(
        """
        with dep as (
            select c.subject_entity as ent, c.object_entity as via
            from claim c
            join entity s on s.entity_id = c.subject_entity
            where c.status = 'asserted'
              and c.subject_entity is not null
              and c.object_entity is not null
              and c.subject_entity <> c.object_entity
              and s.kind = 'company'
              and c.predicate_raw ilike any(%s)
        )
        select distinct a.ent as a, b.ent as b, a.via as via
        from dep a
        join dep b on b.via = a.via and a.ent < b.ent
        where not exists (
            select 1 from edge e
            where (e.src = a.ent and e.dst = b.ent)
               or (e.src = b.ent and e.dst = a.ent))
        order by a, b, via
        limit 50
        """, (PATTERNS,)).fetchall()

    inserted = 0
    for p in pairs:
        exists = con.execute(
            "select 1 from hypothesis where type = 'shared_dependency' "
            "and subjects = %s", ([p["a"], p["b"]],)).fetchone()
        if exists:
            continue
        names = {r["entity_id"]: r["canonical_name"] for r in con.execute(
            "select entity_id, canonical_name from entity where entity_id = any(%s)",
            ([p["a"], p["b"], p["via"]],))}
        a_claims = _side_claims(con, p["a"], p["via"])
        b_claims = _side_claims(con, p["b"], p["via"])
        preds = "/".join(sorted({r["predicate_raw"].lower()
                                 for r in a_claims + b_claims}))
        quotes = "; ".join('"%s"' % r["evidence_quote"]
                           for r in (a_claims[0], b_claims[0])
                           if r["evidence_quote"])
        rationale = (f"{names[p['a']]} and {names[p['b']]} both have {preds} "
                     f"claims pointing at {names[p['via']]}"
                     + (f" ({quotes})" if quotes else "") + ".")
        con.execute(
            "insert into hypothesis (type, subjects, statement, rationale, "
            "test_plan, origin, budget, state, evidence) values "
            "('shared_dependency', %s, %s, %s, %s, %s, %s, 'generated', %s)",
            ([p["a"], p["b"]],
             Jsonb({"template": f"shared dependency via {names[p['via']]}",
                    "via_entity": str(p["via"])}),
             rationale,
             Jsonb(TEST_PLAN),
             Jsonb({"strategy": "attribute_join_v0", "via": str(p["via"])}),
             Jsonb({"tokens": 0}),
             [r["claim_id"] for r in a_claims + b_claims]))
        inserted += 1
    print(f"discover: {inserted} shared_dependency hypotheses")
    return {"hypotheses": inserted}
