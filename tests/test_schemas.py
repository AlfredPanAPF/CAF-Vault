"""Schema-contract tests (build spec v6 §8): the strictifier closes every
object node for AJV strict, and every stage schema validates that stage's
existing mocked fixture output — the same shapes the pipeline tests feed
through the mocked LLM."""
import jsonschema
import pytest
from test_pipeline import EXTRACTION as EXTRACTION_FIXTURE

from graph import schemas

STAGE_SCHEMAS = [
    schemas.EXTRACTION, schemas.SUMMARY, schemas.ADJUDICATION,
    schemas.GARDENER, schemas.HYPOTHESIS, schemas.INVESTIGATE,
    schemas.VERDICT,
]


def _open_object_nodes(node, path="$"):
    """Every object node that enumerates properties must be closed."""
    found = []
    if isinstance(node, dict):
        if (node.get("type") == "object" and "properties" in node
                and node.get("additionalProperties") is not False):
            found.append(path)
        for key, value in node.items():
            found += _open_object_nodes(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found += _open_object_nodes(item, f"{path}[{i}]")
    return found


def test_strictifier_closes_every_object_node():
    raw = {
        "type": "object",
        "properties": {
            "a": {"type": "object", "properties": {"b": {"type": "string"}}},
            "open_map": {"type": "object",
                         "additionalProperties": {"type": "string"}},
            "list": {"type": "array", "items": {
                "type": "object", "properties": {"c": {"type": "integer"}},
                "discriminator": {"propertyName": "c"}}},
        },
    }
    out = schemas.strictify(raw)
    assert _open_object_nodes(out) == []
    assert "discriminator" not in out["properties"]["list"]["items"]
    # open maps (no properties) are never touched
    assert "additionalProperties" not in raw["properties"]["a"]  # copy, not mutation
    assert out["properties"]["open_map"]["additionalProperties"] == {
        "type": "string"}


def test_every_stage_schema_is_closed():
    for schema in STAGE_SCHEMAS:
        assert _open_object_nodes(schema) == [], schema


def _validate(schema, instance):
    jsonschema.validate(instance=instance, schema=schema)


def test_extraction_schema_validates_the_pipeline_fixture():
    _validate(schemas.EXTRACTION, EXTRACTION_FIXTURE)
    # the minimum legal document: nothing asserted, nothing mentioned —
    # shape-legal, and rejected downstream by the extract-stage guard
    _validate(schemas.EXTRACTION, {"mentions": [], "claims": []})
    # literal objects are free-form JSON
    _validate(schemas.EXTRACTION, {"mentions": [
        {"surface": "Nvidia", "type": "company", "count": 3}], "claims": [{
            "subject": {"surface": "Nvidia"},
            "predicate": "reported_revenue",
            "object": {"literal": {"value": 3.2e9, "currency": "EUR",
                                   "as_of": "2026-06-30"}},
            "qualifiers": {"stance": "stated"},
            "valid_from": None, "valid_to": None,
            "evidence_quote": "revenue was EUR 3.2bn", "confidence": 0.9}]})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.EXTRACTION, {"claims": []})   # mentions required
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.EXTRACTION, [{"subject": {"surface": "x"}}])


def test_summary_schema_validates_the_documents_fixture():
    _validate(schemas.SUMMARY, {"summary": "Nvidia supplies boards to AMD.",
                                "key_points": ["One."]})
    _validate(schemas.SUMMARY, {"summary": "Short."})   # key_points optional
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.SUMMARY, {"key_points": ["no summary"]})


def test_adjudication_schema_validates_a_decision_batch():
    _validate(schemas.ADJUDICATION, {"decisions": [
        {"surface": "Alphabet", "mention_id": "m-1", "decision": "match",
         "cik": 1652044, "lei": None, "entity_hint": None,
         "reasoning": "one sentence", "confidence": 0.97},
        {"mention_id": "m-2", "decision": "new_entity",
         "entity_hint": "Zeta Materials Inc"},
        {"mention_id": "m-3", "decision": "ambiguous"},
    ]})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.ADJUDICATION, {"decisions": [
            {"mention_id": "m-1", "decision": "match",
             "confidence": {"why": "an object"}}]})   # the §7 incident shape
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.ADJUDICATION, {"decisions": [
            {"mention_id": "m-1", "decision": "punt"}]})


def test_gardener_schema_validates_the_gardener_fixture():
    _validate(schemas.GARDENER, {"mapping": {
        "supplies": ["supplies", "supplier_of"],
        "acquired": ["acquired", "bought", "took_over"]}})
    _validate(schemas.GARDENER, {"mapping": {}})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.GARDENER, {})


def test_hypothesis_schema_validates_the_discovery_fixture():
    _validate(schemas.HYPOTHESIS, {
        "statement": "Alpha Devices and Beta Systems both depend on "
                     "Gamma Materials.",
        "type": "shared_dependency",
        "rationale": "Both carry supply claims naming Gamma.",
        "test_plan": {"confirm": ["an independent filing naming Gamma"],
                      "refute": ["only generic sector commentary"]},
        "budget": {"tool_calls": 99}})
    # rule 4: empty confirm/refute is a deliberate signal (immediate refute);
    # the schema must not block it
    _validate(schemas.HYPOTHESIS, {
        "statement": "s", "rationale": "r",
        "test_plan": {"confirm": [], "refute": []}})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.HYPOTHESIS, {"statement": "s", "rationale": "r",
                                       "test_plan": {"confirm": []}})


def test_investigate_schema_validates_every_action_shape():
    for action in [
        {"tool": "claims_about", "entity_id": "e-1"},
        {"tool": "search_claims", "text": "free text", "entity_id": None},
        {"tool": "fetch_segment", "claim_id": "c-1"},
        {"tool": "conclude", "assessment": "supported",
         "evidence": ["c-1", "c-2"], "confidence": 0.8,
         "reasoning": "short and concrete"},
    ]:
        _validate(schemas.INVESTIGATE, action)
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.INVESTIGATE, {"tool": "browse_web"})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.INVESTIGATE, {"assessment": "supported"})


def test_verdict_schema_validates_the_verifier_fixture():
    _validate(schemas.VERDICT, {
        "verdict": "promote", "surviving_evidence": ["c-1", "c-2"],
        "confidence": 0.8, "reasoning": "both filings are independent"})
    _validate(schemas.VERDICT, {"verdict": "refute",
                                "reasoning": "contradicted by the 10-K"})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.VERDICT, {"verdict": "maybe", "reasoning": "r"})
    with pytest.raises(jsonschema.ValidationError):
        _validate(schemas.VERDICT, {"verdict": "park"})   # reasoning required
