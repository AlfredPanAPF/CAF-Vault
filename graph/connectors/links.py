"""One-off links: articles, posts and videos pasted into the sources page
(build spec v4 §6.3).

A pasted link is not a source — it is fetched once. `enqueue` puts it in
link_queue, `process_one` runs a single row (the API calls it right after the
insert so the page can show the outcome), and `process` drains the queue in the
worker cycle. Four outcomes: done, duplicate (the vault already had it),
blocked (the site wants a sign-in; the row waits for the credential), failed
(anything else, three attempts).
"""
import re
from urllib.parse import parse_qs, urlsplit

from .. import credentials, db, envelope, fetch
from . import rss, x, youtube
from .manual import extract_article

MAX_ATTEMPTS = 3
SITE_NAMES = {"ft": "FT", "wsj": "WSJ", "substack": "Substack",
              "x": "X", "youtube": "YouTube"}


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def bucket_source(con, url: str):
    """The `link:<host>` source every one-off link from that host lands in."""
    host = host_of(url) or "link"
    return db.get_or_create_source(con, f"link:{host}", "link", url=None,
                                   is_internal=False)


def video_id_of(url: str) -> str:
    parts = urlsplit(url)
    values = parse_qs(parts.query).get("v")
    if values:
        return values[0]
    m = re.search(r"/(?:shorts|embed|v|live)/([\w-]{6,})", parts.path)
    if m:
        return m.group(1)
    if (parts.hostname or "").endswith("youtu.be"):
        return parts.path.strip("/").split("/")[0]
    return ""


def blocked_message(site: str | None, con=None) -> str:
    """"Sign-in needed for FT." while no cookie is saved; once one is and FT
    or WSJ still wall the fetch, the edge block is the reason, not the cookie
    (docs/build-spec-v4-sources.md, hard facts)."""
    name = SITE_NAMES.get(site or "")
    if site in ("ft", "wsj") and con is not None and credentials.is_set(con, site):
        return f"{name} returned its wall for that article. Retry later."
    return f"Sign-in needed for {name}." if name else "Sign-in needed."


# ---------------------------------------------------------------- queue


def enqueue(con, url: str, kind: str, site: str | None, added_by: str,
            title: str | None = None) -> dict:
    """Insert the link, or return the row that already holds this URL. The
    extra `created` key says which of the two happened, so a re-paste can be
    reported as "Already in the vault." rather than as a fresh add."""
    row = con.execute(
        "insert into link_queue (url, kind, site, title, added_by) "
        "values (%s, %s, %s, %s, %s) on conflict (url) do nothing returning *",
        (url, kind, site, title, added_by)).fetchone()
    created = row is not None
    if row is None:
        row = con.execute("select * from link_queue where url=%s",
                          (url,)).fetchone()
    return {**dict(row), "created": created}


def requeue_blocked(con, site: str) -> int:
    """Put this site's blocked links back in the queue (called when a
    credential is saved, §7). Returns how many moved."""
    cur = con.execute(
        "update link_queue set status='queued', attempts=0, error=null, "
        "updated_at=now() where status='blocked' and site=%s", (site,))
    return cur.rowcount


def _finish(con, link_id, status: str, event_id=None, title=None,
            error=None, count_attempt=True) -> dict:
    return dict(con.execute(
        "update link_queue set status=%s, event_id=coalesce(%s, event_id), "
        "title=coalesce(%s, title), error=%s, "
        "attempts=attempts + case when %s then 1 else 0 end, updated_at=now() "
        "where link_id=%s returning *",
        (status, event_id, title, error, count_attempt, link_id)).fetchone())


# ---------------------------------------------------------------- fetchers


def _article(con, row) -> dict:
    url = row["url"]
    resp = fetch.get(url, site=row["site"], con=con, timeout=30)
    if not resp.ok:
        # neither fetch client raises on a status; without this a 404 page
        # ingests as a document and the row finishes green
        raise RuntimeError(f"HTTP {resp.status}")
    title, published, body = extract_article(resp.text, url)
    published = rss.date_of(published)
    doc = (f"# title: {title}\n# source_type: article\n"
           f"# published: {published}\n# origin: {host_of(url)}\n---\n{body}\n")
    meta = {"item_url": url, "title": title, "site": row["site"]}
    if len(body) < rss.THIN_CHARS:
        meta["thin"] = True
    event_id, is_new = envelope.ingest(
        con, bucket_source(con, url), "link", doc.encode("utf-8"), "text/plain",
        ".txt", published_at=published or None, meta=meta)
    return {"event_id": event_id, "is_new": is_new, "title": title}


