"""Tests for validate_plan's node-kind checks + the executability gate (Phase 1).

The gate is the safety rail that lets us author map/gather emission before the
chainer can execute them: with the feature flag OFF, a map/gather node is rejected
as `unsupported_kind` (routing to the design-agent retry) so it can never be
dispatched as a plain task. With the flag ON, a well-formed map/gather validates.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planning.dag_schema import DAGNode, NodeKind, Plan, executable_kinds  # noqa: E402
from planning.validator import validate_plan  # noqa: E402


def _task(nid, tools=None, deps=None):
    return DAGNode(id=nid, prompt="do", tools=tools or [], depends_on=deps or [],
                   output_key="k", output_type="text")


def _map_plan():
    producer = _task("diarize")
    producer.output_type = "json"
    mp = DAGNode(id="m1", prompt="", tools=[], depends_on=["diarize"], output_key="clips",
                 output_type="json", kind=NodeKind.MAP,
                 config={"over": {"from": "diarize", "path": "speakers[*].id"},
                         "item_var": "spk", "template": {"prompt": "clip {{spk}}"}})
    gather = DAGNode(id="g1", prompt="", tools=[], depends_on=["m1"], output_key="all",
                     output_type="json", kind=NodeKind.GATHER, config={"of": "m1"})
    return Plan(plan_id="p", goal="g", nodes=[producer, mp, gather])


def test_task_plan_has_no_kind_errors():
    assert validate_plan(Plan("p", "g", [_task("t1"), _task("t2", deps=["t1"])]), set()) == []


def test_map_gather_rejected_when_flag_off(monkeypatch):
    monkeypatch.delenv("DYNAMIC_FANOUT_ENABLED", raising=False)
    errs = validate_plan(_map_plan(), set())
    types = [e.error_type for e in errs]
    # exactly the two control nodes are gated; the task producer is fine
    assert types.count("unsupported_kind") == 2
    assert "invalid_map" not in types and "invalid_gather" not in types  # they're well-formed


def test_map_gather_accepted_when_flag_on(monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    assert validate_plan(_map_plan(), set()) == []


def test_structural_map_error_flows_through_validate_plan(monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "1")  # not unsupported, so we see the structural error
    bad = DAGNode(id="m1", prompt="", tools=[], depends_on=[], output_key="o", output_type="json",
                  kind=NodeKind.MAP, config={"over": {"from": "nope", "path": "a[*].b[*].c"}})
    errs = validate_plan(Plan("p", "g", [bad]), set())
    assert any(e.error_type == "invalid_map" for e in errs)


def test_unknown_kind_flows_through_validate_plan():
    n = DAGNode(id="x", prompt="", tools=[], depends_on=[], output_key="o", output_type="text",
                kind="wormhole")
    types = {e.error_type for e in validate_plan(Plan("p", "g", [n]), set())}
    assert "unknown_kind" in types


def test_executable_kinds_reflects_flag(monkeypatch):
    monkeypatch.delenv("DYNAMIC_FANOUT_ENABLED", raising=False)
    # "tool" is always executable: the Task Agent's fast path runs it, and it degrades to an
    # ordinary task turn on its prompt whenever the planned call cannot be made as written.
    assert executable_kinds() == frozenset({"task", "synthesis", "tool"})
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "on")
    assert executable_kinds() == frozenset({"task", "synthesis", "tool", "map", "gather"})


def test_cycle_and_kind_checks_coexist(monkeypatch):
    monkeypatch.delenv("DYNAMIC_FANOUT_ENABLED", raising=False)
    a = _task("a", deps=["b"])
    b = _task("b", deps=["a"])
    errs = validate_plan(Plan("p", "g", [a, b]), set())
    assert any(e.error_type == "cycle" for e in errs)


def test_map_must_depend_on_its_over_from_producer(monkeypatch):
    monkeypatch.setenv("DYNAMIC_FANOUT_ENABLED", "true")
    producer = _task("collect")
    producer.output_type = "json"
    # over.from references a real node, but the map fails to list it in depends_on
    mp = DAGNode(id="m1", prompt="", tools=[], depends_on=[], output_key="clips",
                 output_type="json", kind=NodeKind.MAP,
                 config={"over": {"from": "collect", "path": "items"},
                         "item_var": "spk", "template": {"prompt": "clip {{spk}}"}})
    errs = validate_plan(Plan("p", "g", [producer, mp]), set())
    assert any(e.error_type == "invalid_map" and "depends_on" in e.message for e in errs)


def test_edge_onto_producing_node_is_satisfiable():
    # A consumer depending on an output-producing (task) node validates cleanly.
    plan = Plan("p", "g", [_task("author"), _task("format_pdf", deps=["author"])])
    assert [e for e in validate_plan(plan, set()) if e.error_type == "unsatisfiable_dependency"] == []


def test_edge_onto_non_producing_branch_is_unsatisfiable():
    # design §6: depending on a routing node (no durable output) is rejected pre-execution,
    # because the deterministic handoff has nothing to hand the consumer at runtime.
    branch = DAGNode(id="route", prompt="", tools=[], depends_on=[], output_key="r",
                     output_type="text", kind=NodeKind.BRANCH,
                     config={"cases": [{"when": "x", "goto": "a"}]})
    consumer = _task("uses_branch", deps=["route"])
    errs = validate_plan(Plan("p", "g", [branch, consumer]), set())
    unsat = [e for e in errs if e.error_type == "unsatisfiable_dependency"]
    assert len(unsat) == 1 and unsat[0].details["node_id"] == "uses_branch"


# --- P3: async-job node contract (submit -> status -> results; not output_type text) ---------

def _async_node(nid, tools, output_type="json"):
    return DAGNode(id=nid, prompt="do", tools=tools, depends_on=[],
                   output_key="k", output_type=output_type)


def test_async_submit_node_rejects_output_type_text():
    # Submits a fal job but declares output_type 'text' — prose satisfies the contract before the
    # job lands (the stitch-crossfade race, session f9105222).
    n = _async_node("animate", ["fal___submit_job", "fal___get_job_status", "fal___get_job_result"],
                    output_type="text")
    types = [e.error_type for e in validate_plan(Plan("p", "g", [n]), set())]
    assert "async_node_output_type" in types, types


def test_async_submit_node_requires_status_and_result_tools():
    # Granted only submit_job — cannot poll or fetch, so it goes terminal on the bare handle.
    n = _async_node("animate", ["fal___submit_job"], output_type="s3_uri")
    types = [e.error_type for e in validate_plan(Plan("p", "g", [n]), set())]
    assert "async_node_missing_tools" in types, types


def test_well_formed_async_submit_node_validates():
    n = _async_node("animate",
                    ["fal___submit_job", "fal___get_job_status", "fal___get_job_result"],
                    output_type="s3_uri")
    async_errs = [e.error_type for e in validate_plan(Plan("p", "g", [n]), set())
                  if e.error_type.startswith("async_node")]
    assert async_errs == [], async_errs


def test_non_async_node_is_unaffected_by_the_async_rule():
    n = _async_node("write", ["some___tool"], output_type="text")
    types = [e.error_type for e in validate_plan(Plan("p", "g", [n]), set())]
    assert "async_node_output_type" not in types and "async_node_missing_tools" not in types
