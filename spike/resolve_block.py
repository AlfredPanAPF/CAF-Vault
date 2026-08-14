#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""ER blocking pass (design §6, stage 1).

Aggregates company/security mentions from out/claims/*.json and matches them
against the SEC registrant list. Output:
  out/er/auto_resolved.jsonl   confident matches (exact ticker / exact name)
  out/er/ambiguous.jsonl       has candidates, needs adjudication
  out/er/unresolved.jsonl      no candidates (foreign, private, or not a company)
"""
import difflib
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
CLAIMS = ROOT / "out" / "claims"
ER = ROOT / "out" / "er"

SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED",
    "PLC", "LLC", "LP", "SA", "NV", "AG", "SE", "HOLDINGS", "HOLDING", "GROUP",
}


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


def main():
    ER.mkdir(parents=True, exist_ok=True)
    registrants = json.loads((ROOT / "corpus/ref/company_tickers.json").read_text())
    by_ticker, by_name, by_stripped = {}, {}, defaultdict(list)
    for v in registrants.values():
        rec = {"cik": int(v["cik_str"]), "ticker": v["ticker"], "title": v["title"]}
        by_ticker.setdefault(v["ticker"].upper(), rec)
        by_name.setdefault(norm(v["title"]), rec)
        by_stripped[strip_suffix(v["title"])].append(rec)

    # aggregate surfaces across all extraction output
    surfaces = defaultdict(lambda: {"count": 0, "docs": []})
    for f in sorted(CLAIMS.glob("*.json")):
        data = json.loads(f.read_text())
        seen_here = set()
        for m in data.get("mentions", []):
            if m.get("type") not in ("company", "security"):
                continue
            s = m["surface"].strip()
            surfaces[s]["count"] += m.get("count", 1)
            if f.stem not in seen_here:
                surfaces[s]["docs"].append(f.stem)
                seen_here.add(f.stem)

    stripped_keys = list(by_stripped.keys())
    resolved, ambiguous, unresolved = [], [], []
    for surface, info in sorted(surfaces.items(), key=lambda kv: -kv[1]["count"]):
        row = {"surface": surface, **info}
        up, ns, ss = surface.upper().strip(), norm(surface), strip_suffix(surface)
        if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", surface.strip()) and up in by_ticker:
            resolved.append({**row, "match": by_ticker[up], "tier": "ticker"})
        elif ns in by_name:
            resolved.append({**row, "match": by_name[ns], "tier": "name_exact"})
        elif ss and ss in by_stripped and len(by_stripped[ss]) == 1:
            resolved.append({**row, "match": by_stripped[ss][0], "tier": "name_stripped"})
        else:
            cands = []
            if ss:
                if ss in by_stripped:
                    cands = list(by_stripped[ss])
                else:
                    for key in difflib.get_close_matches(ss, stripped_keys, n=5, cutoff=0.82):
                        cands.extend(by_stripped[key])
                    # single-token surfaces ("Nvidia") vs multi-token registrants
                    if not cands and " " not in ss and len(ss) >= 4:
                        cands = [r for k, rs in by_stripped.items()
                                 if k.startswith(ss + " ") or k == ss for r in rs][:5]
            if cands:
                ambiguous.append({**row, "candidates": cands})
            else:
                unresolved.append(row)

    for name, rows in [("auto_resolved", resolved), ("ambiguous", ambiguous), ("unresolved", unresolved)]:
        (ER / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    total = len(surfaces)
    print(f"surfaces: {total} | auto-resolved: {len(resolved)} ({len(resolved)/max(total,1):.0%}) "
          f"| ambiguous: {len(ambiguous)} | unresolved: {len(unresolved)}")


if __name__ == "__main__":
    main()
