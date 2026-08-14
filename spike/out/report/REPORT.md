# Spike report — extraction + ER on 82 real documents

2026-08-14. Phase-0 spike per docs/company-graph-design.md §12. Everything below ran
end to end today: corpus assembly, two-tier LLM extraction, ER blocking + agent
adjudication, and an adversarial verification pass. ~6.5M agent tokens, ~1.5h wall
time, zero failed agents.

## Corpus

| Source | Docs | How |
|---|---|---|
| EDGAR 8-Ks + press-release exhibits | 71 | free official API, 36-company watchlist across Tech & AI / Energy / F&B |
| Premium articles (hand-downloaded) | 5 | parsed from saved HTML |
| Podcasts (Unhedged, AI Daily Brief) | 6 | RSS → mp3 → mlx-whisper large-v3-turbo, no diarization |

## Extraction results

1,932 claims, 1,902 mention entries, all with verbatim evidence quotes.

| Source | Docs | Claims | Claims/doc | Claims/10k chars |
|---|---|---|---|---|
| article | 5 | 98 | 19.6 | 62.6 |
| filing | 71 | 1,446 | 20.4 | 14.4 |
| podcast | 6 | 388 | 64.7 | 22.6 |

Stance: 84% stated, 12% reported, 4% speculative. 38% of claims carry a
numeric/date literal object.

**Predicate vocabulary: 1,065 distinct predicates, 77% used exactly once.**
This is the strongest empirical validation of the design in the whole spike: an open
vocabulary turns into synonym soup immediately (`elected_director`,
`elected_as_director`, `serves_as`, `holds_role`, `held_role_at`, `holds_title`,
`held_title` all coexist). The gardener (§5.3) is not optional.

## Faithfulness (agent-verified, strict audit vs source text)

15-doc sample, both tiers, every claim audited:

| Tier | Claims judged | Supported | Distorted | Unsupported | Precision |
|---|---|---|---|---|---|
| sonnet | 668 | 651 | 14 | 0 | **0.97** |
| haiku | 433 | 386 | 44 | 2 | **0.89** |

- Haiku also produced only **62% of sonnet's claim yield** on the same docs, and
  **3 of 18 output files were malformed JSON** (broke the output contract entirely).
- Dominant haiku failures: overreach (21), missing attribution (11), bad numbers (7).
  Sonnet's rare failures are the same shape but ~4x less frequent.
- **Tiering verdict for the design:** the small tier is fine for triage but not for
  primary extraction — or needs retry-on-malformed plus low-confidence escalation.
  Sonnet-class as the extraction workhorse at ~0.97 faithfulness supports the
  design's assumption.
- Caveat: verdicts are agent-judged (same model family judging sonnet). The human
  pass over `eval/extraction_labels.jsonl` is what makes these numbers trustworthy.

## Entity resolution

530 distinct company/security surfaces. Blocking (exact ticker/name/stripped-name +
fuzzy against the 10,391 SEC registrants) auto-resolved 126 (24%); agents adjudicated
the remaining 404 with document context:

| Outcome | Count |
|---|---|
| Resolved to SEC registrant (blocking + adjudication) | 177 |
| Real entity, not an SEC registrant (foreign/private/deregistered) | 172 |
| Not a company (indices, currencies, note series, generic terms) | 177 |
| Genuinely ambiguous | 4 |

99% of real entities were either resolved or correctly identified as
outside-the-registry. The interesting content is in the failure classes:

1. **Per-mention resolution is mandatory.** The bare surface "Constellation" appears
   self-referentially in both Constellation Brands (STZ) and Constellation Energy
   (CEG) filings. Surface-level (global) resolution cannot be correct even in
   principle. The blocking script aggregated per-surface; the real pipeline must
   resolve per-mention-in-document. (The schema's `mention` table already assumes
   this; the spike shortcut proved why.)
2. **Blocking recall is the weak stage; adjudication quality is high.** Blocking's
   candidate lists repeatedly missed the right answer (CBI → Constellation Brands,
   Bloom → Bloom Energy, "Qualcomm Technologies" → QCOM, TD Securities, GE
   HealthCare) and offered coincidental string matches instead (Ampere → Ameren,
   Vercel → Vericel, Vestas → Vestis). The adjudicator recovered nearly all of them
   from context. Fixes: seed alias table (Google→Alphabet is the canonical case),
   defined-term capture from filings ("'CBI' means Constellation Brands"),
   subsidiary→parent maps, abbreviation keys.
