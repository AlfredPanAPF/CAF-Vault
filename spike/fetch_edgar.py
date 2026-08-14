#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["beautifulsoup4", "lxml", "requests"]
# ///
"""EDGAR: for each watchlist ticker, fetch the two most recent 8-Ks (primary doc +
EX-99 press-release exhibits) and write them as clean text docs.

Free official API, no key. SEC asks for a descriptive User-Agent and <=10 req/s.
"""
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


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    watchlist = json.loads((ROOT / "corpus/ref/watchlist.json").read_text())
    registrants = json.loads((ROOT / "corpus/ref/company_tickers.json").read_text())
    by_ticker = {v["ticker"]: v for v in registrants.values()}

    for sector, tickers in watchlist.items():
        for ticker in tickers:
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
                try:
                    parts = [html_to_text(get(f"{base}/{primary}"))]
                    idx = get(f"{base}/index.json", as_json=True)
                    exhibits = [
                        it["name"] for it in idx["directory"]["item"]
                        if re.search(r"ex[-_]?99", it["name"], re.I)
                        and it["name"].lower().endswith((".htm", ".html"))
                    ][:2]
                    for name in exhibits:
                        parts.append("---- EXHIBIT " + name + " ----\n\n" + html_to_text(get(f"{base}/{name}")))
                except Exception as e:
                    print(f"SKIP {ticker} {acc}: {e}")
                    continue
                body = "\n\n".join(parts)[:MAX_DOC_CHARS]
                out = OUT / f"filing_{ticker}_8K_{fdate}_{accn[-6:]}.txt"
                out.write_text(
                    f"# title: {reg['title']} 8-K filed {fdate}\n# source_type: filing\n"
                    f"# published: {fdate}\n# origin: {base}\n# sector: {sector}\n# ticker: {ticker}\n"
                    f"---\n{body}\n",
                    encoding="utf-8",
                )
                print(f"{out.name}: {len(body)} chars, {len(parts)-1} exhibits")


if __name__ == "__main__":
    main()
