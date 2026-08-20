"""Per-stage JSON schemas for structured output on the claude_code engine
(build spec v6 §2.1). The CLI enforces these server-side (AJV strict) and
hands back a parsed object, so the transport can no longer return the wrong
shape — the 2026-08-20 incident was a top-level array silently truncated to
one claim by _extract_json. The prose shape description stays in each
graph/prompts/*.md (the model still reads it), but the schema is the
contract; required keys follow the caller's actual reads.

AJV strict is stricter than Draft 2020-12: every object that enumerates
`properties` must be closed with additionalProperties:false, and the OpenAPI
`discriminator` keyword is rejected outright. strictify() handles both; open
maps (additionalProperties as a schema, no `properties`) are never touched.
Strictifier ported verbatim from Filter backend_claude_code.py:329-362
(originally IVb's schema_factory.py), taking a plain dict instead of a
pydantic class.
"""
import copy


def _inject_strict(node) -> None:
    """Recursively close object schemas and drop `discriminator` in place."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node.setdefault("additionalProperties", False)
        node.pop("discriminator", None)
        for value in node.values():
            _inject_strict(value)
    elif isinstance(node, list):
        for item in node:
            _inject_strict(item)


def strictify(schema: dict) -> dict:
    """CLI-safe deep copy of a hand-written schema dict."""
    out = copy.deepcopy(schema)
    _inject_strict(out)
    return out


# pipeline/extract.py — reads mentions, claims, defined_terms; never doc_id.
# claim.object is {surface} or {literal}; literal is free-form JSON, so it
# gets a permissive {} node, and qualifiers likewise.
EXTRACTION = strictify({
    "type": "object",
    "properties": {
        "doc_id": {"type": "string"},
        "defined_terms": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "surface": {"type": "string"},
                    "type": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["surface", "type"],
            },
        },
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "object",
                        "properties": {"surface": {"type": "string"}},
                        "required": ["surface"],
                    },
                    "predicate": {"type": "string"},
                    "object": {
                        "type": "object",
                        "properties": {
                            "surface": {"type": ["string", "null"]},
                            "literal": {},
                        },
                    },
                    "qualifiers": {},
                    "valid_from": {"type": ["string", "null"]},
                    "valid_to": {"type": ["string", "null"]},
                    "evidence_quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["subject", "predicate", "object",
                             "evidence_quote"],
            },
        },
    },
    "required": ["mentions", "claims"],
})

# pipeline/summarize.py — _dispose requires summary; key_points is optional
# (its absence is tolerated).
SUMMARY = strictify({
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary"],
})

# pipeline/adjudicate.py — one entry per queued mention. The scalar fields
# (confidence number, entity_hint string-or-null) are what the §7 incident
# was about: object values in scalar placeholders.
ADJUDICATION = strictify({
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "surface": {"type": "string"},
                    "mention_id": {"type": "string"},
                    "decision": {
                        "type": "string",
                        "enum": ["match", "new_entity", "not_a_company",
                                 "ambiguous"],
                    },
                    "cik": {"type": ["integer", "string", "null"]},
                    "lei": {"type": ["string", "null"]},
                    "entity_hint": {"type": ["string", "null"]},
                    "reasoning": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["mention_id", "decision"],
            },
        },
    },
    "required": ["decisions"],
})

# pipeline/gardener.py — an open map of canon -> raw list; no `properties`,
# so the strictifier leaves it open.
GARDENER = strictify({
    "type": "object",
    "properties": {
        "mapping": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    },
    "required": ["mapping"],
})

# discovery/hypothesize.py — confirm/refute must allow EMPTY arrays (no
# minItems): hypothesis.md rule 4 returns empty lists when no falsifiable
# plan exists, and the caller turns that into an immediate refuted verdict —
# a deliberate signal the schema must not block. budget is optional; every
# key has a code fallback.
HYPOTHESIS = strictify({
    "type": "object",
    "properties": {
        "statement": {"type": "string"},
        "type": {"type": "string"},
        "rationale": {"type": "string"},
        "test_plan": {
            "type": "object",
            "properties": {
                "confirm": {"type": "array", "items": {"type": "string"}},
                "refute": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["confirm", "refute"],
        },
        "budget": {
            "type": "object",
            "properties": {"tool_calls": {"type": "integer"}},
        },
    },
    "required": ["statement", "rationale", "test_plan"],
})

# discovery/investigate.py — one action per turn. The model may return one of
# several action shapes; only the discriminating key is required and the
# per-tool fields stay optional (AJV strict rejects oneOf with open
# branches). conclude carries assessment/evidence/reasoning/confidence.
INVESTIGATE = strictify({
    "type": "object",
    "properties": {
        "tool": {
            "type": "string",
            "enum": ["claims_about", "search_claims", "fetch_segment",
                     "conclude"],
        },
        "entity_id": {"type": ["string", "null"]},
        "text": {"type": ["string", "null"]},
        "claim_id": {"type": ["string", "null"]},
        "assessment": {
            "type": "string",
            "enum": ["supported", "refuted", "insufficient"],
        },
        "evidence": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["tool"],
})

# discovery/verify.py — surviving_evidence and confidence have code
# fallbacks and stay optional.
VERDICT = strictify({
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["promote", "park", "refute"]},
        "surviving_evidence": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "reasoning"],
})
