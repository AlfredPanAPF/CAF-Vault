# Spike — extraction + ER on real documents

Throwaway harness (phase 0). Goal: measure claim-extraction quality and
entity-resolution ambiguity on ~100 real documents before building the pipeline.
Nothing here is production code; the learnings and the labeled samples are the output.

## Corpus

Three sectors: Tech & AI, Energy, F&B (watchlist in `corpus/ref/watchlist.json`).

- **Filings** — EDGAR 8-Ks + press-release exhibits for the watchlist (free API)
- **Articles** — hand-downloaded premium articles in `corpus/raw/articles/`
- **Podcasts** — Unhedged (FT) and The AI Daily Brief, transcribed locally with mlx-whisper

## Run order

```
uv run parse_articles.py          # raw/articles/*.html -> text/
uv run fetch_edgar.py             # EDGAR 8-K + exhibits -> text/
uv run fetch_podcasts.py -n 3     # download + transcribe episodes -> text/
uv run fetch_gleif.py             # GLEIF golden copy -> corpus/ref/gleif.sqlite (~730MB)
uv run build_manifest.py          # text/*.txt -> corpus/manifest.jsonl
# extraction: agents apply prompts/extraction.md to each manifest doc,
#             writing out/claims/<doc_id>.json
uv run resolve_block.py           # ER blocking vs SEC registrants -> out/er/
# adjudication + verification: agents apply prompts/er_adjudication.md and
#             prompts/claim_verification.md; results land in out/er/ and out/report/
uv run analyze.py                 # aggregate everything, seed eval/ labeled sets
```

Results of the 2026-08-14 run: `out/report/REPORT.md`. Labeled-set seeds awaiting
human confirmation: `eval/er_labels.jsonl`, `eval/extraction_labels.jsonl`.

Text docs use a small header block (`# title:`, `# source_type:`, `# published:`,
`---`) that `build_manifest.py` reads.

## Outputs

- `out/claims/` — one JSON per doc: mentions + extracted claims
- `out/er/` — blocking results (auto-resolved / ambiguous / unresolved) + adjudications
- `out/report/` — spike report, tier comparison, seeded labeled sets
