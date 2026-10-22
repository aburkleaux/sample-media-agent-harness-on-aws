"""lance-graph compatibility + Cypher/Arrow parity. x86_64 glibc>=2.39 ONLY (CI job ``test:graph``).

Guards the two things that broke silently in the spike:
- read-compat: what the writer produces (Lance format 2.1) must be readable by the lance 1.0.1 crate embedded
  in lance-graph — through ``DirNamespace`` as well as through Arrow;
- query parity: ``CypherQueries`` (lance-graph) and ``ArrowQueries`` must return the same neighbours,
  including an incoming-only node (lance-graph's undirected match drops incoming edges) and a
  self-relationship (no relationship uniqueness).
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("lance")
pytest.importorskip("lance_graph")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from test_project_graph_sync import OWNER, PID, VID, _deps, _reader, _seed, aws  # noqa: E402,F401

from project_graph import layout, writer  # noqa: E402
from project_graph.engine import ArrowQueries, CypherQueries  # noqa: E402
from project_graph.schema import NODE_LABELS, edge_table, graph_config, node_table  # noqa: E402

pytestmark = pytest.mark.lance_graph


def _snapshot(aws, tmp_path):
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    snap, _ = _reader(aws, tmp_path).snapshot(PID)
    return snap


def test_writer_output_is_readable_through_dir_namespace(aws, tmp_path):
    import lance_graph as lg

    _snapshot(aws, tmp_path)
    for kind in ("nodes", "edges"):
        base = f"{tmp_path}/{layout.graph_prefix(PID)}{layout.LAYOUT}/{kind}"
        assert os.path.isdir(base)
    # DirNamespace opens EVERY table the config names inside one directory, and nodes/ and edges/ are separate
    # directories — so read the node directory with a node-only config. What matters is that lance-graph's own
    # (older) lance crate decodes the writer's files; a format-2.2 file panics here.
    b = lg.GraphConfig.builder()
    for label in NODE_LABELS:
        b = b.with_node_label(label, "id")
    q = lg.CypherQuery("MATCH (p:Person) RETURN p.id AS id LIMIT 3").with_config(b.build())
    out = q.execute_with_namespace(lg.DirNamespace(f"{tmp_path}/{layout.graph_prefix(PID)}{layout.LAYOUT}/nodes"))
    assert out.num_rows == 3


def test_cypher_and_arrow_agree_on_every_kind_of_node(aws, tmp_path):
    snap = _snapshot(aws, tmp_path)
    cy, ar = CypherQueries(snap.tables), ArrowQueries(snap.tables)
    probes = [f"project:{PID}", f"user:{OWNER}", f"asset:{VID}", "tag:sport"]
    for label in NODE_LABELS:
        t = snap.tables[node_table(label)]
        probes += t["id"].to_pylist()[:3]
    for nid in probes:
        assert sorted(cy.neighbours(nid)) == sorted(ar.neighbours(nid)), nid


def test_incoming_only_node_is_found_by_cypher(aws, tmp_path):
    snap = _snapshot(aws, tmp_path)
    knows = snap.tables[edge_table("KNOWS")].to_pylist()
    srcs = {e["src"] for e in knows}
    target = next(e["dst"] for e in knows if e["dst"] not in srcs)   # has incoming KNOWS, no outgoing
    got = [e for e in CypherQueries(snap.tables).neighbours(target) if e[2] == "KNOWS"]
    assert got and all(e[1] == target for e in got)


def test_reader_uses_cypher_when_available(aws, tmp_path):
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    r = _reader(aws, tmp_path)
    r.engine = "cypher"
    out = r.handle("neighbourhood", PID, {"nodeId": f"project:{PID}"})
    assert {"OWNS", "MEMBER_OF", "HAS_ASSET"} <= {e[2] for e in out["edges"]}


# ---- graph_query (agent-written Cypher through the guard) --------------------------------------------

def _tools(aws, tmp_path):
    from project_graph.tools import GraphTools

    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    return GraphTools(_reader(aws, tmp_path))


def test_graph_query_runs_guarded_cypher_with_params(aws, tmp_path):
    t = _tools(aws, tmp_path)
    top = t.call("graph_query", {"project_id": PID, "cypher": (
        "MATCH (p:Person)-[:APPEARS_IN]->(s:Scene) RETURN p.name AS name, count(s) AS scenes ORDER BY scenes DESC")})
    assert top["ok"] and top["rows"] and top["cypher"].endswith("LIMIT 200")
    assert top["rows"][0]["scenes"] >= top["rows"][-1]["scenes"]
    members = t.call("graph_query", {"project_id": PID, "cypher":
        "MATCH (u:User)-[:MEMBER_OF]->(p:Project) WHERE p.id = $pid RETURN u.name AS name", "params": {"pid": f"project:{PID}"}})
    assert sorted(r["name"] for r in members["rows"]) == ["Max", "Olive"]


def test_graph_query_engine_errors_go_back_to_the_model(aws, tmp_path):
    t = _tools(aws, tmp_path)
    out = t.call("graph_query", {"project_id": PID, "cypher": "MATCH (p:Person) RETURN q.name"})
    assert out["ok"] is False and out["error"]["code"] == "query_failed"
def test_cypher_neighbourhood_on_a_project_with_empty_tables(aws, tmp_path):
    """Staging 2026-10-08: no tags → empty Tag/TAGGED tables made every node click fail ('Table has no data')."""
    _seed(aws[0], tags=())
    writer.sync_project(PID, _deps(aws, tmp_path))
    r = _reader(aws, tmp_path)
    r.engine = "cypher"
    snap, _ = r.snapshot(PID)
    assert snap.tables[node_table("Tag")].num_rows == 0
    out = r.handle("neighbourhood", PID, {"nodeId": f"project:{PID}"})
    assert out["state"] == "ready" and {"OWNS", "MEMBER_OF", "HAS_ASSET"} <= {e[2] for e in out["edges"]}
    sample = snap.tables[node_table("Person")]["id"].to_pylist()[0]
    assert r.handle("neighbourhood", PID, {"nodeId": sample})["state"] == "ready"


def test_lineage_edges_resolve_in_real_cypher(aws, tmp_path):
    """asset-lineage Phase 3: DERIVED_FROM is Asset→Asset (like KNOWS, no relationship uniqueness) and the
    question "what came from this clip" is an INCOMING match, which lance-graph's undirected pattern drops."""
    from test_project_graph_sync import IMG, JOB, SID  # noqa: PLC0415 — same fixtures as the sync tests

    snap = _snapshot(aws, tmp_path)
    cy, ar = CypherQueries(snap.tables), ArrowQueries(snap.tables)
    for nid in (f"asset:{VID}", f"asset:{IMG}", f"session:{SID}", f"job:{JOB}"):
        assert sorted(cy.neighbours(nid)) == sorted(ar.neighbours(nid)), nid
    came_from = [e for e in cy.neighbours(f"asset:{IMG}") if e[2] == "DERIVED_FROM"]
    assert came_from == [(f"asset:{VID}", f"asset:{IMG}", "DERIVED_FROM")]


def test_graph_query_can_ask_what_came_from_a_clip(aws, tmp_path):
    from test_project_graph_sync import IMG  # noqa: PLC0415

    t = _tools(aws, tmp_path)
    out = t.call("graph_query", {"project_id": PID, "cypher": (
        "MATCH (d:Asset)-[:DERIVED_FROM]->(src:Asset) WHERE src.id = $src RETURN d.name AS name"),
        "params": {"src": f"asset:{IMG}"}})
    assert out["ok"] and [r["name"] for r in out["rows"]] == ["video.file"]
