#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""ER blocking, per mention (design §6 stage 1; rewritten after the spike's
per-surface run proved global resolution wrong — "Constellation" means different
registrants in different documents).

Resolution runs per (doc, surface) with document context, in order:
  1. filer coreference — "the Company"/"the Registrant" in a filing -> the filer
  2. defined terms from extraction output ("CBI" -> "Constellation Brands, Inc.")
  3. seed alias table (corpus/ref/aliases.json)
  4. exact ticker
  5. exact registrant name / suffix-stripped unique name
  6. filer initials — an acronym matching the filer's name initials in its own filing
  7. fuzzy candidates; if the filer is among them, prefer the filer

Outputs (out/er/): mention_resolved.jsonl, mention_ambiguous.jsonl,
mention_unresolved.jsonl, plus a per-surface rollup printed for comparison with
the first run.
"""
import difflib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from er_norm import norm, strip_suffix

ROOT = Path(__file__).parent
CLAIMS = ROOT / "out" / "claims"
ER = ROOT / "out" / "er"
GLEIF_DB = ROOT / "corpus" / "ref" / "gleif.sqlite"

FILER_COREF = {"THE COMPANY", "COMPANY", "THE CORPORATION", "THE REGISTRANT",
               "REGISTRANT", "THE FIRM"}


def initials(name_norm: str) -> str:
    return "".join(w[0] for w in name_norm.split())


def main():
    ER.mkdir(parents=True, exist_ok=True)
    registrants = json.loads((ROOT / "corpus/ref/company_tickers.json").read_text())
    by_ticker, by_name, by_stripped = {}, {}, defaultdict(list)
    for v in registrants.values():
        rec = {"cik": int(v["cik_str"]), "ticker": v["ticker"], "title": v["title"]}
        by_ticker.setdefault(v["ticker"].upper(), rec)
        by_name.setdefault(norm(v["title"]), rec)
        by_stripped[strip_suffix(v["title"])].append(rec)
    stripped_keys = list(by_stripped.keys())

    aliases = {}
    for k, v in json.loads((ROOT / "corpus/ref/aliases.json").read_text()).items():
        if not k.startswith("_") and v.upper() in by_ticker:
            aliases[norm(k)] = by_ticker[v.upper()]

    manifest = {r["doc_id"]: r for r in
                (json.loads(l) for l in (ROOT / "corpus/manifest.jsonl").read_text().splitlines() if l.strip())}

    _doc_cache = {}

    def doc_text(doc_id):
        if doc_id not in _doc_cache:
            p = ROOT / "corpus" / "text" / f"{doc_id}.txt"
            _doc_cache[doc_id] = (p.read_text(encoding="utf-8", errors="replace").lower()
                                  if p.exists() else "")
        return _doc_cache[doc_id]

    def name_lookup(surface):
        """exact/stripped-unique name match -> (rec, tier) or (None, None)"""
        ns, ss = norm(surface), strip_suffix(surface)
        if ns in by_name:
            return by_name[ns], "name_exact"
        if ss and ss in by_stripped and len(by_stripped[ss]) == 1:
            return by_stripped[ss][0], "name_stripped"
        return None, None

    def candidates_for(surface):
        ss = strip_suffix(surface)
        if not ss:
            return []
        cands = []
        if ss in by_stripped:
            cands = list(by_stripped[ss])
        else:
            for key in difflib.get_close_matches(ss, stripped_keys, n=5, cutoff=0.82):
                cands.extend(by_stripped[key])
            if not cands and " " not in ss and len(ss) >= 4:
                cands = [r for k, rs in by_stripped.items()
                         if k.startswith(ss + " ") or k == ss for r in rs][:5]
        return cands

    # GLEIF global registry (2.5M entities) — exact and stripped-name lookups,
    # plus a prefix probe for bare short forms ("Vestas" -> VESTAS WIND SYSTEMS A/S)
    gleif = sqlite3.connect(GLEIF_DB) if GLEIF_DB.exists() else None
    if gleif is None:
        print("NOTE: corpus/ref/gleif.sqlite missing — run fetch_gleif.py; "
              "skipping GLEIF tiers")

    def gleif_rec(row):
        return {"lei": row[0], "title": row[1], "country": row[2],
                "status": row[3], "registry": "gleif"}

    def gleif_lookup(surface):
        """-> (rec, tier) | ('candidates', rows) | None

        Precision-first: a single-token surface matched by stripped/prefix name
        never auto-resolves ("Cursor" is also a Spanish company's legal name);
        it goes to adjudication as candidates. Only exact full legal-name
        matches and multi-token stripped matches resolve directly."""
        if gleif is None:
            return None
        ns, ss = norm(surface), strip_suffix(surface)
        single_token = " " not in ss
        probes = [("n", ns, "gleif_exact"), ("s", ss, "gleif_stripped")]
        for col, val, tier in probes:
            if not val:
                continue
            rows = gleif.execute(
                f"select distinct lei,name,country,status from lei where {col}=? limit 12",
                (val,)).fetchall()
            active = [r for r in rows if r[3] == "ACTIVE"] or rows
            leis = {r[0] for r in active}
            if len(leis) == 1:
                if tier == "gleif_stripped" and single_token:
                    return "candidates", [gleif_rec(active[0])]
                return gleif_rec(active[0]), tier
            if 1 < len(leis) <= 5:
                return "candidates", [gleif_rec(r) for r in active[:5]]
        if ss and len(ss) >= 4:
            rows = gleif.execute(
                "select distinct lei,name,country,status from lei where s like ? limit 6",
                (ss + " %",)).fetchall()
            active = [r for r in rows if r[3] == "ACTIVE"] or rows
            leis = {r[0] for r in active}
            if len(leis) == 1 and not single_token:
                return gleif_rec(active[0]), "gleif_prefix"
            if 1 <= len(leis) <= 5:
                return "candidates", [gleif_rec(r) for r in active[:5]]
        return None

    resolved, ambiguous, unresolved = [], [], []
    tiers = Counter()
    for f in sorted(CLAIMS.glob("*.json")):
        data = json.loads(f.read_text())
        doc = manifest.get(f.stem, {})
        filer = by_ticker.get((doc.get("ticker") or "").upper())
        defined = {norm(k): v for k, v in (data.get("defined_terms") or {}).items()}

        for m in data.get("mentions", []):
            if m.get("type") not in ("company", "security"):
                continue
            surface = m["surface"].strip()
            row = {"doc_id": f.stem, "surface": surface, "count": m.get("count", 1)}
            ns = norm(surface)

            def hit(rec, tier):
                tiers[tier] += 1
                resolved.append({**row, "match": rec, "tier": tier})

            if filer and ns in FILER_COREF:
                hit(filer, "filer_coref"); continue
            if ns in defined:
                rec, _ = name_lookup(defined[ns])
                if rec:
                    hit(rec, "defined_term"); continue
                if filer and strip_suffix(defined[ns]) == strip_suffix(filer["title"]):
                    hit(filer, "defined_term"); continue
            if ns in aliases:
                hit(aliases[ns], "alias"); continue
            if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", surface) and surface.upper() in by_ticker:
                # ticker strings collide across markets ("BYD" is Boyd Gaming's
                # NYSE ticker and a Chinese carmaker) — require the registrant's
                # name to actually appear in the document, else adjudicate
                rec = by_ticker[surface.upper()]
                name_tok = strip_suffix(rec["title"]).split()[0].lower() if strip_suffix(rec["title"]) else ""
                if name_tok and name_tok in doc_text(f.stem):
                    hit(rec, "ticker"); continue
                g = gleif_lookup(surface)
                extra = g[1] if g and g[0] == "candidates" else ([g[0]] if g else [])
                ambiguous.append({**row, "candidates": [rec] + extra})
                continue
            rec, tier = name_lookup(surface)
            if rec:
                hit(rec, tier); continue
            if (filer and re.fullmatch(r"[A-Z]{2,5}", surface)
                    and surface in (initials(norm(filer["title"])),
                                    initials(strip_suffix(filer["title"])))):
                hit(filer, "filer_initials"); continue
            cands = candidates_for(surface)
            if filer and any(c["cik"] == filer["cik"] for c in cands):
                hit(filer, "filer_context"); continue
            g = gleif_lookup(surface)
            if g and g[0] != "candidates":
                rec, tier = g
                # exact legal-name match trumps SEC fuzzy guesses; a stripped/prefix
                # match only auto-resolves when SEC offered nothing at all
                if not cands or tier == "gleif_exact":
                    hit(rec, tier); continue
                ambiguous.append({**row, "candidates": cands + [rec]})
                continue
            if g:
                cands = cands + g[1]
            if cands:
                ambiguous.append({**row, "candidates": cands})
            else:
                unresolved.append(row)

    for name, rows in [("mention_resolved", resolved), ("mention_ambiguous", ambiguous),
                       ("mention_unresolved", unresolved)]:
        (ER / f"{name}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    total = len(resolved) + len(ambiguous) + len(unresolved)
    print(f"mentions: {total} | resolved: {len(resolved)} ({len(resolved)/max(total,1):.0%}) "
          f"| ambiguous: {len(ambiguous)} | unresolved: {len(unresolved)}")
    print(f"tiers: {dict(tiers.most_common())}")

    surf = defaultdict(set)
    for r in resolved:
        surf[r["surface"]].add("resolved")
    for r in ambiguous:
        surf[r["surface"]].add("ambiguous")
    for r in unresolved:
        surf[r["surface"]].add("unresolved")
    fully = sum(1 for v in surf.values() if v == {"resolved"})
    split = {s: v for s, v in surf.items() if len(v) > 1}
    print(f"surfaces: {len(surf)} | fully resolved: {fully} ({fully/max(len(surf),1):.0%}) "
          f"| mixed outcome across docs: {len(split)}")
    for s in list(split)[:8]:
        print(f"  mixed: {s} -> {sorted(split[s])}")


if __name__ == "__main__":
    main()
