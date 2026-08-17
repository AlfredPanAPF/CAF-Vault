"""Hypothesis agent (design §8.3, build spec v3 §3.2). Serializes the claim
subgraph around a triaged candidate — its evidence claims plus recent asserted
claims per subject, with the explicit negative space line — and asks the model
for one falsifiable statement and test plan. No test plan means refuted on the
spot; the row stays as negative memory.
"""
import json

from psycopg.types.json import Jsonb

from .. import config, llm
from .funnel import append_history, record_failure, subject_names

# most recent evidence claims serialized per hypothesis prompt — the stored
# evidence array is corpus-proportional, the prompt must not be
EVIDENCE_CAP = 30

# Shared claim serialization: investigate.py reuses these so both agents see
# one claim shape. Reads go through the claim_asserted firewall view only
# (design §8.4) — open hypotheses and inferred edges are invisible.
CLAIM_SELECT = """
select c.claim_id,
       coalesce(se.canonical_name, c.subject_surface) as subject,
       c.predicate_raw,
       coalesce(oe.canonical_name, c.object_surface) as object,
       c.object_literal, c.evidence_quote, c.observed_at, c.confidence,
       c.lineage_id, s.name as source_name
from claim_asserted c
join event ev on ev.event_id = c.event_id
join source s on s.source_id = ev.source_id
left join entity se on se.entity_id = c.subject_entity
left join entity oe on oe.entity_id = c.object_entity
"""


def claim_line(r) -> str:
    return json.dumps({
        "id": str(r["claim_id"]),
        "subject": r["subject"],
        "predicate": r["predicate_raw"],
        "object": r["object"] if r["object"] is not None else r["object_literal"],
        "quote": r["evidence_quote"],
        "source": r["source_name"],
        "observed_at": r["observed_at"].isoformat() if r["observed_at"] else None,
        "confidence": r["confidence"],
        "lineage": str(r["lineage_id"]),
    }, default=str)


def _subgraph(con, h, names) -> str:
    lines, seen = [], []
    if h["evidence"]:
        # cap at the most recent EVIDENCE_CAP, presented oldest-first
        rows = con.execute(
            CLAIM_SELECT + "where c.claim_id = any(%s) "
            "order by c.observed_at desc, c.claim_id limit %s",
            (list(h["evidence"]), EVIDENCE_CAP)).fetchall()
        for r in sorted(rows, key=lambda r: r["observed_at"]):
            seen.append(r["claim_id"])
            lines.append(claim_line(r))
    for subj in h["subjects"]:
        sql = CLAIM_SELECT + "where (c.subject_entity = %s or c.object_entity = %s)"
        params = [subj, subj]
        if seen:
            sql += " and not (c.claim_id = any(%s))"
            params.append(list(seen))
        sql += " order by c.observed_at desc limit 15"
        for r in con.execute(sql, params):
            seen.append(r["claim_id"])
            lines.append(claim_line(r))
    return ("\n".join(lines) + "\n\n"
            + f"No recorded relationship between {names[0]} and {names[1]}.")


def run(con) -> dict:
    prompt_base = (config.PROMPTS / "hypothesis.md").read_text()
    model = config.MODELS["hypothesize"]
    cap = config.DISCOVERY["budget_tool_calls"]
    rows = con.execute(
        "select * from hypothesis where state = 'triaged' "
        "and not jsonb_path_exists(history, '$[*].refined') "
        "order by score desc nulls last, created_at limit %s",
        (config.DISCOVERY["hypothesize_per_cycle"],)).fetchall()

    refined = refuted = failed = 0
    paused = False
    for h in rows:
        hid = h["hypothesis_id"]
        names = subject_names(con, h["subjects"])
        con.execute("savepoint hyp_item")
        try:
            prompt = (prompt_base
                      + "\n\n# Candidate\n"
                      + f"type: {h['type']}\n"
                      + f"subjects: {names[0]} and {names[1]}\n"
                      + f"statement template: {json.dumps(h['statement'])}\n"
                      + f"candidate rationale: {h['rationale']}\n"
                      + "\n# Claim subgraph\n\n"
                      + _subgraph(con, h, names) + "\n")
            result = llm.complete_json(prompt, model)
        except llm.EngineUnavailable as e:
            con.execute("rollback to savepoint hyp_item")
            print(f"hypothesize: paused, no model available ({e})")
            paused = True
            break
        except Exception as e:
            # one bad item (malformed output, CLI timeout) must not roll back
            # the whole stage or poison the queue head — extract.run pattern
            con.execute("rollback to savepoint hyp_item")
            record_failure(con, h, "hypothesize", e)
            print(f"hypothesize: item {hid} failed ({e})")
            failed += 1
            continue
        plan = result.get("test_plan") or {}
        confirm = [str(x) for x in (plan.get("confirm") or []) if x]
        refute = [str(x) for x in (plan.get("refute") or []) if x]
        if not confirm or not refute:
            # §8.3: no falsifiable test plan -> discarded on the spot, kept
            # as negative memory so dedup never regenerates it (state guard:
            # never flip a row something else moved while we held it)
            con.execute("update hypothesis set state = 'refuted' "
                        "where hypothesis_id = %s and state = 'triaged'", (hid,))
            append_history(con, hid, {"from": "triaged", "to": "refuted",
                                      "note": "no falsifiable test plan"})
            refuted += 1
        else:
            statement = dict(h["statement"] or {})
            statement["text"] = (str(result.get("statement") or "").strip()
                                 or h["rationale"] or "")
            try:
                asked = int((result.get("budget") or {}).get("tool_calls") or cap)
            except (TypeError, ValueError):
                asked = cap
            budget = dict(h["budget"] or {})
            budget["tool_calls"] = max(1, min(asked, cap))
            con.execute(
                "update hypothesis set statement = %s, rationale = %s, "
                "test_plan = %s, budget = %s, updated_at = now() "
                "where hypothesis_id = %s",
                (Jsonb(statement), result.get("rationale") or h["rationale"],
                 Jsonb({"confirm": confirm, "refute": refute}), Jsonb(budget),
                 hid))
            append_history(con, hid, {"refined": {"model": model}})
            refined += 1
        con.execute("release savepoint hyp_item")

    print(f"hypothesize: {refined} refined, {refuted} refuted, {failed} failed")
    return {"refined": refined, "refuted": refuted, "failed": failed,
            "paused": paused}
