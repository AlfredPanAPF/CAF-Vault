"""Claim extraction stage: pending events -> mentions + claims via the LLM.

Each event is processed under a savepoint so one bad document (or a mid-write
DB error) rolls back cleanly and gets its failure recorded without poisoning
the run's transaction. Commits stay in the CLI layer.

Hardening (build spec v6 §3): the prompt file rides the system turn and the
CLI enforces the extraction schema, but shape is not substance — an event
that yields zero mentions AND zero claims, or whose claims are all dropped
by validation, is a failure, never a success. The 2026-08-20 incident was 8
documents recorded as extracted with nothing written.
"""
from datetime import datetime

from psycopg.types.json import Jsonb

from .. import artifacts, config, llm, schemas
from . import summarize

RESOLVED_TYPES = {"company", "security"}  # v0 resolves only these (design §6)

# body cap for the prompt, matching summarize; the old text[:60000] counted
# the connector header against the budget
BODY_CAP = 80000


def _context_line(header: dict) -> str:
    """One line of document facts ahead of the body: title — source, date."""
    title = (header.get("title") or "").strip()
    source = (header.get("feed") or header.get("channel")
              or header.get("origin") or header.get("source_type") or "").strip()
    date = (header.get("published") or "").strip()
    tail = ", ".join(p for p in (source, date) if p)
    if title and tail:
        return f"{title} — {tail}"
    return title or tail


def run(con, limit=10, exclude=None):
    """Extract up to `limit` pending events. `exclude` is a mutable set of
    event ids to skip; failed and transient items are added to it in place so
    cli._extract_drain attempts each item at most once per drain."""
    prompt_base = (config.PROMPTS / "extraction.md").read_text()
    model = config.MODELS["extract"]
    exclude = exclude if exclude is not None else set()
    # triage is not null: every event gets its materiality score (and any
    # material_event alert) before extraction consumes it — an untriaged event
    # waits a cycle rather than skipping triage forever (spec §4.1, §8 order)
    events = con.execute(
        "select event_id, artifact_uri, lineage_id, attempts from event "
        "where status='pending' and triage is not null "
        "and not (event_id = any(%s::uuid[])) "
        "order by fetched_at limit %s", (list(exclude), limit)).fetchall()

    extracted = failed = transient = n_claims = n_mentions = n_dropped = 0
    paused = False
    for ev in events:
        con.execute("savepoint extract_ev")
        try:
            con.execute("update event set status='extracting' where event_id=%s",
                        (ev["event_id"],))
            text = artifacts.get(ev["artifact_uri"]).decode("utf-8", errors="replace")
            header, body = summarize.split_artifact(text)
            if len(body) > BODY_CAP:
                # truncation must at least be visible on the document
                con.execute(
                    "update event set meta = coalesce(meta, '{}'::jsonb) "
                    "|| jsonb_build_object('extract_truncated', true) "
                    "where event_id=%s", (ev["event_id"],))
            context = _context_line(header)
            prompt = (f"# Document (doc_id: {ev['event_id']})\n\n"
                      + (context + "\n\n" if context else "")
                      + body[:BODY_CAP])
            # 48000: a dense document emits its whole envelope in ONE
            # structured turn (adaptive thinking included) — 16000 killed
            # the four largest SemiAnalysis posts with "response exceeded
            # the 16000 output token maximum" on the v6 requeue, and the
            # worst observed envelope was ~40k tokens
            result = llm.complete_json(prompt, model, max_tokens=48000,
                                       schema=schemas.EXTRACTION,
                                       system=prompt_base)
            # claims carry the RESOLVED model id (provenance), not the alias
            model_used = (llm.last_call_info() or {}).get("model") or model

            wrote_m = write_mentions(con, ev["event_id"],
                                     result.get("mentions") or [])
            wrote_c, dropped = write_claims(con, ev, model_used,
                                            result.get("claims") or [])
            if wrote_c == 0 and dropped > 0:
                raise ValueError(f"all {dropped} claims dropped by validation")
            if wrote_m == 0 and wrote_c == 0:
                # zero claims with nonzero mentions stays legal (a document
                # can genuinely assert nothing); zero of both is a failure
                raise ValueError("empty extraction: 0 mentions, 0 claims")
            n_mentions += wrote_m
            n_claims += wrote_c
            n_dropped += dropped
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
        except llm.EngineUnavailable as e:
            # No live model path (seats latched / no key). Pause, don't punish:
            # the event goes back to pending with its attempts untouched, and
            # the rest of the batch waits for the next cycle.
            con.execute("rollback to savepoint extract_ev")
            con.execute("update event set status='pending' where event_id=%s",
                        (ev["event_id"],))
            print(f"extract: paused, no model available ({e})")
            paused = True
            break
        except llm.TransientError as e:
            # Timeout / process death: no verdict on the document, so no
            # strike — the rollback restores status/attempts, the message is
            # recorded, and the batch moves on (spec v6 §5)
            con.execute("rollback to savepoint extract_ev")
            con.execute("update event set last_error=%s where event_id=%s",
                        (str(e)[:500], ev["event_id"]))
            print(f"extract: transient failure ({e})")
            exclude.add(ev["event_id"])
            transient += 1
        except Exception as e:
            con.execute("rollback to savepoint extract_ev")
            attempts = ev["attempts"] + 1
            status = "failed" if attempts >= 2 else "pending"
            con.execute(
                "update event set status=%s, attempts=%s, last_error=%s "
                "where event_id=%s",
                (status, attempts, str(e)[:500], ev["event_id"]))
            exclude.add(ev["event_id"])
            failed += 1

    print(f"extract: {extracted} extracted, {failed} failed, "
          f"{transient} transient, {n_mentions} mentions, {n_claims} claims"
          + (f", {n_dropped} claims dropped" if n_dropped else ""))
    return {"extracted": extracted, "failed": failed, "transient": transient,
            "paused": paused, "claims": n_claims, "mentions": n_mentions,
            "claims_dropped": n_dropped}


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
    """Returns (written, dropped): dropped counts claim dicts the validation
    guards skipped — the caller fails the event when every claim drops."""
    n = dropped = 0
    for c in claims:
        subject_surface = ((c.get("subject") or {}).get("surface") or "").strip()
        predicate = (c.get("predicate") or "").strip()
        obj = c.get("object") or {}
        object_surface = (obj.get("surface") or "").strip() or None
        literal = obj.get("literal")
        if not subject_surface or not predicate:
            dropped += 1
            continue
        if object_surface is None and literal is None:
            dropped += 1
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
    return n, dropped


def parse_iso(v):
    if not v or not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None