def _substack_post(con, row) -> dict:
    url = row["url"]
    parts = urlsplit(url)
    origin = f"{parts.scheme or 'https'}://{parts.netloc}"
    slug = rss.slug_of(url)
    if not slug:
        raise ValueError("That Substack link has no post in it.")
    post = rss.substack_post(con, origin, slug)
    doc = (f"# title: {post['title']}\n# source_type: article\n"
           f"# published: {post['published']}\n# origin: {host_of(url)}\n---\n"
           f"{post['body']}\n")
    event_id, is_new = envelope.ingest(
        con, bucket_source(con, url), "link", doc.encode("utf-8"), "text/plain",
        ".txt", published_at=post["published"] or None,
        meta={"item_url": url, "title": post["title"], "site": "substack"})
    return {"event_id": event_id, "is_new": is_new, "title": post["title"]}


def _youtube_video(con, row) -> dict:
    video_id = video_id_of(row["url"])
    if not video_id:
        raise ValueError("That link has no video in it.")
    out = youtube.ingest_video(con, video_id,
                               source_id=bucket_source(con, row["url"]),
                               link_id=row["link_id"])
    if out.get("queued"):
        # remote ASR (build spec v7 §4): the transcript is on its way from
        # the off-site agent; not a failure, and not this link's attempt
        return out
    if out["event_id"] is None:
        raise RuntimeError(out["skip_reason"] or "No transcript.")
    return out


def _x_post(con, row) -> dict:
    return x.post(con, row["url"], source_id=bucket_source(con, row["url"]))


HANDLERS = {"article": _article, "substack_post": _substack_post,
            "youtube_video": _youtube_video, "x_post": _x_post}


# ---------------------------------------------------------------- running


def process_one(con, link_id) -> dict:
    """Run one queued, failed or blocked row and return it with its outcome."""
    row = con.execute("select * from link_queue where link_id=%s",
                      (link_id,)).fetchone()
    if row is None:
        raise LookupError("That link is not in the queue.")
    if row["status"] in ("done", "duplicate"):
        return dict(row)
    handler = HANDLERS.get(row["kind"])
    if handler is None:
        return _finish(con, link_id, "failed",
                       error=f"Nothing here reads {row['kind']} links.")
    con.execute("update link_queue set source_id=%s where link_id=%s",
                (bucket_source(con, row["url"]), link_id))
    try:
        out = handler(con, row)
    except fetch.SignInNeeded as e:
        print(f"blocked {row['url']}: {e}")
        return _finish(con, link_id, "blocked",
                       error=blocked_message(row["site"] or e.site, con),
                       count_attempt=False)
    except x.XPaused as e:
        # a rate limit or a plan limit is X's clock, not this link's fault:
        # three cycles inside one rate-limit window would otherwise use up the
        # attempt budget and the post would never be fetched
        print(f"paused {row['url']}: {e.message}")
        return _finish(con, link_id, "failed", error=e.message,
                       count_attempt=False)
    except Exception as e:
        message = credentials.scrub(con, str(e))[:300]
        print(f"error {row['url']}: {message}")
        return _finish(con, link_id, "failed", error=message)
    if out.get("queued"):
        # the row stays queued, costs no attempt, and is resolved by
        # asr_queue.complete() when the agent posts the transcript; until
        # then each cycle's re-check is one select, not a download
        return _finish(con, link_id, "queued", title=out.get("title"),
                       count_attempt=False)
    status = "done" if out["is_new"] else "duplicate"
    return _finish(con, link_id, status, event_id=out["event_id"],
                   title=out.get("title"))


def process(con, limit=20):
    """Drain the queue once: queued and failed rows under the attempt cap."""
    counts = {"done": 0, "duplicate": 0, "blocked": 0, "failed": 0}
    rows = con.execute(
        "select link_id from link_queue where status in ('queued','failed') "
        "and attempts < %s order by created_at limit %s",
        (MAX_ATTEMPTS, limit)).fetchall()
    for row in rows:
        try:
            out = process_one(con, row["link_id"])
        except Exception as e:                      # never lose the rest
            print(f"error link {row['link_id']}: {e}")
            counts["failed"] += 1
            continue
        if out["status"] in counts:
            counts[out["status"]] += 1
    print(f"links: {counts}")
    return counts
