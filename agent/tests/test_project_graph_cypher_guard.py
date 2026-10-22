"""The read-only guard on agent-written Cypher (`graph_query`) and the gateway tool schema. Pure — default suite."""
from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_graph import cypher_guard as g  # noqa: E402
from project_graph.schema import NODE_LABELS, RELATIONSHIPS  # noqa: E402


def check(q):
    return g.check(q, NODE_LABELS, RELATIONSHIPS)


@pytest.mark.parametrize("q", [
    "MATCH (p:Person)-[:APPEARS_IN]->(s:Scene) RETURN p.name AS name, count(s) AS n ORDER BY n DESC",
    "MATCH (a:Asset)-[:HAS_SCENE]->(s:Scene)<-[:APPEARS_IN]-(p:Person) WHERE a.id = $aid RETURN DISTINCT p.name",
    "MATCH (a:Person)-[:KNOWS*1..2]->(b:Person) WHERE a.id = $id AND b.id <> a.id RETURN b.id AS id",
    "match (t:Topic) where t.name = 'set the scene; create a mood' return t.id",   # keywords inside a string are fine
])
def test_allowed_queries_get_a_limit(q):
    out = check(q)
    assert out.rstrip().upper().endswith(f"LIMIT {g.MAX_ROWS}")


def test_large_limit_is_lowered_and_small_kept():
    assert check("MATCH (p:Person) RETURN p.id LIMIT 5000").endswith(f"LIMIT {g.MAX_ROWS}")
    assert check("MATCH (p:Person) RETURN p.id LIMIT 7").endswith("LIMIT 7")


@pytest.mark.parametrize("q,code", [
    ("", "invalid_query"),
    (None, "invalid_query"),
    ("MATCH (p:Person) RETURN p; MATCH (q:Person) RETURN q", "invalid_query"),
    ("CREATE (p:Person {id: 'x'})", "unsupported_clause"),
    ("MATCH (p:Person) SET p.name = 'x' RETURN p", "unsupported_clause"),
    ("MATCH (p:Person) DETACH DELETE p", "unsupported_clause"),
    ("MATCH (p:Person) CALL db.labels() YIELD label RETURN label", "unsupported_clause"),
    ("MATCH (p:Person) OPTIONAL MATCH (p)-[:KNOWS]->(q:Person) RETURN p", "unsupported_clause"),
    ("MATCH (p:Person) WITH p RETURN p", "unsupported_clause"),
    ("MATCH (p:Person) RETURN p UNION MATCH (q:Person) RETURN q", "unsupported_clause"),
    ("RETURN 1", "invalid_query"),
    ("MATCH (p:Person)", "invalid_query"),
    ("MATCH (x)-[:APPEARS_IN]->(s:Scene) RETURN x", "unlabelled_node"),
    ("MATCH (p:Robot) RETURN p", "unknown_label"),
    ("MATCH (p:Person)-[r]->(s:Scene) RETURN p", "untyped_relationship"),
    ("MATCH (p:Person)-[:LIKES]->(s:Scene) RETURN p", "unknown_relationship"),
    ("MATCH (a:Person)-[:KNOWS*]->(b:Person) RETURN b", "unbounded_path"),
    ("MATCH (a:Person)-[:KNOWS*1..9]->(b:Person) RETURN b", "unbounded_path"),
    ("MATCH (p:Person) RETURN p // drop it", "invalid_query"),
    ("MATCH (p:Person) RETURN p LIMIT $n", "invalid_query"),
    ("MATCH (p:Person) RETURN p " + "x" * 5000, "invalid_query"),
])
def test_refused_queries(q, code):
    with pytest.raises(g.GuardError) as e:
        check(q)
    assert e.value.code == code


def test_tool_schema_json_matches_the_specs():
    """`tool_schema.json` (what CDK gives the gateway target) must be regenerated whenever TOOL_SPECS change."""
    pytest.importorskip("botocore")
    from project_graph import tools

    on_disk = (pathlib.Path(__file__).parent.parent / "src" / "project_graph" / "tool_schema.json").read_text()
    assert on_disk == tools.emit_schema(), "run: python -m project_graph.tools --emit-schema"
    for spec in json.loads(on_disk):
        assert "project_id" in spec["inputSchema"]["required"], f"{spec['name']}: project_id must be required (Cedar)"
        assert len(f"{tools.TARGET_NAME}___{spec['name']}") <= 64, "gateway tool names are capped at 64 characters"


def test_tool_schema_uses_only_gateway_supported_keywords():
    """AgentCore's inline tool schema (CfnGatewayTarget SchemaDefinition) has only these fields."""
    allowed = {"type", "description", "properties", "required", "items"}
    specs = json.loads((pathlib.Path(__file__).parent.parent / "src" / "project_graph" / "tool_schema.json").read_text())

    def walk(node, where):
        assert set(node) <= allowed, f"{where}: unsupported keys {set(node) - allowed}"
        for k, v in (node.get("properties") or {}).items():
            walk(v, f"{where}.{k}")
        if "items" in node:
            walk(node["items"], f"{where}[]")

    for spec in specs:
        assert set(spec) == {"name", "description", "inputSchema"}
        walk(spec["inputSchema"], spec["name"])


def test_referenced_lists_the_pattern_tables_only():
    q = check("MATCH (a:Asset)-[:HAS_SCENE]->(s:Scene)<-[:APPEARS_IN]-(p:Person) WHERE p.name = 'x' RETURN p.id AS id")
    assert g.referenced(q) == (["Asset", "Scene", "Person"], ["HAS_SCENE", "APPEARS_IN"])
    assert g.referenced(check("MATCH (t:Topic) WHERE t.name = '(x:Fake)' RETURN t.id")) == (["Topic"], [])
