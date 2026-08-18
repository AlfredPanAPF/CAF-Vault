"""X connector: accounts and single posts through the v2 API
(build spec v4 §6.2).

One event per account per cycle, not one per post: a day of posts from an
account reads as one document and keeps the extraction budget sane. The bearer
token comes from the `x` credential; a rate limit or a plan limit stops X for
the cycle and shows on the source row, a 401 raises SignInNeeded.
"""
import re
from datetime import datetime, timezone

import requests
from psycopg.types.json import Jsonb

from .. import credentials, db, envelope, fetch

API = "https://api.x.com/2"
TIMEOUT = 30
TWEET_FIELDS = "created_at,text,note_tweet,public_metrics,entities"
MAX_RESULTS = 100


class XPaused(Exception):
    """Rate limit or plan limit: stop polling X for this cycle."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def token(con) -> str:
    value = credentials.value(con, "x")
    if not value:
        raise fetch.SignInNeeded("x", "no token saved")
    try:
        return credentials.bearer_token(value)
    except credentials.InvalidCredential as e:
        raise fetch.SignInNeeded("x", str(e)) from None


def api_get(con, path: str, params: dict | None = None) -> dict:
    """GET one v2 endpoint. 401 -> SignInNeeded, 429/402 -> XPaused."""
    r = requests.get(f"{API}{path}", params=params or {},
                     headers={"Authorization": f"Bearer {token(con)}",
                              "Accept": "application/json"},
                     timeout=TIMEOUT)
    if r.status_code == 401:
        raise fetch.SignInNeeded("x", "the token was rejected")
    if r.status_code == 429:
        raise XPaused("Rate limited, retry next cycle")
    if r.status_code == 402:
        raise XPaused("X plan does not allow this endpoint")
    if r.status_code >= 400:
        raise RuntimeError(f"x api {path} returned {r.status_code}")
    return r.json() or {}


# ---------------------------------------------------------------- shaping


def expand_urls(post: dict) -> str:
    """Post text with t.co links replaced by what they point at."""
    text = ((post.get("note_tweet") or {}).get("text")
            or post.get("text") or "")
    urls = ((post.get("entities") or {}).get("urls")
            or ((post.get("note_tweet") or {}).get("entities") or {}).get("urls")
            or [])
    for u in urls:
        short, full = u.get("url"), u.get("expanded_url") or u.get("unwound_url")
        if short and full:
            text = text.replace(short, full)
    return text.strip()


def post_url(username: str, post_id: str) -> str:
    return f"https://x.com/{username}/status/{post_id}"


def _block(username: str, post: dict) -> str:
    created = post.get("created_at") or ""
    return (f"[{created}] {expand_urls(post)}\n"
            f"{post_url(username, post.get('id'))}")


def _date(raw: str) -> str:
    try:
        return datetime.fromisoformat((raw or "").replace("Z", "+00:00")) \
            .date().isoformat()
    except ValueError:
        return datetime.now(timezone.utc).date().isoformat()


def document(username: str, posts: list[dict], date: str) -> tuple[str, str]:
    """(title, document) for a batch of posts from one account."""
    count = len(posts)
    title = f"@{username}, {count} post{'' if count == 1 else 's'}, {date}"
    body = "\n\n".join(_block(username, p) for p in posts)
    doc = (f"# title: {title}\n# source_type: social\n# published: {date}\n"
           f"# origin: x.com/@{username}\n---\n{body}\n")
    return title, doc


def post_id_of(url_or_id: str) -> str:
    """'https://x.com/user/status/123' or '123' -> '123'."""
    raw = (url_or_id or "").strip()
    m = re.search(r"/status/(\d+)", raw)
    if m:
        return m.group(1)
    return raw if raw.isdigit() else ""


# ---------------------------------------------------------------- poll


def user_id(con, username: str) -> tuple[str, str]:
    data = api_get(con, f"/users/by/username/{username}").get("data") or {}
    return str(data.get("id") or ""), (data.get("username") or username)


def timeline(con, uid: str, since_id: str | None) -> list[dict]:
    params = {"max_results": MAX_RESULTS, "exclude": "retweets,replies",
              "tweet.fields": TWEET_FIELDS}
    if since_id:
        params["since_id"] = since_id
    return (api_get(con, f"/users/{uid}/tweets", params).get("data") or [])


def _note_error(con, source_id, message: str) -> None:
    con.execute("update source set last_error=%s, last_error_at=now() "
                "where source_id=%s", (message, source_id))


def _save_config(con, source_id, config: dict) -> None:
    con.execute("update source set config = coalesce(config, '{}'::jsonb) || %s "
                "where source_id=%s", (Jsonb(config), source_id))


def poll(con):
    """Poll active x source rows: one event per account with everything posted
    since the stored since_id."""
    counts = {"new": 0, "duplicate": 0, "empty": 0, "blocked": 0, "errors": 0}
    sources = con.execute(
        "select source_id, name, url, config from source "
        "where connector='x' and status='active' order by name").fetchall()
    for src in sources:
        config = src["config"] or {}
        username = (config.get("username") or src["name"].lstrip("@")).lstrip("@")
        try:
            uid = config.get("user_id")
            if not uid:
                uid, username = user_id(con, username)
                if not uid:
                    raise RuntimeError("account not found")
                _save_config(con, src["source_id"],
                             {"user_id": uid, "username": username})
            posts = timeline(con, uid, config.get("since_id"))
        except fetch.SignInNeeded as e:
            print(f"blocked @{username}: {e}")
            _note_error(con, src["source_id"], "Sign-in needed")
            counts["blocked"] += 1
            break   # one token, so the rest of the cycle is blocked too
        except XPaused as e:
            print(f"paused @{username}: {e.message}")
            _note_error(con, src["source_id"], e.message)
            counts["blocked"] += 1
            break
        except Exception as e:
            print(f"error @{username}: {credentials.scrub(con, str(e))}")
            _note_error(con, src["source_id"], "Could not read the account")
            counts["errors"] += 1
            continue
        con.execute("update source set last_polled=now(), last_error=null, "
                    "last_error_at=null where source_id=%s", (src["source_id"],))
        if not posts:
            counts["empty"] += 1
            continue
        posts = sorted(posts, key=lambda p: str(p.get("id") or "").zfill(24))
        date = _date(posts[-1].get("created_at"))
        title, doc = document(username, posts, date)
        newest = str(posts[-1].get("id"))
        _, is_new = envelope.ingest(
            con, src["source_id"], "x", doc.encode("utf-8"), "text/plain", ".txt",
            published_at=date,
            meta={"username": username, "title": title,
                  "post_ids": [str(p.get("id")) for p in posts],
                  "newest_id": newest, "site": "x"})
        counts["new" if is_new else "duplicate"] += 1
        _save_config(con, src["source_id"], {"since_id": newest})
    print(f"x poll: {counts}")
    return counts


def check(con) -> tuple[bool, str]:
    """Live probe for the x credential (§7): read one public account."""
    try:
        api_get(con, "/users/by/username/x")
    except fetch.SignInNeeded as e:
        return False, f"Sign-in failed: {e.detail or 'the token was rejected'}."
    except XPaused as e:
        return False, f"Sign-in failed: {e.message}."
    except Exception as e:
        # the client quotes the Authorization header it choked on, and this
        # message is stored on the credential row and rendered on the page
        return False, f"Sign-in failed: {credentials.scrub(con, str(e))[:200]}."
    return True, "Signed in."


def post(con, url_or_id: str, source_id=None) -> dict:
    """One post into an event, for the one-off link queue (§6.3). Returns
    {event_id, is_new, title, published, url}."""
    pid = post_id_of(url_or_id)
    if not pid:
        raise ValueError("That does not look like a post link.")
    data = api_get(con, f"/tweets/{pid}",
                   {"tweet.fields": TWEET_FIELDS, "expansions": "author_id",
                    "user.fields": "username"})
    item = data.get("data") or {}
    if not item:
        raise RuntimeError("The post is not available.")
    users = (data.get("includes") or {}).get("users") or []
    username = (users[0].get("username") if users else "") or "x"
    item.setdefault("id", pid)
    date = _date(item.get("created_at"))
    title, doc = document(username, [item], date)
    if source_id is None:
        source_id = db.get_or_create_source(con, "link:x.com", "link", url=None,
                                            is_internal=False)
    event_id, is_new = envelope.ingest(
        con, source_id, "x", doc.encode("utf-8"), "text/plain", ".txt",
        published_at=date,
        meta={"username": username, "title": title, "post_ids": [str(pid)],
              "newest_id": str(pid), "item_url": post_url(username, pid),
              "site": "x"})
    return {"event_id": event_id, "is_new": is_new, "title": title,
            "published": date, "url": post_url(username, pid)}
