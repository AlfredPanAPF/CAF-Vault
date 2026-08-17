"""Adversarial verifier + promotion (design §8.5, §8.6, build spec v3 §3.4).

The verifier proposes, the code disposes: after the LLM call, surviving
evidence is re-grounded in claim_asserted and the lineage-independence and
reliability guards (§10.4) run in code. A promote that fails a code gate
degrades to park, never through to the graph.
"""
import json
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .. import alerts, config, llm
from ..pipeline.materialize import half_life_for
from .funnel import append_history, parse_uuid, record_failure, subject_names

# lineage reliability = the lineage ROOT event's source reliability. Null is
# UNPROVEN (§10.4), not a free 0.5: unproven and low-reliability lineages
# collectively count as one, and promotion needs at least one lineage
# explicitly scored at ESTABLISHED or better — never-scored sources can never
# satisfy the established gate.
LOW = 0.4
ESTABLISHED = 0.5

EVIDENCE_SELECT = """
select c.claim_id, c.evidence_quote, c.observed_at, c.lineage_id,
       coalesce(se.canonical_name, c.subject_surface) as subject,
       c.predicate_raw,
       coalesce(oe.canonical_name, c.object_surface) as object,
       s.name as source_name, s.reliability as source_reliability,
       rs.reliability as lineage_reliability
from claim_asserted c
join event ev on ev.event_id = c.event_id
join source s on s.source_id = ev.source_id
join lineage l on l.lineage_id = c.lineage_id
join event re on re.event_id = l.root_event_id
join source rs on rs.source_id = re.source_id
left join entity se on se.entity_id = c.subject_entity
left join entity oe on oe.entity_id = c.object_entity
where c.claim_id = any(%s)
"""


def _prompt(con, h, names, base) -> str:
    ev = (con.execute(EVIDENCE_SELECT, (list(h["evidence"]),)).fetchall()
          if h["evidence"] else [])
    lines = [json.dumps({
        "id": str(r["claim_id"]),
        "subject": r["subject"],
        "predicate": r["predicate_raw"],
        "object": r["object"],
        "quote": r["evidence_quote"],
        "source": r["source_name"],
        "source_reliability": (0.5 if r["source_reliability"] is None
                               else r["source_reliability"]),
        "lineage": str(r["lineage_id"]),
        "observed_at": r["observed_at"].isoformat() if r["observed_at"] else None,
    }, default=str) for r in ev]
    return (base + "\n\n# Hypothesis\n"
            + f"statement: {(h['statement'] or {}).get('text') or h['rationale']}\n"
            + f"subjects: {names[0]} and {names[1]}\n"
            + f"rationale: {h['rationale']}\n"
            + "\n# Evidence claims\n\n"
            + ("\n".join(lines) or "none") + "\n")


