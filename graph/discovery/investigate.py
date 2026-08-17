"""Investigation agent (design §8.4, build spec v3 §3.3). A tool loop with a
hard budget: one growing prompt (base + hypothesis + transcript so far) is
re-sent each turn, one JSON action comes back. The mechanical rules live here,
not in the prompt — every claim read selects from the claim_asserted firewall
view, cited evidence is kept only when the claim id resolves there, and the
loop ends on either budget with a default 'insufficient' conclusion.
"""
import json

from .. import artifacts, config, llm
from .funnel import append_history, parse_uuid, subject_names
from .hypothesize import CLAIM_SELECT, claim_line

ASSESSMENTS = {"supported", "refuted", "insufficient"}

CORRECTIVE = ("Your last output was not a valid action. Answer with exactly "
              "one JSON action from the action list.")


def _claims_about(con, entity_id) -> str:
    e = parse_uuid(entity_id)
    if e is None:
        return "error: entity_id is not a uuid"
    rows = con.execute(
        CLAIM_SELECT + "where c.subject_entity = %s or c.object_entity = %s "
        "order by c.observed_at desc limit 50", (e, e)).fetchall()
    return "\n".join(claim_line(r) for r in rows) or "no asserted claims"


def _search_claims(con, text, entity_id) -> str:
    text = str(text or "").strip()
    if not text:
        return "error: text is required"
    pat = f"%{text}%"
    sql = (CLAIM_SELECT
           + "where (c.evidence_quote ilike %s or c.predicate_raw ilike %s "
           "or coalesce(se.canonical_name, c.subject_surface) ilike %s "
           "or coalesce(oe.canonical_name, c.object_surface) ilike %s)")
    params = [pat, pat, pat, pat]
    e = parse_uuid(entity_id) if entity_id else None
    if e is not None:
        sql += " and (c.subject_entity = %s or c.object_entity = %s)"
        params += [e, e]
    sql += " order by c.observed_at desc limit 30"
    rows = con.execute(sql, params).fetchall()
    return "\n".join(claim_line(r) for r in rows) or "no matching asserted claims"


def _fetch_segment(con, claim_id) -> str:
    c = parse_uuid(claim_id)
    if c is None:
        return "error: claim_id is not a uuid"
    row = con.execute(
        "select c.evidence_quote, ev.artifact_uri from claim_asserted c "
        "join event ev on ev.event_id = c.event_id where c.claim_id = %s",
        (c,)).fetchone()
    if row is None:
        return "error: no asserted claim with that id"
    try:
        text = artifacts.get(row["artifact_uri"]).decode("utf-8", errors="replace")
    except OSError as e:
        return f"error: artifact unavailable ({e})"
    quote = row["evidence_quote"] or ""
    idx = text.find(quote) if quote else -1
    if idx < 0:
        return text[:1200]
    return text[max(0, idx - 600): idx + len(quote) + 600]


def _loop(con, h, prompt_base, model) -> dict:
    names = subject_names(con, h["subjects"])
    base = (prompt_base + "\n\n# Hypothesis\n"
            + f"statement: {(h['statement'] or {}).get('text') or h['rationale']}\n"
            + "subjects: " + ", ".join(
                f"{n} (entity_id {s})" for n, s in zip(names, h["subjects"]))
            + "\n" + f"test plan: {json.dumps(h['test_plan'])}\n")
    cap = config.DISCOVERY["budget_tool_calls"]
    try:
        budget = max(1, min(int((h["budget"] or {}).get("tool_calls") or cap), cap))
    except (TypeError, ValueError):
        budget = cap
    char_cap = config.DISCOVERY["budget_tokens"] * 4

    transcript, chars, turns, concluded = [], 0, 0, None
    while turns < budget:
        prompt = base + "".join(transcript) + "\n## Next action\n"
        action = llm.complete_json(prompt, model)
        turns += 1
        chars += len(prompt) + len(json.dumps(action, default=str))
        tool = action.get("tool") if isinstance(action, dict) else None
        if tool == "conclude":
            concluded = action
            break
        if tool == "claims_about":
            result = _claims_about(con, action.get("entity_id"))
        elif tool == "search_claims":
            result = _search_claims(con, action.get("text"),
                                    action.get("entity_id"))
        elif tool == "fetch_segment":
            result = _fetch_segment(con, action.get("claim_id"))
        else:
            # malformed action: burns a turn, gets one corrective reinjection
            transcript.append(f"\n## Turn {turns}\n{CORRECTIVE}\n")
            continue
        transcript.append(f"\n## Turn {turns}\naction: "
                          f"{json.dumps(action, default=str)}\nresult:\n{result}\n")
        chars += len(result)
        if chars > char_cap:
            break

    assessment, cited, reasoning, conf = "insufficient", [], "", None
    if concluded is not None:
        if concluded.get("assessment") in ASSESSMENTS:
            assessment = concluded["assessment"]
        cited = concluded.get("evidence") or []
        reasoning = str(concluded.get("reasoning") or "")
        try:
            conf = min(1.0, max(0.0, float(concluded.get("confidence"))))
        except (TypeError, ValueError):
            conf = None

    # evidence grounding (§8.4 rule 1): keep only claim ids that resolve in
    # claim_asserted; non-resolving citations are dropped silently
    ids = []
    for v in cited if isinstance(cited, list) else []:
        u = parse_uuid(v)
        if u is not None and u not in ids:
            ids.append(u)
    lineage_by_claim = {}
    if ids:
        lineage_by_claim = {r["claim_id"]: r["lineage_id"] for r in con.execute(
            "select claim_id, lineage_id from claim_asserted "
            "where claim_id = any(%s)", (ids,))}
    evidence = [u for u in ids if u in lineage_by_claim]
    lineages = []
    for u in evidence:
        if lineage_by_claim[u] not in lineages:
            lineages.append(lineage_by_claim[u])
    return {"assessment": assessment, "evidence": evidence,
            "lineages": lineages, "confidence": conf, "turns": turns,
            "reasoning": reasoning}


def run(con) -> dict:
    prompt_base = (config.PROMPTS / "investigation.md").read_text()
    model = config.MODELS["investigate"]
    rows = con.execute(
        "select * from hypothesis where state = 'triaged' "
        "and jsonb_path_exists(history, '$[*].refined') "
        "order by score desc nulls last, created_at limit %s",
        (config.DISCOVERY["investigate_per_cycle"],)).fetchall()

    investigated = 0
    paused = False
    for h in rows:
        hid = h["hypothesis_id"]
        con.execute("savepoint inv_item")
        try:
            con.execute("update hypothesis set state = 'investigating', "
                        "updated_at = now() where hypothesis_id = %s", (hid,))
            out = _loop(con, h, prompt_base, model)
        except llm.EngineUnavailable as e:
            # mid-loop outage: the state flip rolls back with the savepoint,
            # nothing is burned, the hypothesis waits for the next cycle
            con.execute("rollback to savepoint inv_item")
            print(f"investigate: paused, no model available ({e})")
            paused = True
            break
        con.execute(
            "update hypothesis set evidence = %s, lineages = %s, "
            "confidence = %s where hypothesis_id = %s",
            (out["evidence"], out["lineages"], out["confidence"], hid))
        append_history(con, hid, {"investigated": {
            "assessment": out["assessment"], "turns": out["turns"],
            "reasoning": out["reasoning"]}})
        con.execute("release savepoint inv_item")
        investigated += 1

    print(f"investigate: {investigated} investigated")
    return {"investigated": investigated, "paused": paused}
