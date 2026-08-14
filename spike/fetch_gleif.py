#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
"""Download the GLEIF LEI golden copy (Level 1) and load it into a local sqlite
lookup for ER blocking.

The golden copy covers ~2.5M legal entities worldwide: LEI, legal name, country,
status, plus alternate/transliterated names. Output is corpus/ref/gleif.sqlite
with normalized name keys (same normalization as resolve_block.py). The sqlite
file and the downloaded zip stay out of git; re-run to refresh.

Usage:
  uv run fetch_gleif.py                  # download latest golden copy, build db
  uv run fetch_gleif.py <lei2.csv.zip>   # build from an already-downloaded zip
"""
import csv
import io
import re
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

import requests

from er_norm import norm, strip_suffix

ROOT = Path(__file__).parent
REF = ROOT / "corpus" / "ref"
DB = REF / "gleif.sqlite"
ZIP = REF / "gleif_lei2.csv.zip"
META_URL = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
UA = {"User-Agent": "CAF-Vault research moptclaude@gmail.com"}


def find_csv_url(obj):
    """Find the lei2 full-file csv zip URL anywhere in the publish metadata."""
    urls = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and "lei2" in o and o.endswith(".csv.zip"):
            urls.append(o)

    walk(obj)
    full = [u for u in urls if "delta" not in u.lower()]
    return (full or urls or [None])[0]


def download() -> Path:
    meta = requests.get(META_URL, headers=UA, timeout=60).json()
    url = find_csv_url(meta)
    if not url:
        sys.exit(f"could not locate a lei2 csv zip in {META_URL} — "
                 f"download the LEI-CDF golden copy manually from gleif.org and pass its path")
    print(f"downloading {url}")
    t0 = time.time()
    with requests.get(url, headers=UA, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(ZIP, "wb") as f:
            for chunk in r.iter_content(1 << 22):
                f.write(chunk)
                done += len(chunk)
                if total and done % (200 << 20) < (1 << 22):
                    print(f"  {done >> 20}MB / {total >> 20}MB")
    print(f"downloaded {ZIP.stat().st_size >> 20}MB in {time.time() - t0:.0f}s")
    return ZIP


def pick_col(fieldnames, *needles):
    for c in fieldnames:
        if all(n in c for n in needles):
            return c
    return None


def build(zip_path: Path):
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    con.execute("pragma journal_mode=off")
    con.execute("pragma synchronous=off")
    con.execute("create table lei (lei text, name text, country text, status text, "
                "is_alt integer, n text, s text)")

    zf = zipfile.ZipFile(zip_path)
    member = next(m for m in zf.namelist() if m.endswith(".csv"))
    print(f"parsing {member}")
    t0 = time.time()
    with zf.open(member) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
        f = reader.fieldnames
        col_lei = "LEI" if "LEI" in f else pick_col(f, "LEI")
        col_name = pick_col(f, "Entity.LegalName") or pick_col(f, "LegalName")
        col_ctry = pick_col(f, "Entity.LegalAddress.Country") or pick_col(f, "LegalAddress", "Country")
        col_stat = pick_col(f, "Entity.EntityStatus") or pick_col(f, "EntityStatus")
        alt_cols = [c for c in f if re.search(r"OtherEntityNames?\.", c)
                    and not c.endswith((".xmllang", ".type"))]
        print(f"columns: lei={col_lei} name={col_name} country={col_ctry} "
              f"status={col_stat} alt_cols={len(alt_cols)}")

        batch, n_ent, n_alt = [], 0, 0
        for row in reader:
            lei, name = row[col_lei], row[col_name]
            if not lei or not name:
                continue
            country = row.get(col_ctry, "") if col_ctry else ""
            status = row.get(col_stat, "") if col_stat else ""
            batch.append((lei, name, country, status, 0, norm(name), strip_suffix(name)))
            n_ent += 1
            for c in alt_cols:
                v = row.get(c)
                if v:
                    batch.append((lei, v, country, status, 1, norm(v), strip_suffix(v)))
                    n_alt += 1
            if len(batch) >= 50_000:
                con.executemany("insert into lei values (?,?,?,?,?,?,?)", batch)
                batch.clear()
                if n_ent % 500_000 < 50_000:
                    print(f"  {n_ent} entities...")
        con.executemany("insert into lei values (?,?,?,?,?,?,?)", batch)
    print(f"parsed {n_ent} entities + {n_alt} alternate names in {time.time() - t0:.0f}s")
    print("indexing...")
    con.execute("create index idx_n on lei(n)")
    con.execute("create index idx_s on lei(s)")
    con.commit()
    con.close()
    print(f"done: {DB} ({DB.stat().st_size >> 20}MB)")


def main():
    zip_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if zip_path is None:
        zip_path = ZIP if ZIP.exists() else download()
        if ZIP.exists():
            print(f"using existing {ZIP} (delete it to force re-download)")
    build(zip_path)


if __name__ == "__main__":
    main()
