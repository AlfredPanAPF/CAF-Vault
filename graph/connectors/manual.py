"""Manual uploads: a single file (saved article HTML or plain text) -> one event.

HTML goes through the article-container heuristic from the spike; anything else
is treated as plain text and used as-is. `extract_article` (build spec v4 §3.3)
adds two better paths in front of that heuristic — the page's own JSON-LD
`articleBody`, then per-site body selectors — and is what the rss, bridge and
link connectors use for article pages.
"""
import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .. import db, envelope

# Body selectors per site, by host suffix (spec v4 §3.3 step 2). Tried for the
# matching host first, then for every site — Substack custom domains and
# syndicated FT/WSJ pages keep the markup but not the hostname.
ARTICLE_SELECTORS = {
    "ft.com": ".article__content-body, #article-body",
    "wsj.com": 'section[data-type="article-body"], .article-content, '
               '[class*="ArticleBody"]',
    "substack.com": ".body.markup, .available-content",
}
ARTICLE_TYPES = ("NewsArticle", "Article", "BlogPosting", "Report")
PARA_FLOOR = 40   # shorter <p> blocks are navigation and captions, not prose


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
    paras = [p for p in paras if len(p) > PARA_FLOOR]
    return title, published, "\n\n".join(paras)


# ------------------------------------------------------- spec v4 §3.3


def html_to_text(html: str) -> str:
    """An HTML fragment (feed content, a body container) -> plain paragraphs."""
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    paras = [p for p in paras if p]
    if not paras:
        paras = [line.strip() for line in soup.get_text("\n").split("\n")
                 if line.strip()]
    return "\n\n".join(paras)


def _clean(raw: str) -> str:
    """JSON-LD article bodies arrive as text, as escaped HTML, or as both."""
    text = unescape(raw or "")
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "lxml").get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _ld_nodes(data):
    """Every dict in a JSON-LD document, including the ones nested under
    @graph and mainEntity (publishers use both)."""
    if isinstance(data, list):
        for item in data:
            yield from _ld_nodes(item)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "mainEntity", "mainEntityOfPage"):
            if key in data:
                yield from _ld_nodes(data[key])


def _is_article(node: dict) -> bool:
    types = node.get("@type") or ""
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and (t in ARTICLE_TYPES or t.endswith("Article"))
               for t in types)


def jsonld_article(soup) -> dict | None:
    """The page's own JSON-LD article node with an articleBody, or None."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (ValueError, TypeError):
            continue
        for node in _ld_nodes(data):
            if not isinstance(node, dict) or not _is_article(node):
                continue
            body = node.get("articleBody")
            if isinstance(body, list):
                body = "\n\n".join(str(b) for b in body)
            if isinstance(body, str) and body.strip():
                return {"title": node.get("headline") or "",
                        "published": str(node.get("datePublished") or ""),
                        "body": _clean(body)}
    return None


def _paragraphs(container) -> str:
    paras = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    paras = [p for p in paras if len(p) > PARA_FLOOR]
    if paras:
        return "\n\n".join(paras)
    return _clean(container.get_text("\n"))


def _selector_body(soup, url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    ordered = [sel for site, sel in ARTICLE_SELECTORS.items()
               if host == site or host.endswith("." + site)]
    ordered += [sel for sel in ARTICLE_SELECTORS.values() if sel not in ordered]
    for selector in ordered:
        for container in soup.select(selector):
            body = _paragraphs(container)
            if len(body) >= 200:
                return body
    return ""


def extract_article(html: str, url: str = ""):
    """Article HTML -> (title, published, body), spec v4 §3.3: JSON-LD
    articleBody, then the site's body selectors, then the `extract` heuristic."""
    soup = BeautifulSoup(html or "", "lxml")
    title = meta_tag(soup, property="og:title") or (
        soup.title.get_text(strip=True) if soup.title else "untitled")
    published = (
        meta_tag(soup, property="article:published_time")
        or meta_tag(soup, name="date")
        or ""
    )

    ld = jsonld_article(soup)
    if ld:
        return (ld["title"] or title, ld["published"] or published, ld["body"])

    for tag in soup(["script", "style", "noscript", "nav", "header", "footer",
                     "aside", "form", "svg"]):
        tag.decompose()
    body = _selector_body(soup, url)
    if body:
        return title, published, body

    return extract(html)


def ingest_bytes(con, filename, data: bytes):
    """Returns the new event_id, or None when the content is an exact duplicate."""
    name = Path(filename).name
    raw = data.decode("utf-8", errors="replace")
    if Path(name).suffix.lower() in (".htm", ".html"):
        title, published, body = extract_article(raw)
        if len(body) < 500:
            print(f"warn: thin extraction ({len(body)} chars) from {name}")
        doc = (f"# title: {title}\n# source_type: article\n"
               f"# published: {published[:10]}\n---\n{body}\n")
        published_at = published[:10] or None
    else:
        doc = f"# title: {Path(name).stem}\n# source_type: document\n---\n{raw}\n"
        published_at = None
    source_id = db.get_or_create_source(con, "manual:uploads", "manual", is_internal=True)
    event_id, is_new = envelope.ingest(
        con, source_id, "manual", doc.encode("utf-8"), "text/plain", ".txt",
        published_at=published_at, meta={"filename": name})
    print(f"{name}: {'event ' + str(event_id) if is_new else 'duplicate'}")
    return event_id if is_new else None


def ingest_file(con, path):
    """Returns the new event_id, or None when the content is an exact duplicate."""
    path = Path(path)
    return ingest_bytes(con, path.name, path.read_bytes())
