"""Article-feed connector: RSS and Atom feeds of blogs, newsletters, news
sites and Substack publications (build spec v4 §6.4).

Feed rows live in the source table (connector='rss'); each item's linked page
goes through `extract_article`. Three things the v4 sources overhaul adds:

- Atom `<entry>` and `<content:encoded>` are parsed alongside RSS `<item>`.
- A source whose config carries a `site` (ft, wsj, substack) is fetched with
  that site's credential through `graph.fetch`, and a sign-in wall stops the
  source from fetching item pages for the cycle with `last_error =
  "Sign-in needed"` instead of burning every item against the wall. FT and WSJ
  articles are walled at the CDN edge for any server (spec §0), so those two
  keep going on the feed's own teaser: headline plus a line is what the feed
  sells, and an empty source would be the alternative.
- Substack sources use the feed's own `content:encoded` when it holds the full
  post, and the publication's JSON post API with the cookie when it does not
  (paid posts arrive as a preview in the feed).

Sources with no `site` keep the plain `requests` path with the browser UA:
podcast CDNs and small blogs are happiest with it, and it needs no credentials.
"""
import json
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlsplit

import requests

from .. import credentials, envelope, fetch, router
from .manual import extract_article, html_to_text
from .podcast import HEADERS  # browser UA; many sites 403 the requests default

MAX_PAGE_BYTES = 2_000_000
THIN_CHARS = 400
SUBSTACK_FULL_CHARS = 1500   # shorter than this and a paid post is a preview
# sites whose article pages no server can reach (Cloudflare on ft.com, DataDome
# on wsj.com, both at the edge, cookies or not — spec §0 and §11). A wall there
# is permanent, so the feed's teaser is the document, not a reason to skip.
TEASER_SITES = ("ft", "wsj")

_BLOCK_RE = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1>", re.S | re.I)
_CDATA = r"(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?"


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}\b[^>]*>{_CDATA}</{name}>", block, re.S | re.I)
    return unescape(m.group(1).strip()) if m else ""


