#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Aggregate spike outputs into the numbers for the report, and seed the labeled
eval sets (eval/er_labels.jsonl, eval/extraction_labels.jsonl) from adjudication
and verification results."""
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "out"
EVAL = ROOT / "eval"


def jload(p):
    return json.loads(p.read_text())


def jsonl(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def main():
    # ---------------- corpus + claims by source type
    manifest = {r["doc_id"]: r for r in jsonl(ROOT / "corpus/manifest.jsonl")}
    per_type = defaultdict(lambda: {"docs": 0, "claims": 0, "mentions": 0, "chars": 0})
    preds = Counter()
    stances = Counter()
    literals = 0
    total_claims = 0
    for f in sorted((OUT / "claims").glob("*.json")):
        d = jload(f)
        st = manifest.get(f.stem, {}).get("source_type", "?")
        per_type[st]["docs"] += 1
        per_type[st]["claims"] += len(d.get("claims", []))
        per_type[st]["mentions"] += len(d.get("mentions", []))
        per_type[st]["chars"] += manifest.get(f.stem, {}).get("chars", 0)
        for c in d.get("claims", []):
            total_claims += 1
            preds[c.get("predicate", "?")] += 1
            stances[(c.get("qualifiers") or {}).get("stance", "unset")] += 1
            obj = c.get("object") or {}
            if "literal" in obj:
                literals += 1

    print("== corpus / extraction ==")
    for st, v in sorted(per_type.items()):
        print(f"{st:8s} docs={v['docs']:3d} claims={v['claims']:5d} "
              f"claims/doc={v['claims']/max(v['docs'],1):5.1f} "
              f"claims/10kchars={10000*v['claims']/max(v['chars'],1):5.1f}")
    print(f"total claims={total_claims}  distinct predicates={len(preds)} "
          f"(used once: {sum(1 for v in preds.values() if v==1)})")
    print(f"stances: {dict(stances)}")
    print(f"literal objects: {literals} ({literals/max(total_claims,1):.0%})")

    # ---------------- tier comparison (claim counts)
    print("\n== tier comparison: claims per doc ==")
    rows = []
    bad_haiku = []
    for f in sorted((OUT / "claims_haiku").glob("*.json")):
        s = OUT / "claims" / f.name
        if not s.exists():
            continue
        try:
            nh = len(jload(f).get("claims", []))
        except Exception:
            bad_haiku.append(f.stem)
            continue
        rows.append((f.stem, len(jload(s).get("claims", [])), nh))
    if bad_haiku:
        print(f"MALFORMED haiku output ({len(bad_haiku)}): {bad_haiku}")
    for doc, ns, nh in rows:
        print(f"{doc[:58]:58s} sonnet={ns:3d} haiku={nh:3d}")
    if rows:
        ts, th = sum(r[1] for r in rows), sum(r[2] for r in rows)
        print(f"TOTAL sonnet={ts} haiku={th} (haiku yield = {th/max(ts,1):.0%} of sonnet)")

    # ---------------- verification
    print("\n== verification (faithfulness) ==")
    tiers = defaultdict(Counter)
    issue_counts = defaultdict(Counter)
    ext_labels = []
    for f in sorted((OUT / "report/verify").glob("*.json")):
        doc_id, tier = f.stem.rsplit("__", 1)
        try:
            verdicts = jload(f)
        except Exception as e:
            print(f"BAD verify file {f.name}: {e}")
            continue
        for v in verdicts:
            tiers[tier][v.get("verdict", "?")] += 1
            for i in v.get("issues", []):
                issue_counts[tier][i] += 1
            ext_labels.append({"doc_id": doc_id, "tier": tier, **v})
    for tier, c in sorted(tiers.items()):
        n = sum(c.values())
        sup = c.get("supported", 0)
        strict = sup / n if n else 0
        lenient = (sup + c.get("not_material", 0)) / n if n else 0
        print(f"{tier:7s} n={n:4d} supported={sup} distorted={c.get('distorted',0)} "
              f"unsupported={c.get('unsupported',0)} not_material={c.get('not_material',0)} "
              f"| precision strict={strict:.2f} lenient={lenient:.2f}")
        print(f"        issues: {dict(issue_counts[tier].most_common(6))}")

    # ---------------- ER
    print("\n== entity resolution ==")
    auto = jsonl(OUT / "er/auto_resolved.jsonl")
    adj = []
    for f in sorted((OUT / "er/adj").glob("*.json")):
        try:
            adj.extend(jload(f))
        except Exception as e:
            print(f"BAD adj file {f.name}: {e}")
    dec = Counter(a.get("decision", "?") for a in adj)
    n_surf = len(auto) + len(adj)
    print(f"surfaces={n_surf} auto_resolved={len(auto)} adjudicated={len(adj)} -> {dict(dec)}")
    resolved_total = len(auto) + dec.get("match", 0)
    real_es = n_surf - dec.get("not_a_company", 0)
    print(f"resolved to SEC registrant: {resolved_total} | new entities (foreign/private): "
          f"{dec.get('new_entity',0)} | not-a-company: {dec.get('not_a_company',0)} | "
          f"still ambiguous: {dec.get('ambiguous',0)}")
    print(f"of real entities, resolved or identified: "
          f"{(resolved_total + dec.get('new_entity',0))/max(real_es,1):.0%}")

    # ---------------- labeled-set seeds
    EVAL.mkdir(exist_ok=True)
    er_labels = [{"surface": a.get("surface"), "decision": a.get("decision"),
                  "cik": a.get("cik"), "entity_hint": a.get("entity_hint"),
                  "reasoning": a.get("reasoning"), "confidence": a.get("confidence"),
                  "labeled_by": "agent", "human_confirmed": None} for a in adj]
    er_labels += [{"surface": r["surface"], "decision": "match", "cik": r["match"]["cik"],
                   "tier": r["tier"], "labeled_by": "blocking", "human_confirmed": None}
                  for r in auto]
    (EVAL / "er_labels.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in er_labels), encoding="utf-8")
    (EVAL / "extraction_labels.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in ext_labels), encoding="utf-8")
    print(f"\neval seeds: {len(er_labels)} ER labels, {len(ext_labels)} extraction labels "
          f"(human_confirmed=null until reviewed)")


if __name__ == "__main__":
    main()
