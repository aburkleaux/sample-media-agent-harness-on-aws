"""A node must be able to reach the inputs it consumes.

plan-catvideo-music declared its mux node with ONLY
``media-processing___process_media`` while its two dependencies had produced
``https://`` fal urls. M2C reads assets from its own bucket and cannot take a
second external audio track, so the sub-agent refused -- correctly -- with
"missing_capability", having been handed a tool that could not do the job. Its
siblings held exactly the tool that could:

    t1_generate_video  tools=[usr-8ddf64af…___submit_job, …___get_model_schema]  OK
    t2_generate_music  tools=[usr-8ddf64af…___submit_job, …]                     OK
    t3_merge_music…    tools=['media-processing___process_media']                refused

The same planner produced a CORRECT fal-based merge node twice (plan-carvideo01,
plan-dogvideo-merge), so this is inconsistency rather than incapacity -- which is
why the repair widens the tool set instead of failing the plan.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("PLAN_STATE_TABLE", "plans")
os.environ.setdefault("JOB_REGISTRY_TABLE", "jobs")
os.environ.setdefault("SKILLS_S3_BUCKET", "bkt")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "_layers" / "result-resolution" / "python"))
_spec = importlib.util.spec_from_file_location("pc_capability", _HERE / "handler.py")
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

FAL = "usr-8ddf64af24be45c894a7a8c1f254c83f"
FAL_SUBMIT = f"{FAL}___submit_job"
FAL_SCHEMA = f"{FAL}___get_model_schema"
M2C = "media-processing___process_media"

CAT_PLAN = [
    {"id": "t1_generate_video", "tools": [FAL_SUBMIT, FAL_SCHEMA], "depends_on": []},
    {"id": "t2_generate_music", "tools": [FAL_SUBMIT, FAL_SCHEMA], "depends_on": []},
    {"id": "t3_merge_music_into_video", "tools": [M2C],
     "depends_on": ["t1_generate_video", "t2_generate_music"]},
]


def test_the_mux_node_gains_the_tool_that_made_its_inputs():
    node = CAT_PLAN[2]
    tools, note = pc._ensure_node_can_reach_its_inputs(node, CAT_PLAN)
    assert FAL_SUBMIT in tools, "the node must be able to reach its own inputs"
    # The declared tool is kept: widening, not replacing. The sub-agent chooses.
    assert M2C in tools
    assert note, "a planner mismatch must be reported, not silently repaired"
    assert "media-processing" in note and FAL in note


def test_a_correct_node_is_left_completely_alone():
    # plan-carvideo01's merge node already declared fal, so nothing changes.
    plan = [
        {"id": "gen_video", "tools": [FAL_SUBMIT], "depends_on": []},
        {"id": "merge_av", "tools": [FAL_SUBMIT, FAL_SCHEMA], "depends_on": ["gen_video"]},
    ]
    tools, note = pc._ensure_node_can_reach_its_inputs(plan[1], plan)
    assert tools == [FAL_SUBMIT, FAL_SCHEMA]
    assert note == ""


def test_a_root_node_is_never_widened():
    # No dependencies means no inputs to reach; the planner's choice stands.
    node = {"id": "t1", "tools": [M2C], "depends_on": []}
    tools, note = pc._ensure_node_can_reach_its_inputs(node, [node])
    assert tools == [M2C]
    assert note == ""


def test_widening_is_scoped_to_direct_dependencies():
    """A long chain must not accumulate every tool the plan ever used."""
    unrelated = "other-target___do_thing"
    plan = [
        {"id": "a", "tools": [unrelated], "depends_on": []},
        {"id": "b", "tools": [FAL_SUBMIT], "depends_on": ["a"]},
        {"id": "c", "tools": [M2C], "depends_on": ["b"]},
    ]
    tools, _ = pc._ensure_node_can_reach_its_inputs(plan[2], plan)
    assert FAL_SUBMIT in tools
    assert unrelated not in tools, "only DIRECT dependencies contribute"


def test_no_duplicates_are_introduced():
    plan = [
        {"id": "a", "tools": [FAL_SUBMIT], "depends_on": []},
        {"id": "b", "tools": [FAL_SUBMIT], "depends_on": []},
        {"id": "c", "tools": [M2C], "depends_on": ["a", "b"]},
    ]
    tools, _ = pc._ensure_node_can_reach_its_inputs(plan[2], plan)
    assert tools.count(FAL_SUBMIT) == 1


def test_hostile_shapes_do_not_raise():
    for node in (
        {"id": "n"},
        {"id": "n", "tools": None, "depends_on": None},
        {"id": "n", "tools": [None, 7, M2C], "depends_on": ["missing"]},
        {"id": "n", "tools": [M2C], "depends_on": ["a"]},
    ):
        tools, note = pc._ensure_node_can_reach_its_inputs(node, [None, "junk", {"id": "a"}])
        assert isinstance(tools, list) and isinstance(note, str)


def test_a_missing_producer_leaves_the_node_unchanged():
    # A dependency that declared no tools contributes nothing, and must not
    # produce a spurious warning.
    plan = [
        {"id": "a", "tools": [], "depends_on": []},
        {"id": "b", "tools": [M2C], "depends_on": ["a"]},
    ]
    tools, note = pc._ensure_node_can_reach_its_inputs(plan[1], plan)
    assert tools == [M2C]
    assert note == ""


def test_gateway_of_handles_two_separators():
    # Tool names can carry TWO ___ separators (target___aws___call_aws).
    assert pc._gateway_of("target___aws___call_aws") == "target"
    assert pc._gateway_of("local_tool") == ""
    assert pc._gateway_of(None) == ""
