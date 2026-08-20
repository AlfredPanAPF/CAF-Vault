"""Document summaries (build spec v5 §3): one short summary per document the
worker extracted, written by the LLM into document_summary. Derived data,
like edges: rebuildable from the artifact, overwritten when rewritten.

The row doubles as the queue entry. 'requested' is the page asking (served
first, and between cycles); 'pending' is an automatic attempt that failed
once; 'failed' is two strikes; 'skipped' is a document too short to be worth
a model call. Duplicate events are never summarized: their root carries it.

Engine discipline as everywhere: llm.EngineUnavailable pauses the batch and
leaves every row as it was; it never burns an attempt. Commits stay in the
CLI layer, which runs this in small chunks (cli._summarize_drain) so a page
request is never blocked for long on a row the batch is writing and a
worker restart loses at most one chunk.
"""
from psycopg.types.json import Jsonb

from .. import artifacts, config, credentials, llm, schemas

SUMMARY_CAP = 2000        # chars kept from the model's summary
POINTS_CAP = 10           # key points kept
POINT_CAP = 300           # chars per key point
MAX_ATTEMPTS = 2
HEADER_SCAN = 16384       # the connector header lives in the first bytes
RETRY_AFTER = "5 minutes" # a requested row with a strike waits this long

# A requested row is eligible when it has no strike yet, or its strike is
# older than RETRY_AFTER: the nap loop answers requests every fifteen
# seconds, and without the wait a transient model error would burn both
# strikes in half a minute.
REQUESTED_WHERE = f"""
ds.status = 'requested'
and (ds.attempts = 0 or ds.updated_at < now() - interval '{RETRY_AFTER}')
"""

# the same event filters the candidate query applies: the nap loop must not
# see a request the stage can never clear
EVENT_WHERE = "ev.status <> 'duplicate' and ev.lineage_id is not null"

CANDIDATES_SQL = f"""
select ev.event_id, ev.artifact_uri, ev.connector, ev.meta,
       ds.status as summary_status, coalesce(ds.attempts, 0) as attempts
from event ev
left join document_summary ds on ds.event_id = ev.event_id
where {EVENT_WHERE}
  and not (ev.event_id = any(%(exclude)s::uuid[]))
  and (({REQUESTED_WHERE})
       or (%(auto)s and coalesce(ds.status, 'pending') = 'pending'
           and ev.status in ('extracted', 'failed') and ev.triage is not null))
order by (ds.status is not distinct from 'requested') desc, ds.requested_at,
         coalesce(ev.published_at, ev.fetched_at) desc, ev.event_id
limit %(limit)s
"""

REQUESTED_COUNT_SQL = f"""
select count(*) as n
from document_summary ds join event ev on ev.event_id = ds.event_id
where {EVENT_WHERE} and ({REQUESTED_WHERE})
"""

DONE_SQL = """
insert into document_summary (event_id, status, summary, key_points, model,
                              attempts, error, updated_at)
values (%(event_id)s, 'done', %(summary)s, %(key_points)s, %(model)s,
        %(attempts)s, null, now())
on conflict (event_id) do update
   set status = 'done', summary = excluded.summary,
       key_points = excluded.key_points, model = excluded.model,
       attempts = excluded.attempts, error = null, updated_at = now()
"""

# bookkeeping only: an older summary text stays until a new one replaces it
# (a re-request that fails, or a document that came back thin, keeps what
# the page already shows)
BOOKKEEPING_SQL = """
insert into document_summary (event_id, status, attempts, error, updated_at)
values (%(event_id)s, %(status)s, %(attempts)s, %(error)s, now())
on conflict (event_id) do update
   set status = excluded.status, attempts = excluded.attempts,
       error = excluded.error, updated_at = now()
"""


def split_artifact(text: str):
    """The connector header ('# key: value' lines up to the first '---' rule)
    and the body. A header value may run over several lines (rss-bridge and
    Telegram titles do): a line that is not '# key: value' continues the
    previous value. No header line first, or no rule within the first
    HEADER_SCAN characters: header {} and the whole text is the body. The
    body is a slice of the input, not a copy through a line list."""
    header, start = parse_header(text)
    return header, text[start:].strip("\n")


def parse_header(text: str):
    """(header dict, index where the body starts); ({}, 0) without a header."""
    first, _, _ = text.partition("\n")
    if not (first.startswith("# ") and ":" in first):
        return {}, 0
    rule = text.find("\n---\n", 0, HEADER_SCAN)
    if rule < 0:
        if text.endswith("\n---") and len(text) <= HEADER_SCAN:
            rule = len(text) - 4
        else:
            return {}, 0
    header, key = {}, None
    for line in text[:rule].split("\n"):
        if line.startswith("# ") and ":" in line:
            key, _, value = line[2:].partition(":")
            key = key.strip()
            header[key] = value.strip()
        elif key is not None and line.strip():
            header[key] = f"{header[key]} {line.strip()}".strip()
    return header, rule + 5


def request(con, event_id):
    """The page asking for a summary (build spec v5 §5): the row goes to the
    front of the queue; an existing summary stays until it is replaced."""
    con.execute(
        "insert into document_summary (event_id, status, requested_at, "
        "attempts, error) values (%s, 'requested', now(), 0, null) "
        "on conflict (event_id) do update set status='requested', "
        "requested_at=now(), attempts=0, error=null, updated_at=now()",
        (event_id,))


def requested_count(con) -> int:
    """Page requests the stage can act on right now (the worker's nap loop
    asks before and after a requested-only run)."""
    return con.execute(REQUESTED_COUNT_SQL).fetchone()["n"]


