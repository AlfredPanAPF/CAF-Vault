"""Wake parked hypotheses (design §8.6, build spec v3 §3.5). No LLM: a parked
hypothesis subscribes to its wake entities; any asserted claim on one of them
(as subject or object) observed after parked_at sends it back to 'triaged',
where investigation resumes with its accumulated context.
"""
from .. import alerts
from .funnel import append_history, parse_uuid, subject_names


def run(con) -> dict:
    rows = con.execute(
        "select hypothesis_id, subjects, parked_at, wake_conditions "
        "from hypothesis where state = 'parked' and parked_at is not null"
    ).fetchall()

    woke = 0
    for h in rows:
        entities = [u for u in (parse_uuid(e) for e in
                    ((h["wake_conditions"] or {}).get("entities") or []))
                    if u is not None]
        if not entities:
            continue
        hit = con.execute(
            "select 1 from claim_asserted where observed_at > %s "
            "and (subject_entity = any(%s) or object_entity = any(%s)) "
            "limit 1", (h["parked_at"], entities, entities)).fetchone()
        if hit is None:
            continue
        hid = h["hypothesis_id"]
        # state guard: a row a human moved (e.g. rejected to 'refuted') while
        # this batch was in flight must not wake — and on a skipped flip the
        # whole per-item tail is skipped too: no history entry, no alert
        cur = con.execute("update hypothesis set state = 'triaged' "
                          "where hypothesis_id = %s and state = 'parked'",
                          (hid,))
        if cur.rowcount == 0:
            continue
        append_history(con, hid, {"from": "parked", "to": "triaged",
                                  "note": "woke"})
        names = subject_names(con, h["subjects"])
        alerts.emit(con, "hypothesis_wake",
                    f"Hypothesis woke: {names[0]} and {names[1]}",
                    entity_ids=h["subjects"], hypothesis_id=hid)
        woke += 1

    print(f"wake: {woke} woke")
    return {"woke": woke}
