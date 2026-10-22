"""Strict Cedar schema-validation of the knowledge-graph tool permits (`lib/graph/graph-policy.ts`), in CI,
before `cdk deploy` — the same reason and mechanism as `test_search_policy_strict.py`.

The schema per action is built from each tool's real inputSchema (`agent/src/project_graph/tool_schema.json`,
carried in the fixture), the way AgentCore generates it for a Lambda target: `required` properties are required
attributes, the rest optional.

Run:  node infra/test/emit-graph-policy.mjs && uv run --with cedarpy --with pytest pytest infra/test/test_graph_policy_strict.py
"""
import json
import pathlib

import cedarpy
import pytest

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "graph-policy.generated.json"
assert FIXTURE.exists(), f"{FIXTURE} not found — run `node infra/test/emit-graph-policy.mjs` first"
FIX = json.loads(FIXTURE.read_text())

_TYPES = {"string": {"type": "String"}, "integer": {"type": "Long"}, "boolean": {"type": "Boolean"}}


def _attr(prop: dict) -> dict:
    t = prop.get("type")
    if t in _TYPES:
        return dict(_TYPES[t])
    if t == "array":
        return {"type": "Set", "element": _attr(prop.get("items") or {"type": "string"})}
    return {"type": "Record", "attributes": {}}   # free-form objects (params): no attributes to validate


def _schema() -> str:
    common, actions = {}, {}
    for tool in FIX["tools"]:
        req = set(tool["inputSchema"].get("required", []))
        attrs = {k: {**_attr(v), "required": k in req} for k, v in tool["inputSchema"]["properties"].items()}
        common[f"In_{tool['name']}"] = {"type": "Record", "attributes": attrs}
        actions[tool["action"]] = {"appliesTo": {
            "principalTypes": ["OAuthUser"], "resourceTypes": ["Gateway"],
            "context": {"type": "Record", "attributes": {"input": {"type": f"In_{tool['name']}", "required": True}}}}}
    return json.dumps({"AgentCore": {
        "entityTypes": {"OAuthUser": {"shape": {"type": "Record", "attributes": {"id": {"type": "String"}}},
                                      "tags": {"type": "String"}},
                        "Gateway": {"shape": {"type": "Record", "attributes": {}}}},
        "commonTypes": common, "actions": actions}})


SCHEMA = _schema()


def test_one_permit_per_tool():
    assert len(FIX["tools"]) == 6
    assert all(t["action"] == f"project-graph___{t['name']}" for t in FIX["tools"])


@pytest.mark.parametrize("tool", FIX["tools"], ids=[t["name"] for t in FIX["tools"]])
def test_permit_validates_strictly(tool):
    assert 'context.input.project_id == principal.getTag("project_id")' in tool["policy"]
    result = cedarpy.validate_policies(tool["policy"], SCHEMA)
    assert result.validation_passed, f'{tool["name"]}: {result.errors}'


def test_all_permits_validate_together():
    result = cedarpy.validate_policies("\n".join(t["policy"] for t in FIX["tools"]), SCHEMA)
    assert result.validation_passed, result.errors
