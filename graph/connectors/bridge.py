"""rss-bridge connector: sites without feeds, through our private instance
(build spec v4 §5.3).

Sources carry `connector='bridge'` and `config = {"bridge": {"name", "params"}}`.
The client in graph/bridge.py normalizes rss-bridge's JSON into items; this
connector turns the newest few into article events. Bridge items usually carry
the whole post (Telegram, Bluesky, Mastodon); when the item body is thin and it
links out, the linked page is fetched and extracted instead.
"""
import os

from .. import bridge as client
from .. import envelope, fetch, router
from . import rss
from .manual import extract_article, html_to_text

THIN_CHARS = 400


def items_per_cycle() -> int:
    try:
        return max(1, int(os.environ.get("CAF_BRIDGE_ITEMS_PER_CYCLE", "5")))
    except ValueError:
        return 5


def _note_error(con, source_id, message: str) -> None:
    con.execute("update source set last_error=%s, last_error_at=now() "
                "where source_id=%s", (message, source_id))


def body_for(item: dict, con=None) -> str:
    """Item text: the bridge's own HTML, or the linked page when that is thin."""
    body = html_to_text(item.get("content_html") or "")
    if len(body) >= THIN_CHARS or not item.get("url"):
        return body
    try:
        _title, _published, page = extract_article(
            fetch.get(item["url"], con=con, timeout=30).text, item["url"])
    except Exception as e:
        print(f"  page fetch failed ({e}); keeping the bridge text")
        return body
    return page if len(page) > len(body) else body


def poll(con, items_per_source=None):
    """Poll active bridge source rows."""
    counts = {"new": 0, "duplicate": 0, "thin": 0, "errors": 0}
    limit = items_per_source or items_per_cycle()
    sources = con.execute(
        "select source_id, name, url, config from source "
        "where connector='bridge' and status='active' order by name").fetchall()
    for src in sources:
        name = src["name"]
        spec = (src["config"] or {}).get("bridge") or {}
        bridge_name, params = spec.get("name"), spec.get("params") or {}
        if not bridge_name:
            print(f"skip {name}: no bridge in config")
            counts["errors"] += 1
            continue
        try:
            feed = client.display(bridge_name, params)
        except client.BridgeError as e:
            print(f"error {name}: {e}")
            _note_error(con, src["source_id"], "Feed unreachable")
            counts["errors"] += 1
            continue
        except Exception as e:
            print(f"error {name}: bridge call failed ({e})")
            _note_error(con, src["source_id"], "Feed unreachable")
            counts["errors"] += 1
            continue
        con.execute("update source set last_polled=now(), last_error=null, "
                    "last_error_at=null where source_id=%s", (src["source_id"],))
        for item in (feed.get("items") or [])[:limit]:
            raw_url = item.get("url") or item.get("uid") or ""
            # one dedup key across the connectors: links.py stores the router's
            # normalized URL, so rss and bridge do too
            item_url = (router.normalize(raw_url) or raw_url) if raw_url else ""
            if item_url and con.execute(
                    "select 1 from event where meta->>'item_url' in (%s, %s) "
                    "limit 1", (item_url, raw_url)).fetchone():
                counts["duplicate"] += 1
                continue
            try:
                body = body_for(item, con)
            except Exception as e:
                print(f"error {name} '{item.get('title', '')}': {e}")
                counts["errors"] += 1
                continue
            title = item.get("title") or item_url or bridge_name
            published = rss.date_of(item.get("published") or "")
            thin = len(body) < THIN_CHARS
            doc = (f"# title: {title}\n# source_type: article\n"
                   f"# published: {published}\n# origin: {name}\n---\n{body}\n")
            meta = {"feed": name, "title": title, "item_url": item_url,
                    "bridge": bridge_name}
            if thin:
                meta["thin"] = True
            _, is_new = envelope.ingest(
                con, src["source_id"], "bridge", doc.encode("utf-8"),
                "text/plain", ".txt", published_at=published or None, meta=meta)
            counts["new" if is_new else "duplicate"] += 1
            if thin and is_new:
                counts["thin"] += 1
    print(f"bridge poll: {counts}")
    return counts
