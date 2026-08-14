#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["beautifulsoup4", "lxml"]
# ///
"""Saved article HTML -> clean text docs.

Reads corpus/raw/articles/*.htm(l), writes corpus/text/article_<slug>.txt with a
small header block that build_manifest.py understands.
"""
import re
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
RAW = ROOT / "corpus" / "raw" / "articles"
OUT = ROOT / "corpus" / "text"


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:maxlen].rstrip("-")


def meta(soup, **attrs):
    tag = soup.find("meta", attrs=attrs)
    return tag.get("content") if tag and tag.get("content") else None


def extract(html: str):
    soup = BeautifulSoup(html, "lxml")
    title = meta(soup, property="og:title") or (
        soup.title.get_text(strip=True) if soup.title else "untitled"
    )
    published = (
        meta(soup, property="article:published_time")
        or meta(soup, name="date")
        or ""
    )
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"]):
        tag.decompose()

    # Prefer an <article> element; otherwise the container with the most <p> text.
    container = soup.find("article")
    if container is None:
        best, best_len = None, 0
        for parent in {p.parent for p in soup.find_all("p")}:
            length = sum(len(p.get_text(strip=True)) for p in parent.find_all("p", recursive=False))
            if length > best_len:
                best, best_len = parent, length
        container = best or soup

    paras = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    paras = [p for p in paras if len(p) > 40]
    return title, published, "\n\n".join(paras)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in sorted(RAW.glob("*.htm*")):
        title, published, body = extract(f.read_text(encoding="utf-8", errors="replace"))
        if len(body) < 500:
            print(f"WARN thin extraction ({len(body)} chars): {f.name}")
        out = OUT / f"article_{slugify(title)}.txt"
        out.write_text(
            f"# title: {title}\n# source_type: article\n# published: {published[:10]}\n"
            f"# origin: {f.name}\n---\n{body}\n",
            encoding="utf-8",
        )
        print(f"{out.name}: {len(body)} chars")


if __name__ == "__main__":
    main()
