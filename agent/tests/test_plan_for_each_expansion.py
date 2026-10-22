"""Compact for_each nodes expand, at the plan boundary, into exactly the nodes the chainer knows.

Staging plan 81d9198e (8-shot anime restyle) spelled out 8 near-identical node prompts: 4,182
design tokens and 28.6s. A for_each node writes the shared shape once; expand_for_each turns it
into ordinary sibling nodes BEFORE validation, so plan-state rows, the chainer's nodes_json, render
keys (<plan_id>--<node_id>) and external_inputs see nothing new.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from planning.dag_schema import NodeKind, Plan, normalize_tool_nodes  # noqa: E402
from planning.for_each import FOR_EACH_MAX_ITEMS, ForEachError, expand_for_each  # noqa: E402
from planning.validator import validate_plan  # noqa: E402

SUBMIT = "fal___submit_job"
SHOTS = [{"id": f"shot{i}", "image_url": f"https://cdn.example/shot{i}.png", "seconds": 5 + i}
         for i in range(1, 9)]


def _restyle_group():
    return {
        "id": "restyle", "kind": "tool", "depends_on": [], "output_key": "clip",
        "output_type": "json", "tools": [SUBMIT], "endpoint_ids": ["fal-ai/anime/restyle"],
        "prompt": "Restyle {{shot.image_url}} as anime, {{shot.seconds}}s.",
        "config": {"tool": SUBMIT, "arguments": {
            "endpoint_id": "fal-ai/anime/restyle",
            "input": {"image_url": "{{shot.image_url}}", "duration": "{{shot.seconds}}"}}},
        "for_each": {"var": "shot", "items": SHOTS},
    }


def test_eight_shots_expand_into_eight_ordinary_siblings():
    nodes = expand_for_each([_restyle_group()])
    assert [n["id"] for n in nodes] == [f"restyle__shot{i}" for i in range(1, 9)]
    first = nodes[0]
    assert "for_each" not in first
    assert first["prompt"] == "Restyle https://cdn.example/shot1.png as anime, 6s."
    # A whole-string placeholder keeps the item's TYPE (a number stays a number).
    assert first["config"]["arguments"]["input"] == {
        "image_url": "https://cdn.example/shot1.png", "duration": 6}
    assert first["output_key"] == "clip_shot1"
    assert first["endpoint_ids"] == ["fal-ai/anime/restyle"]
    assert len({n["output_key"] for n in nodes}) == 8


def test_dependents_of_the_group_wait_for_every_child():
    stitch = {"id": "stitch", "depends_on": ["restyle"], "prompt": "stitch", "tools": [],
              "output_key": "reel", "output_type": "s3_uri"}
    nodes = expand_for_each([_restyle_group(), stitch])
    assert nodes[-1]["depends_on"] == [f"restyle__shot{i}" for i in range(1, 9)]


def test_a_child_can_depend_on_its_own_sibling_in_another_group():
    analyze = {"id": "analyze", "depends_on": ["restyle__{{shot.id}}"], "prompt": "check {{shot.id}}",
               "tools": [], "output_key": "qa", "output_type": "text",
               "for_each": {"var": "shot", "items": SHOTS[:2]}}
    nodes = expand_for_each([_restyle_group(), analyze])
    by_id = {n["id"]: n for n in nodes}
    assert by_id["analyze__shot2"]["depends_on"] == ["restyle__shot2"]


def test_external_inputs_and_other_variables_are_templated_or_left_alone():
    node = {"id": "report", "depends_on": [], "prompt": "Report on {{v}} via {{item_var}}",
            "tools": [], "output_key": "r", "output_type": "text",
            "external_inputs": [{"plan": "plan-moderation-foundation", "node": "shots__{{v}}"}],
            "for_each": {"var": "v", "items": ["nba", "mlb"]}}
    nodes = expand_for_each([node])
    assert [n["id"] for n in nodes] == ["report__nba", "report__mlb"]
    assert nodes[1]["external_inputs"] == [{"plan": "plan-moderation-foundation", "node": "shots__mlb"}]
    assert nodes[0]["prompt"] == "Report on nba via {{item_var}}"  # a map template's own var survives


def test_plans_without_for_each_are_returned_unchanged():
    plain = [{"id": "t1", "depends_on": [], "prompt": "x {{y}}", "tools": [], "output_key": "k",
              "output_type": "text"}]
    assert expand_for_each(plain) == plain


@pytest.mark.parametrize("spec, msg", [
    ({"var": "shot", "items": []}, "non-empty"),
    ({"var": "shot", "items": "abc"}, "non-empty"),
    ({"var": "9bad", "items": [1]}, "identifier"),
    ({"var": "shot", "items": [{"id": "a"}]}, "no field"),  # {{shot.image_url}} missing
    ({"var": "shot", "items": [{"id": str(i)} for i in range(FOR_EACH_MAX_ITEMS + 1)]}, "max"),
])
def test_malformed_groups_raise_rather_than_half_expand(spec, msg):
    node = _restyle_group()
    node["for_each"] = spec
    with pytest.raises(ForEachError, match=msg):
        expand_for_each([node])


def test_duplicate_item_ids_get_unique_child_ids():
    node = {"id": "t", "depends_on": [], "prompt": "{{x.id}}", "tools": [], "output_key": "k",
            "output_type": "text", "for_each": {"var": "x", "items": [{"id": "a"}, {"id": "a"}]}}
    assert [n["id"] for n in expand_for_each([node])] == ["t__a", "t__a_2"]


def test_expanded_plan_validates_and_serializes_to_the_chainer_shape():
    nodes = expand_for_each([_restyle_group()])
    plan = Plan.from_dict({"plan_id": "plan-anime", "goal": "g", "nodes": nodes})
    normalize_tool_nodes(plan)
    assert all(n.kind == NodeKind.TOOL for n in plan.nodes)
    errors = validate_plan(plan, {SUBMIT, "fal___check_job", "fal___get_job_result"})
    # Only the async-trio verbs are missing until normalize_async_job_nodes adds them.
    assert {e.error_type for e in errors} <= {"async_node_missing_tools"}
    wire = json.loads(json.dumps([n.to_dict() for n in plan.nodes]))
    assert all("for_each" not in n for n in wire)
    assert wire[0]["kind"] == "tool" and wire[0]["config"]["tool"] == SUBMIT


def test_normalize_tool_nodes_grants_the_named_tool():
    plan = Plan.from_dict({"plan_id": "p", "goal": "g", "nodes": [{
        "id": "t", "kind": "tool", "depends_on": [], "prompt": "", "tools": [],
        "output_key": "k", "output_type": "json", "config": {"tool": SUBMIT, "arguments": {}}}]})
    assert normalize_tool_nodes(plan) and plan.nodes[0].tools == [SUBMIT]
    assert normalize_tool_nodes(plan) == []  # idempotent


def test_expanded_children_keep_the_render_key_contract():
    """Render keys of plan nodes are <plan_id>--<node_id>; an expanded child is just a node id."""
    from render.keys import plan_node_render_key

    child = expand_for_each([_restyle_group()])[0]["id"]
    assert plan_node_render_key("81d9198e", child) == f"81d9198e--{child}"


def test_tool_node_arguments_must_be_an_object():
    plan = Plan.from_dict({"plan_id": "p", "goal": "g", "nodes": [{
        "id": "t", "kind": "tool", "depends_on": [], "prompt": "", "tools": [SUBMIT],
        "output_key": "k", "output_type": "json", "config": {"tool": SUBMIT, "arguments": "x"}}]})
    assert "invalid_tool_node" in {e.error_type for e in validate_plan(plan, {SUBMIT})}


@pytest.mark.parametrize("listed,named", [
    ("usr-8ddf___submit_job", "submit_job"),   # plan 593333b5: prefixed in tools, bare in config.tool
    ("submit_job", "usr-8ddf___submit_job"),   # and the other way round
])
def test_normalize_tool_nodes_never_lists_one_tool_twice(listed, named):
    # Both names are the SAME gateway tool; adding the second made the node agent refuse a duplicate
    # registration and every node crash on start.
    plan = Plan.from_dict({"plan_id": "p", "goal": "g", "nodes": [{
        "id": "t", "kind": "tool", "depends_on": [], "prompt": "", "tools": [listed],
        "output_key": "k", "output_type": "json", "config": {"tool": named, "arguments": {}}}]})
    assert normalize_tool_nodes(plan) == []
    assert plan.nodes[0].tools == [listed]
