"""Remote ASR job queue (build spec v7 §3).

The worker enqueues audio items when CAF_ASR=remote; the off-site agent
leases them through the queue API, transcribes, and posts the text back;
completion goes through envelope.ingest so a remote transcript is
byte-identical in form to a locally transcribed one (same doc header, same
event meta, same dedup and lineage).

Library rules: no function here commits — callers do (webapp handlers
commit per request, the worker commits per stage).
"""
import uuid
from pathlib import Path

from . import config, envelope
from .envelope import psycopg_json


def enqueue(con, source_id, connector: str, external_id: str, *, title: str,
            published_at: str | None, doc_prefix: str, event_meta: dict,
            audio_url: str | None = None, audio_path: str | None = None,
            link_id=None):
    """Insert one job; returns its job_id, or None when a row for this
    (connector, external_id) already exists — pending, done or terminal.
    A terminal 'error' row deliberately blocks re-enqueue (§2). `link_id`
    ties the job to a link_queue row, which complete() then resolves."""
    job_id = uuid.uuid4()
    meta = {"doc_prefix": doc_prefix, "event_meta": event_meta}
    if link_id is not None:
        meta["link_id"] = str(link_id)
    row = con.execute(
        "insert into asr_job (job_id, source_id, connector, external_id, "
        "title, published_at, audio_url, audio_path, meta) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (connector, external_id) do nothing returning job_id",
        (job_id, source_id, connector, external_id, title, published_at,
         audio_url, audio_path, psycopg_json(meta))).fetchone()
    return row["job_id"] if row else None


def get_external(con, connector: str, external_id: str):
    """The job row for one item, or None. The connectors' pre-filters key off
    its status: under remote, any row means never download or enqueue again;
    under a local engine, a pending/leased row means the agent owns the item
    and transcribing it here would double-ingest the episode (§4)."""
    return con.execute(
        "select * from asr_job where connector=%s and external_id=%s",
        (connector, external_id)).fetchone()


def lease(con, worker: str):
    """Oldest pending (or lease-expired) job -> leased to `worker`, with the
    attempt counted. `for update skip locked` keeps concurrent agents off
    each other's rows. Returns the job row or None."""
    return con.execute(
        "update asr_job set status='leased', leased_by=%s, "
        "lease_expires=now() + make_interval(mins => %s), attempts=attempts+1 "
        "where job_id = (select job_id from asr_job "
        "  where (status='pending' and (not_before is null or not_before <= now())) "
        "     or (status='leased' and lease_expires < now()) "
        "  order by created_at limit 1 for update skip locked) "
        "returning *", (worker, config.ASR_LEASE_MINUTES)).fetchone()


def get(con, job_id):
    return con.execute("select * from asr_job where job_id=%s",
                       (job_id,)).fetchone()


def complete(con, job, text: str):
    """Transcript -> event, exactly as the inline path would have written it:
    the stored doc_prefix is the connector's document header, the transcript
    and one newline follow. Returns (event_id, is_new). Accepted from any
    non-'done' status — a transcript that arrives after the lease expired,
    or after a terminal error, is still the transcript. A job enqueued from
    the link queue resolves its link row here, so the sources page stops
    saying the link is waiting. The spool file is NOT removed here: the
    caller unlinks after its commit (cleanup_spool), so a commit that never
    lands cannot leave a leased job whose only audio copy is gone."""
    meta = job["meta"] or {}
    doc = f"{meta['doc_prefix']}{text}\n"
    event_id, is_new = envelope.ingest(
        con, job["source_id"], job["connector"], doc.encode("utf-8"),
        "text/plain", ".txt", published_at=job["published_at"] or None,
        meta=meta.get("event_meta") or {})
    con.execute(
        "update asr_job set status='done', event_id=%s, done_at=now(), "
        "error=null where job_id=%s", (event_id, job["job_id"]))
    if meta.get("link_id"):
        con.execute(
            "update link_queue set status='done', event_id=%s, error=null, "
            "updated_at=now() where link_id=%s "
            "and status not in ('done','duplicate')",
            (event_id, uuid.UUID(meta["link_id"])))
    return event_id, is_new


def fail(con, job, error: str):
    """One failed attempt: back to pending behind a retry backoff (so one
    agent pass cannot burn the whole attempt budget on the same broken
    download), or terminal 'error' once the budget is spent. The caller
    removes the terminal job's spool file after its commit (cleanup_spool).
    Returns the new status."""
    terminal = (job["attempts"] or 0) >= config.ASR_MAX_ATTEMPTS
    status = "error" if terminal else "pending"
    con.execute(
        "update asr_job set status=%s, error=%s, leased_by=null, "
        "lease_expires=null, not_before=now() + make_interval(mins => %s) "
        "where job_id=%s",
        (status, (error or "")[:2000], config.ASR_RETRY_MINUTES,
         job["job_id"]))
    return status


def counts(con) -> dict:
    out = {"pending": 0, "leased": 0, "done": 0, "error": 0}
    for r in con.execute("select status as k, count(*) n from asr_job group by 1"):
        out[r["k"]] = r["n"]
    return out


def spool_path(job) -> Path | None:
    """The job's server-held audio file, containment-checked against the
    spool root (same rule as artifacts.read_bounded): anything outside, or
    not a regular file, is treated as absent."""
    if not job["audio_path"]:
        return None
    path = Path(job["audio_path"]).resolve()
    root = config.ASR_SPOOL.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def cleanup_spool(job) -> None:
    """Remove the job's spool file. Called by the API layer AFTER the commit
    that made the completion (or terminal failure) durable — never inside the
    transaction, where a failed commit would leave a live job without its
    audio. A crash between commit and unlink leaves an orphan file, the
    benign direction (§7)."""
    path = spool_path(job)
    if path is not None:
        path.unlink(missing_ok=True)
