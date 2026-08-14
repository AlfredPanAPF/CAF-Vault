"""Manual uploads: a single file (saved article HTML or plain text) -> one event.

HTML goes through the article-container heuristic from the spike; anything else
is treated as plain text and used as-is.
"""
from pathlib import Path

from bs4 import BeautifulSoup

from .. import db, envelope


def meta_tag(soup, **attrs):
    tag = soup.find("meta", attrs=attrs)
    return tag.get("content") if tag and tag.get("content") else None


def extract(html: str):
    """Article HTML -> (title, published, body)."""
    soup = BeautifulSoup(html, "lxml")
    title = meta_tag(soup, property="og:title") or (
        soup.title.get_text(strip=True) if soup.title else "untitled"
    )
    published = (
        meta_tag(soup, property="article:published_time")
        or meta_tag(soup, name="date")
        or ""
    )
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer",
                     "aside", "form", "svg"]):
        tag.decompose()

    # Prefer an <article> element; otherwise the container with the most <p> text.
    container = soup.find("article")
    if container is None:
        best, best_len = None, 0
        for parent in {p.parent for p in soup.find_all("p")}:
            length = sum(len(p.get_text(strip=True))
                         for p in parent.find_all("p", recursive=False))
            if length > best_len:
                best, best_len = parent, length
        container = best or soup

    paras = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    paras = [p for p in paras if len(p) > 40]
    return title, published, "\n\n".join(paras)


def ingest_file(con, path):
    """Returns the new event_id, or None when the content is an exact duplicate."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in (".htm", ".html"):
        title, published, body = extract(raw)
        if len(body) < 500:
            print(f"warn: thin extraction ({len(body)} chars) from {path.name}")
        doc = (f"# title: {title}\n# source_type: article\n"
               f"# published: {published[:10]}\n---\n{body}\n")
        published_at = published[:10] or None
    else:
        doc = f"# title: {path.stem}\n# source_type: document\n---\n{raw}\n"
        published_at = None
    source_id = db.get_or_create_source(con, "manual:uploads", "manual", is_internal=True)
    event_id, is_new = envelope.ingest(
        con, source_id, "manual", doc.encode("utf-8"), "text/plain", ".txt",
        published_at=published_at, meta={"filename": path.name})
    print(f"{path.name}: {'event ' + str(event_id) if is_new else 'duplicate'}")
    return event_id if is_new else None
