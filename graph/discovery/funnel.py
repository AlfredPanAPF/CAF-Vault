"""Discovery triage (design §8.2, build spec v3 §3.1). Algorithmic, no LLM:
hypotheses in 'generated' get score = novelty * materiality * prior; the top
slice moves to 'triaged', the rest keep their score and are re-scored next
cycle. Also home to the small helpers every back-half stage shares.
"""
import uuid
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .. import config

MATERIALITY = {2: 1.0, 1: 0.7, 0: 0.4}   # subjects on the active watchlist


def append_history(con, hypothesis_id, entry: dict):
    """History is an append-only jsonb array; every entry carries "at" (iso-utc)."""
    con.execute(
        "update hypothesis set history = history || %s::jsonb, "
        "updated_at = now() where hypothesis_id = %s",
        (Jsonb({"at": datetime.now(timezone.utc).isoformat(), **entry}),
         hypothesis_id))


def subject_names(con, subjects) -> list:
    """Canonical names in subjects-array order (falls back to the raw id)."""
    names = {r["entity_id"]: r["canonical_name"] for r in con.execute(
        "select entity_id, canonical_name from entity where entity_id = any(%s)",
        (list(subjects),))}
    return [names.get(s, str(s)) for s in subjects]


def parse_uuid(v):
    """Model-supplied ids are untrusted text; a non-uuid must never reach SQL."""
    try:
        return uuid.UUID(str(v))
    except (TypeError, ValueError, AttributeError):
        return None


def run(con) -> dict:
    cfg = config.DISCOVERY
    watched = {r["entity_id"] for r in con.execute(
        "select e.entity_id from entity e join watchlist w "
        "on w.ticker = e.registry_refs->>'ticker' where w.active")}
    rows = con.execute(
        "select hypothesis_id, subjects, origin from hypothesis "
        "where state = 'generated'").fetchall()

    scored = []
    for h in rows:
        a, b = h["subjects"][0], h["subjects"][1]
        co = con.execute(
            "select count(*) as n from ("
            "  select event_id from claim_asserted"
            "  where subject_entity = %s or object_entity = %s"
            "  intersect"
            "  select event_id from claim_asserted"
            "  where subject_entity = %s or object_entity = %s) t",
            (a, a, b, b)).fetchone()["n"]
        novelty = 1.0 / (1.0 + co)
        materiality = MATERIALITY[(a in watched) + (b in watched)]
        prior = cfg["strategy_prior"].get(
            (h["origin"] or {}).get("strategy"), 0.5)
        score = novelty * materiality * prior
        con.execute("update hypothesis set score = %s where hypothesis_id = %s",
                    (score, h["hypothesis_id"]))
        scored.append((score, h["hypothesis_id"]))

    scored.sort(key=lambda t: t[0], reverse=True)
    triaged = 0
    for score, hid in scored[:cfg["triage_top_k"]]:
        if score < cfg["min_score"]:
            break
        con.execute("update hypothesis set state = 'triaged' "
                    "where hypothesis_id = %s", (hid,))
        append_history(con, hid, {"from": "generated", "to": "triaged"})
        triaged += 1

    print(f"funnel: {len(scored)} scored, {triaged} triaged")
    return {"scored": len(scored), "triaged": triaged}
