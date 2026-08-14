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
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
CLAIMS = ROOT / "out" / "claims"
ER = ROOT / "out" / "er"

SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED",
    "PLC", "LLC", "LP", "SA", "NV", "AG", "SE", "HOLDINGS", "HOLDING", "GROUP",
}
FILER_COREF = {"THE COMPANY", "COMPANY", "THE CORPORATION", "THE REGISTRANT",
               "REGISTRANT", "THE FIRM"}


def norm(s: str) -> str:
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_suffix(s: str) -> str:
    toks = norm(s).split()
    while toks and toks[-1] in SUFFIXES:
        toks.pop()
    if toks and toks[0] == "THE":
        toks.pop(0)
    return " ".join(toks)


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
                hit(by_ticker[surface.upper()], "ticker"); continue
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