3. **The registry seed must extend beyond SEC data.** One week of documents produced
   172 real entities with no SEC registration: foreign issuers (Kering, Vestas, Eni,
   Tencent Holdings), private companies (OpenAI, Anthropic, Cursor), and
   broker-dealer/merger-sub subsidiaries. GLEIF + foreign registries are needed
   early, not in phase 3.
4. **Registry history matters (bitemporality again).** Hess and ChampionX are
   formerly-public companies absorbed by acquirers — absent from the current
   registrant snapshot but present in documents. Registry claims need validity
   intervals, exactly as the design specifies.
5. **ASR garbles names.** "Anthropix", "Weatherspoons" (= J D Wetherspoon),
   inconsistent surfaces for the same company within one transcript. Podcast-mention
   resolution needs phonetic/alias matching; the alias-node design covers this, the
   blocking implementation must actually use it.
6. **Policy gap found: subsidiary handling.** Adjudication batches diverged (Morgan
   Stanley & Co. matched to parent; Citigroup Global Markets kept as new entity).
   One written rule is needed; the design's "resolve to the entity the text names,
   record parent link as a claim" is the right rule — the prompt must state it more
   forcefully.

## Other findings

- **Table mush.** Linearized HTML tables in 8-Ks lose label-number association;
  extractors coped by joining cell runs but skipped fine-grained series data
  (AVGO's six-tranche tender tables). Confirms §4.5: structured data (XBRL) must
  bypass prose extraction; add table-aware parsing for the rest.
- **Exhibit fetching by filename regex missed some press releases** (AMD's Q2
  exhibits aren't named `ex99*`). Use the EDGAR filing index's document *type*
  field instead of filename patterns.
- **Schema gap:** multi-number facts (director vote tallies: for/withheld/broker
  non-votes) don't fit a single-literal object; agents stuffed them into
  qualifiers. Acceptable; worth a `values` map in the literal shape.
- **Coreference:** "the Company" (16 occurrences) needs doc-level binding to the
  filer before mention extraction — cheap, high yield for filings.
- Boilerplate-only 8-Ks (cover page, no exhibit) yield ~3 low-value claims each;
  the design's materiality triage would route these away from full extraction.

## What this spike did NOT measure

The two phase-1 gates need **human** labels, not agent labels:

- ER merge precision (~0.99 gate) — `eval/er_labels.jsonl` has all 530 decisions
  with `human_confirmed: null`.
- Extraction precision (~0.9+ before trusting claims) — `eval/extraction_labels.jsonl`
  has 1,101 agent verdicts to spot-check.

A couple of hours of the three of us confirming labels turns these into real eval
sets — the design calls this the highest-leverage early investment, and it's now a
review task, not a build task.

## Addendum (same day): harness fixes applied

The three cheap fixes from the findings above are done and verified:

1. **Exhibit discovery by document type.** `fetch_edgar.py` now reads the filing
   index page's Type column (EX-99.*) instead of matching filenames. Verified on the
   AMD Q2 8-K: it now finds the press release (`q22026991.htm`) and earnings slides
   the filename regex missed. (Corpus not re-fetched — this run's claims stay tied
   to the text they were extracted from.)
2. **Per-mention blocking with document context.** `resolve_block.py` rewritten:
   resolution runs per (doc, surface) with filer coreference ("the Company" → the
   filer), filer-initials ("CBI" in an STZ filing), filer-in-candidates preference,
   a seed alias table (`corpus/ref/aliases.json`: Google→Alphabet, Meta, TSMC…),
   and a hook for extraction-emitted defined terms. Blocking auto-resolution went
   from 24% of surfaces to **32% of mentions**, and the "Constellation" collision
   now resolves correctly per document (filer_context tier) instead of being
   structurally unresolvable.
3. **Defined-term capture.** `prompts/extraction.md` now requires a `defined_terms`
   map (the "'CBI' means Constellation Brands" pattern filings always contain);
   the resolver consumes it. Takes effect on the next extraction run.

Still open from the list: GLEIF/foreign-registry seed (the 172 non-SEC entities),
which is a data acquisition task, not a code fix.

## Recommended next steps

1. Human pass over the two eval seed files (hours, not days).
2. Fix the three cheap harness bugs: per-mention blocking, exhibit-type fetching,
   defined-term capture.
3. Add GLEIF seed + alias table to blocking; rerun the ER stage (it's cheap) and
   measure the auto-resolve rate again.
4. Then start the vertical slice proper (schema is in `schema/001_core.sql`):
   Postgres + object storage, the three connectors productionized from the spike
   scripts, extraction with the spike prompt v0 + fixes above, per-mention ER,
   then the first discovery signal (attribute joins).
