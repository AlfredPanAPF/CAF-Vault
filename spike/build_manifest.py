#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""corpus/text/*.txt -> corpus/manifest.jsonl (one line per doc)."""
import json
from pathlib import Path

ROOT = Path(__file__).parent
TEXT = ROOT / "corpus" / "text"
MANIFEST = ROOT / "corpus" / "manifest.jsonl"


def main():
    rows = []
    for f in sorted(TEXT.glob("*.txt")):
        header, body = {}, []
        in_body = False
        for line in f.read_text(encoding="utf-8").splitlines():
            if in_body:
                body.append(line)
            elif line.strip() == "---":
                in_body = True
            elif line.startswith("# ") and ":" in line:
                k, v = line[2:].split(":", 1)
                header[k.strip()] = v.strip()
        rows.append({
            "doc_id": f.stem,
            "path": str(f),
            "source_type": header.get("source_type", "unknown"),
            "title": header.get("title", f.stem),
            "published": header.get("published", ""),
            "sector": header.get("sector", ""),
            "chars": sum(len(l) for l in body),
        })
    MANIFEST.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    by_type = {}
    for r in rows:
        by_type[r["source_type"]] = by_type.get(r["source_type"], 0) + 1
    print(f"{len(rows)} docs -> {MANIFEST}")
    print(by_type)


if __name__ == "__main__":
    main()