def date_of(raw: str) -> str:
    """RFC 822 (RSS pubDate), ISO 8601 (Atom published/updated) or anything a
    page's own metadata offers -> YYYY-MM-DD, or '' when it is not a date."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else ""


def _link(block: str) -> str:
    """RSS `<link>url</link>` or Atom `<link rel="alternate" href="url"/>`."""
    text = _tag(block, "link")
    if text.startswith("http"):
        return text
    best = ""
    for m in re.finditer(r"<link\b([^>]*)>", block, re.I):
        attrs = m.group(1)
        rel = re.search(r'rel=["\']([^"\']+)', attrs, re.I)
        href = re.search(r'href=["\']([^"\']+)', attrs, re.I)
        if not href:
            continue
        rel = (rel.group(1).lower() if rel else "alternate")
        if rel == "alternate":
            return unescape(href.group(1))
        if rel not in ("enclosure", "self", "replies", "edit") and not best:
            best = unescape(href.group(1))
    return best


def items(feed_text: str):
    """Yield one dict per feed item: title, link (also as `url`), date
    (YYYY-MM-DD), content_html, enclosure, enclosure_type, uid. RSS `<item>`
    and Atom `<entry>` both land in this shape."""
    for m in _BLOCK_RE.finditer(feed_text or ""):
        block = m.group(2)
        title = _tag(block, "title")
        link = _link(block)
        if not (title and link):
            continue
        date = date_of(_tag(block, "pubDate") or _tag(block, "published")
                       or _tag(block, "updated") or _tag(block, "dc:date"))
        content = (_tag(block, "content:encoded") or _tag(block, "content")
                   or _tag(block, "description") or _tag(block, "summary"))
        enc = re.search(r"<enclosure\b[^>]*>", block, re.I) or re.search(
            r'<link\b[^>]*rel=["\']enclosure["\'][^>]*>', block, re.I)
        enc_url = enc_type = ""
        if enc:
            u = (re.search(r'url=["\']([^"\']+)', enc.group(0), re.I)
                 or re.search(r'href=["\']([^"\']+)', enc.group(0), re.I))
            t = re.search(r'type=["\']([^"\']+)', enc.group(0), re.I)
            enc_url = unescape(u.group(1)) if u else ""
            enc_type = t.group(1) if t else ""
        yield {"title": title, "link": link, "url": link, "date": date,
               "content_html": content, "enclosure": enc_url,
               "enclosure_type": enc_type,
               "uid": _tag(block, "guid") or _tag(block, "id") or link}


# ---------------------------------------------------------------- fetching


def fetch_page(url: str) -> str:
    """Plain browser-UA page fetch, for sources with no site credential."""
    r = requests.get(url, headers=HEADERS, timeout=30, stream=True)
    r.raise_for_status()
    data = b""
    for chunk in r.iter_content(65536):
        data += chunk
        if len(data) >= MAX_PAGE_BYTES:
            break
    r.close()
    return data[:MAX_PAGE_BYTES].decode(getattr(r, "encoding", None) or "utf-8",
                                        errors="replace")


def _page(url: str, site: str | None, con=None) -> str:
    """An item page as text. With a site, through `graph.fetch` so the
    credential rides along and a wall raises SignInNeeded; without one, the
    plain requests path."""
    if site:
        return fetch.get(url, site=site, con=con, timeout=30).text
    return fetch_page(url)


def _feed_text(url: str, site: str | None, con=None) -> str:
    """The feed itself. Feeds are public even on the premium sites, so wall
    detection is off here — it is the article pages that get walled. A non-2xx
    answer raises: neither client raises on its own, and a feed that starts
    answering 404 would otherwise parse as zero items and keep clearing
    last_error, so the sources page would call it healthy forever."""
    if site:
        r = fetch.get(url, site=site, con=con, timeout=30, allow_wall=True)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status}")
        return r.text
    # the same capped read the item pages get: an oversized or slow-drip feed
    # must not buffer without a ceiling inside the worker
    return fetch_page(url)


def substack_post(con, origin: str, slug: str) -> dict:
    """`<origin>/api/v1/posts/<slug>` with the Substack cookie ->
    {title, published, body, audience}. A paid post that comes back as a
    preview raises SignInNeeded (spec §3.2)."""
    url = f"{origin.rstrip('/')}/api/v1/posts/{slug}"
    r = fetch.get(url, site="substack", con=con, timeout=30, allow_wall=True,
                  headers={"Accept": "application/json"})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status}")
    try:
        data = json.loads(r.text)
    except ValueError:
        raise ValueError(f"post API did not return JSON ({r.status})") from None
    body_html = data.get("body_html") or ""
    audience = data.get("audience") or "everyone"
    if audience != "everyone" and len(body_html) < SUBSTACK_FULL_CHARS:
        raise fetch.SignInNeeded("substack", "paid post returned a preview")
    return {"title": data.get("title") or "",
            "published": date_of(str(data.get("post_date") or "")),
            "body": html_to_text(body_html), "audience": audience}


def slug_of(url: str) -> str:
    path = urlsplit(url).path
    return path.split("/p/")[-1].strip("/") if "/p/" in path else ""


# ---------------------------------------------------------------- polling


STALE_DAYS = 45


def stale_since(parsed: list[dict], today=None) -> str | None:
    """The newest item date (YYYY-MM-DD) when every dated item in the feed is
    older than STALE_DAYS, else None. Feeds without dates are never stale."""
    dates = sorted(i["date"] for i in parsed if i.get("date"))
    if not dates:
        return None
    newest = dates[-1]
    try:
        newest_d = date.fromisoformat(newest)
    except ValueError:
        return None
    today = today or date.today()
    return newest if (today - newest_d).days > STALE_DAYS else None


# what the sources page shows for a walled premium site: without a cookie the
# fix is to sign in; with one, FT and WSJ still block server fetches at the
# edge (docs/build-spec-v4-sources.md, hard facts) and the honest state is
# that headlines flow and article text does not
HEADLINES_ONLY = "Headlines only this cycle, article pages came back walled"


def wall_message(con, site: str | None) -> str:
    if site in TEASER_SITES and credentials.is_set(con, site):
        return HEADLINES_ONLY
    return "Sign-in needed"


def note_error(con, source_id, message: str) -> None:
    con.execute("update source set last_error=%s, last_error_at=now() "
                "where source_id=%s", (message, source_id))


def teaser(item: dict) -> str:
    """The item's own text from the feed (`description`, `summary` or
    `content:encoded`). For FT and WSJ this is the headline's one-line teaser,
    which is all a server can have."""
    return html_to_text(item.get("content_html") or "")


def _body_for(con, item: dict, site: str | None, substack_origin: str | None,
              walled: bool = False):
    """(published, body) for one item. `walled` means this source already hit
    its sign-in wall this cycle, so the feed's teaser is used and no further
    page fetch is attempted."""
    if substack_origin:
        if len(item["content_html"]) > SUBSTACK_FULL_CHARS:
            return item["date"], html_to_text(item["content_html"])
        slug = slug_of(item["link"])
        if slug:
            post = substack_post(con, substack_origin, slug)
            return item["date"] or post["published"], post["body"]
    if walled:
        return item["date"], teaser(item)
    _title, published, body = extract_article(_page(item["link"], site, con),
                                              item["link"])
    return item["date"] or date_of(published), body


def poll(con, limit_per_feed=5):
    counts = {"new": 0, "duplicate": 0, "thin": 0, "blocked": 0, "errors": 0}
    sources = con.execute(
        "select source_id, name, url, config from source "
        "where connector='rss' and status='active' order by name").fetchall()
    for src in sources:
        feed = src["name"]
        cfg = src["config"] or {}
        site = cfg.get("site")
        substack = cfg.get("substack") if isinstance(cfg.get("substack"), dict) else None
        substack_origin = (substack or {}).get("origin")
        try:
            text = _feed_text(src["url"], site, con)
        except fetch.SignInNeeded as e:
            print(f"error {feed}: {e}")
            note_error(con, src["source_id"], "Sign-in needed")
            counts["blocked"] += 1
            continue
        except Exception as e:
            print(f"error {feed}: feed fetch failed ({e})")
            note_error(con, src["source_id"], "Feed unreachable")
            counts["errors"] += 1
            continue
        con.execute("update source set last_polled=now(), last_error=null, "
                    "last_error_at=null where source_id=%s", (src["source_id"],))
        parsed = list(items(text))
        stale = stale_since(parsed)
        if stale:
            # a feed that answers 200 with nothing new for weeks (the legacy
            # WSJ host froze in Jan 2025 and still serves 200) shows up on the
            # sources page instead of quietly going dark; polling continues
            note_error(con, src["source_id"], f"No new items since {stale}")
        walled = False
        for item in parsed[:limit_per_feed]:
            title = item["title"]
            # the same key links.py stores, so an article pasted once and later
            # met in its feed is one item, not a refetch every cycle
            link = router.normalize(item["link"]) or item["link"]
            if con.execute("select 1 from event where meta->>'item_url' in (%s, %s) "
                           "limit 1", (link, item["link"])).fetchone():
                counts["duplicate"] += 1
                continue
            try:
                published, body = _body_for(con, item, site, substack_origin,
                                            walled=walled)
            except fetch.SignInNeeded as e:
                print(f"blocked {feed} '{title}': {e}")
                note_error(con, src["source_id"], wall_message(con, site))
                counts["blocked"] += 1
                if site not in TEASER_SITES or substack_origin:
                    break   # one wall means the rest of this source is walled
                # FT and WSJ: the wall is the edge, not the session. Keep the
                # cycle going on the teasers the feed already carried.
                walled = True
                published, body = item["date"], teaser(item)
            except Exception as e:
                print(f"error {feed} '{title}': {e}")
                counts["errors"] += 1
                continue
            if walled and not body.strip():
                continue      # a headline with no teaser is not a document
            thin = len(body) < THIN_CHARS
            doc = (f"# title: {title}\n# source_type: article\n"
                   f"# published: {published}\n# origin: {feed}\n---\n{body}\n")
            meta = {"feed": feed, "title": title, "item_url": link}
            if site:
                meta["site"] = site
            if thin:
                meta["thin"] = True
            _, is_new = envelope.ingest(
                con, src["source_id"], "rss", doc.encode("utf-8"), "text/plain",
                ".txt", published_at=published or None, meta=meta)
            counts["new" if is_new else "duplicate"] += 1
            if thin and is_new:
                counts["thin"] += 1
    print(f"rss poll: {counts}")
    return counts
