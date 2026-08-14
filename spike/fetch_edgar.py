#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["beautifulsoup4", "lxml", "requests"]
# ///
"""EDGAR: for each watchlist ticker, fetch the two most recent 8-Ks (primary doc +
EX-99.* exhibits) and write them as clean text docs.

Exhibits are discovered from the filing index page's document *type* column
(EX-99.1 etc.), not filename patterns — filenames like `amd-ex991.htm` vs
`a2q26pressrelease.htm` are not reliable (spike finding).

Free official API, no key. SEC asks for a descriptive User-Agent and <=10 req/s.
"""
import argparse
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
OUT = ROOT / "corpus" / "text"
UA = {"User-Agent": "CAF-Vault research moptclaude@gmail.com"}
PAUSE = 0.15  # stay well under SEC's 10 req/s
FILINGS_PER_COMPANY = 2
MAX_DOC_CHARS = 80_000


def get(url, as_json=False):
    time.sleep(PAUSE)
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json() if as_json else r.text


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def exhibit_docs(base: str, acc: str) -> list[str]:
    """Names of EX-99.* documents, from the filing index page's Type column."""
    names = []
    try:
        soup = BeautifulSoup(get(f"{base}/{acc}-index.htm"), "lxml")
        for tr in soup.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) >= 4 and any(c.upper().startswith("EX-99") for c in cells):
                a = tr.find("a", href=True)
                if not a:
                    continue
                href = a["href"]
                if "ix?doc=" in href:  # iXBRL viewer wrapper
                    href = href.split("doc=")[-1]
                name = href.split("/")[-1]
                if name.lower().endswith((".htm", ".html")):
                    names.append(name)
    except Exception as e:
        print(f"  index page parse failed ({e}); falling back to filename match")
    if not names:  # fallback: old filename heuristic
        try:
            idx = get(f"{base}/index.json", as_json=True)
            names = [it["name"] for it in idx["directory"]["item"]
                     if re.search(r"ex[-_]?99", it["name"], re.I)
                     and it["name"].lower().endswith((".htm", ".html"))]
        except Exception:
            pass
    return names[:3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="single ticker to fetch")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be fetched, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing corpus files (default: skip)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    watchlist = json.loads((ROOT / "corpus/ref/watchlist.json").read_text())
    registrants = json.loads((ROOT / "corpus/ref/company_tickers.json").read_text())
    by_ticker = {v["ticker"]: v for v in registrants.values()}

    for sector, tickers in watchlist.items():
        for ticker in tickers:
            if args.only and ticker != args.only:
                continue
            reg = by_ticker.get(ticker)
            if not reg:
                print(f"SKIP {ticker}: not in SEC registrant list")
                continue
            cik = int(reg["cik_str"])
            try:
                subs = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", as_json=True)
            except Exception as e:
                print(f"SKIP {ticker}: submissions fetch failed ({e})")
                continue
            recent = subs["filings"]["recent"]
            picks = [i for i, f in enumerate(recent["form"]) if f == "8-K"][:FILINGS_PER_COMPANY]
            for i in picks:
                acc = recent["accessionNumber"][i]
                accn = acc.replace("-", "")
                fdate = recent["filingDate"][i]
                primary = recent["primaryDocument"][i]
                base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}"
                out = OUT / f"filing_{ticker}_8K_{fdate}_{accn[-6:]}.txt"
                exhibits = exhibit_docs(base, acc)
                if args.dry_run:
                    print(f"{ticker} {acc} ({fdate}): primary={primary} exhibits={exhibits}")
                    continue
                if out.exists() and not args.force:
                    print(f"skip (exists): {out.name}")
                    continue
                try:
                    parts = [html_to_text(get(f"{base}/{primary}"))]
                    for name in exhibits:
                        parts.append("---- EXHIBIT " + name + " ----\n\n"
                                     + html_to_text(get(f"{base}/{name}")))
                except Exception as e:
                    print(f"SKIP {ticker} {acc}: {e}")
                    continue
                body = "\n\n".join(parts)[:MAX_DOC_CHARS]
                out.write_text(
                    f"# title: {reg['title']} 8-K filed {fdate}\n# source_type: filing\n"
                    f"# published: {fdate}\n# origin: {base}\n# sector: {sector}\n# ticker: {ticker}\n"
                    f"---\n{body}\n",
                    encoding="utf-8",
                )
                print(f"{out.name}: {len(body)} chars, {len(parts)-1} exhibits")


if __name__ == "__main__":
    main()
