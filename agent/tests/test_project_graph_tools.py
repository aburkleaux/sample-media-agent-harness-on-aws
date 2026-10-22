"""Agent graph tools (`project_graph.tools`) over a real pylance graph. Needs the `graph` extra; skipped without it.

The Project boundary is the gateway's Cedar policy (tested in infra); these tests cover what the tools return
inside one Project, the argument checks, and the gateway event handling.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("lance")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from test_project_graph_sync import IMG, OWNER, PID, VID, _deps, _reader, _seed, aws  # noqa: E402,F401

from project_graph import writer  # noqa: E402
from project_graph.tools import TOOL_NAMES, GraphTools, tool_name_from_context  # noqa: E402

OTHER_PID = "p" + "c" * 32


@pytest.fixture
def tools(aws, tmp_path):
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    return GraphTools(_reader(aws, tmp_path))


def call(tools, name, **args):
    return tools.call(name, {"project_id": PID, **args})


def test_schema_lists_every_label_and_relationship(tools):
    out = call(tools, "graph_schema")
    assert out["ok"] and {n["label"] for n in out["nodes"]} >= {"Project", "User", "Asset", "Person"}
    owns = next(r for r in out["relationships"] if r["type"] == "OWNS")
    assert owns["from"] == "User" and owns["to"] == "Project" and owns["rows"] == 1
    assert any("sample" in rule for rule in out["rules"])


def test_find_resolves_names_real_first(tools):
    out = call(tools, "graph_find", text="olive")
    assert out["ok"] and out["nodes"][0]["id"] == f"user:{OWNER}"
    files = call(tools, "graph_find", text="file", types=["Asset"], include_sample=False)
    assert {n["id"] for n in files["nodes"]} == {f"asset:{VID}", f"asset:{IMG}"}
    assert call(tools, "graph_find", text="")["error"]["code"] == "invalid_arguments"
    assert call(tools, "graph_find", text="x", types=["Robot"])["error"]["code"] == "invalid_arguments"


def test_neighbours_directions_and_filters(tools):
    p = f"project:{PID}"
    both = call(tools, "graph_neighbours", node_id=p)
    rels = {e[2] for e in both["edges"]}
    assert {"OWNS", "MEMBER_OF", "TAGGED", "HAS_ASSET"} <= rels
    out_only = call(tools, "graph_neighbours", node_id=p, direction="out")
    assert all(e[0] == p for e in out_only["edges"])
    assets = call(tools, "graph_neighbours", node_id=p, relationships=["HAS_ASSET"], limit=2)
    assert len(assets["edges"]) == 2 and assets["truncated"] is True
    assert call(tools, "graph_neighbours", node_id="asset:nope")["error"]["code"] == "node_not_found"


def test_paths_between_a_member_and_a_file(tools):
    out = call(tools, "graph_paths", from_id=f"user:{OWNER}", to_id=f"asset:{VID}")
    assert out["ok"] and out["hops"] == 2
    steps = out["paths"][0]
    assert steps[0]["to"] == f"project:{PID}" and steps[-1] == {"from": f"project:{PID}", "rel": "HAS_ASSET", "to": f"asset:{VID}"}
    far = call(tools, "graph_paths", from_id=f"user:{OWNER}", to_id=f"asset:{VID}", max_hops=1)
    assert far["paths"] == []


def test_context_enriches_sample_asset_hits(tools):
    sample_asset = call(tools, "graph_find", text="sample", types=["Asset"])["nodes"][0]["id"]
    out = call(tools, "graph_context", asset_ids=[sample_asset, f"asset:{VID}", "asset:missing"])
    first, real, missing = out["assets"]
    assert first["found"] and first["scenes"] and "Person" in first["scenes"][0]
    assert real["found"] and real["scenes"] == []       # no extraction yet for real files
    assert missing["found"] is False


def test_query_without_lance_graph_or_with_bad_cypher(tools):
    bad = call(tools, "graph_query", cypher="MATCH (p:Person) DELETE p")
    assert bad["error"]["code"] == "unsupported_clause"
    try:
        import lance_graph  # noqa: F401
    except ImportError:
        assert call(tools, "graph_query", cypher="MATCH (p:Person) RETURN p.id")["error"]["code"] == "query_unavailable"


def test_call_validates_tool_and_project(tools, aws):
    assert tools.call("drop_tables", {"project_id": PID})["error"]["code"] == "unknown_tool"
    assert tools.call("graph_schema", {})["error"]["code"] == "invalid_arguments"
    assert tools.call("graph_schema", {"project_id": OTHER_PID})["error"]["code"] == "project_not_found"
    assert tools.call("graph_schema", "nope")["error"]["code"] == "invalid_arguments"


def test_graph_off_and_seeding_states(aws, tmp_path):
    _seed(aws[0], graph=False)
    t = GraphTools(_reader(aws, tmp_path))
    assert call(t, "graph_schema")["error"]["code"] == "graph_off"
    aws[0].update_item(Key={"PK": f"PROJECT#{PID}", "SK": "META"}, UpdateExpression="SET graph = :t",
                       ExpressionAttributeValues={":t": True})
    assert call(t, "graph_schema")["error"]["code"] == "graph_seeding"


def test_tool_name_from_gateway_context():
    ctx = SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "project-graph___graph_find"}))
    assert tool_name_from_context(ctx) == "graph_find"
    assert tool_name_from_context(SimpleNamespace(client_context=None)) is None
    assert set(TOOL_NAMES) == {"graph_schema", "graph_find", "graph_neighbours", "graph_paths", "graph_context", "graph_query"}


def test_every_tool_result_carries_a_bounded_subgraph_with_focus(tools):
    p, u, v = f"project:{PID}", f"user:{OWNER}", f"asset:{VID}"
    find = call(tools, "graph_find", text="olive")["subgraph"]
    assert find["focus"] == [u] and find["nodes"][0]["id"] == u
    nb = call(tools, "graph_neighbours", node_id=p)["subgraph"]
    assert nb["focus"] == [p] and any(e[2] == "HAS_ASSET" for e in nb["edges"])
    paths = call(tools, "graph_paths", from_id=u, to_id=v)["subgraph"]
    assert set(paths["focus"]) == {u, v} and {n["id"] for n in paths["nodes"]} >= {u, p, v}
    sample_asset = call(tools, "graph_find", text="sample", types=["Asset"])["nodes"][0]["id"]
    ctx = call(tools, "graph_context", asset_ids=[sample_asset])["subgraph"]
    assert ctx["focus"] == [sample_asset] and any(e[2] == "HAS_SCENE" for e in ctx["edges"])
    for sg in (find, nb, paths, ctx):
        ids = {n["id"] for n in sg["nodes"]}
        assert all(e[0] in ids and e[1] in ids for e in sg["edges"])
        assert set(sg["nodes"][0]) == {"id", "type", "name", "sample"}


def test_graph_query_over_an_empty_table_returns_no_rows_without_the_engine(tools):
    # lance-graph 0.5.4 fails on ANY empty dataset; a pattern over an empty table must answer "no rows" itself.
    snap, _ = tools.reader.snapshot(PID)
    import pyarrow as pa
    from project_graph.schema import arrow_schema
    snap.tables["nodes/Tag"] = pa.Table.from_pylist([], schema=arrow_schema("nodes/Tag"))
    out = call(tools, "graph_query", cypher="MATCH (p:Project)-[:TAGGED]->(t:Tag) RETURN t.name AS tag")
    assert out["ok"] and out["rows"] == [] and out["row_count"] == 0 and "no rows" in out["note"]
