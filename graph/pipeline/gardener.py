"""Predicate gardener (design §5.3): the open raw-predicate vocabulary gets a
versioned canonical mapping so queries don't drown in synonym soup. Claims
keep their raw predicates; each run writes a FULL mapping as a new version —
append-only, never a rewrite.
"""
import re

from .. import config, llm

TRIGGER_FIRST = 30      # distinct raws before the first mapping exists
TRIGGER_UNMAPPED = 20   # distinct raws absent from the current version


def run(con) -> dict:
    raws = con.execute(
        "select lower(predicate_raw) as raw, count(*) as n from claim "
        "where status='asserted' group by 1 order by count(*) desc, 1").fetchall()
    latest = con.execute("select max(version) as v from predicate_map"
                         ).fetchone()["v"]
    mapped = set() if latest is None else {r["predicate_raw"] for r in con.execute(
        "select predicate_raw from predicate_map where version=%s", (latest,))}
    unmapped = [r for r in raws if r["raw"] not in mapped]

    triggered = (len(raws) >= TRIGGER_FIRST if latest is None
                 else len(unmapped) >= TRIGGER_UNMAPPED)
    if not triggered:
        print(f"garden: not triggered ({len(raws)} raws, "
              f"{len(unmapped)} unmapped)")
        return {"triggered": False, "raws": len(raws),
                "unmapped": len(unmapped)}

    prompt = ((config.PROMPTS / "gardener.md").read_text()
              + "\n\n# Raw predicates (predicate, claim count)\n\n"
              + "\n".join(f"{r['raw']}, {r['n']}" for r in raws))
    try:
        out = llm.complete_json(prompt, config.MODELS["garden"])
    except llm.EngineUnavailable as e:
        print(f"garden: paused, no model available ({e})")
        return {"triggered": True, "paused": True}

    mapping = out.get("mapping")
    if not isinstance(mapping, dict):
        raise ValueError("gardener response has no mapping object")

    # code disposes: canons snake_case, every input raw exactly once (first
    # assignment wins), unknown raws dropped, missing raws self-mapped
    input_set = {r["raw"] for r in raws}
    assigned = {}
    duplicates = 0
    for canon_name, raw_list in mapping.items():
        canon_norm = re.sub(r"[^a-z0-9]+", "_", str(canon_name).lower()).strip("_")
        if not canon_norm or not isinstance(raw_list, list):
            continue
        for raw in raw_list:
            key = str(raw).lower()
            if key not in input_set:
                continue
            if key in assigned:
                duplicates += 1
                continue
            assigned[key] = canon_norm
    if duplicates:
        print(f"garden: {duplicates} raws assigned more than once; first kept")
    self_mapped = 0
    for r in raws:
        if r["raw"] not in assigned:
            assigned[r["raw"]] = r["raw"]
            self_mapped += 1

    version = (latest or 0) + 1
    with con.cursor() as cur:
        cur.executemany(
            "insert into predicate_map (version, predicate_raw, predicate_canon) "
            "values (%s, %s, %s)",
            [(version, raw, canon) for raw, canon in sorted(assigned.items())])

    canons = len(set(assigned.values()))
    print(f"garden: version {version}, {len(assigned)} raws -> {canons} canons "
          f"({self_mapped} self-mapped)")
    return {"triggered": True, "version": version, "raws": len(assigned),
            "canons": canons, "self_mapped": self_mapped,
            "duplicates": duplicates}
