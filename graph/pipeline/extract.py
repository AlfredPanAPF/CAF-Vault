"""Claim extraction stage: pending events -> mentions + claims via the LLM.

Each event is processed under a savepoint so one bad document (or a mid-write
DB error) rolls back cleanly and gets its failure recorded without poisoning
the run's transaction. Commits stay in the CLI layer.
"""
from datetime import datetime

from psycopg.types.json import Jsonb

from .. import artifacts, config, llm

RESOLVED_TYPES = {"company", "security"}  # v0 resolves only these (design §6)


def run(con, limit=10):
    prompt_base = (config.PROMPTS / "extraction.md").read_text()
    model = config.MODELS["extract"]
    events = con.execute(
        "select event_id, artifact_uri, lineage_id, attempts from event "
        "where status='pending' order by fetched_at limit %s", (limit,)).fetchall()

    extracted = failed = n_claims = n_mentions = 0
    for ev in events:
        con.execute("savepoint extract_ev")
        try:
            con.execute("update event set status='extracting' where event_id=%s",
                        (ev["event_id"],))
            text = artifacts.get(ev["artifact_uri"]).decode("utf-8", errors="replace")
            prompt = (prompt_base + f"\n\n# Document (doc_id: {ev['event_id']})\n\n"
                      + text[:60000])
            result = llm.complete_json(prompt, model, max_tokens=16000)

            n_mentions += write_mentions(con, ev["event_id"], result.get("mentions") or [])
            n_claims += write_claims(con, ev, model, result.get("claims") or [])
            defined_terms = result.get("defined_terms") or {}
            if defined_terms:
                con.execute(
                    "update event set meta = coalesce(meta, '{}'::jsonb) "
                    "|| jsonb_build_object('defined_terms', %s) where event_id=%s",
                    (Jsonb(defined_terms), ev["event_id"]))

            con.execute("update event set status='extracted' where event_id=%s",
                        (ev["event_id"],))
            con.execute("release savepoint extract_ev")
            extracted += 1
        except Exception as e:
            con.execute("rollback to savepoint extract_ev")
            attempts = ev["attempts"] + 1
            status = "failed" if attempts >= 2 else "pending"
            con.execute(
                "update event set status=%s, attempts=%s, last_error=%s "
                "where event_id=%s",
                (status, attempts, str(e)[:500], ev["event_id"]))
            failed += 1

    print(f"extract: {extracted} extracted, {failed} failed, "
          f"{n_mentions} mentions, {n_claims} claims")
    return {"extracted": extracted, "failed": failed,
            "claims": n_claims, "mentions": n_mentions}


def write_mentions(con, event_id, mentions):
    """One row per distinct surface per event. mention has no type column:
    company/security surfaces get resolver 'pending', everything else
    'skipped-v0'; 'pending' wins if a surface is tagged both ways."""
    resolver_by_surface = {}
    for m in mentions:
        surface = (m.get("surface") or "").strip()
        if not surface:
            continue
        resolver = "pending" if m.get("type") in RESOLVED_TYPES else "skipped-v0"
        if resolver == "pending" or surface not in resolver_by_surface:
            resolver_by_surface[surface] = resolver
    for surface, resolver in resolver_by_surface.items():
        con.execute(
            "insert into mention (event_id, surface, resolver) values (%s,%s,%s)",
            (event_id, surface, resolver))
    return len(resolver_by_surface)


def write_claims(con, ev, model, claims):
    n = 0
    for c in claims:
        subject_surface = ((c.get("subject") or {}).get("surface") or "").strip()
        predicate = (c.get("predicate") or "").strip()
        obj = c.get("object") or {}
        object_surface = (obj.get("surface") or "").strip() or None
        literal = obj.get("literal")
        if not subject_surface or not predicate:
            continue
        if object_surface is None and literal is None:
            continue  # nothing to hang the claim on; DB check would reject it
        con.execute(
            "insert into claim (subject_surface, predicate_raw, object_surface, "
            "object_literal, qualifiers, event_id, lineage_id, observed_at, "
            "valid_from, valid_to, confidence, extractor, status, evidence_quote) "
            "values (%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,'asserted',%s)",
            (subject_surface, predicate, object_surface,
             Jsonb(literal) if literal is not None else None,
             Jsonb(c["qualifiers"]) if c.get("qualifiers") else None,
             ev["event_id"], ev["lineage_id"],
             parse_iso(c.get("valid_from")), parse_iso(c.get("valid_to")),
             c.get("confidence") or 0.5, model, c.get("evidence_quote")))
        n += 1
    return n


def parse_iso(v):
    if not v or not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None
