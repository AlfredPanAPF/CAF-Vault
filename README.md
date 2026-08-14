# CAF Company Graph

A self-updating company knowledge graph for the CAF investment team. Agents monitor
sources (filings, paid press, podcasts, blogs), extract structured claims with
provenance, resolve every company mention to a canonical entity, and store the result
as a temporal graph. A discovery layer proposes non-obvious links and keeps only the
ones with independent evidence.

The full design is in [docs/company-graph-design.md](docs/company-graph-design.md).

## Layout

```
docs/       design documents (the spec the build follows)
schema/     Postgres DDL — the durable data model
spike/      phase-0 throwaway spike: real docs through extraction + ER,
            to measure claim quality and resolution ambiguity before
            building the pipeline proper
```

## Status

Phase 0 (spike) — validating extraction and entity-resolution quality on ~100 real
documents across three sectors (Tech & AI, Energy, F&B) before building the
vertical slice. See `spike/README.md`.

## Phase-1 gates (from the design, §12)

- Entity-resolution merge precision ~0.99 on a labeled eval set
- Promoted-link precision ~0.7 as judged by the team
