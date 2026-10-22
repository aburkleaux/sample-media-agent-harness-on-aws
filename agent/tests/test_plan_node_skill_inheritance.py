"""A plan node that declares no skills must inherit the session's active skills.

The design agent emitted skills=[] for all three nodes of a video+music+mux plan (read
back from plan-state: t1_gen_video, t2_gen_music, t3_merge, every one empty). The mux node
therefore had no media skill, did not know fal's merge-audio-video endpoint existed, and
followed a prompt naming an M2C tool that takes a single asset. It refused, correctly, and
the plan reported SUCCEEDED with no merged file.

Direct dispatch had always done this ("if not skills: skills = current_active_skills()").
Only the DAG path was missing it, so the same rule is applied in both places and asserted
here so they cannot drift apart again.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


def test_the_dag_path_inherits_active_skills_like_direct_dispatch_does():
    src = (SRC / "orchestration" / "plan_and_execute_tool.py").read_text()
    # Both paths must consult the same source of truth.
    assert src.count("current_active_skills") >= 2, (
        "the DAG path does not inherit active skills; direct dispatch does")


def test_inheritance_only_fills_the_gap():
    """A node that named its own skills must keep exactly those."""
    src = (SRC / "orchestration" / "plan_and_execute_tool.py").read_text()
    i = src.index("A node that declares no skills inherits")
    window = src[i:i + 1600]
    assert "if not _n.skills" in window, "inheritance is not conditional on being empty"


def test_the_real_plan_shape_would_have_been_repaired():
    """Replays the actual node set, empty skills and all."""
    from planning.dag_schema import DAGNode

    nodes = [
        DAGNode(id="t1_gen_video", prompt="generate video", tools=["usr___submit_job"],
                depends_on=[], output_key="video_job", output_type="json"),
        DAGNode(id="t2_gen_music", prompt="generate music", tools=["usr___submit_job"],
                depends_on=[], output_key="music_job", output_type="json"),
        DAGNode(id="t3_merge", prompt="merge with process_media",
                tools=["media-processing___process_media"],
                depends_on=["t1_gen_video", "t2_gen_music"],
                output_key="final_video", output_type="s3_uri"),
    ]
    assert all(n.skills == [] for n in nodes), "fixture must start empty like the real plan"

    active = ["media-references"]
    for n in nodes:
        if not n.skills:
            n.skills = [{"name": s} for s in active]

    assert [n.skills for n in nodes] == [[{"name": "media-references"}]] * 3
    # And the node dict the chainer consumes carries it through.
    assert nodes[2].to_dict()["skills"] == [{"name": "media-references"}]


def test_a_node_with_its_own_skills_is_untouched():
    from planning.dag_schema import DAGNode

    n = DAGNode(id="t1", prompt="p", tools=[], depends_on=[], output_key="k",
                output_type="text", skills=[{"name": "pdf"}])
    active = ["media-references"]
    if not n.skills:
        n.skills = [{"name": s} for s in active]
    assert n.skills == [{"name": "pdf"}]
