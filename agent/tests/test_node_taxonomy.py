"""Tests for the DAG node taxonomy (Phase 1 scaffolding).

Covers: backward-compatible serialization (a plain TASK node must serialize
exactly as before), kind/config round-trip for the new kinds, and the structural
kind validator (validate_node_kinds) for MAP/GATHER/BRANCH/TOOL.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planning.dag_schema import DAGNode, NodeKind, Plan, validate_node_kinds  # noqa: E402


def _task(nid, deps=None):
    return DAGNode(id=nid, prompt="do", tools=["t"], depends_on=deps or [],
                   output_key="k", output_type="text")


# --- backward compatibility -------------------------------------------------

def test_task_node_serialization_unchanged():
    """A default TASK node must NOT emit kind/config, so existing plans/tests are byte-identical."""
    d = _task("t1").to_dict()
    assert "kind" not in d and "config" not in d
    assert d == {
        "id": "t1", "prompt": "do", "tools": ["t"], "depends_on": [],
        "output_key": "k", "output_type": "text", "skills": [],
    }


def test_from_dict_defaults_to_task():
    n = DAGNode.from_dict({"id": "t1", "prompt": "p", "tools": [], "depends_on": [],
                           "output_key": "k", "output_type": "text"})
    assert n.kind == NodeKind.TASK and n.config == {}


def test_legacy_plan_roundtrips():
    plan = Plan(plan_id="p", goal="g", nodes=[_task("t1"), _task("t2", ["t1"])])
    assert Plan.from_dict(plan.to_dict()).to_dict() == plan.to_dict()


# --- new kinds round-trip ---------------------------------------------------

def test_map_node_roundtrip_emits_kind_and_config():
    m = DAGNode(id="m1", prompt="", tools=[], depends_on=["diarize"],
                output_key="clips", output_type="json", kind=NodeKind.MAP,
                config={"over": {"from": "diarize", "path": "speakers[*].id"},
                        "item_var": "speaker",
                        "template": {"prompt": "clip {{speaker}}", "tools": ["media___process_media"],
                                     "output_type": "s3_uri"}})
    d = m.to_dict()
    assert d["kind"] == "map" and d["config"]["item_var"] == "speaker"
    assert DAGNode.from_dict(d).config == m.config


# --- validate_node_kinds ----------------------------------------------------

def _plan(*nodes):
    return Plan(plan_id="p", goal="g", nodes=list(nodes))


def test_valid_map_gather_pair_has_no_errors():
    diarize = _task("diarize")
    mp = DAGNode(id="m1", prompt="", tools=[], depends_on=["diarize"], output_key="o",
                 output_type="json", kind=NodeKind.MAP,
                 config={"over": {"from": "diarize", "path": "speakers[*].id"},
                         "item_var": "spk", "template": {"prompt": "do {{spk}}"}})
    gather = DAGNode(id="g1", prompt="", tools=[], depends_on=["m1"], output_key="o",
                     output_type="json", kind=NodeKind.GATHER, config={"of": "m1"})
    assert validate_node_kinds(_plan(diarize, mp, gather)) == []


def test_unknown_kind_flagged():
    n = DAGNode(id="x", prompt="", tools=[], depends_on=[], output_key="o",
                output_type="text", kind="wormhole")
    errs = validate_node_kinds(_plan(n))
    assert [e.error_type for e in errs] == ["unknown_kind"]


def test_map_bad_path_and_missing_fields_flagged():
    n = DAGNode(id="m", prompt="", tools=[], depends_on=[], output_key="o",
                output_type="json", kind=NodeKind.MAP,
                config={"over": {"from": "nope", "path": "a[*].b[*].c"}})
    types = {e.error_type for e in validate_node_kinds(_plan(n))}
    assert types == {"invalid_map"}  # bad from-ref, bad path, missing item_var + template.prompt


def test_gather_must_reference_a_map():
    task = _task("t1")
    gather = DAGNode(id="g", prompt="", tools=[], depends_on=["t1"], output_key="o",
                     output_type="json", kind=NodeKind.GATHER, config={"of": "t1"})
    errs = validate_node_kinds(_plan(task, gather))
    assert [e.error_type for e in errs] == ["invalid_gather"]


def test_branch_requires_on_and_cases():
    n = DAGNode(id="b", prompt="", tools=[], depends_on=[], output_key="o",
                output_type="text", kind=NodeKind.BRANCH, config={})
    errs = validate_node_kinds(_plan(n))
    assert all(e.error_type == "invalid_branch" for e in errs) and errs


def test_tool_node_requires_tool_name():
    n = DAGNode(id="tl", prompt="", tools=[], depends_on=[], output_key="o",
                output_type="json", kind=NodeKind.TOOL, config={})
    assert [e.error_type for e in validate_node_kinds(_plan(n))] == ["invalid_tool_node"]


def test_known_kinds_cover_the_taxonomy():
    assert NodeKind.KNOWN == frozenset({"task", "tool", "synthesis", "map", "gather", "branch"})