def requested_pending(con) -> bool:
    return requested_count(con) > 0


def error_text(con, e: Exception) -> str:
    """The line the page shows under "Could not summarize.": fixed copy for
    the two expected failures, otherwise the message with any stored
    credential value scrubbed (the field is user-visible), cut to 500."""
    if isinstance(e, OSError):
        return "The document text is no longer on disk."
    if isinstance(e, ValueError):
        return "The model did not return a summary."
    return credentials.scrub(con, str(e))[:500]


def _dispose(out) -> tuple[str, list[str]]:
    """Code disposes: a non-empty summary string, key points a list of
    non-empty strings; anything else is a model failure."""
    if not isinstance(out, dict):
        raise ValueError("summary response is not an object")
    summary = out.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary response has no summary text")
    summary = " ".join(summary.split())[:SUMMARY_CAP]
    points = []
    raw = out.get("key_points")
    if isinstance(raw, list):
        for p in raw:
            if isinstance(p, str) and p.strip():
                points.append(" ".join(p.split())[:POINT_CAP])
            if len(points) >= POINTS_CAP:
                break
    return summary, points


def _prompt(header: dict, body: str) -> str:
    """The per-document user turn; the static prompt file rides the system
    turn (byte-identical across a batch, the only shape that can get prefix
    cache reuse — and it separates our instructions from fetched text)."""
    facts = [f"- {k}: {v}" for k, v in header.items()
             if k in ("title", "source_type", "published", "feed", "ticker",
                      "channel", "origin") and v]
    cut = len(body) > config.SUMMARY_MAX_CHARS
    text = body[:config.SUMMARY_MAX_CHARS]
    note = ("\n\n(The text is cut here; the document continues.)" if cut else "")
    return ("# Document facts\n\n" + ("\n".join(facts) or "- none")
            + "\n\n# Document text\n\n" + text + note)


def run(con, limit=None, requested_only=False, exclude=None) -> dict:
    """Summarize up to `limit` candidates (default config.SUMMARIZE_PER_CYCLE;
    0 does nothing). Returns counts plus `paused` and `requested_left`, the
    page requests still waiting after this call. `exclude` is a mutable set
    of event ids to skip; transient items are added in place so the drain
    attempts each row at most once per drain (a transient row keeps its
    status and attempts, so it would otherwise be re-selected next chunk at
    up to a 20-minute timeout per attempt)."""
    limit = config.SUMMARIZE_PER_CYCLE if limit is None else max(0, limit)
    prompt_base = (config.PROMPTS / "summary.md").read_text()
    model = config.MODELS["summarize"]
    exclude = exclude if exclude is not None else set()
    rows = con.execute(CANDIDATES_SQL,
                       {"auto": not requested_only, "limit": limit,
                        "exclude": list(exclude)}).fetchall()

    summarized = skipped = failed = transient = 0
    paused = False
    for ev in rows:
        con.execute("savepoint summarize_ev")
        try:
            text = artifacts.get(ev["artifact_uri"]).decode("utf-8", errors="replace")
            header, body = split_artifact(text)
            if len(body) < config.SUMMARY_MIN_CHARS:
                con.execute(BOOKKEEPING_SQL, {
                    "event_id": ev["event_id"], "status": "skipped",
                    "attempts": ev["attempts"], "error": None})
                con.execute("release savepoint summarize_ev")
                skipped += 1
                continue
            out = llm.complete_json(_prompt(header, body), model,
                                    max_tokens=2000, schema=schemas.SUMMARY,
                                    system=prompt_base)
            summary, points = _dispose(out)
            # the summary row carries the RESOLVED model id (provenance)
            model_used = (llm.last_call_info() or {}).get("model") or model
            con.execute(DONE_SQL, {
                "event_id": ev["event_id"], "summary": summary,
                "key_points": Jsonb(points), "model": model_used,
                "attempts": ev["attempts"]})
            con.execute("release savepoint summarize_ev")
            summarized += 1
        except llm.EngineUnavailable as e:
            # no live model path: leave the row exactly as it was and stop;
            # a quota window must not burn attempts across the corpus
            con.execute("rollback to savepoint summarize_ev")
            con.execute("release savepoint summarize_ev")
            print(f"summarize: paused, no model available ({e})")
            paused = True
            break
        except llm.TransientError as e:
            # timeout / process death: no verdict on the document, so no
            # strike — status and attempts stay as they were, the message
            # lands on the row, the batch moves on (spec v6 §5)
            con.execute("rollback to savepoint summarize_ev")
            con.execute("release savepoint summarize_ev")
            con.execute(BOOKKEEPING_SQL, {
                "event_id": ev["event_id"],
                "status": ev["summary_status"] or "pending",
                "attempts": ev["attempts"], "error": error_text(con, e)})
            exclude.add(ev["event_id"])
            transient += 1
        except Exception as e:
            con.execute("rollback to savepoint summarize_ev")
            con.execute("release savepoint summarize_ev")
            attempts = ev["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                status = "failed"
            elif ev["summary_status"] == "requested":
                status = "requested"
            else:
                status = "pending"
            con.execute(BOOKKEEPING_SQL, {
                "event_id": ev["event_id"], "status": status,
                "attempts": attempts, "error": error_text(con, e)})
            failed += 1

    left = requested_count(con)
    print(f"summarize: {summarized} summarized, {skipped} skipped, "
          f"{failed} failed"
          + (f", {transient} transient" if transient else "")
          + (", paused" if paused else "")
          + (f", {left} requested waiting" if left else ""))
    return {"summarized": summarized, "skipped": skipped, "failed": failed,
            "transient": transient, "paused": paused, "requested_left": left}
