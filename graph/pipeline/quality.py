"""Quality layer (design §10.1, §10.3): contradiction detection between
asserted claims and source reliability scoring. Two worker stages, no LLM.
Claim staleness (§10.2) lives in graph/staleness.py; both stages resolve
predicates through the gardener's canonical mapping (§5.3).
"""
from collections import defaultdict
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .. import alerts

# predicates that hold one value at a time (§10.1): a canon containing any of
# these keywords makes two different objects with overlapping validity a conflict
SINGLE_VALUED = ("ceo", "cfo", "chair", "headquarter", "incorporat", "ticker",
                 "parent", "owner")
LITERAL_REL_DIFF = 0.05


def predicate_canon_map(con) -> dict:
    """{raw: canon} from the latest predicate_map version; {} when unmapped."""
    latest = con.execute("select max(version) as v from predicate_map"
                         ).fetchone()["v"]
    if latest is None:
        return {}
    return {r["predicate_raw"]: r["predicate_canon"] for r in con.execute(
        "select predicate_raw, predicate_canon from predicate_map where version=%s",
        (latest,))}


def canon(pmap: dict, predicate: str) -> str:
    p = (predicate or "").lower()
    return pmap.get(p, p)


def _overlaps(a, b) -> bool:
    """Validity intervals overlap; a null bound is open (§10.1)."""
    if a["valid_from"] and b["valid_to"] and a["valid_from"] > b["valid_to"]:
        return False
    if b["valid_from"] and a["valid_to"] and b["valid_from"] > a["valid_to"]:
        return False
    return True


def _literal_value(claim):
    lit = claim["object_literal"]
    if not isinstance(lit, dict):
        return None
    v = lit.get("value")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _literal_key(claim):
    """Two literals are comparable only on the same period and denomination —
    "€3.2B" vs "$3.5B" is a currency difference, not a conflict (§5.4)."""
    lit = claim["object_literal"]
    return (lit.get("as_of") or lit.get("period"), lit.get("currency"),
            lit.get("unit"))


def _watchlist_entities(con) -> set:
    return {r["entity_id"] for r in con.execute(
        "select e.entity_id from entity e join watchlist w "
        "on w.ticker = e.registry_refs->>'ticker' where w.active")}


def _raw_subject(con, claim, cache):
    """The claim's subject identity BEFORE merge mapping: link_claims bakes the
    same_as canonical into claim.subject_entity, but the resolving mention
    keeps the raw resolved_entity for audit. Claims without a resolving
    mention were linked directly and keep their stored subject."""
    key = claim["claim_id"]
    if key not in cache:
        row = con.execute(
            "select resolved_entity from mention "
            "where event_id=%s and surface=%s and resolved_entity is not null "
            "limit 1", (claim["event_id"], claim["subject_surface"])).fetchone()
        cache[key] = row["resolved_entity"] if row else claim["subject_entity"]
    return cache[key]


