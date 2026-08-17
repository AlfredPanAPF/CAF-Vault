"""EDGAR connector: recent 8-Ks (primary doc + EX-99.* exhibits) as text events.

Exhibits are discovered from the filing index page's document *type* column
(EX-99.1 etc.), not filename patterns — filenames like `amd-ex991.htm` vs
`a2q26pressrelease.htm` are not reliable (spike finding).

Free official API, no key. SEC asks for a descriptive User-Agent and <=10 req/s.
"""
import json
import re
import time
import warnings

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from .. import config, db, envelope

PAUSE = 0.15  # stay well under SEC's 10 req/s
MAX_DOC_CHARS = 80_000


def get(url, as_json=False):
    time.sleep(PAUSE)
    r = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=30)
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


def poll(con, tickers=None, filings_per_company=2):
    counts = {"new": 0, "duplicate": 0, "skipped": 0, "errors": 0}
    watchlist = con.execute("select ticker, sector from watchlist where active").fetchall()
    sector_of = {r["ticker"]: r["sector"] for r in watchlist}
    registrants = json.loads(config.SEC_TICKERS.read_text())
    by_ticker = {v["ticker"]: v for v in registrants.values()}
    todo = list(tickers) if tickers else [r["ticker"] for r in watchlist]

    for ticker in todo:
        reg = by_ticker.get(ticker)
        if not reg:
            print(f"skip {ticker}: not in SEC registrant list")
            counts["skipped"] += 1
            continue
        cik = int(reg["cik_str"])
        subs_url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
        try:
            subs = get(subs_url, as_json=True)
        except Exception as e:
            print(f"error {ticker}: submissions fetch failed ({e})")
            counts["errors"] += 1
            continue
        source_id = db.get_or_create_source(con, f"edgar:{ticker}", "edgar", url=subs_url)
        recent = subs["filings"]["recent"]
        picks = [i for i, f in enumerate(recent["form"]) if f == "8-K"][:filings_per_company]
        for i in picks:
            acc = recent["accessionNumber"][i]
            if con.execute("select 1 from event where connector='edgar' "
                           "and meta->>'accession'=%s limit 1", (acc,)).fetchone():
                counts["duplicate"] += 1
                continue
            accn = acc.replace("-", "")
            fdate = recent["filingDate"][i]
            primary = recent["primaryDocument"][i]
            base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}"
            try:
                parts = [html_to_text(get(f"{base}/{primary}"))]
                for name in exhibit_docs(base, acc):
                    parts.append("---- EXHIBIT " + name + " ----\n\n"
                                 + html_to_text(get(f"{base}/{name}")))
            except Exception as e:
                print(f"error {ticker} {acc}: {e}")
                counts["errors"] += 1
                continue
            body = "\n\n".join(parts)[:MAX_DOC_CHARS]
            title = f"{reg['title']} 8-K filed {fdate}"
            doc = (f"# title: {title}\n# source_type: filing\n"
                   f"# published: {fdate}\n# ticker: {ticker}\n---\n{body}\n")
            _, is_new = envelope.ingest(
                con, source_id, "edgar", doc.encode("utf-8"), "text/plain", ".txt",
                published_at=fdate,
                meta={"ticker": ticker, "sector": sector_of.get(ticker),
                      "form": "8-K", "title": title, "origin": base, "accession": acc})
            counts["new" if is_new else "duplicate"] += 1
    print(f"edgar poll: {counts}")
    return counts
