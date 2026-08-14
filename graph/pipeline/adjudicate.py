"""ER adjudication (design §6 stage 2): ambiguous mentions wait in er_queue;
one batched LLM pass decides match / new_entity / not_a_company / ambiguous.
Ambiguous rows stay pending and retry on later passes (capped at 3, then
'failed'). After applying decisions, claim linking re-runs via resolve.link_claims.
"""
import json
from collections import Counter

from psycopg.types.json import Jsonb

from .. import artifacts, config, llm
from . import resolve

MAX_PASSES = 3


def _snippet(text, surface, width=300):
    idx = text.lower().find(surface.lower())
    if idx < 0:
        return text[:2 * width]
    return text[max(0, idx - width): idx + len(surface) + width]


def _match_entity(con, decision, candidates):
    """Pin a 'match' decision to a registry record -> entity_id or None."""
    cik = decision.get("cik")
    try:
        cik = int(cik) if cik is not None else None
    except (TypeError, ValueError):
        cik = None
    if cik:
        reg = con.execute("select cik, ticker, title from registry_sec where cik=%s",
                          (cik,)).fetchone()
        if reg:
            return resolve.entity_for_sec(con, reg["cik"], reg["ticker"], reg["title"])
        for c in candidates:
            if c.get("cik") == cik:
                return resolve.entity_for_sec(con, cik, c.get("ticker"), c.get("title"))
        return None
    # GLEIF match: only trust an LEI that was actually among the candidates
    gleif_cands = [c for c in candidates if c.get("lei")]
    lei = decision.get("lei")
    if lei:
        for c in gleif_cands:
            if c["lei"] == lei:
                return resolve.entity_for_gleif(con, lei, c.get("title"), c.get("country"))
        return None
    if len(gleif_cands) == 1:
        c = gleif_cands[0]
        return resolve.entity_for_gleif(con, c["lei"], c.get("title"), c.get("country"))
    return None


def run(con, limit=20):
    rows = con.execute(
        "select q.mention_id, q.candidates, q.decision as prior, m.surface, "
        "m.event_id, e.artifact_uri from er_queue q "
        "join mention m on m.mention_id = q.mention_id "
        "join event e on e.event_id = m.event_id "
        "where q.status = 'pending' order by q.created_at limit %s",
        (limit,)).fetchall()
    if not rows:
        print("adjudicate: queue empty")
        return {}

    parts = [(config.PROMPTS / "er_adjudication.md").read_text(),
             "\n\n# Mentions to adjudicate\n"]
    texts = {}
    for r in rows:
        if r["event_id"] not in texts:
            try:
                texts[r["event_id"]] = artifacts.get(r["artifact_uri"]).decode(
                    "utf-8", errors="replace")
            except OSError:
                texts[r["event_id"]] = ""
        parts.append(
            f"\n## mention_id: {r['mention_id']}\n"
            f"surface: {r['surface']}\n"
            f"context: ...{_snippet(texts[r['event_id']], r['surface'])}...\n"
            f"candidates: {json.dumps(r['candidates'])}\n")
    parts.append(
        '\nReturn ONE JSON object: {"decisions": [{"surface", "mention_id", '
        '"decision", "cik"|null, "lei"|null, "entity_hint"|null, "reasoning", '
        '"confidence"}, ...]} with one entry per mention above, echoing its '
        'mention_id exactly. Set "cik" when matching an SEC candidate and "lei" '
        'when matching a GLEIF candidate.')
    out = llm.complete_json("".join(parts), config.MODELS["adjudicate"])
    decisions = {str(d.get("mention_id")): d for d in out.get("decisions", [])
                 if isinstance(d, dict) and d.get("mention_id")}

    counts = Counter()
    for r in rows:
        d = decisions.get(str(r["mention_id"]))
        if not d:
            counts["skipped"] += 1
            continue
        decision = d.get("decision")
        confidence = d.get("confidence") or 0.8
        entity_id = None
        if decision == "match":
            entity_id = _match_entity(con, d, r["candidates"])
            if entity_id is None:
                # a match we cannot pin to a registry record is not a match
                decision = "ambiguous"
                d = {**d, "decision": "ambiguous",
                     "note": "match without usable registry id"}
        if decision == "match":
            con.execute(
                "update mention set resolved_entity=%s, "
                "resolver='adjudicated-v1:match', confidence=%s where mention_id=%s",
                (entity_id, confidence, r["mention_id"]))
        elif decision == "new_entity":
            name = d.get("entity_hint") or r["surface"]
            entity_id = con.execute(
                "insert into entity (kind, canonical_name, registry_refs) "
                "values ('company', %s, %s) returning entity_id",
                (name, Jsonb({}))).fetchone()["entity_id"]
            con.execute(
                "update mention set resolved_entity=%s, "
                "resolver='adjudicated-v1:new_entity', confidence=%s where mention_id=%s",
                (entity_id, confidence, r["mention_id"]))
        elif decision == "not_a_company":
            con.execute(
                "update mention set resolver='not_company', confidence=%s "
                "where mention_id=%s", (confidence, r["mention_id"]))
        elif decision == "ambiguous":
            passes = ((r["prior"] or {}).get("passes") or 0) + 1
            if passes >= MAX_PASSES:
                con.execute(
                    "update er_queue set status='failed', decision=%s, "
                    "decided_at=now() where mention_id=%s",
                    (Jsonb({**d, "passes": passes}), r["mention_id"]))
                counts["failed"] += 1
            else:
                con.execute("update er_queue set decision=%s where mention_id=%s",
                            (Jsonb({**d, "passes": passes}), r["mention_id"]))
                counts["ambiguous"] += 1
            continue
        else:
            counts["skipped"] += 1
            continue
        con.execute(
            "update er_queue set status='decided', decision=%s, decided_at=now() "
            "where mention_id=%s", (Jsonb(d), r["mention_id"]))
        counts[decision] += 1

    linked = resolve.link_claims(con)
    print(f"adjudicate: {len(rows)} queued | decisions: {dict(counts)} "
          f"| {linked} claim links")
    return dict(counts)
