"""Download the GLEIF LEI golden copy (Level 1) and load it into a local sqlite
lookup for ER blocking. Ported from spike/fetch_gleif.py into the package.

The golden copy covers ~2.5M legal entities worldwide: LEI, legal name, country,
status, plus alternate/transliterated names. Output is config.GLEIF_SQLITE with
normalized name keys (same normalization as pipeline/resolve.py). The download
is ~500MB; the zip is kept next to the sqlite so a rebuild skips the download.

`ensure()` is the boot hook: no-op when the sqlite already exists, otherwise
download + build. resolve.py degrades gracefully when the sqlite is missing,
so callers may treat any failure here as non-fatal.

Manual usage:
  python -m graph.gleif_fetch                  # download latest golden copy, build db
  python -m graph.gleif_fetch <lei2.csv.zip>   # build from an already-downloaded zip
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

from . import config
from .er_norm import norm, strip_suffix

META_URL = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
UA = {"User-Agent": config.USER_AGENT}


def _zip_path() -> Path:
    return config.GLEIF_SQLITE.with_name("gleif_lei2.csv.zip")


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
    zip_path = _zip_path()
    meta = requests.get(META_URL, headers=UA, timeout=60).json()
    url = find_csv_url(meta)
    if not url:
        raise RuntimeError(
            f"could not locate a lei2 csv zip in {META_URL} — download the "
            f"LEI-CDF golden copy manually from gleif.org and pass its path")
    print(f"gleif: downloading {url} (~500MB, one-off)")
    t0 = time.time()
    with requests.get(url, headers=UA, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(1 << 22):
                f.write(chunk)
                done += len(chunk)
                if total and done % (200 << 20) < (1 << 22):
                    print(f"  {done >> 20}MB / {total >> 20}MB")
    print(f"gleif: downloaded {zip_path.stat().st_size >> 20}MB in {time.time() - t0:.0f}s")
    return zip_path


def pick_col(fieldnames, *needles):
    for c in fieldnames:
        if all(n in c for n in needles):
            return c
    return None


def build(zip_path: Path):
    db_path = config.GLEIF_SQLITE
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.execute("pragma journal_mode=off")
    con.execute("pragma synchronous=off")
    con.execute("create table lei (lei text, name text, country text, status text, "
                "is_alt integer, n text, s text)")

    zf = zipfile.ZipFile(zip_path)
    member = next(m for m in zf.namelist() if m.endswith(".csv"))
    print(f"gleif: parsing {member}")
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
    print(f"gleif: parsed {n_ent} entities + {n_alt} alternate names in {time.time() - t0:.0f}s")
    print("gleif: indexing...")
    con.execute("create index idx_n on lei(n)")
    con.execute("create index idx_s on lei(s)")
    con.commit()
    con.close()
    print(f"gleif: done: {db_path} ({db_path.stat().st_size >> 20}MB)")


def _usable_zip(path: Path) -> bool:
    """False for partial downloads (interrupted fetch leaves a truncated file
    that zipfile rejects); the caller deletes and re-downloads."""
    if not path.exists():
        return False
    if zipfile.is_zipfile(path):
        return True
    print(f"gleif: {path} is not a valid zip (interrupted download?) — removing")
    path.unlink()
    return False


def ensure() -> Path:
    """Build config.GLEIF_SQLITE if missing; no-op when it already exists."""
    db_path = config.GLEIF_SQLITE
    if db_path.exists():
        return db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path = _zip_path()
    if _usable_zip(zip_path):
        print(f"gleif: using existing {zip_path} (delete it to force re-download)")
    else:
        zip_path = download()
    build(zip_path)
    return db_path


def main():
    if len(sys.argv) > 1:
        build(Path(sys.argv[1]))
    else:
        zip_path = _zip_path()
        if _usable_zip(zip_path):
            print(f"gleif: using existing {zip_path} (delete it to force re-download)")
        else:
            config.GLEIF_SQLITE.parent.mkdir(parents=True, exist_ok=True)
            zip_path = download()
        build(zip_path)


if __name__ == "__main__":
    main()