def contradictions(con) -> dict:
    """Detect conflicting asserted claims (§10.1). Same-lineage pairs
    auto-resolve (newer supersedes); cross-lineage pairs stay open, alerting
    when the subject is on the active watchlist."""
    pmap = predicate_canon_map(con)
    claims = con.execute(
        "select claim_id, subject_entity, subject_surface, predicate_raw, "
        "object_entity, object_literal, lineage_id, event_id, observed_at, "
        "valid_from, valid_to "
        "from claim where status='asserted' and subject_entity is not null"
    ).fetchall()
    groups = defaultdict(list)
    for c in claims:
        groups[(c["subject_entity"], canon(pmap, c["predicate_raw"]))].append(c)

    watchlist = _watchlist_entities(con)
    counts = {"object_conflict": 0, "literal_conflict": 0}
    auto_resolved = alerted = 0
    superseded = set()   # claims retired this run never enter a later pair
    raw_subjects = {}    # claim_id -> pre-merge subject identity (memoized)

    for (subject, pcanon), group in sorted(
            groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda c: str(c["claim_id"]))
        single = any(k in pcanon for k in SINGLE_VALUED)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a["claim_id"] in superseded or b["claim_id"] in superseded:
                    continue
                if not _overlaps(a, b):
                    continue
                kind = None
                if (single and a["object_entity"] and b["object_entity"]
                        and a["object_entity"] != b["object_entity"]):
                    kind = "object_conflict"
                else:
                    va, vb = _literal_value(a), _literal_value(b)
                    if (va is not None and vb is not None
                            and _literal_key(a) == _literal_key(b)):
                        denom = max(abs(va), abs(vb))
                        if denom and abs(va - vb) / denom > LITERAL_REL_DIFF:
                            kind = "literal_conflict"
                if kind is None:
                    continue
                row = con.execute(
                    "insert into contradiction (subject_entity, predicate_canon, "
                    "claim_a, claim_b, kind) values (%s,%s,%s,%s,%s) "
                    "on conflict (claim_a, claim_b) do nothing "
                    "returning contradiction_id",
                    (subject, pcanon, a["claim_id"], b["claim_id"], kind)
                ).fetchone()
                if row is None:   # pair already recorded on an earlier run
                    continue
                counts[kind] += 1
                # same-lineage supersession only when the pair's identity is
                # merge-independent: if the subjects coincide solely because an
                # active merge re-pointed them, the pair must stay open for
                # review (design §15 — a wrong merge would otherwise write an
                # irreversible 'superseded' into the append-only log).
                if (a["lineage_id"] == b["lineage_id"]
                        and _raw_subject(con, a, raw_subjects)
                        == _raw_subject(con, b, raw_subjects)):
                    older, newer = sorted(
                        (a, b), key=lambda c: (c["observed_at"], str(c["claim_id"])))
                    cur = con.execute(
                        "update claim set status='superseded', superseded_by=%s "
                        "where claim_id=%s and status='asserted'",
                        (newer["claim_id"], older["claim_id"]))
                    # no longer asserted either way — never enters a later pair
                    superseded.add(older["claim_id"])
                    if cur.rowcount == 0:
                        # a concurrent resolve retired it first; leave the new
                        # pair open for human review, claim nothing
                        continue
                    con.execute(
                        "update contradiction set status='auto_resolved', "
                        "resolution=%s, resolved_at=now() where contradiction_id=%s",
                        (Jsonb({"kept": str(newer["claim_id"]),
                                "mode": "same_lineage"}), row["contradiction_id"]))
                    auto_resolved += 1
                elif subject in watchlist:
                    name = con.execute(
                        "select canonical_name from entity where entity_id=%s",
                        (subject,)).fetchone()["canonical_name"]
                    alerts.emit(con, "contradiction",
                                f"Conflicting claims on {name}",
                                entity_ids=[subject])
                    alerted += 1

    found = counts["object_conflict"] + counts["literal_conflict"]
    print(f"contradictions: {found} new ({counts['object_conflict']} object, "
          f"{counts['literal_conflict']} literal), {auto_resolved} auto-resolved, "
          f"{alerted} alerts")
    return {"found": found, "object": counts["object_conflict"],
            "literal": counts["literal_conflict"],
            "auto_resolved": auto_resolved, "alerts": alerted}


def reliability(con) -> dict:
    """Score sources by how their claims fared (§10.3): corroboration across
    independent lineages up, supersession down. Sources under 5 claims keep
    their existing score — too little signal to move it."""
    pmap = predicate_canon_map(con)
    rows = con.execute(
        "select c.subject_entity, c.predicate_raw, c.object_entity, "
        "c.lineage_id, c.status, e.source_id "
        "from claim c join event e on e.event_id = c.event_id").fetchall()

    lineages_by_key = defaultdict(set)
    for c in rows:
        if (c["status"] == "asserted" and c["subject_entity"]
                and c["object_entity"]):
            key = (c["subject_entity"], canon(pmap, c["predicate_raw"]),
                   c["object_entity"])
            lineages_by_key[key].add(c["lineage_id"])
    corroborated_keys = {k for k, ls in lineages_by_key.items() if len(ls) >= 2}

    by_source = defaultdict(list)
    for c in rows:
        by_source[c["source_id"]].append(c)

    scored = 0
    computed_at = datetime.now(timezone.utc).isoformat()
    for source_id, cs in by_source.items():
        n = len(cs)
        if n < 5:
            continue
        corroborated = sum(
            1 for c in cs
            if c["status"] == "asserted" and c["subject_entity"]
            and c["object_entity"]
            and (c["subject_entity"], canon(pmap, c["predicate_raw"]),
                 c["object_entity"]) in corroborated_keys)
        superseded = sum(1 for c in cs if c["status"] == "superseded")
        cs_share, ss_share = corroborated / n, superseded / n
        score = min(0.95, max(0.1, 0.5 + 0.3 * cs_share - 0.4 * ss_share))
        con.execute(
            "update source set reliability=%s, reliability_detail=%s "
            "where source_id=%s",
            (score, Jsonb({"n_claims": n,
                           "corroborated_share": round(cs_share, 3),
                           "superseded_share": round(ss_share, 3),
                           "computed_at": computed_at}), source_id))
        scored += 1

    print(f"reliability: {scored} sources scored")
    return {"sources": scored}