def run(con) -> dict:
    prompt_base = (config.PROMPTS / "verifier.md").read_text()
    model = config.MODELS["verify"]
    rows = con.execute(
        "select * from hypothesis where state = 'investigating' "
        "and jsonb_path_exists(history, '$[*].investigated') "
        "order by updated_at, created_at limit %s",
        (config.DISCOVERY["verify_per_cycle"],)).fetchall()

    promoted = parked = refuted = failed = 0
    paused = False
    for h in rows:
        hid = h["hypothesis_id"]
        names = subject_names(con, h["subjects"])
        con.execute("savepoint ver_item")
        try:
            out = llm.complete_json(_prompt(con, h, names, prompt_base), model)
        except llm.EngineUnavailable as e:
            con.execute("rollback to savepoint ver_item")
            print(f"verify: paused, no model available ({e})")
            paused = True
            break
        except Exception as e:
            # one bad item (malformed output, CLI timeout) must not roll back
            # the whole stage or poison the queue head — extract.run pattern
            con.execute("rollback to savepoint ver_item")
            record_failure(con, h, "verify", e)
            print(f"verify: item {hid} failed ({e})")
            failed += 1
            continue

        # ---- code enforcement after the call (spec §3.4) ----
        surv_ids = []
        for v in (out.get("surviving_evidence") or []):
            u = parse_uuid(v)
            if u is not None and u not in surv_ids:
                surv_ids.append(u)
        by_id = ({r["claim_id"]: r for r in con.execute(EVIDENCE_SELECT,
                                                        (surv_ids,))}
                 if surv_ids else {})
        surviving = [by_id[u] for u in surv_ids if u in by_id]

        rel_by_lineage = {}
        for r in surviving:
            rel = r["lineage_reliability"]
            rel_by_lineage[r["lineage_id"]] = None if rel is None else float(rel)
        low = [lin for lin, rel in rel_by_lineage.items()
               if rel is None or rel < LOW]
        independent = (len(rel_by_lineage) - len(low)) + (1 if low else 0)
        required = config.LINEAGES_REQUIRED.get(
            h["type"], config.LINEAGES_REQUIRED_DEFAULT)

        try:
            conf = min(1.0, max(0.0, float(out.get("confidence"))))
        except (TypeError, ValueError):
            conf = 0.5
        reasoning = str(out.get("reasoning") or "")
        raw_verdict = out.get("verdict")
        verdict, note = raw_verdict, None
        if raw_verdict == "promote":
            if independent < required:
                verdict = "park"
                note = (f"promote degraded: {independent} independent "
                        f"lineage(s), {required} required")
            elif not any(rel is not None and rel >= ESTABLISHED
                         for rel in rel_by_lineage.values()):
                verdict = "park"
                note = "promote degraded: no lineage from an established source"
        elif raw_verdict not in ("park", "refute"):
            verdict = "park"
            note = f"unrecognized verdict {raw_verdict!r}"

        checked = {"verdict": verdict, "lineages": independent,
                   "confidence": conf}
        if verdict == "promote":
            trail = {"hypothesis_id": str(hid), "verdict": "promote",
                     "confidence": conf, "reasoning": reasoning,
                     "lineages": independent, "verifier": model,
                     "verified_at": datetime.now(timezone.utc).isoformat()}
            con.execute(
                "insert into edge (src, dst, predicate, origin, claim_ids, "
                "evidence_trail, confidence, last_evidence_at, half_life_days) "
                "values (%s, %s, %s, 'inferred', %s, %s, %s, %s, %s) "
                "on conflict (src, dst, predicate, origin) do update set "
                "claim_ids = excluded.claim_ids, "
                "confidence = excluded.confidence, "
                "last_evidence_at = excluded.last_evidence_at, "
                "evidence_trail = excluded.evidence_trail",
                (h["subjects"][0], h["subjects"][1], h["type"],
                 [r["claim_id"] for r in surviving], Jsonb(trail), conf,
                 max(r["observed_at"] for r in surviving),
                 half_life_for(h["type"])))
            con.execute("update hypothesis set state = 'promoted', "
                        "confidence = %s where hypothesis_id = %s "
                        "and state = 'investigating'", (conf, hid))
            append_history(con, hid, {"from": "investigating",
                                      "to": "promoted", "verified": checked})
            alerts.emit(con, "promoted_link",
                        f"New link: {names[0]} and {names[1]}",
                        body=(h["statement"] or {}).get("text") or h["rationale"],
                        entity_ids=h["subjects"], hypothesis_id=hid)
            promoted += 1
        elif verdict == "park":
            via = (h["statement"] or {}).get("via_entity")
            wake = ([str(s) for s in h["subjects"]]
                    + ([str(via)] if via else []))
            con.execute(
                "update hypothesis set state = 'parked', parked_at = now(), "
                "wake_conditions = %s where hypothesis_id = %s "
                "and state = 'investigating'",
                (Jsonb({"entities": wake}), hid))
            entry = {"from": "investigating", "to": "parked",
                     "verified": checked}
            if note:
                entry["note"] = note
            append_history(con, hid, entry)
            parked += 1
        else:
            con.execute("update hypothesis set state = 'refuted' "
                        "where hypothesis_id = %s "
                        "and state = 'investigating'", (hid,))
            append_history(con, hid, {"from": "investigating",
                                      "to": "refuted", "verified": checked})
            refuted += 1
        con.execute("release savepoint ver_item")

    print(f"verify: {promoted} promoted, {parked} parked, {refuted} refuted, "
          f"{failed} failed")
    return {"promoted": promoted, "parked": parked, "refuted": refuted,
            "failed": failed, "paused": paused}
