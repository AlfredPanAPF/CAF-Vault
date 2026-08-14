# Entity-resolution adjudication — spike v0

You are deciding which canonical company a mention refers to. You get: the surface
form, snippets of the documents it appeared in, and candidate registrants from
blocking (SEC EDGAR registrant list: name, ticker, CIK).

## Output shape (one JSON object per mention)

```json
{
  "surface": "Alphabet",
  "decision": "match",
  "cik": 1652044,
  "reasoning": "one sentence",
  "confidence": 0.97
}
```

- **decision**: `match` (one candidate is correct — set `cik`) |
  `new_entity` (a real company, not in the candidate list — e.g. foreign or private;
  set `entity_hint` with best-known legal name and country) |
  `not_a_company` (index, currency, generic term, person, etc.) |
  `ambiguous` (genuinely undecidable from context — say what would decide it)

## Rules

1. Use the document context. "Arm" in a semiconductor transcript is Arm Holdings;
   "arm" in "investment arm" is not a company.
2. Parent vs. subsidiary matters: match to the entity the text refers to, not the
   listed parent, and note the relationship in reasoning if the candidate list only
   has the parent.
3. Tickers match exactly or not at all.
4. Do not force a match. `new_entity` for foreign/private companies is the correct
   answer, not a failure. The SEC list only covers US-listed registrants.
5. Confidence below 0.8 → prefer `ambiguous` with an explanation.
