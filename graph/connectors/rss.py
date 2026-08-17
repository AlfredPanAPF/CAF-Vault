"""Generic article-feed connector: RSS feeds of blogs/newsletters/news sites.

Feed rows live in the source table (connector='rss'); each item's linked page
goes through the same article-container heuristic as manual uploads. Paywalled
or JS-only pages yield thin text — those are ingested anyway with meta thin:
true (the upload path is the fallback for paywalled sources).
"""
import re
from email.utils import parsedate_to_datetime
from html import unescape

import requests

from .. import envelope
from .manual import extract
from .podcast import HEADERS  # browser UA; many sites 403 the requests default

MAX_PAGE_BYTES = 2_000_000
THIN_CHARS = 400


def items(rss_text: str):
    for item in re.findall(r"<item>(.*?)</item>", rss_text, re.S):
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        link = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", item, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", item)
        if not (title and link):
            continue
        date = ""
        if pub:
            try:
                date = parsedate_to_datetime(pub.group(1)).date().isoformat()
            except Exception:
                pass
        yield unescape(title.group(1).strip()), date, unescape(link.group(1).strip())


def fetch_page(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30, stream=True)
    r.raise_for_status()
    data = b""
    for chunk in r.iter_content(65536):
        data += chunk
        if len(data) >= MAX_PAGE_BYTES:
            break
    r.close()
    return data[:MAX_PAGE_BYTES].decode(r.encoding or "utf-8", errors="replace")


def poll(con, limit_per_feed=5):
    counts = {"new": 0, "duplicate": 0, "thin": 0, "errors": 0}
    sources = con.execute(
        "select source_id, name, url from source "
        "where connector='rss' and status='active' order by name").fetchall()
    for src in sources:
        feed = src["name"]
        try:
            r = requests.get(src["url"], headers=HEADERS, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"error {feed}: feed fetch failed ({e})")
            counts["errors"] += 1
            continue
        con.execute("update source set last_polled=now() where source_id=%s",
                    (src["source_id"],))
        for title, date, link in list(items(r.text))[:limit_per_feed]:
            if con.execute("select 1 from event where meta->>'item_url'=%s limit 1",
                           (link,)).fetchone():
                counts["duplicate"] += 1
                continue
            try:
                _page_title, published, body = extract(fetch_page(link))
            except Exception as e:
                print(f"error {feed} '{title}': {e}")
                counts["errors"] += 1
                continue
            published = date or published[:10]
            thin = len(body) < THIN_CHARS
            doc = (f"# title: {title}\n# source_type: article\n"
                   f"# published: {published}\n# origin: {feed}\n---\n{body}\n")
            meta = {"feed": feed, "title": title, "item_url": link}
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
